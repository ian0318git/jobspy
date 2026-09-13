#!/usr/bin/env bash
# ════════════════════════════════════════════════
# 🔍 執行工作搜尋（爬蟲）
# ════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 啟用虛擬環境
if [ ! -d ".venv" ]; then
    echo "[!] 找不到 .venv，請先執行 ./run.sh"
    exit 1
fi
source .venv/bin/activate

echo "============================================"
echo "🔍 開始搜尋工作..."
echo "   搜尋條件: Melbourne Embedded Jobs"
echo "   搜尋平台: LinkedIn + Indeed + Seek + Jora"
echo "============================================"

python linkedin_job_search.py

echo ""
echo "[OK] 搜尋完成！結果存放在 search_results/"
ls -lt search_results/ | head -4
