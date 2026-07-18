#!/usr/bin/env python3
"""
=====================================================================
 『俺の売りシグナル』出口戦略マトリックス 自動生成ツール
 コンセプト: いま売らずに持ち続けたら、何日後にいくら損する可能性があるか
 軸: 下落モメンタム複合スコア（速度＋加速度＋ドローダウン＋出来高）
=====================================================================
使い方: python sell/run.py   （リポジトリルートから）
        python run.py        （sell/ から）
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELL_DIR = os.path.join(ROOT, "sell")

MOM_BUCKETS = ["Lv3 急落", "Lv2 加速", "Lv1 軟調", "Lv0 平穏"]


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_csv(ticker):
    path = os.path.join(ROOT, "data", f"{ticker}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["Date"] = pd.to_datetime(df["Date"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("Date").dropna(subset=["Close"]).reset_index(drop=True)


# ===================== 指標計算（買い側と共通ロジック） =====================

def calc_indicators(df, cfg):
    df = df.copy()
    rsi_p = cfg.get("rsi_period", 14)
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(rsi_p).mean()
    loss = -delta.clip(upper=0).rolling(rsi_p).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss))
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
    ig, ac, hi = zones.get("Ignition", 3.0), zones.get("Active", 1.5), zones.get("High", 0.8)

    def vz(r):
        if pd.isna(r): return None
        if r >= ig: return "Ignition"
        if r >= ac: return "Active"
        if r >= hi: return "High"
        return "Normal"
    df["vol_zone"] = df["vol_ratio"].apply(vz)

    def score(row):
        mas = ["MA5", "MA20", "MA50"] if pd.isna(row.get("MA200")) else ["MA5", "MA50", "MA200"]
        s = 0
        for m in mas:
            if pd.isna(row[m]): return None
            if row["Close"] >= row[m]: s += 1
        return s
    df["sig_score"] = df.apply(score, axis=1)
    return df


# ===================== 下落モメンタム複合スコア =====================

def calc_momentum(df):
    """速度・加速度・ドローダウン・出来高の4成分から0〜10点の下落スコアを合成"""
    df = df.copy()
    df["ret5"] = (df["Close"] / df["Close"].shift(5) - 1) * 100          # 速度: 直近5日騰落率
    df["accel"] = df["dev_MA20"] - df["dev_MA20"].shift(5)               # 加速度: MA20乖離の5日変化(pt)
    df["dd60"] = (df["Close"] / df["Close"].rolling(60, min_periods=20).max() - 1) * 100  # 60日高値DD
    df["chg1"] = df["Close"].pct_change() * 100

    def pts(row):
        if pd.isna(row["ret5"]) or pd.isna(row["accel"]) or pd.isna(row["dd60"]):
            return None
        p_speed = 3 if row["ret5"] <= -10 else 2 if row["ret5"] <= -5 else 1 if row["ret5"] <= -2 else 0
        p_accel = 2 if row["accel"] <= -8 else 1 if row["accel"] <= -4 else 0
        p_dd = 3 if row["dd60"] <= -25 else 2 if row["dd60"] <= -15 else 1 if row["dd60"] <= -7 else 0
        p_vol = 0
        if not pd.isna(row["chg1"]) and row["chg1"] < 0 and row["vol_zone"] in ("Ignition", "Active"):
            p_vol = 2 if row["vol_zone"] == "Ignition" else 1
        return (p_speed, p_accel, p_dd, p_vol)

    parts = df.apply(pts, axis=1)
    df["pt_speed"] = parts.apply(lambda x: x[0] if x else None)
    df["pt_accel"] = parts.apply(lambda x: x[1] if x else None)
    df["pt_dd"] = parts.apply(lambda x: x[2] if x else None)
    df["pt_vol"] = parts.apply(lambda x: x[3] if x else None)
    df["mom_score"] = parts.apply(lambda x: sum(x) if x else None)

    def bucket(s):
        if s is None or (isinstance(s, float) and pd.isna(s)): return None
        if s >= 6: return "Lv3 急落"
        if s >= 3: return "Lv2 加速"
        if s >= 1: return "Lv1 軟調"
        return "Lv0 平穏"
    df["mom_bucket"] = df["mom_score"].apply(bucket)
    return df


# ===================== バックテスト（全営業日・重い方） =====================

def _stats(rets):
    if not rets:
        return None
    a = np.array(rets)
    return {
        "n": int(len(a)),
        "avg_ret": round(float(a.mean()), 2),
        "med": round(float(np.median(a)), 2),
        "loss_rate": round(float((a < 0).mean()) * 100, 1),
        "p10": round(float(np.percentile(a, 10)), 2),
        "worst": round(float(a.min()), 2),
        "best": round(float(a.max()), 2),
        "p_down10": round(float((a <= -10).mean()) * 100, 1),
        "p_down20": round(float((a <= -20).mean()) * 100, 1),
    }


def backtest_matrix(df, hold_days):
    """各営業日について「その日売らずにN日持ち続けたら」のリターンをバケット別に全数集計"""
    cells = {}
    closes = df["Close"].values
    buckets = df["mom_bucket"].values
    for d in hold_days:
        pool = {b: [] for b in MOM_BUCKETS}
        for i in range(len(df) - d):
            b = buckets[i]
            if b is None or (isinstance(b, float) and pd.isna(b)):
                continue
            pool[b].append((closes[i + d] / closes[i] - 1) * 100)
        for b in MOM_BUCKETS:
            cells[f"{d}_{b}"] = _stats(pool[b])
    return {"days": hold_days, "buckets": MOM_BUCKETS, "cells": cells}


def backtest_mae(df, hold_days):
    """最大下振れ(MAE): N日以内につけた最安値までの下落率をバケット別に集計"""
    out = {b: {} for b in MOM_BUCKETS}
    closes = df["Close"].values
    lows = df["Low"].fillna(df["Close"]).values
    buckets = df["mom_bucket"].values
    for d in hold_days:
        pool = {b: [] for b in MOM_BUCKETS}
        for i in range(len(df) - d):
            b = buckets[i]
            if b is None or (isinstance(b, float) and pd.isna(b)):
                continue
            mn = lows[i + 1:i + d + 1].min()
            pool[b].append((mn / closes[i] - 1) * 100)
        for b in MOM_BUCKETS:
            if pool[b]:
                a = np.array(pool[b])
                out[b][str(d)] = {
                    "n": int(len(a)),
                    "med": round(float(np.median(a)), 2),
                    "p10": round(float(np.percentile(a, 10)), 2),
                    "worst": round(float(a.min()), 2),
                }
            else:
                out[b][str(d)] = None
    return out


def _ret_cells(df, starts, hold_days):
    cells = {}
    closes = df["Close"].values
    for d in hold_days:
        rets = [(closes[i + d] / closes[i] - 1) * 100 for i in starts if i + d < len(df)]
        cells[str(d)] = _stats(rets)
    return cells


def transition_analysis(df, hold_days):
    """買い側シグナルの転落瞬間（MA本数が減った日）からのその後のリターン分布"""
    ss = df["sig_score"].values
    defs = [
        ("3→2", "3本 → 2本以下に転落", lambda p, c: p == 3 and c <= 2),
        ("2→1", "2本 → 1本以下に転落", lambda p, c: p == 2 and c <= 1),
        ("1→0", "1本 → 0本に転落", lambda p, c: p == 1 and c == 0),
    ]
    out = {}
    for key, label, cond in defs:
        starts = []
        for i in range(1, len(df)):
            p, c = ss[i - 1], ss[i]
            if p is None or c is None or pd.isna(p) or pd.isna(c):
                continue
            if cond(int(p), int(c)):
                starts.append(i)
        out[key] = {"label": label, "n_events": len(starts), "cells": _ret_cells(df, starts, hold_days)}
    return out


def find_episodes(df, col="mom_bucket"):
    eps, cur = [], None
    vals = df[col].values
    for i in range(len(df)):
        b = vals[i]
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


def episode_analysis(df, eps, hold_days):
    """エピソード（同一Lvの連続区間）起点からのリターン + 区間中どこまで掘れたか"""
    out = {}
    closes = df["Close"].values
    for b in MOM_BUCKETS:
        bs = [e for e in eps if e["bucket"] == b]
        if not bs:
            continue
        starts = [e["start_i"] for e in bs]
        depths, durs = [], []
        for e in bs:
            seg = closes[e["start_i"]:e["end_i"] + 1]
            depths.append((seg.min() / closes[e["start_i"]] - 1) * 100)
            durs.append(e["end_i"] - e["start_i"] + 1)
        out[b] = {
            "n_ep": len(bs),
            "entry": _ret_cells(df, starts, hold_days),
            "depth_med": round(float(np.median(depths)), 1),
            "depth_worst": round(float(min(depths)), 1),
            "dur_avg": round(float(np.mean(durs)), 1),
            "dur_max": int(max(durs)),
        }
    return out


def compute_breadth(dfs):
    breadth = {}
    for t, df in dfs.items():
        chg = df["Close"].pct_change()
        for i in range(1, len(df)):
            if pd.isna(chg.iloc[i]):
                continue
            k = df.iloc[i]["Date"].strftime("%Y-%m-%d")
            d0 = breadth.setdefault(k, {"down": 0, "total": 0})
            d0["total"] += 1
            if chg.iloc[i] < 0:
                d0["down"] += 1
    return breadth


def load_fmp_enrichment():
    path = os.path.join(ROOT, "data", "fmp_enrichment.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def rnd(v, nd=1):
    return round(float(v), nd) if v is not None and not pd.isna(v) else None


def main():
    print("=" * 60)
    print(" 『俺の売りシグナル』出口戦略マトリックス 生成ツール")
    print("=" * 60)
    cfg = load_config()
    tickers = cfg["tickers"]
    hold_days = cfg.get("hold_days", [5, 10, 20, 40, 80])
    today = datetime.now().strftime("%Y-%m-%d")
    enrichment = load_fmp_enrichment()

    # パス1: 全銘柄読み込み（セクター連動判定のため）
    dfs = {}
    for t in tickers:
        print(f"\n▶ {t} を読み込み中...")
        df = load_csv(t)
        if df is None:
            print(f"  ❌ {t}: data/{t}.csv が見つからずスキップ")
            continue
        df = calc_indicators(df, cfg)
        df = calc_momentum(df)
        dfs[t] = df
        print(f"  ✓ {len(df)}日分 ({df['Date'].min().date()} 〜 {df['Date'].max().date()})")

    breadth = compute_breadth(dfs)
    results, pooled_rows = {}, []

    # パス2: 銘柄ごとの分析
    for t, df in dfs.items():
        print(f"\n▶ {t} を分析中...")
        matrix = backtest_matrix(df, hold_days)
        mae = backtest_mae(df, hold_days)
        trans = transition_analysis(df, hold_days)
        eps = find_episodes(df)
        ep_stats = episode_analysis(df, eps, hold_days)

        # モメンタム履歴（タイムライン用）と継続日数
        hist, streak, prev = [], 0, None
        for _, row in df.iterrows():
            b = row["mom_bucket"]
            if b is None:
                continue
            streak = streak + 1 if b == prev else 1
            prev = b
            hist.append({"date": row["Date"].strftime("%Y-%m-%d"), "bucket": b,
                         "streak": streak, "price": round(float(row["Close"]), 2)})
        dur_map = {}
        if hist:
            run_bk, run_n = hist[0]["bucket"], 1
            for hh in hist[1:]:
                if hh["bucket"] == run_bk:
                    run_n += 1
                else:
                    dur_map.setdefault(run_bk, []).append(run_n)
                    run_bk, run_n = hh["bucket"], 1
            dur_map.setdefault(run_bk, []).append(run_n)
        dur_stats = {bk: {"avg": round(float(np.mean(v)), 1), "max": int(max(v)), "count": len(v)}
                     for bk, v in dur_map.items()}

        last = df.iloc[-1]
        cur = {
            "date": last["Date"].strftime("%Y-%m-%d"),
            "price": round(float(last["Close"]), 2),
            "rsi": rnd(last["RSI"]),
            "sig_score": int(last["sig_score"]) if not pd.isna(last["sig_score"]) else None,
            "dev_MA20": rnd(last["dev_MA20"]),
            "dev_MA50": rnd(last["dev_MA50"]),
            "ret5": rnd(last["ret5"]),
            "accel": rnd(last["accel"]),
            "dd60": rnd(last["dd60"]),
            "vol_zone": last["vol_zone"],
            "mom_score": int(last["mom_score"]) if not pd.isna(last["mom_score"]) else None,
            "mom_bucket": last["mom_bucket"],
            "pts": {
                "speed": int(last["pt_speed"]) if not pd.isna(last["pt_speed"]) else None,
                "accel": int(last["pt_accel"]) if not pd.isna(last["pt_accel"]) else None,
                "dd": int(last["pt_dd"]) if not pd.isna(last["pt_dd"]) else None,
                "vol": int(last["pt_vol"]) if not pd.isna(last["pt_vol"]) else None,
            },
        }
        holding = cfg.get("my_holdings", {}).get(t)
        if holding and holding.get("qty", 0) > 0:
            cur["holding_qty"] = holding["qty"]
            cur["holding_avg"] = holding["avg_cost_usd"]
            cur["holding_pnl_pct"] = round((cur["price"] / holding["avg_cost_usd"] - 1) * 100, 1)

        bd = breadth.get(cur["date"])
        results[t] = {
            "matrix": matrix,
            "mae": mae,
            "transitions": trans,
            "episodes": ep_stats,
            "episodes_per_bucket": {b: sum(1 for e in eps if e["bucket"] == b) for b in set(e["bucket"] for e in eps)},
            "current": cur,
            "mom_history": hist,
            "duration_stats": dur_stats,
            "current_streak": hist[-1]["streak"] if hist else 0,
            "sector_today": bd,
            "period_start": df["Date"].min().strftime("%Y-%m-%d"),
            "period_end": df["Date"].max().strftime("%Y-%m-%d"),
            "n_days": len(df),
            "fmp": enrichment.get(t, {}),
        }
        # 全銘柄合算プール用
        pooled_rows.append(df[["mom_bucket", "sig_score", "Close", "Low"]].assign(_t=t))
        print(f"  📊 現在: ${cur['price']} / 下落スコア {cur['mom_score']}/10 / {cur['mom_bucket']} (継続{results[t]['current_streak']}日)")

    if not results:
        print("\n❌ 有効なデータがありませんでした。")
        return

    # ===== 全銘柄合算マトリックス（サンプル数を稼ぐ） =====
    print("\n▶ 全銘柄合算プールを集計中...")
    pooled_cells = {}
    for d in hold_days:
        pool = {b: [] for b in MOM_BUCKETS}
        for t, df in dfs.items():
            closes = df["Close"].values
            buckets = df["mom_bucket"].values
            for i in range(len(df) - d):
                b = buckets[i]
                if b is None or (isinstance(b, float) and pd.isna(b)):
                    continue
                pool[b].append((closes[i + d] / closes[i] - 1) * 100)
        for b in MOM_BUCKETS:
            pooled_cells[f"{d}_{b}"] = _stats(pool[b])
    pooled = {"days": hold_days, "buckets": MOM_BUCKETS, "cells": pooled_cells,
              "n_tickers": len(dfs)}

    out = {"tickers": results, "pooled": pooled, "generated": today, "days": hold_days}

    outdir = os.path.join(SELL_DIR, cfg.get("output_dir", "results"))
    os.makedirs(outdir, exist_ok=True)
    json_path = os.path.join(outdir, f"exit_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n💾 データ保存: {json_path}")

    tpl_path = os.path.join(SELL_DIR, "template.html")
    if not os.path.exists(tpl_path):
        print("❌ sell/template.html が見つかりません。")
        sys.exit(1)
    with open(tpl_path, encoding="utf-8") as f:
        template = f.read()
    tickers_list = list(results.keys())
    first = tickers_list[0]
    tabs = "".join(
        f'<button class="tab{" active" if t == first else ""}" onclick="showTicker(\'{t}\')">{t}</button>'
        for t in tickers_list
    )
    html = (template
            .replace("__DATA__", json.dumps(out, ensure_ascii=False))
            .replace("__TABS__", tabs)
            .replace("__FIRST__", first)
            .replace("__TODAY__", today)
            .replace("__PERIOD__", f"{results[first]['period_start']} 〜 {results[first]['period_end']}"))
    html_path = os.path.join(outdir, f"exit_{today}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"💾 ダッシュボード: {html_path}")
    print("\n✅ 完了！")


if __name__ == "__main__":
    main()
