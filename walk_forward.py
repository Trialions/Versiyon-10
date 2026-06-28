# walk_forward.py — TRBOT V8 — Walk-Forward Test
# Klasik walk-forward: aylik bagimsiz backtest segmentleri
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import yaml

from backtest import Backtester, _fetch_candles, _load_symbols, resolve_president_execution_mode, get_network_error_count
from backtest import Backtester, _fetch_candles, _load_symbols, get_network_error_count
from weekly_symbol_universe import select_universe_for_window, write_universe_history


def _load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_BINANCE_LAUNCH_MS = 1483228800000  # 2017-01-01 UTC


def _month_ranges(start_str: str, end_str: str) -> List[tuple]:
    """start_str..end_str arasindaki aylik (baslangic_ms, bitis_ms, ay_str) listesi doner."""
    import datetime
    start = datetime.datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.datetime.strptime(end_str, "%Y-%m-%d")
    ranges = []
    cur = start.replace(day=1)
    while cur <= end:
        # Ayin son gunu
        if cur.month == 12:
            nxt = cur.replace(year=cur.year+1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month+1, day=1)
        seg_end = min(nxt, end + datetime.timedelta(days=1))
        ranges.append((
            int(cur.timestamp() * 1000),
            int(seg_end.timestamp() * 1000),
            cur.strftime("%Y-%m"),
        ))
        cur = nxt
    return ranges


