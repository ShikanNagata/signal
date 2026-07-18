#!/usr/bin/env python3
"""
=====================================================================
 『俺の買いシグナル』3x3 期待値マトリックス 自動生成ツール
 (v2: エピソードベース押し目分析つき)
=====================================================================
使い方: python run.py
=====================================================================
"""

import json
import os
import sys
from datetime import datetime

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("pip install pandas numpy")
    sys.exit(1)

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


def load_config(path="config.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_data(ticker, period):
    if not HAS_YF:
        return None
    try:
        df = yf.Ticker(ticker).history(period=period)
        if df.empty:
            print(f"  warning  {ticker}: データが空でした")
            return None
        df = df.reset_index()
        df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"  warning  {ticker}: 取得失敗 ({e})")
        return None


def load_csv_fallback(ticker):
    path = os.path.join("data", f"{ticker}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["Date"] = pd.to_datetime(df["Date"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("Date").dropna(subset=["Close"]).reset_index(drop=True)


def calc_indicators(df, cfg):
    df = df.copy()
    rsi_p = cfg.get("rsi_period", 14)
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(rsi_p).mean()
    loss = -delta.clip(upper=0).rolling(rsi_p).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]
    for w in [5, 20, 50, 200]:
        df[f"MA{w}"] = df["Close"].rolling(w).mean()
        df[f"dev_MA{w}"] = (df["Close"] / df[f"MA{w}"] - 1) * 100
    df["vol_ratio"] = df["Volume"] / df["Volume"].rolling(40).median()
    zones = cfg.get("volume_zones", {})
    ig = zones.get("Ignition", 3.0)
    ac = zones.get("Active", 1.5)
    hi = zones.get("High", 0.8)
    def vz(r):
        if pd.isna(r): return None
        if r >= ig: return "Ignition"
        if r >= ac: return "Active"
        if r >= hi: return "High"
        return "Normal"
    df["vol_zone"] = df["vol_ratio"].apply(vz)
    def score(row):
        if pd.isna(row.get("MA200")):
            mas = ["MA5", "MA20", "MA50"]
        else:
            mas = ["MA5", "MA50", "MA200"]
        s = 0
        for m in mas:
            if pd.isna(row[m]): return None
            if row["Close"] >= row[m]: s += 1
        return s
    df["sig_score"] = df.apply(score, axis=1)
    return df


def signal_bucket(score, rsi, overheated=70):
    if pd.isna(score) or pd.isna(rsi):
        return None
    if score == 3 and rsi < overheated: return "3/3 健全"
    if score == 3 and rsi >= overheated: return "3/3 過熱"
    if score == 2: return "2/3 警戒"
    if score == 1: return "1/3 弱い"
    if score == 0: return "0/3 底値"
    return None


def backtest(df, cfg, has_ma200=True):
    hold_days = cfg.get("hold_days", [5, 10, 20, 40, 80])
    overheated = cfg.get("rsi_overheated", 70)
    min_col = "MA200" if has_ma200 else "MA50"
    rows = []
    for i in range(len(df)):
        if pd.isna(df.iloc[i][min_col]):
            continue
        e = df.iloc[i]
        bucket = signal_bucket(e["sig_score"], e["RSI"], overheated)
        if bucket is None:
            continue
        for d in hold_days:
            if i + d >= len(df): continue
            ret = (df.iloc[i + d]["Close"] / e["Close"] - 1) * 100
            rows.append({"hold_days": d, "return_pct": ret, "bucket": bucket})
    return pd.DataFrame(rows)


def make_matrix(bt, cfg):
    hold_days = cfg.get("hold_days", [5, 10, 20, 40, 80])
    buckets = ["3/3 健全", "3/3 過熱", "2/3 警戒", "1/3 弱い", "0/3 底値"]
    cells = {}
    for d in hold_days:
        sub = bt[bt["hold_days"] == d]
        for b in buckets:
            c = sub[sub["bucket"] == b]
            if len(c) == 0:
                cells[f"{d}_{b}"] = None
            else:
                cells[f"{d}_{b}"] = {
                    "n": len(c),
                    "avg_ret": round(c["return_pct"].mean(), 2),
                    "win_rate": round((c["return_pct"] > 0).mean() * 100, 1),
                    "best": round(c["return_pct"].max(), 2),
                    "worst": round(c["return_pct"].min(), 2),
                }
    return {"days": hold_days, "buckets": buckets, "cells": cells}


def compute_bucket_col(df, cfg):
    overheated = cfg.get("rsi_overheated", 70)
    df["bucket_col"] = df.apply(
        lambda r: signal_bucket(r["sig_score"], r["RSI"], overheated)
        if (not pd.isna(r["sig_score"]) and not pd.isna(r["RSI"])) else None,
        axis=1
    )
    return df


