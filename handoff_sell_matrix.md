# 売りシグナル（出口戦略）マトリックス — 新規セッション引き継ぎ資料

この資料は、既存の「俺の買いシグナル」ダッシュボードと同じCoworkフォルダ（`signal-matrix`）を使う**別チャット**に貼り付けて、売りマトリックス（出口戦略）を作ってもらうためのものです。買い側の開発で得た仕様・データソースの癖をすべてまとめてあるので、これを渡せばゼロから探り直す必要はありません。

---

## 1. これから作ってほしいもの

既存の「買いシグナル」3x3マトリックス（RSI×移動平均スコアで『いま買うといくら期待値があるか』を出すツール）と対になる、**売りシグナル（出口戦略）マトリックス**を作成する。

コンセプト：「いま売らずに持ち続けたら、何日後にいくら損する可能性があるか」を過去データの統計から示す。下落の「勢い」（下落速度・下げ幅の加速度）を新しい軸として加味する。

### 確認してほしい設計分岐（ユーザーに質問すること）

新しいチャットの冒頭で、以下をユーザーに確認すること：

1. **バックテストの重さ**：
   - (A) 「あの日あの値段で売らなかったら、その後何日でいくら下がったか」を全銘柄・全営業日にわたって統計を取る（買い側のバックテストと同じ思想、計算は重め）
   - (B) 現在の価格・指標から見た「今売らないリスク」を過去の類似パターンから推定するだけ（軽め）
   - → ユーザーの返答は前チャットでは保留。要確認。

2. **下落モメンタムの定義**：ユーザーは「下がり方の勢いも加味したい」と明言。候補：
   - 直近5日の下落率（速度）
   - MA20/MA50からの乖離とその変化率（加速度）
   - 直近高値からのドローダウン幅
   - 出来高を伴う下落かどうか（Ignition/Activeゾーンとの掛け合わせ）
   これらのどれを軸にするか、または複数組み合わせて新しい「下落バケット」を作るかを設計すること。

---

## 2. 既存システム（買い側）の場所と構造

作業フォルダ: `signal-matrix`（Coworkの接続フォルダそのもの）

```
signal-matrix/
├── config.json              ← 銘柄リスト・パラメータ設定（共通で使い回せる）
├── run.py                   ← 買いマトリックス生成スクリプト（本体ロジック）
├── template.html            ← ダッシュボードのHTMLテンプレート（run.pyが読み込む）
├── data/
│   ├── WULF.csv, AMD.csv, MRVL.csv, CRCL.csv, NBIS.csv, TXG.csv
│   │     ← 日足OHLCV。ヘッダー: Date,Open,High,Low,Close,Volume
│   │     ← Date形式: "YYYY/MM/DD 16:00:00"
│   ├── fmp_enrichment.json  ← インサイダー取引・機関投資家・ニュースの補足データ
│   └── publish_config.json ← GitHub Pages公開用（user/repo/token/url）tokenは秘密
└── results/
    └── matrix_YYYY-MM-DD.html / .json  ← 生成物（日付つき）
```

config.jsonの主要フィールド:
```json
{
  "tickers": ["WULF", "AMD", "MRVL", "CRCL", "NBIS", "TXG"],
  "history_period": "550d",
  "hold_days": [5, 10, 20, 40, 80],
  "rsi_period": 14,
  "rsi_overheated": 70,
  "volume_zones": {"Ignition": 3.0, "Active": 1.5, "High": 0.8},
  "output_dir": "results",
  "my_holdings": {"WULF": {"qty":140,"avg_cost_usd":15.04}, ...}
}
```

## 3. 買い側の指標ロジック（run.py の中身）

売り側を設計する上でそのまま流用・裏返しできるロジック。

- **RSI(14)**, **MACD(12,26,9)**, **MA5/20/50/200** と各MAからの乖離率(`dev_MA*`)を計算
- **出来高ゾーン**: 40日中央値比で Ignition(3倍〜)/Active(1.5倍〜)/High(0.8倍〜)/Normal
- **sig_score**: Close が MA5/MA50/MA200（MA200がなければMA5/20/50）の何本上にあるか（0〜3）
- **bucket**: sig_score と RSI から5分類 → `3/3 健全`, `3/3 過熱`, `2/3 警戒`, `1/3 弱い`, `0/3 底値`
- **backtest()**: 各日・各バケットについて、hold_days（5/10/20/40/80日）後のリターンを全部集計 → 平均・勝率・ベスト・ワースト
- **エピソード分析（find_episodes）**: 同じバケットが連続する区間を1エピソードとして数え、重複サンプルの偏りを避ける。エピソード起点からのリターンをMA200の上/下で分けても集計
- **トリガー分析**: 弱いバケット中に「RSI底打ち反転」「MA5上抜け」「MACDヒストグラム2日連続改善」のいずれかが点灯した日 vs 非点灯日の成績比較
- **押し目の深さ**: 弱いバケットのエピソード中、MA20からの乖離が最終的にどこまで掘れたか（中央値・最悪値）
- **セクター連動判定（breadth）**: 同日に何銘柄が下落したかを見て、個別要因か全面安かを判別
- **保有ポジションの含み損益**: config.jsonのmy_holdingsと現在値から算出