def run_walkforward(cfg: dict, symbols: List[str], start_str: str,
                    end_str: str, interval: str, out_dir: str):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # V9.0.3 FIX (sifir-mum teshisi): WF, _month_ranges()'e ne verilirse onu
    # sorgusuz aylara bolup _fetch_candles()'a gonderiyordu. Yanlis girilmis
    # bir yil (orn. "2005-04") sessizce "her ay icin 0 mum" sonucuna goturuyordu.
    # Artik calismaya BASLAMADAN ACIK hata verir.
    import datetime
    try:
        start_dt = datetime.datetime.strptime(start_str, "%Y-%m-%d")
        end_dt   = datetime.datetime.strptime(end_str, "%Y-%m-%d")
    except ValueError as e:
        print(f"\n[TARIH_HATASI] start/end format hatasi (YYYY-MM-DD bekleniyor): "
              f"start={start_str!r} end={end_str!r} -> {e}\n")
        sys.exit(1)
    start_ms_check = int(start_dt.timestamp() * 1000)
    end_ms_check   = int(end_dt.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    if start_ms_check >= end_ms_check:
        print(f"\n[TARIH_HATASI] start ({start_str}) >= end ({end_str}). WF BASLATILMIYOR.\n")
        sys.exit(1)
    if end_ms_check < _BINANCE_LAUNCH_MS:
        print(f"\n[TARIH_HATASI] Girilen tarih araligi ({start_str} .. {end_str}) Binance'in kurulus "
              f"tarihinden (2017-01-01) ONCE! Bu tarihte HICBIR sembol icin veri yoktur — tum aylar "
              f"icin '0 mum / 0 islem / PnL=0' alirsiniz. Yil yanlis girilmis olabilir (orn. '2005' "
              f"yerine '2025' kastedilmis olabilir). WF BASLATILMIYOR — tarihi duzeltip tekrar deneyin.\n")
        sys.exit(1)
    if start_ms_check > now_ms:
        print(f"\n[TARIH_HATASI] start tarihi ({start_str}) gelecekte! WF BASLATILMIYOR.\n")
        sys.exit(1)

    months = _month_ranges(start_str, end_str)
    monthly_results = []

    for start_ms, end_ms, month_str in months:
        month_dir = out_path / month_str
        month_dir.mkdir(exist_ok=True)
        print(f"\n[WF] Ay: {month_str} ─────────────────────────────────")

        # V9.2 FIX: candidate pool artık config'teki backtest_candidate_file'dan
        # (varsayılan symbols_top120.json) okunuyor — eski _load_symbols() sabit
        # symbols_top70.json'a bağlıydı, candidate_top'u görmezden geliyordu.
        seg_symbols = symbols
        wcfg = cfg.get("weekly_symbol_rotation", {}) or {}
        if wcfg.get("enabled", False):
            import universe_manager as _um
            lookback_days = int(wcfg.get("lookback_days", 30))
            candidate_top = int(wcfg.get("candidate_top", max(len(symbols), 120)))
            candidate_file = str(wcfg.get("backtest_candidate_file", "symbols_top120.json"))
            candidates = _um.load_candidate_pool_file(candidate_file, candidate_top)
            lookback_start = start_ms - lookback_days * 24 * 3600 * 1000
            _net_before = get_network_error_count()
            select_candles = {s: _fetch_candles(s, interval, lookback_start, start_ms) for s in candidates}
            _net_delta = get_network_error_count() - _net_before
            uni_result = _um.build_universe_for_window(
                candidates, select_candles, as_of_ms=start_ms, lookback_days=lookback_days,
                top=int(wcfg.get("top_n", len(symbols))), network_error_count=_net_delta)
            seg_symbols = uni_result["selected"]  # KRİTİK: ham fallback YOK, CORE_FALLBACK garantili
            if uni_result.get("core_fallback_used"):
                print(f"  ⚠️ CORE_FALLBACK_USED — DATA_QUALITY_FAIL ({month_str}): {seg_symbols}")
            print(f"  [UniverseManager {month_str}] candidate={uni_result['candidate_count']} "
                  f"selected={uni_result['selected_count']} source={uni_result['source']} "
                  f"network_error_count={uni_result.get('network_error_count', 0)} "
                  f"dropped={len(uni_result.get('dropped_symbols_with_reason', {}))}")
            _um.write_universe_files(uni_result, str(month_dir / "_universe"), mode="wf", as_of_ms=start_ms, cfg=cfg)
            if bool(wcfg.get("write_history", True)):
                write_universe_history(str(out_path / str(wcfg.get("history_file", "symbol_universe_history.csv"))), month_str, seg_symbols,
                                       {"mode": "walk_forward_segment", "as_of_ms": start_ms,
                                        "source": uni_result["source"], "data_quality_ok": uni_result["data_quality_ok"]})
        candles_by_sym = {}
        htf_candles    = {}
        for sym in seg_symbols:
            clist = _fetch_candles(sym, interval, start_ms, end_ms)
            candles_by_sym[sym] = clist
            htf_candles[sym]    = _fetch_candles(sym, "1h", start_ms, end_ms)
            print(f"  {sym}: {len(clist)} mum", flush=True)

        bt     = Backtester(cfg, str(month_dir), interval=interval)
        result = bt.run(seg_symbols, candles_by_sym, htf_candles)
        summ   = result["summary"]

        row = {
            "Ay":           month_str,
            "Islem":        summ.get("Toplam_Islem", 0),
            "WinRate":      summ.get("Kazanma_Orani", "0%"),
            "NetPnL":       summ.get("Net_PnL_USD", "0"),
            "Getiri":       summ.get("Getiri_Pct", "0%"),
            "BitisEquity":  summ.get("Bitis_Equity", "0"),
        }
        monthly_results.append(row)
        print(f"  → PnL: {row['NetPnL']} | WR: {row['WinRate']} | "
              f"Islem: {row['Islem']}")

    # Toplam ozet
    total_pnl = sum(float(r["NetPnL"]) for r in monthly_results)
    positive   = sum(1 for r in monthly_results if float(r["NetPnL"]) > 0)
    total_tr   = sum(int(r["Islem"]) for r in monthly_results)

    wf_summary = {
        "Donem":           f"{start_str} → {end_str}",
        "Toplam_Ay":       len(monthly_results),
        "Pozitif_Ay":      positive,
        "Toplam_Islem":    total_tr,
        "Toplam_PnL":      round(total_pnl, 4),
        "Ort_Aylik_PnL":   round(total_pnl / max(len(monthly_results), 1), 4),
    }

    # wf_summary.json
    with open(out_path / "wf_summary.json", "w", encoding="utf-8") as f:
        json.dump(wf_summary, f, ensure_ascii=False, indent=2)

    # wf_monthly.csv
    if monthly_results:
        with open(out_path / "wf_monthly.csv", "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(monthly_results[0].keys()),
                               delimiter=";")
            w.writeheader()
            w.writerows(monthly_results)

    print(f"\n[WF] TAMAMLANDI ───────────────────────────────────")
    print(f"  Toplam PnL: {total_pnl:.4f} USD")
    print(f"  Pozitif ay: {positive}/{len(monthly_results)}")
    print(f"  Sonuc:      {out_dir}")
    return wf_summary


def main():
    parser = argparse.ArgumentParser(description="TRBOT V8 Walk-Forward Test")
    parser.add_argument("--start",    type=str, required=True)
    parser.add_argument("--end",      type=str, required=True)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--top",      type=int, default=20)
    parser.add_argument("--out",      type=str, default="walkforward_results/latest")
    parser.add_argument("--config",   type=str, default="config_online.yaml")
    args = parser.parse_args()

    cfg     = _load_cfg(args.config)
    resolve_president_execution_mode(cfg)
    symbols = _load_symbols(args.top)
    run_walkforward(cfg, symbols, args.start, args.end,
                    args.interval, args.out)


if __name__ == "__main__":
    main()
