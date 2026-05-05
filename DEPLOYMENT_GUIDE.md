# 大黒屋シフト管理システム デプロイ手順書

**プログラム未経験者向け**　所要時間：約30分〜1時間

このガイドに従えば、システムをインターネット上に公開して経営陣にURLを共有できます。

---

## 📚 目次

1. [事前準備（10分）](#step-0-事前準備)
2. [GitHub アカウント作成（5分）](#step-1-github-アカウント作成)
3. [コードを GitHub にアップロード（15分）](#step-2-コードを-github-にアップロード)
4. [Streamlit Cloud にデプロイ（10分）](#step-3-streamlit-cloud-にデプロイ)
5. [API キーを設定（5分）](#step-4-api-キーを設定)
6. [URL を経営陣に共有](#step-5-url-を経営陣に共有)
7. [トラブルシューティング](#トラブルシューティング)
8. [ローカルで動作確認したい場合](#ローカルで動作確認したい場合)

---

## 事前に決めておくこと

このシステムには **少し機密度の高いデータ**（従業員名・シフト・設定など）が含まれます。
GitHub には **必ず「プライベートリポジトリ」** で保存してください（手順書通りで自動的にそうなります）。

---

## STEP 0: 事前準備

### 必要なもの

- ✅ メールアドレス（GitHub と Streamlit のアカウント作成用）
- ✅ クレジットカード（Anthropic API キー取得用 / 月数百円〜）
- ✅ 30分〜1時間の時間
- ✅ 普段使っているWebブラウザ（Chrome 推奨）

### 用意するメモ

以下の情報をテキストエディタなどにメモしておくと便利です：

```
GitHub ユーザー名:kinoshita-cmyk
GitHub のメールアドレス:kinoshita@infofactory.jp
GitHub のパスワード:chtrust16291629ch← 強固なパスワード推奨

Streamlit Cloud のメール:kinoshita@infofactory.jp← GitHub と同じでOK

Anthropic API キー:          (STEP 4 で取得)
```

---

## STEP 1: GitHub アカウント作成

GitHub は **コードを保存する場所** です。Streamlit Cloud がここからコードを読み取ります。

### 1-1. アカウント作成

1. https://github.com/signup にアクセス
2. メールアドレスを入力 → Continue
3. パスワードを設定（**強固なものに**）→ Continue
4. ユーザー名を決める（例：`daikokuya-admin`）→ Continue
   - **半角英数字とハイフンのみ**
5. メール認証コードを入力
6. 「Free」プランを選択（無料）

### 1-2. ログイン確認

https://github.com/ にアクセスして、自分のユーザー名が右上に出ていればOKです。

---

## STEP 2: コードを GitHub にアップロード

### 2-1. 新しいリポジトリ（保存場所）を作成

1. https://github.com/new にアクセス
2. 以下のように入力：
   - **Repository name**: `daikokuya-shift`
   - **Description**: `大黒屋シフト管理システム` （任意）
   - **Public / Private**: **必ず「Private」を選択！**（重要）
   - **Add a README file**: チェックを外す
   - **Add .gitignore / license**: そのまま（None / None）
3. 「**Create repository**」をクリック

### 2-2. コードをアップロード（ドラッグ＆ドロップ方式）

#### 方法A: ブラウザで直接アップロード（最も簡単）

1. 作成したリポジトリのページが開いているはず
2. 「**uploading an existing file**」というリンクをクリック
3. **アップロードするファイル・フォルダを選ぶ**：

   Finder（Mac）または Explorer（Windows）で以下を開く：
   ```
   /Users/kinoshitayoshihide/daikokuya-shift/
   ```

4. **以下のファイル・フォルダ全部** をブラウザにドラッグ＆ドロップ：

   ```
   ✅ アップロードするもの:
      app/             （フォルダごと）
      prototype/       （フォルダごと）
      .streamlit/      （フォルダごと）
      .gitignore
      requirements.txt
      DEPLOYMENT_GUIDE.md
      README.md（あれば）
      start_app.sh（あれば）
   ```

   ```
   ❌ アップロードしてはいけないもの:
      data/sample_employee_contract.docx  （給与情報）
      data/may_2026_shift.xlsx            （シフト履歴）
      backups/                            （バックアップデータ）
      output/                             （生成されたファイル）
      config/                             （個人設定）
      locks/                              （ロック情報）
      .env                                （API キー等）
      .DS_Store                           （Mac 隠しファイル）
   ```

   ※ `.gitignore` ファイルが正しく設定されているので、上記の ❌ ファイルは GitHub の Web 画面でも自動的に除外されます。心配せずドラッグしてOK。

5. ページ下部の「**Commit changes**」セクションで：
   - **Commit message**: `初回アップロード`
   - 「**Commit changes**」ボタンをクリック

6. アップロードが完了するまで待つ（ファイル数に応じて数分）

#### 方法B: GitHub Desktop を使う（よりプロ向け、推奨）

時間に余裕があれば、こちらの方が将来の更新が楽です：

1. https://desktop.github.com/ から GitHub Desktop をダウンロード・インストール
2. アプリを開いて GitHub アカウントでログイン
3. 「Add an existing repository from your hard drive」を選択
4. `/Users/kinoshitayoshihide/daikokuya-shift/` を指定
5. 「Publish repository」をクリック → **Keep this code private** にチェック → Publish

---

## STEP 3: Streamlit Cloud にデプロイ

### 3-1. アカウント作成

1. https://streamlit.io/cloud にアクセス
2. 「**Sign up**」または「**Get started**」をクリック
3. **「Continue with GitHub」** を選択（GitHub と連携してログイン）
4. 「Authorize Streamlit」を許可

### 3-2. アプリをデプロイ

1. 右上の「**Create app**」または「**New app**」をクリック
2. 「**Deploy a public app from GitHub**」を選択（プライベートリポジトリでも実はOK）
3. 以下を入力：

   | 項目 | 入力内容 |
   |------|---------|
   | **Repository** | `あなたのユーザー名/daikokuya-shift` |
   | **Branch** | `main`（自動入力されているはず） |
   | **Main file path** | `app/app.py` |
   | **App URL (custom subdomain)** | `daikokuya-shift` （好きな名前。世界で一意） |

4. 「**Advanced settings**」を開く
5. 「**Python version**」を `3.11` に設定（推奨）
6. 「**Secrets**」は STEP 4 で設定するので、今は何も入れなくてOK
7. 「**Deploy!**」をクリック

### 3-3. デプロイの完了を待つ

3〜10分かかります。進捗ログがブラウザに表示されます。
最後に「**You can now view your Streamlit app**」と出れば成功！

URL が発行されます（例: `https://daikokuya-shift.streamlit.app`）

---

## STEP 4: API キーを設定

AI機能（自然言語解析・対話）を使うために必要です。

### 4-1. Anthropic API キーを取得

1. https://console.anthropic.com/ にアクセス
2. 「Sign up」または「Login」（GoogleアカウントでもOK）
3. クレジットカード情報を登録（**月数百円〜数千円程度の従量課金**）
4. ログイン後、左メニュー「**API Keys**」 → 「**Create Key**」
5. キーに名前を付けて作成（例: `daikokuya-shift-prod`）
6. **表示されたキーをコピー**（`sk-ant-...` で始まる長い文字列）
   - ⚠ このキーは1度しか表示されないのでメモ帳などに保存してください

### 4-2. Streamlit Cloud にキー類を登録

⚠ **重要**：API キーに加えて、**経営者用パスワード**と**マジックリンク用の塩**もここで設定します。

1. https://share.streamlit.io/ で自分のアプリを開く
2. 右上「**⋮**」（縦3点） → 「**Settings**」
3. 左メニュー「**Secrets**」を選択
4. 以下をすべて入力：

   ```toml
   # Claude API キー（AI対話・自然言語解析用）
   ANTHROPIC_API_KEY = ""

   # 経営者用パスワード（経営陣・代表のみに共有）
   MANAGER_PASSWORD = "tuiteru7304"

   # マジックリンク用の秘密の塩（外部に絶対漏らさない）
   MAGIC_LINK_SALT = "daikokuya-secret-salt-2026-chtrust16291629Ch"
   ```

   #### パスワードと塩の決め方（推奨）
   - **MANAGER_PASSWORD**: 8文字以上で英数字混在、推測されにくいもの
     - 例: `Daikokuya-Mgr-2026!`
   - **MAGIC_LINK_SALT**: 16文字以上のランダム文字列
     - 例: `daikokuya-magiclink-salt-x7k9m2p4q8`
     - この値を変えると **全従業員のマジックリンクが一括無効化** されます（緊急時に使えます）

5. 「**Save**」をクリック
6. アプリが自動で再起動されます（30秒〜1分）

### 4-3. アクセス方法

| ロール | アクセス方法 | アクセス可能な画面 |
|--------|------------|------------------|
| **経営者** | URL を開いて MANAGER_PASSWORD でログイン | すべて（経営者ビュー / 従業員ビュー / 過去シフト閲覧 / 設定） |
| **従業員** | LINE で送られた **個別マジックリンク** をタップ → 自動ログイン | 従業員ビュー / 過去シフト閲覧 のみ |

### 4-4. 従業員へマジックリンクを配布

1. 経営者として `MANAGER_PASSWORD` でログイン
2. **「⚙️ 設定」 → 「🔗 マジックリンク」** タブを開く
3. アプリの公開URL（例: `https://daikokuya-shift.streamlit.app`）を入力
4. 全従業員のマジックリンクが一覧表示される
5. 各従業員の「📤 LINE送信用メッセージ」を**コピーボタン**で取得
6. LINE で各従業員に**個別に**送信

→ 従業員はLINEで届いたURLをタップするだけで、自分専用の希望提出画面が開きます。

### 4-5. GitHub 自動バックアップを設定（強く推奨）

⚠ **重要**: Streamlit Cloud の保存領域は**コード更新時にリセット**されるため、GitHub への自動バックアップを設定することを強く推奨します。設定後は、従業員が希望を提出するたびに自動的に GitHub にデータが保存され、データ消失リスクがほぼゼロになります。

#### ① バックアップ用リポジトリを作成（プライベート）

1. https://github.com/new を開く
2. 以下を入力：
   - **Repository name**: `daikokuya-shift-data`
   - **Description**: （任意）「シフト管理データのバックアップ」
   - **Public/Private**: **必ず Private を選択！**（従業員データが入るため）
   - 他はデフォルトのまま
3. 「**Create repository**」をクリック

#### ② Personal Access Token (PAT) を作成

1. https://github.com/settings/tokens?type=beta を開く
   - これは **Fine-grained tokens** の作成ページ（推奨・より安全）
2. 「**Generate new token**」をクリック
3. 以下を設定：
   - **Token name**: `Daikokuya Shift Backup`
   - **Expiration**: 1 year（または 365 days）
   - **Resource owner**: 自分のアカウント（kinoshita-cmyk）
4. **Repository access** で：
   - **「Only select repositories」** を選択
   - 「Select repositories」のドロップダウンから **`daikokuya-shift-data`** を選択
5. **Permissions** セクション：
   - **Repository permissions** を展開
   - **Contents** を **「Read and write」** に変更
   - 他はそのままでOK
6. ページ最下部の「**Generate token**」をクリック
7. **表示されたトークン**（`github_pat_...` で始まる長い文字列）を**必ずコピー**
   - ⚠ この画面を閉じると二度と見られません
   - メモ帳などに一時保存してください

#### ③ Streamlit Secrets にトークンを追加

1. https://share.streamlit.io/ で自分のアプリを開く
2. 右上「⋮」 → 「**Settings**」 → 「**Secrets**」
3. 既存の Secrets に**追加**（既存のものは消さない）：

   ```toml
   GITHUB_TOKEN = "github_pat_あなたがコピーしたトークン"
   GITHUB_BACKUP_REPO = "kinoshita-cmyk/daikokuya-shift-data"
   ```

4. 「**Save**」をクリック
5. アプリが自動再起動（30秒〜1分）

#### ④ 接続テスト

1. アプリで経営者ログイン
2. **「⚙️ 設定」 → 「💾 バックアップ」** タブを開く
3. 上部に **「✅ GitHub 自動バックアップ 有効」** と表示されることを確認
4. **「🔌 接続テスト」** ボタンをクリック
5. 「✅ 接続OK！」と出れば設定完了

これ以降、従業員が希望を提出するたびに、自動的に `daikokuya-shift-data` リポジトリにデータが保存されます。経営者は何もしなくて大丈夫です！

---

## STEP 5: URL を経営陣に共有

### 共有方法

経営陣には MANAGER_PASSWORD、従業員には EMPLOYEE_PASSWORD を別々に共有します。

#### 経営陣向けメッセージ例

```
【大黒屋シフト管理システム テスト用】

URL: https://daikokuya-shift.streamlit.app
パスワード: ●●●●●●●●（経営者用）

1. 上のリンクをタップ
2. パスワードを入力してログイン
3. 経営者ビューの「🔄 シフトを自動生成」を押すと
   AIが自動でシフトを作ってくれます
4. 「💬 AI対話」タブで、AIに質問・指示できます

ご意見・気になる点があればフィードバックお願いします！
```

#### 従業員向けメッセージ例（**個別に配布**）

経営者画面の「🔗 マジックリンク」タブから、**従業員ごとの専用URL**を取得して個別送信します：

```
【大黒屋シフト管理システム ご案内】

楯さん専用のリンクをお送りします。

▼ こちらをタップしてください
https://daikokuya-shift.streamlit.app/?token=09c641683e51984b

このURLは楯さん専用ですので、他の方には共有しないでください。
スマホのお気に入りやLINEのトークノートに保存しておくと便利です。

毎月25日までに翌月分のシフト希望をご提出ください。
よろしくお願いいたします。
```

⚠ **重要**: マジックリンクは従業員ごとに異なります。経営者画面で各人のリンクをコピーして、**個別の LINE メッセージ**として送ってください。

### 経営陣の操作の流れ

1. URL にアクセス（スマホ・PCどちらもOK）
2. 「📊 経営者ビュー」で操作
3. 「👤 従業員ビュー」で従業員の入力体験を確認
4. 「⚙️ 設定」でルール設定や従業員管理を確認

---

## トラブルシューティング

### ❓ デプロイ中に "ModuleNotFoundError" が出る

→ `requirements.txt` のパッケージリストが不足している可能性があります。
   GitHub の `requirements.txt` を確認して、必要なパッケージが書かれているか確認してください。

### ❓ アプリは表示されるが「⚠ Claude API キーが設定されていません」と出る

→ STEP 4 の Secrets 設定が反映されていません。
   - アプリ右上「⋮」→ Settings → Secrets を確認
   - 保存後、アプリを再起動（右上「⋮」→ Reboot app）

### ❓ コードを修正したい

→ GitHub のファイルを編集すると、Streamlit Cloud が自動で再デプロイします。
   - 軽微な修正：GitHub の Web 画面で直接編集（ファイルを開いて鉛筆アイコン）
   - 大規模修正：GitHub Desktop で同期 → コミット → プッシュ

### ❓ アプリの停止／削除したい

→ https://share.streamlit.io/ で対象アプリの右の「⋮」→ Delete

### ❓ 月額料金は？

| サービス | 料金 |
|---------|------|
| GitHub | 無料 |
| Streamlit Community Cloud | **無料** |
| Anthropic API | 従量課金（**月数百円〜数千円程度**） |

→ AI 対話を多用しなければ月 1,000 円以下に収まる想定です。

### ❓ 知らない人にURLを知られて勝手に使われたら？

→ Streamlit Cloud の「Settings」→「Sharing」から **特定のメールアドレスのみアクセス可** に制限できます：

1. アプリの Settings → Sharing
2. 「Limit access to viewers」をオン
3. 経営陣のメールアドレスを追加
4. 招待メールが届き、Googleアカウントでログインしないと開けなくなる

---

## ローカルで動作確認したい場合

デプロイ前に手元のMacで動かしたい場合：

```bash
# プロジェクトフォルダに移動
cd /Users/kinoshitayoshihide/daikokuya-shift

# 必要なパッケージをインストール（初回のみ）
pip3 install --user -r requirements.txt

# 起動
streamlit run app/app.py
```

ブラウザで `http://localhost:8501` を開く。

---

## 参考：ファイル構成

```
daikokuya-shift/
├── app/                       ← Streamlit Web UI
│   └── app.py
├── prototype/                 ← Python ロジック
│   ├── models.py
│   ├── employees.py
│   ├── generator.py（OR-Tools シフト最適化）
│   ├── validator.py
│   └── ...
├── .streamlit/
│   └── config.toml            ← テーマ・設定
├── data/                      ← サンプルデータ（一部）
├── backups/                   ← 自動バックアップ（GitHub に上げない）
├── config/                    ← 設定ファイル（GitHub に上げない）
├── locks/                     ← ロック情報（GitHub に上げない）
├── .gitignore                 ← GitHub に上げないファイル一覧
├── requirements.txt           ← 必要 Python パッケージ
└── DEPLOYMENT_GUIDE.md        ← このファイル
```

---

## ヘルプ

うまくいかない場合は、以下の情報を共有してください：

- どのSTEPで止まったか
- エラーメッセージのスクリーンショット
- 試したことのリスト

LINE などで気軽に相談してください。
