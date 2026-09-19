#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# 🔍 Embedded Job Board — 停止腳本（systemd 薄殼）
#
#   舊版用 `kill <pid>`，但服務由 systemd 以 Restart=always 託管，
#   程序被殺後 systemd 會在 5 秒後把它救回來 —— 也就是說舊版 stop.sh
#   從來沒有真正停掉過服務，只會讓人以為停好了。
#   只有 systemctl stop 才會讓 systemd 放棄重啟（明確的 stop 不受 Restart= 影響）。
# ═══════════════════════════════════════════════════════════════════════════════
set -u

SERVICE="jobboard.service"

if ! systemctl --user is-active --quiet "$SERVICE"; then
    echo "[!] $SERVICE 目前沒有在執行"
    exit 0
fi

echo "正在停止 $SERVICE ..."
systemctl --user stop "$SERVICE" || {
    echo "[失敗] stop 失敗"
    systemctl --user status "$SERVICE" --no-pager
    exit 1
}

sleep 1
if systemctl --user is-active --quiet "$SERVICE"; then
    echo "[失敗] 服務仍在執行中"
    systemctl --user status "$SERVICE" --no-pager
    exit 1
fi

echo "[OK] $SERVICE 已停止"
echo "     重新啟動請用 ./run.sh"
echo "     （注意：停止看板期間，外部掃描仍在跑，但不會有停滯偵測）"
