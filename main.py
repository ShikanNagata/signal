#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 売買シグナル統合ランナー（GitHub Actions / ローカル共用）
=====================================================================
処理の流れ:
  1. yfinance で株価を取得し data/*.csv を更新（買い・売り共通データ）
  2. 買いマトリックス生成  … run.py      → results/matrix_YYYY-MM-DD.html
  3. 売りマトリックス生成  … sell/run.py → sell/results/exit_YYYY-MM-DD.html
  4. docs/ へ公開用コピー（GitHub Pages: /signal/ と /signal/exit/）
  5. 最新シグナルJSON＋トレードルールを Gemini API に渡しレポート生成
  6. Gmail で自分宛にレポート送信

デザインについて:
  HTMLは template.html / sell/template.html の __DATA__ 等を
  文字列置換で穴埋めするだけ（既存 run.py の仕組みをそのまま使用）。
  LLMがHTMLを生成する工程は一切ないため、デザインは絶対に崩れない。

使い方:
  python main.py                 # 全部実行
  python main.py --skip-fetch    # 株価取得をスキップ（既存CSV使用）
  python main.py --skip-gemini   # Gemini呼び出しなし（ルールベース要約で代替）
  python main.py --skip-email    # メール送信なし（ローカルテスト用）

環境変数（GitHub Secrets から渡す）:
  GEMINI_API_KEY      … Google AI Studio のAPIキー
  GEMINI_MODEL        … 省略時 gemini-3.5-flash（失敗時は自動フォールバック）
  GMAIL_ADDRESS       … 送信元Gmailアドレス
  GMAIL_APP_PASSWORD  … Gmailのアプリパスワード（16桁）
  MAIL_TO             … 省略時 GMAIL_ADDRESS と同じ
=====================================================================
"""

import argparse
import importlib.util
import json
import os
import smtplib
import ssl
import sys
import traceback
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)  # run.py / sell/run.py は相対パス前提なのでルートに固定

SITE_URL = "https://shikannagata.github.io/signal/"
NOINDEX_TAG = '<meta name="robots" content="noindex, nofollow">'

# モデルは環境変数で差し替え可。先頭から順に試す（提供終了時の保険）
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]

TRADE_RULES = """あなたは私の専属トレードアシスタントです。提供されたJSONデータに基づき、以下の行動フローに従って各銘柄の「本日のアクション」を簡潔にまとめたレポートを出力してください。

■ 売りルール（下落の危険度チェック：sell_side.mom_bucket を使用）
- Lv0 平穏 ＆ Lv1 軟調：ここでは何もしない（追加買いの有無は買いルールで判定）
- Lv2 加速：買い増し停止。大きい保有・含み益銘柄を1/3減らす
- Lv3 急落：パニック売りはしない。防衛線を割った分だけ1/3減らす。買い増しは禁止

■ 買いルール（反転確認：buy_side の rsi_turn, ma5_cross, macd_up の3つのTrueの数を使用）
- Trueが0〜1個：買わない。現金維持。
- Trueが2個：予定額の1/4だけ打診買い
- Trueが3個：もう1/4を追加

■ 追加データの読み方（hold_outlook_current_bucket）
「現在と同じ下落レベルだった過去の日に、売らずにN日持ち続けたらどうなったか」の統計:
- avg_ret_pct: N日後の平均リターン(%) / loss_rate_pct: N日後に値下がりしていた確率(%)
- p10_pct: 悪い方から10%のシナリオ（10回に1回はこれ以下を食らう）
- mae_med_pct / mae_p10_pct: N日間の途中でつけた最大の下振れ（中央値／悪い10%）
- 各期間の avg_pnl_usd / p10_loss_usd: 保有評価額に換算した平均損益・悪い10%シナリオの損失（ドル）
- holding の value_usd=現在の評価額 / pnl_usd=取得来損益＄ / day_change_usd=前日比＄
- トップレベルの usdjpy: ドル円レート。円換算はこれを掛けた概算を使うこと（自分で為替を推測しない）
- 金額に言及するときはドルと円の両方を書くこと（例: -$437（約-6.6万円））

保有中の銘柄には必ず「いま売らずに持ち続けた場合の見通し」を1〜2行入れること。
例:「10日持つと平均+2.7%だが43%の確率で下落。悪い10%なら-9.3%（約$230の含み減）。しかも途中では中央値でも-11%掘るので、そこで狼狽売りしない覚悟が必要」
平均・下落確率・悪い10%の3点セットで判断できるように書くこと。

出力形式: 銘柄ごとに「本日のアクション」を1〜3行＋保有銘柄は上記の見通し1〜2行。最後に全体サマリーを2〜3行。
保有していない銘柄（holdingがnull）に売りアクションは不要。日本語で簡潔に。
最後に「数字は過去統計であり将来を保証しない」旨を一言添える。"""


# ===================== 1. 株価CSV更新 =====================

def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def update_csvs(cfg):
    """yfinanceで取得してdata/*.csvを上書き。失敗した銘柄は既存CSVを温存。"""
    try:
        import yfinance as yf
    except ImportError:
        print("⚠ yfinance未インストール。既存CSVをそのまま使います。")
        return
    import pandas as pd
    period = cfg.get("history_period", "550d")
    os.makedirs("data", exist_ok=True)
    for t in cfg["tickers"]:
        try:
            df = yf.Ticker(t).history(period=period)
            if df.empty:
                raise ValueError("empty dataframe")
            df = df.reset_index()
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
            df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
            if len(df) < 250:
                raise ValueError(f"rows={len(df)} too few")
            df.to_csv(os.path.join("data", f"{t}.csv"), index=False)
            print(f"  ✓ {t}: {len(df)}日分更新 (〜{df['Date'].max().date()})")
        except Exception as e:
            print(f"  ⚠ {t}: 取得失敗 ({e}) → 既存CSVを使用")


def fetch_usdjpy():
    """ドル円レート取得（失敗したらNone＝円換算なしで続行）"""
    try:
        import yfinance as yf
        h = yf.Ticker("USDJPY=X").history(period="5d")
        rate = round(float(h["Close"].iloc[-1]), 2)
        print(f"  ✓ USD/JPY = {rate}")
        return rate
    except Exception as e:
        print(f"  ⚠ ドル円取得失敗 ({e}) → 円換算なしで続行")
        return None


def prev_close(ticker):
    """CSVの最後から2行目の終値（前日比計算用）"""
    try:
        with open(os.path.join("data", f"{ticker}.csv"), encoding="utf-8") as f:
            rows = f.read().strip().splitlines()
        return float(rows[-2].split(",")[4])
    except Exception:
        return None


# ===================== 2-3. 買い/売りマトリックス生成 =====================

def run_buy():
    import run as buy
    buy.HAS_YF = False  # CSVを唯一のデータ源にして売り側と完全一致させる
    buy.HTML_TEMPLATE = buy._load_template()
    if not buy.HTML_TEMPLATE:
        raise RuntimeError("template.html が見つかりません")
    buy.main()


def run_sell():
    spec = importlib.util.spec_from_file_location(
        "sell_run", os.path.join(ROOT, "sell", "run.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


# ===================== 4. docs/ へ公開用コピー =====================

def _copy_with_noindex(src, dst):
    with open(src, encoding="utf-8") as f:
        html = f.read()
    if NOINDEX_TAG not in html:
        html = html.replace("<head>", "<head>\n" + NOINDEX_TAG, 1)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ {dst}")


def publish(today):
    buy_html = os.path.join("results", f"matrix_{today}.html")
    sell_html = os.path.join("sell", "results", f"exit_{today}.html")
    if not os.path.exists(buy_html) or not os.path.exists(sell_html):
        raise RuntimeError("生成されたHTMLが見つかりません")
    _copy_with_noindex(buy_html, os.path.join("docs", "index.html"))
    _copy_with_noindex(sell_html, os.path.join("docs", "exit", "index.html"))


# ===================== 5. Gemini用サマリーJSON =====================

def build_summary(today, usdjpy=None):
    with open(os.path.join("results", f"matrix_{today}.json"), encoding="utf-8") as f:
        buy = json.load(f)
    with open(os.path.join("sell", "results", f"exit_{today}.json"), encoding="utf-8") as f:
        sell = json.load(f)["tickers"]

    summary = {"date": today, "usdjpy": usdjpy, "tickers": []}
    for t, b in buy.items():
        cur = b["current"]
        trig = b.get("trigger_analysis", {}).get("today", {})
        true_count = sum(1 for k in ("rsi_turn", "ma5_cross", "macd_up") if trig.get(k))
        s = sell.get(t, {}).get("current", {})
        holding = None
        if cur.get("holding_qty"):
            holding = {"qty": cur["holding_qty"],
                       "avg_cost_usd": cur["holding_avg"],
                       "pnl_pct": cur["holding_pnl_pct"]}

        # 「売らずにN日持ったら」統計（現在の下落レベルの行だけ抜粋）
        bucket = s.get("mom_bucket")
        mtx_cells = sell.get(t, {}).get("matrix", {}).get("cells", {})
        mae_rows = (sell.get(t, {}).get("mae", {}) or {}).get(bucket, {}) if bucket else {}
        outlook = {}
        for d in (5, 10, 20):
            cell = mtx_cells.get(f"{d}_{bucket}") if bucket else None
            if not cell:
                continue
            o = {"avg_ret_pct": cell["avg_ret"],
                 "loss_rate_pct": cell["loss_rate"],
                 "p10_pct": cell["p10"]}
            m = mae_rows.get(str(d))
            if m:
                o["mae_med_pct"] = m["med"]
                o["mae_p10_pct"] = m["p10"]
            outlook[f"{d}d"] = o
        if holding:
            value = holding["qty"] * cur["price"]
            cost = holding["qty"] * holding["avg_cost_usd"]
            holding["value_usd"] = round(value)
            holding["pnl_usd"] = round(value - cost)
            pc = prev_close(t)
            if pc:
                holding["day_change_usd"] = round((cur["price"] - pc) * holding["qty"])
                holding["day_change_pct"] = round((cur["price"] / pc - 1) * 100, 2)
            for o in outlook.values():
                o["avg_pnl_usd"] = round(value * o["avg_ret_pct"] / 100)
                o["p10_loss_usd"] = round(value * o["p10_pct"] / 100)
        summary["tickers"].append({
            "ticker": t,
            "price": cur["price"],
            "holding": holding,
            "sell_side": {
                "mom_bucket": s.get("mom_bucket"),
                "mom_score": s.get("mom_score"),
                "streak_days": sell.get(t, {}).get("current_streak"),
                "ret5_pct": s.get("ret5"),
                "drawdown60d_pct": s.get("dd60"),
                "vol_zone": s.get("vol_zone"),
            },
            "hold_outlook_current_bucket": outlook or None,
            "buy_side": {
                "bucket": cur.get("bucket"),
                "sig_score": cur.get("sig_score"),
                "rsi": cur.get("rsi"),
                "rsi_turn": bool(trig.get("rsi_turn")),
                "ma5_cross": bool(trig.get("ma5_cross")),
                "macd_up": bool(trig.get("macd_up")),
                "true_count": true_count,
            },
            "sector_breadth_today": b.get("sector_today"),
        })
    return summary


def holdings_section(summary):
    """Pythonで確定計算した保有状況（Geminiに依存しない正確な数字のセクション）"""
    rate = summary.get("usdjpy")

    def jpy(usd):
        if rate is None:
            return ""
        v = usd * rate
        return f"（約{v/10000:+,.1f}万円）" if abs(v) >= 10000 else f"（約{v:+,.0f}円）"

    lines = ["【保有状況】" + (f"USD/JPY={rate}" if rate else "（円換算レート取得失敗のためドルのみ）")]
    tot_v = tot_p = 0.0
    for tk in summary["tickers"]:
        h = tk.get("holding")
        if not h:
            continue
        v, p = h["value_usd"], h["pnl_usd"]
        tot_v += v
        tot_p += p
        dc = h.get("day_change_usd")
        day = f" / 前日比 {dc:+,}$" if dc is not None else ""
        lines.append(f"■ {tk['ticker']}: {h['qty']}株 × ${tk['price']} = ${v:,}{jpy(v).replace('+','')}")
        lines.append(f"   取得来 {p:+,}$（{h['pnl_pct']:+.1f}%）{jpy(p)}{day}")
        ol = tk.get("hold_outlook_current_bucket") or {}
        for key, label in (("10d", "10日後"), ("20d", "20日後")):
            o = ol.get(key)
            if o and "avg_pnl_usd" in o:
                lines.append(
                    f"   売らずに{label}: 平均 {o['avg_pnl_usd']:+,}$ {jpy(o['avg_pnl_usd'])}"
                    f" / 悪い10%だと {o['p10_loss_usd']:+,}$ {jpy(o['p10_loss_usd'])}"
                    f"（下落確率{o['loss_rate_pct']:.0f}%）")
    if tot_v:
        lines.append("―" * 20)
        lines.append(f"合計評価額 ${tot_v:,.0f}{jpy(tot_v).replace('+','')} / 取得来 {tot_p:+,.0f}$ {jpy(tot_p)}")
    lines.append("※過去統計に基づく参考値。将来を保証するものではありません")
    return "\n".join(lines)


# ===================== 6. Gemini API =====================

def call_gemini(summary):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("⚠ GEMINI_API_KEY未設定 → ルールベース要約で代替")
        return None
    prompt = (TRADE_RULES + "\n\n【本日のシグナルデータ（JSON）】\n"
              + json.dumps(summary, ensure_ascii=False, indent=1))
    models = [os.environ.get("GEMINI_MODEL", "").strip() or GEMINI_FALLBACK_MODELS[0]]
    models += [m for m in GEMINI_FALLBACK_MODELS if m not in models]
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": api_key})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.load(r)
            text = "".join(p.get("text", "")
                           for p in res["candidates"][0]["content"]["parts"])
            if text.strip():
                print(f"  ✓ Gemini ({model}) でレポート生成")
                return text.strip()
        except Exception as e:
            msg = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    msg = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
            print(f"  ⚠ {model} 失敗: {e} {msg} → 次のモデルを試行")
    print("⚠ 全Geminiモデル失敗 → ルールベース要約で代替")
    return None


def rule_based_report(summary):
    """Geminiが使えない日でもメールが届くようにする保険（ルールをそのまま機械適用）"""
    lines = ["（Gemini不通のためルールを機械適用した自動要約です）", ""]
    for tk in summary["tickers"]:
        t, sell_b = tk["ticker"], tk["sell_side"]["mom_bucket"] or "?"
        n = tk["buy_side"]["true_count"]
        held = tk["holding"] is not None
        if sell_b.startswith("Lv3"):
            act = "急落。パニック売り禁止。防衛線を割った分だけ1/3減。買い増し禁止" if held else "急落中。買い増し禁止（新規も見送り）"
        elif sell_b.startswith("Lv2"):
            act = "加速。買い増し停止。大きい保有・含み益なら1/3減を検討" if held else "下落加速中。新規買いは停止"
        else:
            act = {0: "買わない。現金維持", 1: "買わない。現金維持",
                   2: "反転シグナル2つ点灯 → 予定額の1/4だけ打診買い",
                   3: "反転シグナル3つ点灯 → もう1/4を追加"}[n]
            if not held and n < 2:
                act = "様子見（アクションなし）"
        pnl = f" 損益{tk['holding']['pnl_pct']:+.1f}%" if held else ""
        lines.append(f"■ {t} (${tk['price']}{pnl}) [{sell_b} / 買いTrue{n}個]")
        lines.append(f"   → {act}")
        ol = tk.get("hold_outlook_current_bucket") or {}
        d10 = ol.get("10d")
        if held and d10:
            usd = f"（約${abs(d10['p10_loss_usd']):,}の含み減）" if d10.get("p10_loss_usd") else ""
            lines.append(f"   売らずに10日持った過去統計: 平均{d10['avg_ret_pct']:+.1f}% / "
                         f"下落確率{d10['loss_rate_pct']:.0f}% / 悪い10%で{d10['p10_pct']:+.1f}%{usd}")
    return "\n".join(lines)


# ===================== 7. Gmail送信 =====================

def send_mail(subject, body):
    addr = os.environ.get("GMAIL_ADDRESS", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or addr
    if not addr or not pw:
        print("⚠ GMAIL_ADDRESS / GMAIL_APP_PASSWORD 未設定 → メール送信スキップ")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = addr
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context()) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to], msg.as_string())
    print(f"  ✓ メール送信完了 → {to}")


# ===================== メイン =====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-gemini", action="store_true")
    ap.add_argument("--skip-email", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    today = datetime.now().strftime("%Y-%m-%d")

    print("\n===== 1/6 株価データ更新 =====")
    if args.skip_fetch:
        print("  （スキップ：既存CSVを使用）")
    else:
        update_csvs(cfg)

    print("\n===== 2/6 買いマトリックス生成 =====")
    run_buy()

    print("\n===== 3/6 売りマトリックス生成 =====")
    run_sell()

    print("\n===== 4/6 公開用コピー (docs/) =====")
    publish(today)

    print("\n===== 5/6 AIレポート生成 =====")
    usdjpy = fetch_usdjpy()
    summary = build_summary(today, usdjpy)
    report = None if args.skip_gemini else call_gemini(summary)
    if report is None:
        report = rule_based_report(summary)

    lv3 = sum(1 for x in summary["tickers"]
              if (x["sell_side"]["mom_bucket"] or "").startswith("Lv3"))
    lv2 = sum(1 for x in summary["tickers"]
              if (x["sell_side"]["mom_bucket"] or "").startswith("Lv2"))
    buys = sum(1 for x in summary["tickers"] if x["buy_side"]["true_count"] >= 2)
    subject = f"【売買シグナル】{today} | Lv3:{lv3} Lv2:{lv2} | 買い候補:{buys}"
    body = (f"本日のトレードアシスタントレポート（{today}）\n"
            f"買いダッシュボード: {SITE_URL}\n"
            f"売りダッシュボード: {SITE_URL}exit/\n"
            + "=" * 40 + "\n"
            + holdings_section(summary) + "\n"
            + "=" * 40 + "\n\n" + report + "\n\n" + "=" * 40
            + "\n\n【本日の生データ】\n"
            + json.dumps(summary, ensure_ascii=False, indent=1))

    print("\n----- レポート本文 -----\n" + report + "\n------------------------")

    print("\n===== 6/6 メール送信 =====")
    if args.skip_email:
        print("  （スキップ）")
    else:
        send_mail(subject, body)

    print("\n✅ 全処理完了")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
