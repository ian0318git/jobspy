"""Jora (au.jora.com) scraper.

Jora 是伺服器端渲染的職缺搜尋站，頁面本身可解析，但外層有 Cloudflare 保護，
因此必須使用 ``create_session(is_tls=True)``（tls-client 指紋偽裝）。
純 requests 會被擋下。

兩個經實測確認、直接影響設計的站點特性：

1. **模糊寬鬆匹配** —— Jora 對*任何*查詢都回滿 15 筆。`ORAN engineer` 會回傳
   風電與土木專案工程師。搜尋卡片上**完全沒有薪資欄位**（實測 0/15），
   所以 ``compensation`` 一律為 None。噪音不在此模組過濾 —— 交給下游
   ``linkedin_job_search.py`` 的關聯性評分（MIN_SCORE）處理。

2. **徹底忽略日期參數** —— ``&d=``、``&date=``、``&posted=`` 實測皆無效
   （四種測法回傳完全相同的結果集）。因此 ``hours_old`` 只能在這裡以
   ``.job-listed-date`` 客戶端過濾，且刻意排在抓詳情頁*之前*，
   以免對即將被丟棄的職缺發出多餘請求。

注意：``tls_client`` 的回應物件**沒有** ``raise_for_status()``，且
``create_session`` 在 TLS 路徑上會**靜默忽略** ``has_retry``。本模組因此
自帶重試迴圈，並以嚴格的 ``status_code == 200`` 判斷成功。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from jobspy.model import (
    Country,
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import create_logger, create_session

log = create_logger("Jora")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class JoraScraper(Scraper):
    base_url = "https://au.jora.com"
    search_path = "/j"

    # 分頁與成本上限。Jora 每頁固定 15 張卡片，而約 66% 的結果過不了
    # 7 天門檻，所以實務上每詞只能湊到約 10 筆（低於 results_wanted）。
    # 這是刻意接受的：Jora 是補充性來源，且模糊匹配讓後續頁數的關聯性遞減。
    # 消費端會跨 48 個搜尋詞串接後以 job_url 去重，單詞不足會被吸收。
    max_pages = 2
    max_detail_fetches = 40
    deadline_seconds = 90

    detail_delay = 0.3
    page_delay = 0.35
    request_timeout = 20
    retry_backoff = 1.0

    # 403 刻意不在清單內：Cloudflare 封鎖不是暫時性錯誤，重試只會浪費
    # 請求數並可能加深封鎖。
    retryable_statuses = frozenset({429, 500, 502, 503, 504, 520, 522, 524})

    # `mo` 必須排在 `[hdw]` 之前。若寫成 `([hdwmo])`，"5mo ago" 會讓
    # `m` 被當成單位、殘留的 "o ago" 無法匹配而整體失敗，該職缺就會
    # fallback 成「今天」而**通過** 7 天過濾 —— 日期過濾會被靜默停用。
    _AGE_RE = re.compile(r"(\d+)\s*(mo|[hdw])\s*ago")

    # Jora 的位置字串是空白分隔（"Southbank VIC"），沒有逗號，
    # 所以 Seek 的 .split(",")[0] 慣用法在此會丟掉州別。
    _STATE_RE = re.compile(
        r"^(?P<city>.+?)\s+(?P<state>VIC|NSW|QLD|WA|SA|TAS|ACT|NT)$",
        re.IGNORECASE,
    )

    _CHALLENGE_MARKERS = (
        "just a moment",
        "cf-chl",
        "attention required",
        "enable javascript and cookies",
    )

    # 徽章文字會被 Jora 直接黏在標題後方，中間既無空白也無子元素
    # （實測原始 HTML：<a ...>Senior Platform Reliability EngineerNew</a>），
    # 所以無法靠 DOM 結構辨識。注意卡片上的 data-impression-badge-status
    # 屬性**不可**作為依據 —— 實測 120 張卡片全部帶 "NEW:NEW"，但只有
    # 少數的標題真的黏了徽章文字。
    #
    # 掃描 225 張卡片後，實際觀察到的字彙表只有 "New" 一個值，且一律緊接在
    # 小寫字母之後。因此只在這個接縫特徵成立時剝除，避免誤刪標題中真正
    # 以 " New" 結尾的詞（那種情況有大寫前的空白，不會匹配）。
    _BADGE_LABELS = ("New",)
    _TRAILING_BADGE_RE = re.compile(
        r"(?<=[a-z])(" + "|".join(_BADGE_LABELS) + r")$"
    )
    # 用來發現未列入字彙表的新徽章：只記錄、不動作。
    _UNKNOWN_BADGE_RE = re.compile(r"(?<=[a-z])([A-Z][a-z]+)$")

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(
            Site.JORA, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent
        )
        self.scraper_input: ScraperInput | None = None
        self.session = None
        self._deadline = float("inf")

    # ── 主要流程 ────────────────────────────────────────────────────────────

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        search_term = scraper_input.search_term
        if not search_term:
            log.warning("Jora: empty search_term, returning no jobs")
            return JobResponse(jobs=[])

        self.session = create_session(
            proxies=self.proxies, ca_cert=self.ca_cert, is_tls=True
        )
        self._deadline = time.monotonic() + self.deadline_seconds

        results_wanted = scraper_input.results_wanted or 15
        cutoff = (
            timedelta(hours=scraper_input.hours_old)
            if scraper_input.hours_old
            else None
        )
        # 沒有 hours_old 時只抓一頁，避免無界抓取。
        max_pages = self.max_pages if cutoff is not None else 1

        candidates = self._collect_candidates(
            search_term, results_wanted, cutoff, max_pages
        )
        jobs = self._attach_descriptions(candidates)

        log.info(
            f"Jora: kept {len(jobs)} of {len(candidates)} date-passing candidates"
        )
        return JobResponse(jobs=jobs)

    def _collect_candidates(
        self,
        search_term: str,
        results_wanted: int,
        cutoff: timedelta | None,
        max_pages: int,
    ) -> list[tuple[JobPost, timedelta | None]]:
        """走訪搜尋頁，回傳 (職缺, 年齡) 且已通過日期過濾的清單。"""
        seen_ids: set[str] = set()
        candidates: list[tuple[JobPost, timedelta | None]] = []

        for page in range(1, max_pages + 1):
            cards = self._fetch_cards(search_term, page)
            if not cards:
                # 0 張卡片通常代表被封鎖或結果耗盡，兩種情況都不該繼續打下一頁。
                break

            new_on_page = 0
            for card in cards:
                try:
                    parsed = self._extract_job_info(card)
                except Exception as exc:
                    log.debug(f"Jora: error extracting job card: {exc}")
                    continue
                if parsed is None:
                    continue

                job_post, age = parsed
                # 只在有 id 時去重 —— 否則所有無 id 的卡片會塌縮成同一個 None 鍵。
                if job_post.id is not None:
                    if job_post.id in seen_ids:
                        continue
                    seen_ids.add(job_post.id)

                new_on_page += 1

                if cutoff is not None and age is not None and age > cutoff:
                    continue

                candidates.append((job_post, age))
                if len(candidates) >= results_wanted:
                    break

            if len(candidates) >= results_wanted:
                break
            if new_on_page == 0:
                log.info(f"Jora: no new jobs on page {page}, ending pagination")
                break
            if page < max_pages:
                time.sleep(self.page_delay)

        return candidates

    def _attach_descriptions(
        self, candidates: list[tuple[JobPost, timedelta | None]]
    ) -> list[JobPost]:
        """逐筆補上完整描述。

        卡片階段已經填入 .job-abstract（約 181 字），所以就算詳情頁抓失敗
        或撞到截止時間，描述仍非空 —— 下游三個分析引擎仍可運作，只是分數
        會偏低。這是刻意的降級路徑，不是錯誤。
        """
        jobs: list[JobPost] = []
        attempted = 0
        failures = 0

        for job_post, _age in candidates:
            if (
                attempted >= self.max_detail_fetches
                or time.monotonic() > self._deadline
            ):
                jobs.append(job_post)
                continue

            attempted += 1
            description = self._fetch_full_description(job_post.job_url)
            if description:
                job_post.description = description
            else:
                failures += 1
            jobs.append(job_post)
            time.sleep(self.detail_delay)

        if attempted and failures / attempted > 0.2:
            log.warning(
                f"Jora: {failures}/{attempted} detail fetches failed — "
                f"possible mid-run block; descriptions fell back to card abstract"
            )
        return jobs

    # ── HTTP ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        }

    def _get(self, url: str, attempts: int = 3):
        """帶重試的 GET。

        tls_client 的回應沒有 raise_for_status()，且 create_session 在 TLS
        路徑上忽略 has_retry，所以重試邏輯必須自己在這裡實作。
        """
        for attempt in range(1, attempts + 1):
            response = None
            try:
                response = self.session.get(
                    url, headers=self._headers(), timeout_seconds=self.request_timeout
                )
            except Exception as exc:
                log.debug(f"Jora: request error for {url}: {type(exc).__name__}: {exc}")

            if response is not None and response.status_code == 200:
                return response

            code = getattr(response, "status_code", None)
            if code is not None and code not in self.retryable_statuses:
                # 3xx 也算在內：tls_client 預設不跟隨重導向，所以非 200
                # 一律當失敗，絕不把重導向殘骸丟進 BeautifulSoup。
                log.warning(f"Jora: HTTP {code} (not retryable) for {url}")
                return None

            if attempt < attempts:
                if time.monotonic() > self._deadline:
                    log.warning(f"Jora: deadline reached while retrying {url}")
                    return None
                time.sleep(self.retry_backoff * attempt)

        log.warning(f"Jora: giving up on {url} after {attempts} attempts")
        return None

    def _build_search_url(self, query: str, page: int) -> str:
        params: dict[str, str | int] = {"q": query, "p": page}
        location = self._normalise_location(
            self.scraper_input.location if self.scraper_input else None
        )
        if location:
            params["l"] = location
        return f"{self.base_url}{self.search_path}?{urlencode(params)}"

    @staticmethod
    def _normalise_location(location: str | None) -> str | None:
        """剝除尾端國名，保留 "City, State" 形式。

        消費端傳入的是三段的 "Melbourne, Victoria, Australia"，但 Jora 自身
        自動完成產生的是兩段的 "Melbourne, Victoria"。若三段值被誤解析成
        錯誤地理範圍，下游的 Melbourne 過濾器會把所有結果靜默丟棄 ——
        變成 0 筆卻沒有任何錯誤訊息。這裡花一行避免那個失敗模式。
        """
        if not location:
            return None
        parts = [part.strip() for part in location.split(",") if part.strip()]
        if len(parts) > 2 and parts[-1].lower() in ("australia", "au"):
            parts = parts[:-1]
        return ", ".join(parts) if parts else None

    # ── 解析 ───────────────────────────────────────────────────────────────

    def _fetch_cards(self, query: str, page: int) -> list | None:
        url = self._build_search_url(query, page)
        response = self._get(url)
        if response is None:
            return None

        text = response.text or ""
        if not text:
            log.warning(f"Jora: empty response body for page {page}")
            return None

        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception as exc:
            log.warning(f"Jora: failed to parse page {page}: {exc}")
            return None

        cards = soup.select(".job-card")
        if not cards:
            if self._looks_like_challenge(text):
                log.warning(
                    f"Jora: page {page} served a Cloudflare challenge, not results"
                )
            else:
                log.warning(f"Jora: no job cards on page {page}")
            return None

        log.debug(f"Jora: found {len(cards)} job cards on page {page}")
        return cards

    @classmethod
    def _looks_like_challenge(cls, text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in cls._CHALLENGE_MARKERS)

    def _extract_job_info(self, card) -> tuple[JobPost, timedelta | None] | None:
        """回傳 (職缺, 年齡)。

        與 Seek 的簽章不同是刻意的：年齡要在卡片層級算一次，同時供日期
        過濾與 date_posted 推導使用，避免重複解析同一個字串。
        """
        title, href = self._extract_title_and_href(card)
        if not title or not href:
            return None

        job_post = JobPost(
            id=self._extract_job_id(card, href),
            title=title,
            company_name=self._text_of(card, ".job-company"),
            job_url=self._canonical_url(href),
            location=self._build_location(self._text_of(card, ".job-location")),
            description=self._text_of(card, ".job-abstract"),
            is_remote=None,
            compensation=None,  # Jora 搜尋卡片沒有任何薪資欄位
        )

        age = self._parse_age(self._text_of(card, ".job-listed-date"))
        job_post.date_posted = (
            (datetime.now() - age).date() if age is not None else datetime.now().date()
        )
        return job_post, age

    def _extract_title_and_href(self, card) -> tuple[str | None, str | None]:
        """標題與 href 必須取自**同一個** anchor，否則兩者可能錯位。

        `h2.job-title` 內含兩個 <a>（-desktop-only 與 -mobile-only），
        直接對整個 h2 取 text 會得到重複的標題字串。
        """
        anchor = (
            card.select_one("h2.job-title a.show-job-description")
            or card.select_one("h2.job-title a[href]")
            or card.select_one("a.job-link[href]")
            or card.select_one('a[href*="/job/"]')
        )
        href = anchor.get("href") if anchor is not None else None

        title = self._clean_text(anchor.get_text(" ", strip=True)) if anchor else ""
        if not title:
            # 退路：對整個 h2 取文字，此時 desktop/mobile 的重複會現形，
            # 交給 _dedupe_title 砍半。anchor 路徑不需要這個防護。
            heading = card.select_one("h2.job-title")
            if heading is not None:
                title = self._dedupe_title(
                    self._clean_text(heading.get_text(" ", strip=True))
                )

        return (self._strip_badge(title) or None), href

    @classmethod
    def _strip_badge(cls, title: str) -> str:
        """移除 Jora 黏在標題尾端的徽章文字（實測為 "New"）。"""
        if cls._TRAILING_BADGE_RE.search(title):
            stripped = cls._TRAILING_BADGE_RE.sub("", title).strip()
            if stripped:
                return stripped
        elif cls._UNKNOWN_BADGE_RE.search(title):
            log.debug(f"Jora: possible unknown badge glued to title: {title!r}")
        return title

    def _canonical_url(self, href: str) -> str:
        """把 href 轉成不含追蹤參數的正規網址。

        Jora 的 href 帶著**每次搜尋都不同**的追蹤參數（sol_key / tk / sq / sr），
        所以同一筆職缺在不同搜尋詞下的 URL 字串並不相同。若不剝除，消費端的
        ``drop_duplicates(subset=["job_url"])`` 會完全失效 —— 實測單一職缺
        （Andromeda Robotics 的 Embedded Linux Engineer）因此在最終結果中
        重複出現 22 次。正規路徑 /job/<slug>-<id> 本身已含穩定的 job id，
        且實測仍可正常取得內容（HTTP 200，描述長度差異僅屬正常浮動）。
        """
        parts = urlsplit(urljoin(self.base_url + "/", href))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def _extract_job_id(self, card, href: str) -> str | None:
        payload = card.get("data-jd-payload")
        if payload:
            try:
                job_id = json.loads(payload).get("jobId")
                if job_id:
                    return f"jora-{job_id}"
            except (ValueError, TypeError):
                log.debug("Jora: could not parse data-jd-payload, falling back to href")

        match = re.search(r"-([0-9a-f]{32})", href or "")
        return f"jora-{match.group(1)}" if match else None

    def _build_location(self, raw: str | None) -> Location | None:
        text = self._clean_text(raw) if raw else ""
        if not text:
            # 不偽造 Melbourne：Jora 的模糊搜尋會回傳區域外職缺，
            # 捏造位置會讓它們滲漏進結果。交給下游的 Melbourne 過濾器丟棄。
            return None

        match = self._STATE_RE.match(text)
        if match:
            return Location(
                city=match.group("city").strip(),
                state=match.group("state").upper(),
                country=Country.AUSTRALIA,
            )
        return Location(city=text, country=Country.AUSTRALIA)

    def _parse_age(self, raw: str | None) -> timedelta | None:
        """解析相對時間字串如 'Posted 4d ago'、'Posted 5mo ago'。

        無法解析時回傳 None，呼叫端視為「保留」。這是刻意的寬鬆偏差 ——
        誤丟一筆職缺無法挽回，多留一筆則會被下游的 MIN_SCORE 淘汰。
        """
        if not raw:
            return None

        match = self._AGE_RE.search(raw.lower())
        if not match:
            return None

        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            return timedelta(hours=value)
        if unit == "d":
            return timedelta(days=value)
        if unit == "w":
            return timedelta(weeks=value)
        return timedelta(days=30 * value)  # "mo"

    def _fetch_full_description(self, job_url: str) -> str | None:
        response = self._get(job_url)
        if response is None:
            return None

        try:
            soup = BeautifulSoup(response.text or "", "html.parser")
        except Exception as exc:
            log.debug(f"Jora: failed to parse detail page {job_url}: {exc}")
            return None

        container = soup.select_one(".job-view-content")
        if container is None:
            log.debug(f"Jora: no .job-view-content on {job_url}")
            return None

        for tag in container.find_all(["script", "style"]):
            tag.decompose()

        # 用 " " 當分隔符再收斂空白。Seek 的 get_text(strip=True) 會把相鄰
        # 詞彙黏在一起（"bare-metalRTOS"），而下游引擎對描述做關鍵字比對，
        # 黏合字串會造成靜默漏判。
        return self._clean_text(container.get_text(" ", strip=True)) or None

    # ── 小工具 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _clean_text(text: str | None) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def _dedupe_title(title: str) -> str:
        """若字串前後半完全相等則砍半（desktop/mobile 重複的標題）。

        兩個 anchor 以 ``get_text(" ", strip=True)`` 連接時會插入一個空格，
        所以實際字串是 ``"X X"``（長度 2n+1，**奇數**）。因此不能只檢查
        偶數長度 —— 兩個切點都要試。
        """
        size = len(title)
        if not size:
            return title

        half = size // 2
        for split in (half, half + 1):
            if 0 < split < size:
                left = title[:split].strip()
                right = title[split:].strip()
                if left and left == right:
                    return left
        return title

    @classmethod
    def _text_of(cls, card, selector: str) -> str | None:
        element = card.select_one(selector)
        if element is None:
            return None
        return cls._clean_text(element.get_text(" ", strip=True)) or None