売り側では例えば「3/3健全から2/3警戒に落ちた瞬間からのその後のリターン分布」「下落速度が一定以上のエピソードだけを抽出した損失統計」のような形で、上のロジックを裏返して使える。

## 4. データソース・APIの癖（つまずいた点）

FMP（Financial Modeling Prep）のMCPツール群を使う。以下は実際に検証して分かった制約:

- **`chart`エンドポイント（日足の過去データ取得）はこのプランでは全銘柄ほぼACCESS DENIED。使えない。**
- 代わりに **`technicalIndicators`ツールの`simple-moving-average`エンドポイント**（periodLength, timeframe, from_date, to_date指定）を使うと、レスポンスに`{date, open, high, low, close, volume}`が全部含まれてくる。これをOHLCVソースとして使う。SMAの値自体は使わなくてOK。
- `insiderTrades`ツールの`insider-trade-statistics`は**`limit`パラメータを渡しても無視され、常に全四半期履歴を返す**。トークン消費が大きいので、必要な直近1〜2四半期だけを抽出してJSONに保存する運用にしている（fmp_enrichment.json）。
- `form13F`の`positions-summary`は最新四半期（提出期限前）だと機関投資家データが激減して見える（未提出分が反映されていないだけ）。**1つ前の完全な四半期を使うこと。**
- 銘柄の存在確認は`search`ツールの`search-symbol`エンドポイント（`query`パラメータ、`symbol`ではない）。

## 5. GitHub Pages公開フロー（既存の仕組みをそのまま流用可能）

`data/publish_config.json`に`user`/`repo`/`token`/`url`が入っている。**tokenは絶対にログや出力に書かない。**

```bash
# 必ず /tmp の新規ディレクトリで作業する（マウントされたsignal-matrix内でgit操作するとファイルロックで失敗する）
D=/tmp/pub_$(date +%s)
git clone -q "https://<user>:<token>@github.com/<user>/<repo>.git" "$D"
cd "$D"
cp <生成したhtml> index.html   # または売り用に別のファイル名/サブパスにする
# <head>直後に <meta name="robots" content="noindex, nofollow"> を挿入（未挿入なら）
git config user.email "..." && git config user.name "..."
git add . && git commit -m "..." && git push origin main
```

売り側を別URLで公開したい場合は、同じリポジトリ内に `exit/index.html` のようなサブパスを作るか、別リポジトリを新規に用意するかを検討すること（例: `https://shikannagata.github.io/signal/` と `https://shikannagata.github.io/signal/exit/` など）。

## 6. 推奨フォルダ構成（分離案）

```
signal-matrix/
├── config.json          ← 共通（銘柄リストなど）
├── data/                ← 共通（CSV, fmp_enrichment.json）
├── buy/                 ← 既存 run.py, template.html をここに整理してもよい
│   └── results/
└── sell/                ← 新規
    ├── run.py            ← 新規: 売りマトリックス生成ロジック
    ├── template.html      ← 新規: 出口戦略用ダッシュボードテンプレート
    └── results/
```

CSVと補足データ（インサイダー・機関投資家・ニュース）は買い側と共有して問題ない。銘柄リストもconfig.jsonを共有すればTXG追加のような変更が両方に自動反映される。

## 7. 自動更新タスクについて

買い側は「毎朝自動でダッシュボード再生成→GitHub Pages公開」のスケジュールタスクとして動いている。売り側を作ったら、同様のタスクを別途スケジュール登録するか、既存タスクに売り生成のステップを追加するかを決めること（scheduleスキルを使用）。

---

以上を新しいチャットに貼り付けて、「これを踏まえて売りシグナル（出口戦略）マトリックスを設計・実装してほしい」と伝えれば、ゼロから調査し直すことなく始められます。
