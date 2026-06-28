# trapped_distance_filter.py — "Tuzaklanmis Pozisyon Asimetrisi" filtresi (V2 — DUZELTME)
# ============================================================================
# DUZELTME GECMISI: V1, fiyati backtest.py'nin canli akisindan aliyordu ve
# 14-gunluk isinma penceresini sembolun V10 rotasyonuna GIRDIGI andan
# baslatiyordu -- OI verisi cok daha eskiye gitse bile. Bu, kisa sureli
# rotasyona giren semboller (orn. 2 hafta aktif kalan BNB) icin filtrenin
# HICBIR ZAMAN gercek bir sinyal uretememesine yol acti (gercek backtest'te
# dogrulandi: BNBUSDT 11-25 Ekim 2025 arasi aktifti, 14-gunluk pencere hic
# tamamlanamadi, btm trade'ler korumasiz gecti).
#
# V2 COZUMU: Artik fiyat da OFFLINE bir CSV'den (fetch_daily_close.py) alinir,
# ve TUM (sembol,gun) sinyalleri __init__ aninda ONCEDEN hesaplanir -- OI
# verisinin gercek baslangic tarihinden itibaren, V10'un o sembolu ne zaman
# rotasyona aldigindan TAMAMEN BAGIMSIZ. allows_long/allows_short artik
# basit bir lookup'tir, stateful/incremental degildir.
from __future__ import annotations
import csv
from collections import deque


class TrappedDistanceFilter:
    def __init__(self, oi_csv_path: str, price_csv_path: str = "", window: int = 14, mode: str = "cross"):
        self.enabled = False
        self.window = window
        self.mode = mode
        self.signal_by_symbol_date = {}  # (symbol,'YYYY-MM-DD') -> 'cross_up'/'cross_down'/'level_pos'/'level_neg'/'neutral'
        if not oi_csv_path or not price_csv_path:
            if oi_csv_path or price_csv_path:
                print("[TrappedDistanceFilter] UYARI: hem oi_csv_path HEM price_csv_path gerekli, "
                      "filtre DEVRE DISI.")
            return
        try:
            oi_by_symbol = self._load_oi(oi_csv_path)
            price_by_symbol = self._load_price(price_csv_path)
        except FileNotFoundError as e:
            print(f"[TrappedDistanceFilter] UYARI: dosya bulunamadi ({e}), filtre DEVRE DISI.")
            return
        except Exception as e:
            print(f"[TrappedDistanceFilter] UYARI: okuma hatasi ({e}), filtre DEVRE DISI.")
            return

        n_processed = 0
        for symbol, oi_series in oi_by_symbol.items():
            px_series = price_by_symbol.get(symbol)
            if not px_series:
                continue
            dates = sorted(set(oi_series.keys()) & set(px_series.keys()))
            h = deque(maxlen=window)
            prev_sign = None
            for d in dates:
                oi_val = oi_series[d]
                close = px_series[d]
                prev_oi = h[-1][1] if h else None
                oi_chg = (oi_val - prev_oi) if prev_oi is not None else 0.0
                pos_flow = max(oi_chg, 0.0)
                h.append((d, oi_val, close, pos_flow))
                if len(h) < window:
                    continue
                w_sum = sum(r[3] for r in h)
                if w_sum <= 0:
                    continue
                wp_sum = sum(r[3] * r[2] for r in h)
                trapped_avg_cost = wp_sum / w_sum
                curr_sign = 1 if close > trapped_avg_cost else (-1 if close < trapped_avg_cost else 0)

                if mode == "cross":
                    if prev_sign is not None and prev_sign < 0 and curr_sign > 0:
                        sig = "cross_up"
                    elif prev_sign is not None and prev_sign > 0 and curr_sign < 0:
                        sig = "cross_down"
                    else:
                        sig = "neutral"
                else:
                    sig = "level_pos" if curr_sign > 0 else ("level_neg" if curr_sign < 0 else "neutral")

                self.signal_by_symbol_date[(symbol, d)] = sig
                prev_sign = curr_sign
                n_processed += 1

        self.enabled = n_processed > 0
        if self.enabled:
            n_sym = len({k[0] for k in self.signal_by_symbol_date})
            n_cross = sum(1 for v in self.signal_by_symbol_date.values() if v in ("cross_up", "cross_down"))
            print(f"[TrappedDistanceFilter] {n_sym} sembol, {n_processed} gun-sembol sinyal "
                  f"ON-HESAPLANDI (mod={mode}, pencere={window} gun, kirilim sayisi={n_cross}).")
        else:
            print("[TrappedDistanceFilter] UYARI: hic sinyal uretilemedi (OI ve fiyat verisi "
                  "ortusmuyor olabilir), filtre DEVRE DISI.")

    @staticmethod
    def _load_oi(path):
        out = {}
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            oi_col = "sum_open_interest_value" if "sum_open_interest_value" in fieldnames else "oi_value"
            date_col = "date" if "date" in fieldnames else "ts"
            for r in reader:
                sym = r.get("symbol")
                d = str(r.get(date_col, ""))[:10]
                if not sym or not d:
                    continue
                try:
                    val = float(r[oi_col])
                except (KeyError, ValueError, TypeError):
                    continue
                out.setdefault(sym, {})[d] = val
        return out

    @staticmethod
    def _load_price(path):
        out = {}
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                sym = r.get("symbol")
                d = str(r.get("date", ""))[:10]
                if not sym or not d:
                    continue
                try:
                    val = float(r["close"])
                except (KeyError, ValueError, TypeError):
                    continue
                out.setdefault(sym, {})[d] = val
        return out

    def get_signal(self, symbol: str, date_str: str):
        if not self.enabled:
            return None
        return self.signal_by_symbol_date.get((symbol, date_str[:10]))

    def allows_long(self, symbol: str, date_str: str, close_price: float = None) -> bool:
        sig = self.get_signal(symbol, date_str)
        if sig is None:
            return True
        return sig in ("cross_up", "level_pos")

    def allows_short(self, symbol: str, date_str: str, close_price: float = None) -> bool:
        sig = self.get_signal(symbol, date_str)
        if sig is None:
            return True
        return sig in ("cross_down", "level_neg")