def find_episodes(df):
    """同一バケットの連続区間（エピソード）を検出。重複サンプル問題への対処の基本単位"""
    eps, cur = [], None
    for i in range(len(df)):
        b = df.iloc[i]["bucket_col"]
        if b is None or (isinstance(b, float) and pd.isna(b)):
            continue
        if cur is None or b != cur["bucket"]:
            if cur:
                eps.append(cur)
            cur = {"bucket": b, "start_i": i, "end_i": i}
        else:
            cur["end_i"] = i
    if cur:
        eps.append(cur)
    return eps


def _ret_cells(df, starts, hold_days):
    """指定した起点インデックス群からの保有日数別リターン統計"""
    cells = {}
    for d in hold_days:
        rets = [(df.iloc[i + d]["Close"] / df.iloc[i]["Close"] - 1) * 100
                for i in starts if i + d < len(df)]
        if rets:
            cells[str(d)] = {
                "n": len(rets),
                "avg_ret": round(float(np.mean(rets)), 2),
                "win_rate": round(float(np.mean([r > 0 for r in rets])) * 100, 1),
                "best": round(max(rets), 2),
                "worst": round(min(rets), 2),
            }
        else:
            cells[str(d)] = None
    return cells


def episode_analysis(df, eps, cfg):
    """エピソード起点ベースの期待値 + MA200上下（押し目 vs 崩れ）の分割"""
    hold_days = cfg.get("hold_days", [5, 10, 20, 40, 80])
    buckets = ["3/3 健全", "3/3 過熱", "2/3 警戒", "1/3 弱い", "0/3 底値"]
    out = {}
    for b in buckets:
        starts = [e["start_i"] for e in eps if e["bucket"] == b]
        if not starts:
            continue
        ma_ok = [i for i in starts if not pd.isna(df.iloc[i]["MA200"])]
        above = [i for i in ma_ok if df.iloc[i]["Close"] >= df.iloc[i]["MA200"]]
        below = [i for i in ma_ok if df.iloc[i]["Close"] < df.iloc[i]["MA200"]]
        out[b] = {
            "n_ep": len(starts),
            "entry": _ret_cells(df, starts, hold_days),
            "above_ma200": {"n_ep": len(above), "cells": _ret_cells(df, above, hold_days)} if above else None,
            "below_ma200": {"n_ep": len(below), "cells": _ret_cells(df, below, hold_days)} if below else None,
        }
    return out


def trigger_analysis(df, cfg):
    """弱いシグナル日(sig_score<=1)で反転トリガー点灯日 vs 非点灯日の成績比較（日ベース・重複あり）"""
    hold_days = cfg.get("hold_days", [5, 10, 20, 40, 80])
    oversold = cfg.get("rsi_oversold", 30)
    rsi_turn = (df["RSI"].shift(1) < oversold) & (df["RSI"] > df["RSI"].shift(1))
    ma5_cross = (df["Close"] > df["MA5"]) & (df["Close"].shift(1) <= df["MA5"].shift(1))
    macd_up = (df["MACD_hist"] > df["MACD_hist"].shift(1)) & \
              (df["MACD_hist"].shift(1) > df["MACD_hist"].shift(2))
    trig = (rsi_turn | ma5_cross | macd_up).fillna(False)
    weak = df["sig_score"].notna() & (df["sig_score"] <= 1) & df["bucket_col"].notna()
    with_t = [i for i in range(len(df)) if weak.iloc[i] and trig.iloc[i]]
    no_t = [i for i in range(len(df)) if weak.iloc[i] and not trig.iloc[i]]
    last = len(df) - 1
    today = {
        "rsi_turn": bool(rsi_turn.iloc[last]) if last >= 1 else False,
        "ma5_cross": bool(ma5_cross.iloc[last]) if last >= 1 else False,
        "macd_up": bool(macd_up.iloc[last]) if last >= 2 else False,
    }
    return {
        "oversold": oversold,
        "today": today,
        "with_trigger": {"n_days": len(with_t), "cells": _ret_cells(df, with_t, hold_days)},
        "no_trigger": {"n_days": len(no_t), "cells": _ret_cells(df, no_t, hold_days)},
    }


def dip_depth_analysis(df, eps):
    """弱いバケットのエピソードでMA20乖離が最終的にどこまで掘れたか（分割エントリーの目安）"""
    out = {}
    for b in ["2/3 警戒", "1/3 弱い", "0/3 底値"]:
        depths = []
        for e in eps:
            if e["bucket"] != b:
                continue
            seg = df.iloc[e["start_i"]:e["end_i"] + 1]["dev_MA20"].dropna()
            if len(seg):
                depths.append(float(seg.min()))
        if depths:
            out[b] = {
                "n_ep": len(depths),
                "median": round(float(np.median(depths)), 1),
                "worst": round(min(depths), 1),
            }
    return out


