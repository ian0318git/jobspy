#!/usr/bin/env python3
"""地點過濾與解析回歸測試 —— 對應 2026-09-29 回報的 GWA「IoT Developer」誤收。

不需 pytest、不需網路、不碰外部服務：

    .venv/bin/python tests/test_location_filter.py

退出碼 0 = 全數通過；1 = 有失敗（失敗清單印在最後）。

背景：使用者在看板上發現 GWA Group Limited 的「IoT Developer」被列為墨爾本
職缺，但它實際在 Prestons, Sydney NSW。根因有兩層：

  1. linkedin_job_search.py 的墨爾本白名單用子字串比對，白名單裡的
     "preston"（Preston, VIC）命中了雪梨的 "Prestons"。
  2. jobspy/seek/__init__.py 用 location_raw.split(",")[0] 取地點，把州別
     與都會區一起丟掉 —— "Prestons, Sydney NSW" 只剩 "Prestons"，下游
     無從判斷州別。舊寫法同時也誤殺合法職缺："Cremorne, Melbourne VIC"
     丟掉 "Melbourne" 後只剩 "Cremorne"，而它不在白名單內。

⚠️ 本檔的 Seek 地點字串【大多】是 2026-09-29 對 au.seek.com 實測抓回的原樣輸出，
沒有改寫 —— 用自己編的格式測等於在測自己的假設。

  但【不是全部】（2026-09-29 審查 NIT-1 更正）：比對 11 份抓取檔（352 張卡片、
  144 個相異字串）後確認，'Melbourne VIC 3000'、'Victoria, Australia'、
  'South Australia' 這 3 筆在抓取檔裡找不到，是為了探測解析失敗路徑而【人工合成】的。
  其中 'Melbourne VIC 3000' 的郵遞區號當初是憑「Seek 部分卡片會帶郵遞區號」這個
  未經證實的印象寫的 —— 抓取檔裡一張都沒有。留著它仍值得（解析器該容忍郵遞區號），
  但要知道它是合成的，不能宣稱是實測。

⚠️ 已知覆蓋限制（誠實揭露）：本測試不涵蓋實際的網路抓取，也不涵蓋
linkedin_job_search.py 的 main() 流程 —— 後者需要 48 個搜尋詞的即時爬取。
is_melbourne_location() 與 _parse_location() 這兩個純函式是本次修正的
全部邏輯，兩者都在此被直接覆蓋；而 _extract_job_info() 的接線則由
「端到端：Seek 卡片」一節以離線 HTML 驗證。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup  # noqa: E402

import linkedin_job_search as ljs  # noqa: E402
from jobspy.model import Country, Location  # noqa: E402
from jobspy.seek import SeekScraper  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


# ═══ 1. Seek 地點解析 ════════════════════════════════════════════════════════
# 每一筆的左邊都是實測原樣字串。city 保留「郊區, 都會區」兩段是刻意的：
# 都會區是下游過濾器認得的地名（"Melbourne"），丟掉它會讓 Cremorne、Surrey
# Hills 這類不在白名單內的郊區被誤殺。
print("\n── Seek 地點解析（輸入為 au.seek.com 實測原樣輸出） ──")

_SEEK_REAL = (
    ("Prestons, Sydney NSW", "Prestons, Sydney", "NSW"),
    ("Cremorne, Melbourne VIC", "Cremorne, Melbourne", "VIC"),
    ("Melbourne VIC", "Melbourne", "VIC"),
    ("Brisbane QLD", "Brisbane", "QLD"),
    ("North Sydney, Sydney NSW", "North Sydney, Sydney", "NSW"),
    ("Southport, Gold Coast QLD", "Southport, Gold Coast", "QLD"),
    ("Melbourne Airport, Melbourne VIC", "Melbourne Airport, Melbourne", "VIC"),
    ("Dry Creek, Adelaide SA", "Dry Creek, Adelaide", "SA"),
    ("Toowoomba, Toowoomba & Darling Downs QLD",
     "Toowoomba, Toowoomba & Darling Downs", "QLD"),
)
for raw, want_city, want_state in _SEEK_REAL:
    got_city, got_state = SeekScraper._parse_location(raw)
    check(
        f"{raw!r} → city/state 皆須保留",
        (got_city, got_state) == (want_city, want_state),
        f"得到 ({got_city!r}, {got_state!r})，預期 ({want_city!r}, {want_state!r})",
    )

# 郵遞區號：Seek 部分卡片會帶，不可讓它污染 city 或讓整體解析失敗。
check(
    "帶郵遞區號時仍須解析出州別，且郵遞區號不得混進 city",
    SeekScraper._parse_location("Melbourne VIC 3000") == ("Melbourne", "VIC"),
    f"得到 {SeekScraper._parse_location('Melbourne VIC 3000')!r}",
)

# 州別殿後是這個格式的關鍵。若某天 Seek 改成「STATE City」或別的形式，
# 寧可解析失敗（原字串保留 + warning）也不要猜錯州別 —— 猜錯州別會讓外州
# 職缺通過過濾，比缺州別更糟。
check(
    "解析不出來時：state 必須是 None，且州別文字必須留在 city",
    SeekScraper._parse_location("Victoria, Australia") == ("Victoria", None),
    f"得到 {SeekScraper._parse_location('Victoria, Australia')!r}",
)
check(
    "（承上）尾端國名由 country 負責，不得留在 city（否則顯示成「…, Australia, Australia」）",
    Location(
        city=SeekScraper._parse_location("Victoria, Australia")[0],
        country=Country.AUSTRALIA,
    ).display_location()
    == "Victoria, Australia",
)
# ⚠️ 這一項守著一個真實的坑：移除國名若圖方便寫成 [,\s]*Australia$，
# "South Australia" 會被削成 "South" —— 州別關卡就再也看不到它了。
check(
    "（承上）'South Australia' 不得被國名移除邏輯削掉，且仍須被排除",
    SeekScraper._parse_location("South Australia") == ("South Australia", None)
    and not ljs.is_melbourne_location("South Australia, Australia"),
    f"得到 {SeekScraper._parse_location('South Australia')!r}",
)

# 這條是本次誤收的直接見證：舊寫法 split(",")[0] 會讓它變成 "Prestons"。
check(
    "迴歸：'Prestons, Sydney NSW' 不得再被截成 'Prestons'（丟失州別與都會區）",
    SeekScraper._parse_location("Prestons, Sydney NSW")
    == ("Prestons, Sydney", "NSW"),
    f"得到 {SeekScraper._parse_location('Prestons, Sydney NSW')!r}",
)


# ═══ 2. 端到端：Seek 卡片 → Location ════════════════════════════════════════
# 上節測純函式，這節確認 _extract_job_info() 真的有接上它 —— 否則有人把
# 呼叫點改回 split(",")[0] 時，上面的測試仍然全綠。
print("\n── 端到端：Seek 搜尋卡片 HTML → Location ──")

_CARD_HTML = """
<article>
  <a data-automation="jobTitle" href="/job/94638761">IoT Developer</a>
  <span data-automation="jobCompany">GWA Group Limited</span>
  <span data-automation="jobLocation">Prestons, Sydney NSW</span>
  <span data-automation="jobListingDate">3d ago</span>
