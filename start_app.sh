#!/bin/bash
# 大黒屋シフト管理システム 起動スクリプト
# 使い方: ./start_app.sh

cd "$(dirname "$0")"

echo "========================================"
echo "  大黒屋シフト管理システム"
echo "========================================"
echo ""

# 8501 が他の Streamlit アプリで使われている場合は、次の空きポートを使う。
PORT="${STREAMLIT_PORT:-8501}"
while lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

if [ -z "$BYPASS_AUTH" ] && [ -z "$MANAGER_PASSWORD" ] && [ -z "$MAGIC_LINK_SALT" ]; then
  echo "⚠️  認証設定が見つかりません。"
  echo ""
  echo "ローカルで画面確認だけしたい場合:"
  echo "  BYPASS_AUTH=1 ./start_app.sh"
  echo ""
  echo "本番に近い形で確認する場合:"
  echo "  MANAGER_PASSWORD='任意のパスワード' MAGIC_LINK_SALT='任意の長い文字列' ./start_app.sh"
  echo ""
fi

echo "起動中..."
echo "ブラウザで http://localhost:${PORT} を開いてください。"
echo ""
echo "✋ 終了するには Ctrl+C を押してください。"
echo ""

python3 -m streamlit run app/app.py \
  --server.port="${PORT}" \
  --server.headless=true \
  --browser.gatherUsageStats=false