def compute_breadth(dfs):
    """日付ごとのセクター騰落（何銘柄下落したか）。全面安と個別安の判別に使う"""
    breadth = {}
    for t, df in dfs.items():
        chg = df["Close"].pct_change()
        for i in range(1, len(df)):
            if pd.isna(chg.iloc[i]):
                continue
            k = df.iloc[i]["Date"].strftime("%Y-%m-%d")
            d0 = breadth.setdefault(k, {"down": 0, "total": 0, "down_tickers": []})
            d0["total"] += 1
            if chg.iloc[i] < 0:
                d0["down"] += 1
                d0["down_tickers"].append(t)
    return breadth


def recent_weak_episodes(df, eps, breadth, limit=3):
    """直近の弱いエピソード: 深さ・セクター全面安か・起点から20日後リターン"""
    weak_eps = [e for e in eps if e["bucket"] in ("1/3 弱い", "0/3 底値")]
    items = []
    for e in weak_eps[-limit:]:
        i0, i1 = e["start_i"], e["end_i"]
        seg = df.iloc[i0:i1 + 1]["dev_MA20"].dropna()
        start_key = df.iloc[i0]["Date"].strftime("%Y-%m-%d")
        ret20 = None
        if i0 + 20 < len(df):
            ret20 = round(float(df.iloc[i0 + 20]["Close"] / df.iloc[i0]["Close"] - 1) * 100, 1)
        binfo = breadth.get(start_key)
        items.append({
            "bucket": e["bucket"],
            "start": start_key,
            "end": df.iloc[i1]["Date"].strftime("%Y-%m-%d"),
            "days": int(i1 - i0 + 1),
            "depth": round(float(seg.min()), 1) if len(seg) else None,
            "sector_wide": bool(binfo and binfo["total"] >= 2 and binfo["down"] / binfo["total"] >= 0.75),
            "ret_20d": ret20,
        })
    return items


