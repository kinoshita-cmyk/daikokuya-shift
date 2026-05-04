#!/bin/bash
# 大黒屋シフト管理システム 起動スクリプト
# 使い方: ./start_app.sh

cd "$(dirname "$0")"

echo "========================================"
echo "  大黒屋シフト管理システム"
echo "========================================"
echo ""
echo "起動中..."
echo "ブラウザが自動で開きます。"
echo ""
echo "✋ 終了するには Ctrl+C を押してください。"
echo ""

python3 -m streamlit run app/app.py --server.port=8501
