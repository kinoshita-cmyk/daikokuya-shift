# 大黒屋シフト管理システム

ブランド買取「大黒屋」5店舗・19名のシフトを **AI で自動生成** するシステム。

## 🎯 システムの実証成果

2026年5月のシフトで実証検証した結果：

| 比較項目 | 手動作成（顧問） | **AI生成** |
|---------|-----------------|-----------|
| エラー件数 | 13〜16件 | **0件** ✨ |
| 連勤違反 | 11件 | 0件 |
| 希望違反 | 4件 | 0件 |
| 店舗人数不足 | 5件 | 0件 |
| 3連休禁止違反 | 5件 | 0件 |

過去12ヶ月の手動シフトを検証した結果、**毎月30〜80件の制約違反**があり、AI 化の効果が極めて大きいことが確認できました。

## 🚀 使い方（クイックスタート）

### 1. Web UI を起動

ターミナルで以下を実行：

```bash
cd /Users/kinoshitayoshihide/daikokuya-shift
./start_app.sh
```

起動時に表示される `http://localhost:8501` などのURLをブラウザで開くと、操作画面が表示されます。
8501 が使用中の場合は、自動で 8502 以降の空きポートに切り替わります。

認証設定がまだ無い状態でローカル確認だけしたい場合は、以下のように起動します：

```bash
BYPASS_AUTH=1 ./start_app.sh
```

### 2. シフトを生成

1. サイドバーで「📊 経営者ビュー」を選択
2. 「🔄 シフトを自動生成」ボタンをクリック
3. 1〜2分待つと AI が最適なシフト案を生成

### 3. 結果を確認・出力

- 「📋 シフト表」タブ：色分けされたシフト表
- 「✅ 検証結果」タブ：制約違反のチェック
- 「📊 統計」タブ：出勤日数・目標達成度
- 「📥 出力」タブ：Excel ファイルとしてダウンロード

## 📁 プロジェクト構成

```
daikokuya-shift/
├── README.md                ← このファイル
├── start_app.sh             ← Web UI 起動スクリプト
├── app/
│   └── app.py               ← Streamlit Web UI
├── prototype/               ← Python ロジック
│   ├── models.py                    ← データ型
│   ├── employees.py                 ← 従業員19名マスタ
│   ├── rules.py                     ← 店舗ルール・制約
│   ├── may_2026_data.py             ← 5月の希望データ
│   ├── generator.py                 ← シフト自動生成（OR-Tools）
│   ├── validator.py                 ← シフト検証
│   ├── excel_loader.py              ← Excel読み込み
│   ├── excel_exporter.py            ← Excel書き出し
│   ├── backup.py                    ← バックアップ管理
│   ├── preference_parser.py         ← 自然言語解析（Claude API）
│   ├── compare_ai_vs_manual.py      ← AI vs 手動 比較
│   └── validate_historical.py       ← 過去シフトの検証
├── data/                    ← 元データ
│   ├── rules_2026_05.txt            ← 顧問のシフト作成ルール
│   ├── may_2026_shift.xlsx          ← 過去シフト全データ
│   └── shift_template.xlsx          ← Excel テンプレート
├── output/                  ← AI 生成シフトの出力先
└── backups/                 ← 自動バックアップ
    └── YYYY-MM/
        ├── shift_*.json             ← シフトのスナップショット
        ├── preferences_*.json       ← 希望データの履歴
        └── edits_*.jsonl            ← 編集履歴（追記専用）
```

## 🔧 開発者向け：個別スクリプトの実行

```bash
# シフト自動生成 + 検証 + Excel 出力
python3 -m prototype.run_may_2026

# AI vs 手動 比較
python3 -m prototype.compare_ai_vs_manual

# 過去シフトの検証
python3 -m prototype.validate_historical

# Excel 出力テスト
python3 -m prototype.excel_exporter

# バックアップテスト
python3 -m prototype.backup

# 自然言語解析テスト（要 ANTHROPIC_API_KEY）
python3 -m prototype.preference_parser
```

## 🔑 Claude API キーの設定（自然言語解析を使う場合）

1. https://console.anthropic.com/ で API キーを取得
2. ターミナルで設定：
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```
3. または Web UI の「⚙️ 設定」画面から入力

## 📊 主な機能

### ✅ 完成済み
- [x] AI シフト自動生成（OR-Tools CP-SAT）
- [x] 19名 × 31日 × 5店舗 の最適化
- [x] 連勤・休日数・店舗人数・希望充足の制約
- [x] Excel 入出力（既存フォーマット互換）
- [x] バックアップ機構（シフト・希望・編集履歴）
- [x] Web UI（経営者・従業員ビュー）
- [x] 制約違反のリアルタイム検証
- [x] 出勤日数の目標達成度可視化
- [x] 過去シフト履歴の検証
- [x] 自然言語希望解析（Claude API）

### 🚧 今後の拡張候補
- [ ] PDF出力（Excel→PDF変換）
- [ ] AI 対話による微調整（"Xさんを別の人に変えて"などのチャット操作）
- [ ] LINE 公式アカウント連携（希望提出をLINEで完結）
- [ ] クラウド本番デプロイ（Vercel + Supabase）
- [ ] マジックリンク認証
- [ ] 個別の有給休暇管理

## 🏢 運用ルール（仕様）

### 5店舗
| 店舗 | 記号 | 通常人数 |
|------|------|---------|
| 赤羽駅前店 | ○ | エコ1+チケット2 |
| 赤羽東口店 | □ | エコ1（月曜休店） |
| 大宮駅前店 | △ | エコ2+チケット1 |
| 大宮西口店 | ☆ | エコ1（楯さん専任） |
| 大宮すずらん通り店 | ◆ | エコ1+チケット2 OR エコ2+チケット1 |

### 営業モード（自動切替）
- **通常**: 全5店舗、1日11名
- **省人員（GW・お盆・SW）**: 全5店舗、1日9〜10名
- **最小営業**: 赤羽駅前店・大宮駅前店のみ
- **営業停止**: 12/31〜1/2

### ハード制約
- 各店舗の最低必要人数（モード別）
- 東口・西口に必ずエコ1名
- 大宮に春山 or 下地 必須（アンカー）
- 東口は月曜休店、月3回まで特定ペアでエコ2可
- 連勤上限：5連勤までハード許容、4連勤超えはソフトペナルティ
- 休み希望日厳守
- 月内最低休日数：個人別（8〜11日）

## 📝 ライセンス・注意事項

- 本システムは大黒屋の社内利用を想定して開発されたものです
- Claude API（Anthropic）を使用しており、利用は従量課金制
- データはローカルに保存されており、外部送信はありません（API呼び出し時を除く）

---

Generated with Claude (Anthropic)