def load_fmp_enrichment():
    path = os.path.join("data", "fmp_enrichment.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main():
    print("=" * 60)
    print(" 『俺の買いシグナル』3x3 マトリックス生成ツール")
    print("=" * 60)
    cfg = load_config()
    tickers = cfg["tickers"]
    period = cfg.get("history_period", "550d")
    today = datetime.now().strftime("%Y-%m-%d")
    enrichment = load_fmp_enrichment()
    results = {}

    # ===== パス1: 全銘柄を先に読み込む（セクター連動判定のため） =====
    dfs = {}
    for t in tickers:
        print(f"\n▶ {t} を読み込み中...")
        df = fetch_data(t, period)
        if df is None:
            df = load_csv_fallback(t)
            if df is not None:
                print(f"  📁 data/{t}.csv から読み込みました")
        if df is None:
            print(f"  ❌ {t}: データ取得できず、スキップ")
            continue
        print(f"  ✓ {len(df)}日分 ({df['Date'].min().date()} 〜 {df['Date'].max().date()})")
        df = calc_indicators(df, cfg)
        df = compute_bucket_col(df, cfg)
        dfs[t] = df

    breadth = compute_breadth(dfs)

    # ===== パス2: 銘柄ごとの分析 =====
    for t, df in dfs.items():
        print(f"\n▶ {t} を分析中...")
        has_ma200 = not pd.isna(df["MA200"].iloc[-1])
        bt = backtest(df, cfg, has_ma200)
        matrix = make_matrix(bt, cfg)
        eps = find_episodes(df)

        # バケット推移履歴と継続日数
        streaks, streak_cnt, prev_b = [], 0, None
        for b in df["bucket_col"]:
            if b is None:
                streaks.append(0)
            elif b == prev_b:
                streak_cnt += 1
                streaks.append(streak_cnt)
            else:
                streak_cnt = 1
                prev_b = b
                streaks.append(streak_cnt)
        df["b_streak"] = streaks
        bucket_history = [
            {"date": row["Date"].strftime("%Y-%m-%d"), "bucket": row["bucket_col"], "streak": int(row["b_streak"]), "price": round(float(row["Close"]), 2)}
            for _, row in df.iterrows() if row["bucket_col"] is not None
        ]
        dur_map = {}
        if bucket_history:
            run_bk, run_n = bucket_history[0]["bucket"], 1
            for bh in bucket_history[1:]:
                if bh["bucket"] == run_bk:
                    run_n += 1
                else:
                    dur_map.setdefault(run_bk, []).append(run_n)
                    run_bk, run_n = bh["bucket"], 1
            dur_map.setdefault(run_bk, []).append(run_n)
        bucket_duration_stats = {
            bk: {"avg": round(float(np.mean(runs)), 1), "max": int(max(runs)), "count": len(runs)}
            for bk, runs in dur_map.items()
        }
        current_streak = bucket_history[-1]["streak"] if bucket_history else 0

        last = df.iloc[-1]
        def ma_val(col):
            v = last.get(col)
            return round(float(v), 2) if v is not None and not pd.isna(v) else None
        cur = {
            "date": last["Date"].strftime("%Y-%m-%d"),
            "price": round(float(last["Close"]), 2),
            "rsi": round(float(last["RSI"]), 1) if not pd.isna(last["RSI"]) else None,
            "sig_score": int(last["sig_score"]) if not pd.isna(last["sig_score"]) else None,
            "ma5":   ma_val("MA5"),
            "ma20":  ma_val("MA20"),
            "ma50":  ma_val("MA50"),
            "ma200": ma_val("MA200"),
            "dev_MA5":   round(float(last["dev_MA5"]),  1) if not pd.isna(last["dev_MA5"])  else None,
            "dev_MA20":  round(float(last["dev_MA20"]), 1) if not pd.isna(last["dev_MA20"]) else None,
            "dev_MA50":  round(float(last["dev_MA50"]), 1) if not pd.isna(last["dev_MA50"]) else None,
            "dev_MA200": round(float(last["dev_MA200"]),1) if not pd.isna(last["dev_MA200"])else None,
            "vol_zone": last["vol_zone"],
        }
        cur["bucket"] = signal_bucket(cur["sig_score"], cur["rsi"], cfg.get("rsi_overheated", 70))
        holding = cfg.get("my_holdings", {}).get(t)
        if holding and holding.get("qty", 0) > 0:
            pnl_pct = (cur["price"] / holding["avg_cost_usd"] - 1) * 100
            cur["holding_qty"] = holding["qty"]
            cur["holding_avg"] = holding["avg_cost_usd"]
            cur["holding_pnl_pct"] = round(pnl_pct, 1)

        ep_buckets = set(e["bucket"] for e in eps)
        results[t] = {
            "matrix": matrix, "current": cur,
            "episodes_per_bucket": {b: sum(1 for e in eps if e["bucket"] == b) for b in ep_buckets},
            "episode_analysis": episode_analysis(df, eps, cfg),
            "trigger_analysis": trigger_analysis(df, cfg),
            "dip_depth": dip_depth_analysis(df, eps),
            "recent_weak_episodes": recent_weak_episodes(df, eps, breadth),
            "sector_today": breadth.get(cur["date"]),
            "bucket_history": bucket_history,
            "bucket_duration_stats": bucket_duration_stats,
            "current_streak": current_streak,
            "period_start": df["Date"].min().strftime("%Y-%m-%d"),
            "period_end": df["Date"].max().strftime("%Y-%m-%d"),
            "n_days": len(df), "has_ma200": has_ma200,
            "fmp": enrichment.get(t, {}),
        }
        print(f"  📊 現在: ${cur['price']} / RSI {cur['rsi']} / {cur['bucket']} (継続{current_streak}日目)")

    if not results:
        print("\n❌ 有効なデータがありませんでした。終了します。")
        return

    outdir = cfg.get("output_dir", "results")
    os.makedirs(outdir, exist_ok=True)
    json_path = os.path.join(outdir, f"matrix_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 データ保存: {json_path}")
    html = build_html(results, today, cfg)
    html_path = os.path.join(outdir, f"matrix_{today}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"💾 ダッシュボード: {html_path}")
    print(f"\n✅ 完了！ {html_path} をブラウザで開いてください。")


def build_html(results, today, cfg):
    data_json = json.dumps(results, ensure_ascii=False)
    tickers = list(results.keys())
    first = tickers[0]
    tabs = "".join(
        f'<button class="tab{" active" if t == first else ""}" onclick="showTicker(\'{t}\')">{t}</button>'
        for t in tickers
    )
    template = HTML_TEMPLATE
    template = template.replace("__DATA__", data_json)
    template = template.replace("__TABS__", tabs)
    template = template.replace("__FIRST__", first)
    template = template.replace("__TODAY__", today)
    template = template.replace("__PERIOD__", f"{results[first]['period_start']} 〜 {results[first]['period_end']}")
    return template


def _load_template():
    if os.path.exists("template.html"):
        with open("template.html", encoding="utf-8") as f:
            return f.read()
    return ""


HTML_TEMPLATE = ""

if __name__ == "__main__":
    HTML_TEMPLATE = _load_template()
    if not HTML_TEMPLATE:
        print("❌ template.html が見つかりません。")
        sys.exit(1)
    main()