</article>
"""
_card = BeautifulSoup(_CARD_HTML, "html.parser").find("article")
_post = SeekScraper()._extract_job_info(_card)
check(
    "卡片解析出的 JobPost.location 必須帶州別（接線檢查）",
    _post is not None
    and _post.location is not None
    and _post.location.state == "NSW",
    f"得到 {(_post.location.state if _post and _post.location else None)!r}",
)
check(
    "承上，顯示字串必須是 'Prestons, Sydney, NSW, Australia'",
    _post is not None
    and _post.location is not None
    and _post.location.display_location() == "Prestons, Sydney, NSW, Australia",
    f"得到 {(_post.location.display_location() if _post and _post.location else None)!r}",
)


# ═══ 3. 墨爾本判定 ═══════════════════════════════════════════════════════════
print("\n── is_melbourne_location() ──")

# 必須排除的（外州）。前三筆是本次回報的實際案例與其變體。
_MUST_REJECT = (
    ("Prestons, Sydney, NSW, Australia", "回報案例：雪梨 Prestons，子字串 preston 誤命中"),
    ("Prestons, Australia", "回報案例的舊格式（無州別），僅憑子字串也會誤收"),
    ("Prestons, Sydney NSW", "未經解析的原始 Seek 字串"),
    ("Sydney, New South Wales, Australia", "州名全名形式（LinkedIn）"),
    ("Brisbane, Queensland, Australia", "昆士蘭"),
    ("Richmond, Sydney, NSW, Australia", "同名 suburb：Richmond 在 NSW 也有"),
    ("Epping, NSW, Australia", "同名 suburb：Epping 在 NSW 也有"),
    ("Acton, Australian Capital Territory, Australia", "ACT 全名形式"),
    ("Perth, Western Australia, Australia", "WA 全名形式"),
)
for loc, why in _MUST_REJECT:
    check(f"排除 {loc!r}（{why}）", not ljs.is_melbourne_location(loc))

# 必須保留的（維多利亞州）。這些是【誤殺】的防線：修正若下手太重，
# 會把合法墨爾本職缺一起丟掉，而且不會有人發現。
_MUST_KEEP = (
    ("Preston, Victoria, Australia", "白名單原意：Preston 是墨爾本 suburb，不可被 \b 誤殺"),
    ("Melbourne, Victoria, Australia", "LinkedIn 的標準格式"),
    ("Cremorne, Melbourne, VIC, Australia", "舊寫法會誤殺：Cremorne 不在白名單，靠都會區得救"),
    ("Surrey Hills, Melbourne, VIC, Australia", "舊寫法會誤殺：同上"),
    ("Mount Waverley, Victoria, Australia", "白名單內地名"),
    ("Werribee, Australia", "白名單內地名，無州別"),
    ("Melbourne VIC 3000", "帶郵遞區號"),
    ("Watsonia, Victoria, Australia", "詞邊界防線：'watsonia' 不含獨立的 'wa'"),
    ("Point Cook, Victoria, Australia", "詞邊界防線：'point' 不含獨立的 'nt'"),
)
for loc, why in _MUST_KEEP:
    check(f"保留 {loc!r}（{why}）", ljs.is_melbourne_location(loc))

# ── 兩個已知邊界，【刻意如此】而非意外（2026-09-29 審查 MINOR-4 / MINOR-5）──
#
# MINOR-4：沒有州別時，同名 suburb 只能靠地名白名單決定，所以會放行。
#   為什麼不改成「無州別的同名 suburb 一律排除」：那會誤殺合法的墨爾本同名
#   suburb。Jora 端的信條寫著「誤丟一筆職缺無法挽回」，本專案一貫選擇不誤殺。
#
#   實測範圍（2026-09-29，掃 search_results/*.csv：216 檔、7816 列）：
#   105 個相異 location 中 20 筆無州別，其中 18 筆命中本白名單，合計 667 次；
#   全部 699 次無州別出現【100% 來自 seek】。最多者 'Port Melbourne, Australia' x118、
#   'Richmond, Australia' x98、'East Melbourne, Australia' x73。
#   seek 端的州別已由本次修正補回，所以這個類別在現行來源上已經關閉。
#   留著這些斷言是把「已知」變成「決策」：哪天別的作者（例如 Indeed 的
#   admin1Code 為 None 時）真的產生這種字串，這裡會提醒你它是有意識的取捨。
# 這一類其實有【兩種不同機制】，標籤不可混用（2026-09-29 審查 NIT-2 更正：
# 原本 5 筆全掛在「同名 suburb」底下，其中 2 筆根本不是）：
#   (a) 同名 suburb —— 該地名在 VIC 與他州都有，所以無州別時無法分辨。
#   (b) 白名單詞被當成【更長地名的一部分】—— 'Brunswick Heads' 不是 'Brunswick'，
#       'Victoria Park' 也不是 'Victoria'，只是白名單詞剛好在那裡是獨立單詞。
#       \b 擋不掉這種：\bbrunswick\b 在 "brunswick heads" 裡是命中的。
# 兩者都只在【字串裡沒有任何州別 token】時才放行 —— 一旦帶了州別，州別關卡先攔下。
for loc, kind in (
    ("Epping, Australia", "(a) 同名 suburb"),
    ("Burwood, Australia", "(a) 同名 suburb"),
    ("Richmond, Australia", "(a) 同名 suburb"),
    ("Brunswick Heads, Australia", "(b) 白名單詞嵌在更長地名裡"),
    ("Victoria Park, Australia", "(b) 白名單詞嵌在更長地名裡"),
):
    check(f"已知模糊性（無州別，刻意放行）{kind}：{loc!r}",
          ljs.is_melbourne_location(loc))

# MINOR-5：列了兩個州時一律排除，這是【方向性收緊】—— 舊的子字串法會收下
#   "melbourne vic & sydney nsw"（因為含 "melbourne" 子字串）。刻意選嚴：
#   一筆職缺列了兩個州，我們無法知道實際工作地在哪。
#   可達性：144 個相異 live 字串與 105 個相異歷史 location 中都沒有這種字串
#   （未觀察到）。
for loc in ("Melbourne VIC & Sydney NSW", "Perth WA, Melbourne VIC"):
    check(f"多州字串必須排除（方向性收緊，非退化）：{loc!r}",
          not ljs.is_melbourne_location(loc))

# ── 已知缺口：沒有國別關卡（2026-09-29 審查 MINOR-4）──────────────────────────
# 州別閘門只認澳洲州名，所以【外國的墨爾本同名地點會放行】。這一類在本輪的
# /tmp/battery.py 裡就已經被自己的測試判成 FALSE POSITIVE，卻沒有寫進任何記錄
# —— 現在釘在這裡，讓它從「漏掉的洞」變成「有意識的取捨」。
#
# 為什麼不現在修：四個來源都以澳洲為範圍（jobspy 的 LOCATION 是
# "Melbourne, Victoria, Australia"），可達性低；而「列一張非澳洲國名清單」是個
# 開放集合，只列一半會給人「已經擋住了」的假信心，比誠實揭露更糟。
#
# 要修的話請往【拒絕已知非澳洲國名】的方向加 gate，【不要】改成「必須含
# Australia」—— 那會殺掉以國碼 AU（而非國名）結尾的字串：歷史資料裡有 24 個
# 相異 location、878 次是這種（'Melbourne, VIC, AU' x420、'Cremorne, VIC, AU' x57…）。
#
# ⚠️ 這裡原本舉 'Werribee' 當反例是錯的：資料裡的實際字串是 'Werribee, Australia'，
# 含 "Australia"，在那道 gate 下不會被殺；而且 105 個歷史 location 裡沒有一個是
# 完全不含逗號的裸地名。教訓：舉反例也要量，不能憑感覺挑一個「看起來沒有」的。
for loc in (
    "Melbourne, Florida, United States",
    "Victoria, British Columbia, Canada",
    "Preston, Lancashire, United Kingdom",
    "Richmond, BC, Canada",
):
    check(f"已知缺口（無國別關卡，刻意放行）：{loc!r}",
          ljs.is_melbourne_location(loc))

# 空值不得讓整批掃描爆掉（pandas 的 NaN 會以字串 "nan" 進來）。
check(
    "空值／None／NaN 必須回 False 而非拋錯",
    not ljs.is_melbourne_location("")
    and not ljs.is_melbourne_location("nan")
    and not ljs.is_melbourne_location(None),
)


# ═══ 4. 兩條 pattern 的詞邊界，各自單獨驗證 ═════════════════════════════════
# 第 3 節的每一筆都會先過「州別關卡」，所以關卡本身可能掩蓋掉另一條 pattern
# 沒修好。這裡繞過 is_melbourne_location()，直接對兩條 pattern 各自驗證。
#
# ⚠️ 2026-09-29 審查 NIT-1 更正：這裡原本有一項 ("newcastle west", False)，
# 註解說它在驗 \bwa\b —— 但 'newcastle west' 整串不含 "wa" 子字串（也不含
# "nt"），有沒有 \b 都回 False。那是一項【空轉測試】：它通過的理由與註解
# 宣稱的無關。下面改用真的含子字串、但不是獨立 token 的輸入。
print("\n── 兩條 pattern 的詞邊界（繞過州別關卡） ──")

for loc, want in (
    ("prestons", False),   # 含 "preston" 子字串，但 \b 讓它不命中
    ("preston", True),     # 白名單原意
    ("eppings", False),
    ("epping", True),
    ("werribee", True),
):
    got = bool(re.search(ljs.MELB_AREA_PATTERN, loc))
    check(f"白名單 {loc!r}", got == want, f"得到 {got}，預期 {want}")

# 這四筆才是 \bwa\b / \bnt\b / \bact\b / \bsa\b 的隔離案例：整串不含任何州別，
# 所以只有詞邊界本身能決定結果 —— 子字串比對會全部命中（已在註解標示）。
for loc, want, why in (
    ("watsonia", False, "含 'wa' 子字串"),
    ("point cook", False, "含 'nt' 子字串"),
    ("acton", False, "含 'act' 子字串"),
    ("salisbury", False, "含 'sa' 子字串"),
    ("new south wales", True, "州名全名（正向控制）"),
    ("nsw", True, "州別縮寫（正向控制）"),
):
    got = bool(re.search(ljs.NON_VIC_STATE_PATTERN, loc))
    check(f"非 VIC 州別 {loc!r}（{why}）", got == want, f"得到 {got}，預期 {want}")


# ═══ 結果 ════════════════════════════════════════════════════════════════════
print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 項失敗：")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全數通過")
sys.exit(0)
