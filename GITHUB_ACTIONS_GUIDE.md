# GitHub Actionsで自動実行する手順 🚀

ノートパソコンの電源に関係なく、毎日**7:00 (JST)** と **21:00 (JST)** に自動でメールが届くようにします。

**所要時間：約10分**  
**料金：完全無料**

---

## なぜGitHub Actionsが良いか

- ✅ 24時間365日、GitHubのサーバーで動く（あなたのMacがオフでもOK）
- ✅ 無料（パブリックリポジトリなら無制限、プライベートでも月2000分まで無料）
- ✅ パスワード等は暗号化された「Secrets」で安全に保管
- ✅ 失敗したらメールで通知が来る

---

## 手順

### ステップ1：GitHubアカウントを作る（持っていない場合）

https://github.com/signup でアカウント作成。無料です。

---

### ステップ2：新しいリポジトリを作る

1. GitHubにログインして、右上の **「+」** → **「New repository」**
2. 設定：
   - **Repository name**: `econ-news-bot`（好きな名前でOK）
   - **Privacy**: ⚠️ **Private** を選択（重要！パブリックでも動きますが、プライベートの方が安全）
   - 他はそのままでOK
3. **「Create repository」** をクリック

---

### ステップ3：プロジェクトをアップロードする

#### 方法A：ブラウザでアップロード（簡単）

1. 作ったリポジトリのページで **「uploading an existing file」** リンクをクリック
2. `econ_news_bot` フォルダの中身を**全部ドラッグ＆ドロップ**：
   - `econ_news_bot.py`
   - `requirements.txt`
   - `README.md`
   - `.gitignore`
   - `env_example.txt`
   - `.github` フォルダ（中の `workflows/news.yml` も一緒に）
3. ⚠️ **`.env` ファイルは絶対にアップロードしないでください**（パスワードが入っているため）
4. ページ下部で **「Commit changes」** をクリック

#### 方法B：ターミナルでアップロード（Git使い慣れている人向け）

```bash
cd econ_news_bot
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/econ-news-bot.git
git push -u origin main
```

---

### ステップ4：パスワード等を「Secrets」に登録する 🔐

これがGitHub Actionsで実行するときに使われます。

1. リポジトリページで **「Settings」** タブをクリック
2. 左サイドバーで **「Secrets and variables」** → **「Actions」**
3. **「New repository secret」** ボタンを押して、以下を**一つずつ**追加します：

| Name（名前） | Secret（値） |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | あなたのGmailアドレス |
| `SMTP_PASS` | Googleアプリパスワード（16文字） |
| `FROM_ADDR` | あなたのGmailアドレス |
| `FROM_NAME` | `Economic News Bot` |
| `TO_ADDRS` | ニュースを受け取りたいメールアドレス |

各Secretを入力したら **「Add secret」** をクリック。7個全部追加します。

⚠️ Secretsは一度登録すると中身を見ることはできません（編集・削除のみ可能）。パスワードは安全に保管されます。

---

### ステップ5：手動でテスト実行する

スケジュールを待たなくても、手動でテストできます。

1. リポジトリの **「Actions」** タブをクリック
2. 左サイドバーで **「Economic News Bot」** をクリック
3. 右側の **「Run workflow」** ボタン → ドロップダウンが出る → **「Run workflow」** をもう一度クリック
4. 数秒で実行が始まる。緑のチェックマーク ✅ が出れば成功
5. メール受信箱を確認 📧

**失敗したら（赤い×印）**：実行をクリックすると、どこでエラーになったか詳細ログが見られます。よくあるエラー：
- `SMTPAuthenticationError` → `SMTP_PASS`のアプリパスワードが間違っている
- `KeyError: 'SMTP_HOST'` → Secretの名前のスペルミス（大文字小文字も区別される）

---

### ステップ6：自動実行を確認する

ステップ5が成功したら、もう何もしなくてOK。**毎日7:00と21:00 (JST) に自動でメールが届きます。**

実行履歴は **Actions** タブでいつでも確認できます。

---

## カスタマイズ

### 時間を変更したい

`.github/workflows/news.yml` を編集します。`cron` の時刻は **UTC** で指定します（JSTから9時間引く）。

```yaml
- cron: '0 22 * * *'   # 07:00 JST
- cron: '0 12 * * *'   # 21:00 JST
```

例：朝6時に変えたい → 6 - 9 = -3 → 前日のUTC 21時 → `'0 21 * * *'`

[crontab.guru](https://crontab.guru) で簡単に確認できます。

### 1日1回にしたい

不要な行を `#` でコメントアウトするだけ：

```yaml
- cron: '0 22 * * *'   # 07:00 JST
# - cron: '0 12 * * *'   # 21:00 JST  ← 夜の配信を停止
```

### 設定値を変えたい（記事数、解説の長さなど）

`news.yml` の `env:` セクションで変更できます：

```yaml
MACRO_COUNT: '5'           # マクロ経済ニュースの本数
CORPORATE_GEO_COUNT: '5'   # 企業・地政学ニュースの本数
SUMMARY_CHAR_LIMIT: '600'  # 解説の文字数
```

---

## ⚠️ 1つだけ注意

GitHub Actionsの `cron` は、サーバーの混雑によって**数分〜10分程度遅れる**ことがあります。「7:00ピッタリ」じゃなく「7:00〜7:10の間」と思ってください。気になる場合は cron 時刻を `'55 21 * * *'` のように5分早めに設定するのもアリです。

---

## トラブルシューティング

| 症状 | 解決方法 |
|---|---|
| Actionsタブに何も表示されない | `.github/workflows/news.yml` が正しい場所にあるか確認。フォルダ構造が `.github/workflows/news.yml` になっている必要があります |
| 実行が赤い×印で失敗 | クリックしてログを確認。多くはSecretのスペルミスか、アプリパスワードの間違い |
| 何週間も動いていない | GitHubのポリシーで、60日間リポジトリにアクティビティがないと cron が一時停止される。何でもいいので少しコミット（READMEの編集など）すれば再開 |
