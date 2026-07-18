# クラウド完全自動化 セットアップ手順

PCを起動しなくても、毎朝8時（日本時間・火〜土）にGitHubのサーバー上で
「株価取得 → 買い/売りHTML更新 → Gemini判定 → Gmail送信」が全自動で走ります。

## 仕組み（30秒で理解）

```
GitHub Actions（毎朝8:00 JST）
  └ main.py
      1. yfinanceで株価CSV更新（data/*.csv）
      2. run.py       → 買いHTML生成（template.htmlの__DATA__穴埋めのみ）
      3. sell/run.py  → 売りHTML生成（sell/template.htmlの穴埋めのみ）
      4. docs/ にコピー → GitHub Pagesで公開（URLは今まで通り）
      5. シグナルJSON＋トレードルールをGeminiに渡してレポート生成
      6. Gmailで自分宛に送信
```

- **デザインは絶対に崩れません**: LLMがHTMLを書く工程はゼロ。テンプレートの
  `__DATA__` を文字列置換するだけ（今までのrun.pyと同じ仕組み）。
- **公開URL**: 買い `https://shikannagata.github.io/signal/` ／
  売り `https://shikannagata.github.io/signal/exit/`（変更なし）
- Geminiが落ちてもメールは届きます（ルールを機械適用した予備レポートに自動切替）。

## 手順1: コードを signal リポジトリに入れる

PowerShellで（Claudeに頼めば代行も可能）:

```powershell
cd $HOME\Downloads
git clone https://github.com/ShikanNagata/signal.git signal-repo
robocopy signal-matrix signal-repo /E /XD __pycache__ results .git /XF publish_config.json
cd signal-repo
git add -A
git commit -m "Add cloud automation (main.py + GitHub Actions)"
git push
```

※ `publish_config.json`（トークン入り）と過去のresults類はコピー・コミットされません（.gitignoreでも二重に防止）。

## 手順2: Secrets（秘密情報）を3つ登録

GitHubの `signal` リポジトリ → **Settings → Secrets and variables → Actions → New repository secret**

| Name | 値 | 取得方法 |
|---|---|---|
| `GEMINI_API_KEY` | AIza... | https://aistudio.google.com/apikey で「APIキーを作成」（無料） |
| `GMAIL_ADDRESS` | shican2000@gmail.com | 自分のGmailアドレス |
| `GMAIL_APP_PASSWORD` | 16桁 | https://myaccount.google.com/apppasswords でアプリパスワード作成（要2段階認証。通常のパスワードでは送信できません） |

## 手順3: GitHub Pagesの公開元を docs/ に変更

リポジトリ → **Settings → Pages** → Build and deployment:
- Source: **Deploy from a branch**
- Branch: **main**（デフォルトブランチ） / フォルダ: **/docs** → Save

これでURLはそのまま、公開元だけ `docs/` に切り替わります。
（切替後、リポジトリ直下の古い index.html / exit/ は不要なので消してOK）

## 手順4: 手動テスト実行

リポジトリ → **Actions** タブ → 「Daily signal update」→ **Run workflow**。
2〜3分で完了し、①2つのダッシュボードが今日の日付に更新 ②Gmailにレポート到着、を確認。

以降は毎朝8:00 JST（火〜土＝米市場が動いた翌朝）に全自動実行されます。

## 補足

- **Geminiモデル**: `gemini-3.5-flash` を使用（1.5-flashは提供終了）。将来モデルが
  終了しても `gemini-2.5-flash` → `gemini-flash-latest` へ自動フォールバック。
  変更したい時は schedule.yml の `GEMINI_MODEL` を書き換えるだけ。
- **インサイダー・13Fデータ（fmp_enrichment.json）**: 四半期更新のデータなので
  週1回、CoworkでFMPコネクタから更新→リポにpushすれば十分（Claudeに設定依頼可）。
  日次のActionsはリポ内のファイルをそのまま読みます。
- **旧Coworkの毎朝タスク**: Actionsの動作確認後、停止または週1のFMP更新タスクに
  変更してください（そのままだと二重更新でトークンがもったいない）。
- **ローカルでのテスト**: `python main.py --skip-email --skip-gemini` でメール・AI
  なしの動作確認ができます。
