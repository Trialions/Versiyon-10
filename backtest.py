# backtest.py — TRBOT President System V8 — Backtest Motoru
# BOA (Block Outcome Analyzer), ghost signal, post-analiz, filter events dahil
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fetch_guard import guarded_get, classify_fetch_exception, NETWORK_ERROR_KINDS


import yaml
import numpy as np

# ─── Strateji ve Hesaplama ────────────────────────────────────────────────────
from strategy_core import score_symbol
# V9 FIX (post-exit analytics): _rsi/_adx, mevcut score_symbol() icinde zaten
# kullanilan ayni fonksiyonlar — SADECE offline post-exit analiz kolonlari
# icin yeniden kullaniliyor, hicbir karar/skor mantigina dahil edilmiyor.
from strategy_core import _rsi as _post_rsi, _adx as _post_adx
from adaptive_sl import compute as adaptive_sl_compute
# değişiklik başlangıcı — dominans rejim filtresi (yeni, izole modul)
from dominance_filter import DominanceRegime
from funding_filter import FundingRegime
from trapped_distance_filter import TrappedDistanceFilter
# değişiklik bitişi
from market_regime import MarketRegimeDetector
from symbol_manager import SymbolManager
from adaptive_exit import classify_trade
from block_outcome_analyzer import build_block_outcome, write_block_outcome_reports
from pump_filter import compute_pump_risk
from regime_router import RegimeRouter, RegimePacket
from relative_strength import RelativeStrengthEngine, RelativeStrengthResult

# ─── President karar motoru (ortak pipeline) ──────────────────────────────────
from president_runtime import PresidentRuntime
from modules.decision_packet import Action, Side

# ─── Sabitler ─────────────────────────────────────────────────────────────────
COMMISSION_PCT_DEFAULT = 0.0004  # %0.04
SLIPPAGE_PCT_DEFAULT   = 0.0003  # %0.03

# ─── Yardimci ─────────────────────────────────────────────────────────────────
def _load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_BINANCE_LAUNCH_MS = int(datetime.datetime(2017, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)

# V9.3: network-hatası sayacı (DNS/timeout/connection/429/5xx) — fold/segment
# bazında "bu pencerede network sorunu oldu mu" sorusuna cevap vermek için
# basit bir mutable sayaç (liste-of-one, modüller arası import sorunsuz paylaşım için).
_NETWORK_ERROR_COUNT = [0]


def get_network_error_count() -> int:
    """Şu ana kadar (process ömründe) classify_fetch_exception() ile NETWORK_ERROR_KINDS
    sınıfına giren toplam hata sayısı. Fold/segment başına delta almak için kullanılır."""
    return _NETWORK_ERROR_COUNT[0]


def _fetch_candles(symbol: str, interval: str, start_ts: int, end_ts: int,
                   cache_dir: str = "data/cache") -> List[dict]:
    """
    Binance Spot REST API ile mum verisi ceker, cache'e kaydeder. V9.3'ten
    itibaren TÜM HTTP istekleri fetch_guard.guarded_get() üzerinden gider
    (throttle + DNS/timeout/connection/429/5xx retry+backoff).

    V9.0.3 FIX (sifir-mum teshisi): Onceki versiyon HER hata turunde
    (HTTP hatasi, Binance error-payload, network exception, gecersiz tarih
    araligi) sessizce [] donduruyordu VE bu bos sonucu cache'e yaziyordu —
    yani bir kerelik gecici bir hata bile o tarih araligi icin SONSUZA KADAR
    "0 mum" olarak cache'de kaliyordu. Artik:
      1) Tarih araligi once mantik kontrolunden gecer (start>=end, Binance
         kurulus tarihinden (2017) once, veya gelecekte mi).
      2) HER hata durumu (HTTP status, Binance error payload, network
         exception, beklenmeyen response tipi) acik [FETCH_ERROR] logu ile
         symbol/interval/start/end/url/response-snippet bilgisiyle basilir.
      3) Hata durumunda sonuc cache'E YAZILMAZ (bir dahaki calistirmada
         tekrar denenir). Sadece basarili (hatasiz) cekimler cache'lenir.
      4) Eskiden yazilmis BOS bir cache dosyasi bulunursa (onceki hatali
         surumden kalmis olabilir) [FETCH_WARN] ile bildirilip yeniden cekilir.
      5) V9.3: DNS/timeout/connection hatalari (FETCH_NETWORK_ERROR) ile
         API'nin basariyla dondurdugu gercek 0-mum durumu (ZERO_CANDLES/
         SYMBOL_UNAVAILABLE_AT_DATE) ACIKCA AYRISTIRILIR — ikincisi sembolun
         o tarihte gercekten olmadigini gosterir, birincisi sadece gecici bir
         ag sorunudur ve retry/backoff'tan sonra hala basarisizsa
         _NETWORK_ERROR_COUNT'a yazilir (fold/segment data_quality icin).
    """
    os.makedirs(cache_dir, exist_ok=True)

    # ── 1) Tarih araligi sanity-check ───────────────────────────────────
    now_ms = int(time.time() * 1000)
    if start_ts >= end_ts:
        print(f"[FETCH_ERROR] {symbol} {interval}: GECERSIZ ARALIK start_ts({start_ts}) >= end_ts({end_ts})")
        return []
    if end_ts < _BINANCE_LAUNCH_MS:
        sdt = datetime.datetime.utcfromtimestamp(start_ts / 1000)
        edt = datetime.datetime.utcfromtimestamp(end_ts / 1000)
        print(f"[FETCH_ERROR] {symbol} {interval}: istenen tarih araligi ({sdt} .. {edt}) "
              f"Binance'in kurulus tarihinden (2017-01-01) ONCE! Binance'te bu tarihte HIC "
              f"veri yok, dolayisiyla '0 mum' BEKLENEN bir sonuctur — fakat bu, GUI'den/CLI'den "
              f"gelen start/end tarihinin YANLIS girildigine isarettir (orn. yil '2005' yazilmis "
              f"olabilir, '2025' yerine). Lutfen tarih araligini kontrol edin.")
        return []
    if start_ts > now_ms:
        sdt = datetime.datetime.utcfromtimestamp(start_ts / 1000)
        print(f"[FETCH_ERROR] {symbol} {interval}: start_ts GELECEKTE ({sdt}, simdi={datetime.datetime.utcfromtimestamp(now_ms/1000)}) — tarih hatali olabilir.")
        return []

    fn = os.path.join(cache_dir, f"{symbol}_{interval}_{start_ts}_{end_ts}.json")
    if os.path.exists(fn):
        try:
            with open(fn, encoding="utf-8") as f:
                cached = json.load(f)
            if cached:
                return cached
            print(f"[FETCH_WARN] {symbol} {interval}: cache dosyasi BOS bulundu ({fn}) — "
                  f"onceki hatali bir cekimden kalmis olabilir, yeniden cekiliyor.")
        except Exception as e:
            print(f"[FETCH_WARN] {symbol} {interval}: cache okuma hatasi ({fn}): {e} — yeniden cekiliyor.")

    url = "https://api.binance.com/api/v3/klines"
    all_candles: List[dict] = []
    cur = start_ts
    had_error = False
    had_network_error = False

    while cur < end_ts:
        try:
            r = guarded_get(
                url,
                params={
                    "symbol": symbol, "interval": interval,
                    "startTime": cur, "endTime": end_ts, "limit": 1000,
                },
                timeout=20,
                label=f"{symbol} {interval} shard start={cur}",
            )
            if r.status_code != 200:
                had_error = True
                print(f"[FETCH_ERROR] {symbol} {interval}: HTTP {r.status_code} "
                      f"url={url} start={cur} end={end_ts} body={r.text[:300]!r}")
                break
            data = r.json()
            if isinstance(data, dict):
                # Binance hata payload'i: {"code":-1121,"msg":"Invalid symbol."}
                had_error = True
                print(f"[FETCH_ERROR] {symbol} {interval}: Binance API hata payload'i dondurdu: "
                      f"{data} (start={cur} end={end_ts} url={url})")
                break
            if not isinstance(data, list):
                had_error = True
                print(f"[FETCH_ERROR] {symbol} {interval}: beklenmeyen response tipi {type(data).__name__}: "
                      f"{str(data)[:200]!r} (start={cur} end={end_ts})")
                break
            if not data:
                # API 200 + bos liste dondu -> bu araliktaki GERCEK veri yok (ZERO_CANDLES/
                # SYMBOL_UNAVAILABLE_AT_DATE — network hatasi DEGIL, sembol o tarihte yok).
                break
            for d in data:
                all_candles.append({
                    "open_time":  d[0], "open": float(d[1]),
                    "high":       float(d[2]), "low": float(d[3]),
                    "close":      float(d[4]), "volume": float(d[5]),
                    "close_time": d[6],
                })
            cur = data[-1][0] + 1
            if len(data) < 1000:
                break
        except Exception as e:
            had_error = True
            kind = classify_fetch_exception(e)
            if kind in NETWORK_ERROR_KINDS:
                had_network_error = True
                _NETWORK_ERROR_COUNT[0] += 1
            print(
                f"[FETCH_ERROR] {symbol} {interval}: {kind}: {type(e).__name__}: {e} "
                f"(start={cur} end={end_ts} url={url})"
            )
            break

    if not all_candles:
        if had_network_error:
            print(f"[FETCH_ERROR] {symbol} {interval}: FETCH_NETWORK_ERROR — 0 mum donuyor CUNKU "
                  f"ag/DNS/timeout hatasi olustu (detay yukarida, retry/backoff tukendi). Bu sonuc "
                  f"cache'e YAZILMAYACAK ve sembol 'tarihte yok' SAYILMAYACAK.")
        elif had_error:
            print(f"[FETCH_ERROR] {symbol} {interval}: 0 mum donuyor CUNKU fetch hatasi olustu "
                  f"(detay yukarida). Bu sonuc cache'e YAZILMAYACAK — bir dahaki calistirmada "
                  f"tekrar denenecek.")
        else:
            print(f"[FETCH_WARN] {symbol} {interval}: API basariyla yanit verdi ama bu araliktaki "
                  f"({datetime.datetime.utcfromtimestamp(start_ts/1000)} .. "
                  f"{datetime.datetime.utcfromtimestamp(end_ts/1000)}) gercek mum verisi 0 — "
                  f"ZERO_CANDLES/SYMBOL_UNAVAILABLE_AT_DATE (sembol bu tarihte henuz listelenmemis olabilir).")

    # Sadece HATASIZ sonuclar cache'e yazilir — boylece gecici bir ag hatasi
    # asla sonsuza kadar "0 mum" olarak cache'de kalmaz.
    if not had_error:
        try:
            with open(fn, "w", encoding="utf-8") as f:
                json.dump(all_candles, f)
        except Exception as e:
            print(f"[FETCH_WARN] {symbol} {interval}: cache yazma hatasi ({fn}): {e}")

    return all_candles


def _load_symbols(top: int = 20) -> List[str]:
    """symbols_top70.json veya fallback listesi."""
    try:
        if os.path.exists("symbols_top70.json"):
            with open("symbols_top70.json", encoding="utf-8") as f:
                syms = json.load(f)
            if isinstance(syms, list) and syms:
                return syms[:top]
    except Exception:
        pass
    return ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
            "ADAUSDT","DOTUSDT","AVAXUSDT","MATICUSDT","LINKUSDT"][:top]


# ─── Ana Backtest Sinifi ──────────────────────────────────────────────────────
class Backtester:

    def __init__(self, cfg: dict, out_dir: str, mode: str = "normal",
                 president_enabled: bool = True, interval: str = "1h"):
        self.cfg     = cfg
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mode    = mode
        self.interval = interval
        self.president_enabled = bool(president_enabled)

        risk  = cfg.get("risk", {})
        lim   = cfg.get("limits", {})
        thr   = cfg.get("thresholds", {})
        misc  = cfg.get("misc", {})
        account = cfg.get("account", {})
        ptp   = cfg.get("partial_tp", {})
        mtf   = cfg.get("mtf", {})
        # değişiklik başlangıcı — dominans filtresi config'ten okunur, dosya
        # yoksa/bozuksa sessizce devre dışı kalır (sistemi asla bozmaz)
        dom_cfg = cfg.get("dominance_filter", {})
        self.dominance_enabled = bool(dom_cfg.get("enabled", False))
        self.dominance = DominanceRegime(dom_cfg.get("csv_path", "")) if self.dominance_enabled else None
        # değişiklik başlangıcı — FUNDING-GATED SHORT filtresi (yeni, izole)
        fund_cfg = cfg.get("funding_gate_filter", {})
        self.funding_filter_enabled = bool(fund_cfg.get("enabled", False))
        self.funding_regime = FundingRegime(fund_cfg.get("csv_path", "")) if self.funding_filter_enabled else None
        # değişiklik başlangıcı — TUZAKLANMIS POZISYON ASIMETRISI filtresi (yeni)
        trap_cfg = cfg.get("trapped_distance_filter", {})
        self.trapped_filter_enabled = bool(trap_cfg.get("enabled", False))
        self.trapped_filter = TrappedDistanceFilter(
            trap_cfg.get("csv_path", ""),
            price_csv_path=trap_cfg.get("price_csv_path", ""),
            window=int(trap_cfg.get("window", 14)),
            mode=str(trap_cfg.get("mode", "cross")),
        ) if self.trapped_filter_enabled else None
        # değişiklik bitişi
        # değişiklik bitişi
        # değişiklik başlangıcı — GERCEK calisma-zamani durumunu HER ZAMAN
        # acikca bildiren bir durum dosyasi yaz. config_snapshot.json'daki
        # "enabled" SADECE config'te ne yazdigini gosterir, dosyanin GERCEKTEN
        # yuklenip yuklenmedigini DEGIL. Bu karisikligi onlemek icin (tam olarak
        # bu sorunun teshisini hizlandirmak icin) ayri, supheye yer birakmayan
        # bir dosya yaziyoruz.
        self._dominance_runtime_status = {
            "config_enabled": self.dominance_enabled,
            "actually_loaded": bool(self.dominance and self.dominance.enabled),
            "days_loaded": len(self.dominance.by_date) if (self.dominance and self.dominance.enabled) else 0,
            "csv_path_used": dom_cfg.get("csv_path", "") if self.dominance_enabled else None,
            # değişiklik başlangıcı — funding filtresi durumu da ayni dosyaya eklendi
            "funding_filter_config_enabled": self.funding_filter_enabled,
            "funding_filter_actually_loaded": bool(self.funding_regime and self.funding_regime.enabled),
            "funding_filter_symbols_loaded": len(self.funding_regime.by_symbol) if (self.funding_regime and self.funding_regime.enabled) else 0,
            "funding_filter_csv_path_used": fund_cfg.get("csv_path", "") if self.funding_filter_enabled else None,
            # değişiklik başlangıcı — trapped distance durumu da eklendi
            "trapped_filter_config_enabled": self.trapped_filter_enabled,
            "trapped_filter_actually_loaded": bool(self.trapped_filter and self.trapped_filter.enabled),
            "trapped_filter_mode": trap_cfg.get("mode", "cross") if self.trapped_filter_enabled else None,
            "trapped_filter_csv_path_used": trap_cfg.get("csv_path", "") if self.trapped_filter_enabled else None,
            "trapped_filter_price_csv_path_used": trap_cfg.get("price_csv_path", "") if self.trapped_filter_enabled else None,
            "trapped_filter_signal_count": len(self.trapped_filter.signal_by_symbol_date) if (self.trapped_filter and self.trapped_filter.enabled) else 0,
            # değişiklik bitişi
            # değişiklik bitişi
        }
        try:
            import os as _os, json as _json
            with open(_os.path.join(out_dir, "dominance_filter_status.json"), "w", encoding="utf-8") as _f:
                _json.dump(self._dominance_runtime_status, _f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # değişiklik bitişi
        # değişiklik bitişi

        self.equity          = float(account.get("starting_equity_usdt", misc.get("starting_equity_usdt", cfg.get("risk", {}).get("starting_equity_usdt", 1000.0))))
        self.commission      = float(misc.get("commission_pct", 0.04)) / 100
        self.slippage        = float(misc.get("slippage_pct", 0.03)) / 100

        self.sl_pct          = float(risk.get("hard_stop_pct", 1.5)) / 100
        self.tp_pct          = float(risk.get("take_profit_min_pct", 3.0)) / 100
        self.trail_step      = float(risk.get("trailing_step_pct", 0.7)) / 100
        self.atr_multiplier  = float(risk.get("atr_multiplier", 2.0))
        self.max_stop_pct    = float(risk.get("max_stop_pct", 4.5)) / 100
        self.use_atr_stop    = bool(risk.get("use_atr_stop", True))
        self.use_trailing    = bool(risk.get("use_trailing", True))
        self.min_hold_bars   = max(1, int(risk.get("min_hold_minutes", 60)) // 60)
        self.risk_per_trade  = float(risk.get("risk_per_trade_pct", 1.0)) / 100
        self.min_profit_cls  = float(risk.get("min_profit_close_pct", 3.0)) / 100

        dt = cfg.get("dynamic_trail", {})
        self.dynamic_trail   = bool(dt.get("enabled", True))
        self.dt_min          = float(dt.get("min_pct", 0.5)) / 100
        self.dt_max          = float(dt.get("max_pct", 2.5)) / 100
        self.dt_atr_m        = float(dt.get("atr_mult", 0.5))

        self.score_long_open  = float(thr.get("score_long_open", 97.0))
        self.score_short_open = float(thr.get("score_short_open", 5.0))
        self.score_close      = float(thr.get("score_close", 50.0))

        self.max_open_pos    = int(lim.get("max_open_positions", 3))
        self.max_trades_day  = int(lim.get("max_trades_per_day", 8))
        self.max_hold_bars   = int(lim.get("max_hold_hours", 48))
        self.daily_target    = float(lim.get("daily_target_pct", 10.0)) / 100
        self.daily_loss_lim  = float(lim.get("daily_loss_limit_pct", 3.0)) / 100

        self.partial_tp_en   = bool(ptp.get("enabled", True))
        self.tp1_r_mult      = float(ptp.get("tp1_r_mult", 0.75))
        self.tp1_close_pct   = float(ptp.get("close_pct", 0.40))

        # V8.5 TP1 Progress Manager: TP1'e ilerlemeyen pozisyonda riski azaltır.
        tpm = cfg.get("tp1_progress_manager", {})
        self.tp1_prog_enabled = bool(tpm.get("enabled", True))
        self.tp1_prog_check_bars = int(tpm.get("check_after_bars", 5))
        self.tp1_prog_min_progress = float(tpm.get("min_progress_to_tp1", 0.25))
        self.tp1_prog_reduce_pct = float(tpm.get("reduce_pct", 0.35))
        self.tp1_prog_only_if_not_profitable = bool(tpm.get("only_reduce_if_not_profitable", True))
        self.tp1_prog_tighten_bars = int(tpm.get("tighten_trail_after_bars", 4))
        self.tp1_prog_tighten_mult = float(tpm.get("tighten_trail_mult", 0.55))
        self.tp1_prog_early_exit_bars = int(tpm.get("early_exit_after_bars", 8))
        self.tp1_prog_early_exit_r = float(tpm.get("early_exit_if_change_below_r", -0.45))

        # V9 FIX: TP1 Progress Manager artik tek basina sure+ilerlemeye gore
        # reduction/early-exit yapmiyor — bagimsiz piyasa teyitleri (RSI/ADX/
        # volume/HTF/BTC/AE_Class/MFE) gerektirebiliyor. market_confirm_enabled
        # varsayilan FALSE: config'te ayrica acilmadan eski davranis BIREBIR
        # aynen calisir (geriye uyumlu, A/B-0 referansi bozulmaz).
        self.tp1_prog_mc_enabled       = bool(tpm.get("market_confirm_enabled", False))
        self.tp1_prog_min_conf_reduce  = int(tpm.get("min_confirmations_reduce", 3))
        self.tp1_prog_min_conf_early   = int(tpm.get("min_confirmations_early_exit", 4))

        self.tp1_prog_c_rsi_en         = bool(tpm.get("confirm_rsi_enabled", True))
        self.tp1_prog_c_rsi_long_max   = float(tpm.get("confirm_rsi_long_max", 72.0))
        self.tp1_prog_c_rsi_short_min  = float(tpm.get("confirm_rsi_short_min", 30.0))

        self.tp1_prog_c_adx_en         = bool(tpm.get("confirm_adx_enabled", True))
        self.tp1_prog_c_adx_min        = float(tpm.get("confirm_adx_min", 20.0))

        self.tp1_prog_c_vol_en         = bool(tpm.get("confirm_volume_enabled", True))
        self.tp1_prog_c_vol_lookback   = int(tpm.get("confirm_volume_lookback", 20))
        self.tp1_prog_c_vol_recent     = int(tpm.get("confirm_volume_recent", 3))
        self.tp1_prog_c_vol_min_ratio  = float(tpm.get("confirm_volume_min_ratio", 0.75))

        self.tp1_prog_c_htf_en         = bool(tpm.get("confirm_htf_enabled", True))
        self.tp1_prog_c_htf_long_min   = float(tpm.get("confirm_htf_long_min", 50.0))
        self.tp1_prog_c_htf_short_max  = float(tpm.get("confirm_htf_short_max", 50.0))

        self.tp1_prog_c_btc_en         = bool(tpm.get("confirm_btc_enabled", True))
        self.tp1_prog_c_btc_lookback   = int(tpm.get("confirm_btc_lookback", 4))
        self.tp1_prog_c_btc_drop_pct   = float(tpm.get("confirm_btc_drop_pct", 1.5))
        self.tp1_prog_c_btc_rise_pct   = float(tpm.get("confirm_btc_rise_pct", 1.5))

        self.tp1_prog_c_ae_en          = bool(tpm.get("confirm_ae_class_enabled", True))
        self.tp1_prog_c_ae_bad         = set(tpm.get("confirm_ae_bad_classes", ["CHOP_RISK", "EXHAUSTED"]) or [])

        self.tp1_prog_c_mfe_en         = bool(tpm.get("confirm_mfe_enabled", True))
        self.tp1_prog_c_mfe_min_r      = float(tpm.get("confirm_mfe_min_r", 0.20))

        self.mtf_enabled     = bool(mtf.get("enabled", True))
        self.mtf_long_min    = float(mtf.get("htf_long_min", 55.0))
        # V9 FIX (SHORT parity): engine.py'deki mtf_short_max okunuyor (satır 82).
        # Config'te alan yoksa engine.py ile aynı default (45.0) kullanılır.
        self.mtf_short_max   = float(mtf.get("htf_short_max", 45.0))

        # ADX filtresi
        adx_f = cfg.get("adx_filter", {})
        self.adx_filter_en   = bool(adx_f.get("enabled", True))
        self.adx_thr         = float(adx_f.get("threshold", 29.0))

        # require_adx_when_filter_enabled: ADX filtresi açıkken ADX=0 (hesaplanamadı)
        # gelirse sinyal otomatik BYPASS edilir (eski davranış). Bu alan True olursa
        # ADX=0 durumunda da sinyal bloklanır (bypass yok) — varsayılan eski davranışla
        # aynı (False) olduğu için mevcut testler etkilenmez.
        ie = cfg.get("indicator_engine", {})
        self.require_adx_strict = bool(ie.get("require_adx_when_filter_enabled", False))

        # V9 FIX (SHORT parity): engine.py'deki rsi_filter.min_short okunuyor
        # (satır 103). backtest.py'de RSI filtresi hiç yoktu — SADECE SHORT
        # tarafı eklendi (rsi_max_long / LONG davranışı kasıtlı olarak EKLENMEDİ,
        # mevcut LONG davranışı değişmesin).
        rsi_f = cfg.get("rsi_filter", {})
        self.rsi_filter_en  = bool(rsi_f.get("enabled", False))
        self.rsi_min_short  = float(rsi_f.get("min_short", 30.0))

        # BTC genel düşüş filtresi — varsayılan KAPALI (enabled=false). Açılırsa,
        # BTC son N mumda drop_pct'ten fazla düştüyse TÜM LONG sinyaller bloklanır
        # (Core Long dahil — short_surgeon.btc_risk_off'tan AYRI, o sadece SHORT
        # dalına özel bir koruma; bu filtre LONG tarafı için genel bir güvenlik kapısı).
        btc_f = cfg.get("btc_filter", {})
        self.btc_filter_en      = bool(btc_f.get("enabled", False))
        self.btc_filter_candles = int(btc_f.get("lookback_candles", 4))
        self.btc_filter_drop    = float(btc_f.get("drop_pct", 1.5))

        # Backtest'in varsayılan President modu — SADECE CLI'da --president-mode
        # verilmediyse kullanılır (CLI argümanı her zaman önceliklidir, geriye
        # uyumluluk bozulmaz). "simulated_active" = mevcut varsayılan davranış
        # (president_enabled=True, shadow_mode config'teki değeriyle).
        self.default_president_mode = str(cfg.get("backtest", {}).get(
            "president_execution_mode", "simulated_active"))

        # Kara liste
        bl = cfg.get("symbol_blacklist", {})
        self.bl_enabled      = bool(bl.get("enabled", False))
        self.bl_symbols      = set(s.upper() for s in (bl.get("symbols") or []))

        # Ghost analiz
        ghost = cfg.get("ghost_trade_analysis", {})
        self.ghost_en        = bool(ghost.get("enabled", True))
        self.ghost_fwd_bars  = int(ghost.get("lookforward_bars", 12))
        self.ghost_min_score = float(ghost.get("min_score_to_track", 90.0))

        self.regime_detector = MarketRegimeDetector(cfg)
        self.sym_mgr = SymbolManager(cfg, starting_equity=self.equity)

        # V10 Phase-1: aktif test edilebilir Regime Router + Relative Strength.
        # mode=shadow -> sadece log/kolon; mode=soft/hard -> score/size/block etkisi.
        self.regime_router = RegimeRouter(cfg)
        self.relative_strength = RelativeStrengthEngine(cfg)
        self._active_rs_snapshot = {}

        rot = cfg.get("position_rotation", {})
        self.rotation_enabled = bool(rot.get("enabled", False))
        self.rotation_min_score = float(rot.get("min_candidate_score", 90.0))
        self.rotation_min_delta = float(rot.get("min_score_delta", 12.0))
        self.rotation_shadow = bool(rot.get("shadow_mode", True))
        self.rotation_allow_close_profitable = bool(rot.get("allow_close_profitable", False))
        self.rotation_max_per_day = int(rot.get("max_rotations_per_day", 2))
        self._daily_rotations: Dict[str, int] = defaultdict(int)

        # ── President global candidate ranking / BOA feedback ────────────────
        pr = cfg.get("president", {}) or {}
        gr = pr.get("global_ranking", {}) or {}
        self.global_ranking_enabled = bool(gr.get("enabled", True))
        self.rank_reject_log = bool(gr.get("write_rank_rejections", True))
        self.rank_max_candidates_per_bar = int(gr.get("max_candidates_per_bar", 999))
        self.rank_bad_quality_below = float(gr.get("bad_quality_below", 58.0))
        self.rank_chop_labels = set(str(x).upper() for x in gr.get("chop_labels", ["CHOP_RISK", "EXHAUSTED"]))
        # V9.0.5 FIX (ghost-config temizliği): write_candidate_ranking_csv
        # önceden tanımlıydı ama hiç okunmuyordu (yazım kayıtsız-şartsız
        # yapılıyordu). Artık gerçekten gate ediyor (varsayılan True ->
        # davranış DEĞİŞMEDİ). reject_reasons listesi _rank_reject_reason()
        # tarafından üretilen sabit string'lerle EŞLEŞMESİ İÇİN bir
        # doğrulama/sanity-check olarak kullanılıyor (bkz. _rank_reject_reason).
        self.write_candidate_ranking_csv = bool(gr.get("write_candidate_ranking_csv", True))
        self.rank_reject_reasons_allowed = set(gr.get("reject_reasons", [
            "RANK_REJECTED_LOWER_SCORE", "RANK_REJECTED_BAD_QUALITY",
            "RANK_REJECTED_CHOP_RISK", "RANK_REJECTED_SYMBOL_PENALTY",
        ]))

        bf = pr.get("boa_feedback", {}) or {}
        self.boa_feedback_enabled = bool(bf.get("enabled", True))
        self.boa_feedback_weight = float(bf.get("weight", 1.0))
        self.boa_feedback_max_adj = float(bf.get("max_adjustment", 6.0))
        self.boa_feedback_min_count = int(bf.get("min_count", 8))
        self.boa_feedback_file = Path(str(bf.get("memory_file", "data/boa_feedback_memory.json")))
        self.boa_feedback_memory = self._load_boa_feedback_memory()

        # ── V9.0.5 FIX (ghost-config temizliği): backtest_output_integrity
        # bölümü tanımlıydı ama hiçbir yazım hiç kontrol etmiyordu (her şey
        # kayıtsız-şartsız yazılıyordu). Artık 4 alan da ilgili yazım
        # noktalarını gerçekten gate ediyor. Varsayılanlar hepsi True ->
        # mevcut davranış DEĞİŞMEDİ, sadece artık config-driven.
        oi = cfg.get("backtest_output_integrity", {}) or {}
        self.write_active_universe = bool(oi.get("write_active_universe", True))
        self.write_candidate_ranking_events = bool(oi.get("write_candidate_ranking_events", True))
        self.write_boa_feedback_memory_placeholder = bool(oi.get("write_boa_feedback_memory_placeholder", True))
        self.write_tp1_progress_fields = bool(oi.get("write_tp1_progress_fields", True))

        # ── President karar motoru (backtest/WF/robustluk hepsi ayni motoru kullanir)
        self.runtime = None
        if self.president_enabled:
            self.runtime = PresidentRuntime(cfg, data_dir=str(self.out_dir / "_president"))

        # Durum degiskenleri
        self.open_positions: Dict[str, dict] = {}
        self.trades:    List[dict] = []
        self.equity_curve: List[Tuple[str, float]] = []
        self.filter_events: List[dict] = []
        self.ranking_events: List[dict] = []
        self.rotation_events: List[dict] = []
        self.ghost_signals: List[dict] = []
        self.block_outcomes: List[dict] = []
        self.block_events:   List[dict] = []   # GERCEK BOA: bloklanan sinyaller
        self._cbs: Dict[str, List[dict]] = {}  # post-analiz icin mum referansi
        self._daily_trade_count: Dict[str, int] = defaultdict(int)
        self._daily_pnl: Dict[str, float] = defaultdict(float)
        self._pnl_running = 0.0

    # ── Ana Dongu ─────────────────────────────────────────────────────
    def run(self, symbols: List[str], candles_by_sym: Dict[str, List[dict]],
            htf_candles: Dict[str, List[dict]] = None,
            btc_candles: List[dict] = None,
            rotation_schedule: List[tuple] = None) -> dict:
        """
        Tek bir backtest dongusu.
        candles_by_sym: {symbol: [mum_dict,...]}
        htf_candles: {symbol: [1h_mum_dict,...]} (MTF icin)
        btc_candles: BTCUSDT mumlari — rejim tespiti ve btc_filter icin AYRI kanal.
                     symbols/candles_by_sym icine KARISTIRILMAZ (BTCUSDT trade
                     edilebilir aday olarak islenmesin diye).
        rotation_schedule: V9 FIX — gercek haftalik universe rotasyonu icin
                     [(effective_from_ms, {symbol,...}), ...] listesi (ms'e gore
                     sirali). Verilmezse tum 'symbols' her zaman aktif kabul edilir
                     (eski STATIC davranis, geriye uyumlu).
        """
        htf_candles = htf_candles or {}
        btc_candles = btc_candles or []
        self._cbs = candles_by_sym
        self._run_symbols = list(symbols or [])

        # V9 FIX: rotasyon takvimi — ts ilerledikce aktif evren degisir.
        _rotation = sorted(rotation_schedule, key=lambda x: x[0]) if rotation_schedule else [(0, set(symbols))]
        _rot_idx = 0
        _active_universe = set(_rotation[0][1])
        self.rotation_events.append({
            "ts": time.strftime("%Y-%m-%d %H:%M", time.gmtime(_rotation[0][0] // 1000)) if _rotation[0][0] else "",
            "symbols": sorted(_active_universe),
        })
        all_ts = sorted(set(
            c["open_time"]
            for sym in symbols
            for c in candles_by_sym.get(sym, [])
        ))

        # Her sembol icin mum indexi
        sym_idx = {sym: 0 for sym in symbols}
        sym_prices  = {sym: [] for sym in symbols}
        sym_highs   = {sym: [] for sym in symbols}
        sym_lows    = {sym: [] for sym in symbols}
        sym_vols    = {sym: [] for sym in symbols}
        htf_prices  = {sym: [] for sym in symbols}

        # HTF pre-load
        for sym in symbols:
            for c in htf_candles.get(sym, []):
                htf_prices[sym].append(float(c["close"]))

        # V9 FIX: BTCUSDT artik trade evreninde olmasa bile rejim/btc_filter
        # icin ayri bir zaman-indeksli seri olarak takip edilir.
        _btc_idx = 0
        _btc_closes: List[float] = []

        for ts in all_ts:
            # Sembol bazli guncelle
            for sym in symbols:
                clist = candles_by_sym.get(sym, [])
                idx   = sym_idx[sym]
                if idx < len(clist) and clist[idx]["open_time"] == ts:
                    c = clist[idx]
                    sym_prices[sym].append(float(c["close"]))
                    sym_highs[sym].append(float(c["high"]))
                    sym_lows[sym].append(float(c["low"]))
                    sym_vols[sym].append(float(c["volume"]))
                    sym_idx[sym] += 1

            # V9 FIX: BTC serisini ayri kanaldan, trade evreninden bagimsiz ilerlet.
            if _btc_idx < len(btc_candles) and btc_candles[_btc_idx]["open_time"] == ts:
                _bc = btc_candles[_btc_idx]
                _btc_closes.append(float(_bc["close"]))
                self.regime_detector.update(float(_bc["close"]))
                _btc_idx += 1

            # V9 FIX: haftalik rotasyon — bu ts bir sonraki rotasyon noktasina
            # ulasti/gecti mi kontrol et, aktif evreni guncelle ve logla.
            while _rot_idx + 1 < len(_rotation) and ts >= _rotation[_rot_idx + 1][0]:
                _rot_idx += 1
                _active_universe = set(_rotation[_rot_idx][1])
                self.rotation_events.append({
                    "ts": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts // 1000)),
                    "symbols": sorted(_active_universe),
                })

            # Tarih
            date_str = time.strftime("%Y-%m-%d", time.gmtime(ts // 1000))
            regime   = self.regime_detector.get_regime()

            # V10 Phase-1: Bu timestamp'te tüm sembollerin o ana kadar kapanmış
            # mumlarından Relative Strength snapshot üret. Look-ahead yoktur;
            # sym_prices/sym_vols sadece yukarıda bu timestamp'e kadar güncellendi.
            try:
                self._active_rs_snapshot = self.relative_strength.compute_all(sym_prices, sym_vols)
            except Exception as _rs_e:
                self._active_rs_snapshot = {}
                try:
                    (self.out_dir / "relative_strength_error.txt").write_text(str(_rs_e), encoding="utf-8")
                except Exception:
                    pass

            # V8.5.5: aynı mumdaki tüm adayları önce topla, sonra President ranking ile seç.
            ranking_candidates = []
            ts_str  = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts // 1000))

            for sym in symbols:
                prices  = sym_prices[sym]
                if len(prices) < 50:
                    continue
                highs   = sym_highs[sym]
                lows    = sym_lows[sym]
                vols    = sym_vols[sym]
                htf_p   = htf_prices.get(sym, [])

                result  = score_symbol(prices, highs, lows, vols)
                score   = result["final_score"]

                # Acik pozisyon yonetimi ranking'den önce yapılır; kapanan pozisyon aynı mumda kapasite açabilir.
                if sym in self.open_positions:
                    self._manage_position(sym, prices, result, ts_str, date_str, ts,
                                          vols=vols, htf_p=htf_p, btc_prices=_btc_closes)
                    continue

                # V9 FIX: rotasyon disinda kalan sembol icin YENI aday degerlendirilmez
                # (acik pozisyonlar yukarida etkilenmeden yonetiliyor).
                if sym not in _active_universe:
                    continue

                self._resolve_pending_sl_bt(sym, ts)

                if self.global_ranking_enabled and self.president_enabled and self.runtime:
                    cand = self._evaluate_candidate_for_ranking(
                        sym, score, result, prices, highs, lows, vols, htf_p, regime, ts_str, date_str, ts,
                        _btc_closes, self._active_rs_snapshot,
                    )
                    if cand:
                        ranking_candidates.append(cand)
                else:
                    self._try_open(sym, score, result, prices, highs, lows, vols,
                                   htf_p, regime, ts_str, date_str, ts,
                                   _btc_closes)

            if self.global_ranking_enabled and self.president_enabled and self.runtime and ranking_candidates:
                self._open_ranked_candidates(ranking_candidates, ts_str, date_str, ts)

            # Equity kaydi (her timestamp)
            self.equity_curve.append((
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts // 1000)),
                round(self.equity + self._pnl_running, 4)
            ))

        # Donem sonu: tum acik pozisyonlari kapat (son mum zamanıyla)
        last_candle_ts = all_ts[-1] if all_ts else 0
        self._force_close_all(last_candle_ts)
        # Force-close sonrası equity_curve'e gerçek son değeri yaz
        final_eq = round(self.equity + self._pnl_running, 4)
        if self.equity_curve:
            last_ts = self.equity_curve[-1][0]
            self.equity_curve.append((last_ts + " [EOT]", final_eq))
        self._post_boa_analysis()
        return self._generate_report()


    # ── Profesyonel PnL yardımcıları ─────────────────────────────────
    def _fee_cost(self, price: float, qty: float) -> float:
        return float(price) * float(qty) * (self.commission + self.slippage)

    def _gross_pnl(self, side: str, entry: float, exit_price: float, qty: float) -> float:
        return ((exit_price - entry) if side == "LONG" else (entry - exit_price)) * qty

    # ── V8.5.5 President Global Ranking / BOA Feedback ───────────────
    def _load_boa_feedback_memory(self) -> dict:
        """Önceki testlerden üretilmiş BOA hafızasını okur. Aynı testin gelecek verisini kullanmaz."""
        try:
            path = self.boa_feedback_file
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
        return {}

    def _boa_feedback_report(self, sym: str, side: str, regime: str, reason_hint: str = "") -> dict:
        """BOA hafızasından küçük bir edge üretir. President'ı baypas etmez; sadece feature'dır."""
        if not self.boa_feedback_enabled:
            return {"enabled": False, "adjustment": 0.0, "reason": "disabled"}
        mem = self.boa_feedback_memory or {}
        keys = [
            f"symbol:{sym}:{side}",
            f"regime:{regime}:{side}",
            f"reason:{reason_hint}:{side}" if reason_hint else "",
            f"side:{side}",
        ]
        adj_sum = 0.0; weight_sum = 0.0; used = []
        for k in keys:
            if not k or k not in mem:
                continue
            rec = mem.get(k, {}) or {}
            n = int(rec.get("count", 0) or 0)
            if n < self.boa_feedback_min_count:
                continue
            edge = float(rec.get("edge", 0.0) or 0.0)
            w = min(1.0, n / max(self.boa_feedback_min_count * 4, 1))
            adj_sum += edge * w; weight_sum += w
            used.append({"key": k, "count": n, "edge": round(edge, 3)})
        adj = (adj_sum / weight_sum) if weight_sum > 0 else 0.0
        adj = max(-self.boa_feedback_max_adj, min(self.boa_feedback_max_adj, adj * self.boa_feedback_weight))
        return {"enabled": True, "adjustment": round(adj, 3), "used": used}

    def _evaluate_candidate_for_ranking(self, sym: str, score: float, result: dict,
                                        prices: list, highs: list, lows: list, vols: list,
                                        htf_p: list, regime: str, ts_str: str, date_str: str,
                                        ts_ms: int = 0, btc_prices: list = None, rs_snapshot: dict = None):
        """Sert filtrelerden geçip President kararı OPEN olan adayı ranking havuzuna alır."""
        price   = prices[-1] if prices else 0.0
        if price <= 0:
            return None

        # V10 Phase-1 context: RegimePacket + RelativeStrengthResult hesaplanır.
        # Shadow modda karar değişmez; soft/hard etkileri President OPEN sonrası uygulanır.
        try:
            regime_packet = self.regime_router.evaluate(sym, prices, highs, lows, vols, btc_prices or [], legacy_regime=regime)
        except Exception as _rr_e:
            regime_packet = RegimePacket(mode="error", regime_reason=f"router_error:{_rr_e}")
        try:
            rs_result = (rs_snapshot or {}).get(sym)
            if rs_result is None:
                rs_result = RelativeStrengthResult(mode="missing", rs_reason="snapshot_missing")
        except Exception as _rs_e:
            rs_result = RelativeStrengthResult(mode="error", rs_reason=f"rs_error:{_rs_e}")

        atr_pct = result.get("components", {}).get("atr_pct", 0.0)
        adx_val = result.get("components", {}).get("adx", 0.0)
        rsi_val = result.get("components", {}).get("rsi", 50.0)
        is_long_candidate = score >= self.score_long_open
        # V9 FIX (mimari düzeltme): "score <= score_short_open" proxy'si SHORT
        # özel filtreler için KALDIRILDI — merkezi score, gerçek President
        # kararını (short_surgeon branch + PresidentRuntime.evaluate) temsil
        # etmez. SHORT özel filtreler (RSI_TOO_LOW, MTF_NO_CONFIRM_SHORT) artık
        # AŞAĞIDA, packet.side == "SHORT" kesinleştikten SONRA uygulanıyor
        # (bkz. self.runtime.evaluate() çağrısının hemen altı).

        if sym != "BTCUSDT" and self.bl_enabled and sym in self.bl_symbols:
            self._log_filter("SYMBOL_BLACKLIST", sym, score, ts_str)
            if is_long_candidate:
                self._record_block(ts_ms, sym, "SYMBOL_BLACKLIST", price, regime, score)
            return None

        if self._daily_trade_count.get(date_str, 0) >= self.max_trades_day:
            if is_long_candidate:
                self._log_filter("DAILY_TRADE_LIMIT", sym, score, ts_str)
                self._record_block(ts_ms, sym, "DAILY_TRADE_LIMIT", price, regime, score)
            return None

        # V8.5.9 FIX: Günlük hedef/kayıp kapısı — engine.py'deki
        # _daily_target_hit() / _daily_loss_hit() ile parity.
        # Önceden backtest'te bu kapı yoktu: live'da gün içi limit dolunca
        # işlem duruyordu ama backtest'te durmuyordu → PnL olduğundan iyi görünüyordu.
        equity_now = self.equity + self._pnl_running
        day_pnl    = self._daily_pnl.get(date_str, 0.0)
        if self.daily_target > 0 and day_pnl >= equity_now * self.daily_target:
            self._log_filter("DAILY_TARGET_HIT", sym, score, ts_str,
                             extra={"day_pnl": round(day_pnl, 2)})
            return None
        if self.daily_loss_lim > 0 and day_pnl <= -(equity_now * self.daily_loss_lim):
            self._log_filter("DAILY_LOSS_LIMIT", sym, score, ts_str,
                             extra={"day_pnl": round(day_pnl, 2)})
            if is_long_candidate:
                self._record_block(ts_ms, sym, "DAILY_LOSS_LIMIT", price, regime, score)
            return None

        # V9 FIX (SHORT parity): engine.py'de ADX side'dan bağımsız uygulanır
        # (satır ~586). is_long_candidate gate'i kaldırıldı — artık her aday
        # için (LONG veya SHORT) aynı şekilde çalışıyor, engine.py ile birebir.
        adx_blocks = self.adx_filter_en and (
            (adx_val > 0 and adx_val < self.adx_thr) or
            (adx_val <= 0 and self.require_adx_strict)
        )
        if adx_blocks:
            self._log_filter("ADX_TOO_LOW", sym, score, ts_str, extra={"adx": round(adx_val, 1)})
            self._record_block(ts_ms, sym, "ADX_TOO_LOW", price, regime, score)
            return None

        # değişiklik başlangıcı — DOMINANS REJIM FILTRESI (yeni, izole, A/B test edilebilir)
        # Bulgu: BTC.D kendi 20g EMA'sinin USTUNDEYKEN (BTC-rotasyon/risk-off rejimi)
        # hem LONG hem SHORT trade'ler anlamli sekilde daha kotu performans gosteriyor
        # (538 gunluk gercek backtest'te dogrulandi). side'dan bagimsiz uygulanir.
        if self.dominance_enabled and self.dominance is not None and self.dominance.is_risk_off(ts_ms):
            snap = self.dominance.snapshot(ts_ms)
            self._log_filter("DOMINANCE_RISK_OFF", sym, score, ts_str, extra=snap)
            self._record_block(ts_ms, sym, "DOMINANCE_RISK_OFF", price, regime, score)
            return None
        # değişiklik bitişi

        # V9 FIX (SHORT parity): engine.py'de BTC genel düşüş filtresi de
        # side'dan bağımsız uygulanır (satır ~603) — is_long_candidate gate'i
        # kaldırıldı.
        if self.btc_filter_en and btc_prices and len(btc_prices) >= self.btc_filter_candles + 1:
            _b_start = btc_prices[-(self.btc_filter_candles + 1)]
            _b_end   = btc_prices[-1]
            _b_drop  = (_b_end - _b_start) / _b_start * 100 if _b_start > 0 else 0.0
            if _b_drop <= -self.btc_filter_drop:
                self._log_filter("BTC_FILTER_DROP", sym, score, ts_str, extra={"btc_drop_pct": round(_b_drop, 2)})
                self._record_block(ts_ms, sym, "BTC_FILTER_DROP", price, regime, score)
                return None

        htf_sc = 100.0 if not self.mtf_enabled else 50.0
        if self.mtf_enabled and len(htf_p) >= 50:
            try:
                htf_sc = score_symbol(htf_p)["final_score"]
            except Exception:
                htf_sc = 50.0
            if is_long_candidate and htf_sc < self.mtf_long_min:
                self._log_filter("MTF_NO_CONFIRM", sym, score, ts_str, extra={"htf": round(htf_sc, 1), "side": "LONG"})
                self._record_block(ts_ms, sym, "MTF_NO_CONFIRM", price, regime, score)
                return None

        ts_sec = (ts_ms / 1000) if ts_ms else time.time()
        sentiment = "BEARISH" if regime == "BEARISH" else ("BULLISH" if regime in ("BULL", "TREND") else "NEUTRAL")

        # V8.5.8 Pump/Manipülasyon Filtresi — SERT BLOK DEĞİL. Şüpheli ani
        # hacim+fiyat patlaması tespit edilirse skor cezalandırılır ve
        # (açılırsa) pozisyon boyutu küçültülür; sinyal engellenmez.
        pump_info = compute_pump_risk(prices, vols, self.cfg)
        if pump_info.get("is_pump"):
            score = max(0.0, score - pump_info["score_penalty"])
            self._log_filter("PUMP_RISK_SOFT", sym, score, ts_str, extra={
                "vol_ratio": pump_info["vol_ratio"], "price_chg_pct": pump_info["price_chg_pct"],
                "score_penalty": pump_info["score_penalty"], "size_mult": pump_info["size_mult"],
            })

        # BOA feedback aynı testin geleceğini kullanmaz; sadece geçmiş/önceki hafıza feature'ıdır.
        # Side henüz kesinleşmediği için ilk etapta nötr verilir; President side seçtikten sonra packet.extra'da güncellenir.
        result = dict(result or {})
        result["regime_router_report"] = regime_packet.to_dict()
        result["relative_strength_report"] = rs_result.to_dict()
        result["boa_feedback_report"] = self._boa_feedback_report(sym, "LONG", regime)
        packet = self.runtime.evaluate(sym, ts_sec, score, result, regime, htf_sc, sentiment,
                                       prices, highs, lows, vols, btc_prices)
        if packet.side.value in ("LONG", "SHORT"):
            boa_rep = self._boa_feedback_report(sym, packet.side.value, regime)
            packet.extra["boa_feedback_report"] = boa_rep
            # President kararından sonra ranking score'a da aynı küçük edge eklenir.
            rank_score = max(0.0, min(100.0, float(packet.final_score) + float(boa_rep.get("adjustment", 0.0))))
        else:
            rank_score = float(packet.final_score)

        # V10 Phase-1: raporlar packet.extra üzerinden pozisyona ve CSV'ye taşınır.
        try:
            packet.extra["regime_router_report"] = regime_packet.to_dict()
            packet.extra["relative_strength_report"] = rs_result.to_dict()
            packet.extra["module_name"] = "core_long" if packet.side.value == "LONG" else ("short_surgeon" if packet.side.value == "SHORT" else "none")
        except Exception:
            pass

        if packet.action == Action.OPEN:
            # V10 Phase-1 ACTIVE TEST: sadece LONG kararına uygula; SHORT motoru bu fazda değiştirilmez.
            if packet.side.value == "LONG":
                # değişiklik başlangıcı — TUZAKLANMIS POZISYON ASIMETRISI (yeni, A/B test edilebilir)
                # Bulgu: fiyat, OI-akis agirlikli ortalama maliyetin USTUNDEYSE
                # (yakin gecmiste acilan pozisyonlar KARDA) LONG daha guvenilir.
                # "cross" modunda sadece TAZE gecis aninda (5-30g pencere
                # taramasinda win %92.6-97.6, parametre-bagimsiz dogrulandi).
                if self.trapped_filter_enabled and self.trapped_filter is not None:
                    if not self.trapped_filter.allows_long(sym, date_str, price):
                        self._log_filter("TRAPPED_DIST_LONG_BLOCKED", sym, score, ts_str, extra={"side": "LONG"})
                        self._record_block(ts_ms, sym, "TRAPPED_DIST_LONG_BLOCKED", price, regime, score, side="LONG")
                        return None
                # değişiklik bitişi
                vr = self._apply_v10_phase1_controls(sym, score, rank_score, packet, regime_packet, rs_result, ts_str, ts_ms, price, regime)
                if vr is None:
                    return None
                rank_score, packet = vr

            # V9 FIX (mimari düzeltme): SHORT özel filtreler artık GERÇEK
            # President kararına (packet.side == "SHORT") bağlı — merkezi
            # score'a göre tahmin edilmiyor. engine.py'deki post-decision
            # side-aware kontrollerle birebir aynı (satır ~640-645).
            if packet.side.value == "SHORT":
                # değişiklik başlangıcı — FUNDING-GATED SHORT (yeni, izole, A/B test edilebilir)
                # Bulgu: funding<=0 (kalabalik-short, perp short ODER) iken SHORT
                # acmak anlamli sekilde daha kotu (gercek trade verisinde p=0.0019
                # ile dogrulandi). funding>0 sartini saglamayan SHORT adaylari
                # burada engellenir — diger tum SHORT mantigindan ONCE.
                if self.funding_filter_enabled and self.funding_regime is not None:
                    if not self.funding_regime.is_funding_positive(sym, ts_ms):
                        self._log_filter("FUNDING_FILTER_SHORT_BLOCKED", sym, score, ts_str,
                                          extra={"side": "SHORT"})
                        self._record_block(ts_ms, sym, "FUNDING_FILTER_SHORT_BLOCKED", price, regime, score, side="SHORT")
                        return None
                # değişiklik bitişi
                # değişiklik başlangıcı — TUZAKLANMIS POZISYON ASIMETRISI (SHORT tarafi)
                # fiyat maliyetin ALTINDAYSA (kalabalik zaten zararda) SHORT daha guvenilir.
                if self.trapped_filter_enabled and self.trapped_filter is not None:
                    if not self.trapped_filter.allows_short(sym, date_str, price):
                        self._log_filter("TRAPPED_DIST_SHORT_BLOCKED", sym, score, ts_str, extra={"side": "SHORT"})
                        self._record_block(ts_ms, sym, "TRAPPED_DIST_SHORT_BLOCKED", price, regime, score, side="SHORT")
                        return None
                # değişiklik bitişi
                # weakness_short_gate: short_surgeon kaynaklı ve weakness_score'u
                # yüksek SHORT adaylar için ayrı (gevşetilmiş) HTF/RSI eşiği.
                # Config'te yoksa (varsayılan disabled) davranış AYNEN korunur —
                # default_short_gate (mtf_short_max/rsi_min_short) hiç değişmez.
                sg = ((self.cfg.get("short_surgeon", {}) or {}).get("weakness_short_gate", {}) or {})
                sg_enabled = bool(sg.get("enabled", False))
                ss_vote = (packet.branch_votes or {}).get("short_surgeon") if hasattr(packet, "branch_votes") else None
                ss_weakness = float(((getattr(ss_vote, "debug", None) or {}).get("weakness", 0.0)) or 0.0)
                is_weakness_short = bool(
                    sg_enabled and ss_vote is not None and
                    getattr(ss_vote, "action", None) == Action.OPEN and
                    getattr(ss_vote, "side", None) == Side.SHORT and
                    ss_weakness >= float(sg.get("min_weakness_score", 75.0))
                )
                if is_weakness_short:
                    eff_htf_max = float(sg.get("htf_short_max", self.mtf_short_max))
                    eff_rsi_min = float(sg.get("rsi_min_short", self.rsi_min_short))
                    gate_label = "SHORT_GATE_WEAKNESS_RELAXED"
                else:
                    eff_htf_max = self.mtf_short_max
                    eff_rsi_min = self.rsi_min_short
                    gate_label = "SHORT_GATE_DEFAULT"

                if self.rsi_filter_en and rsi_val < eff_rsi_min:
                    self._log_filter("RSI_TOO_LOW", sym, score, ts_str, extra={"rsi": round(rsi_val, 1), "side": "SHORT", "gate": gate_label})
                    self._record_block(ts_ms, sym, "RSI_TOO_LOW", price, regime, score, side="SHORT")
                    return None
                if self.mtf_enabled and htf_sc > eff_htf_max:
                    self._log_filter("MTF_NO_CONFIRM_SHORT", sym, score, ts_str, extra={"htf": round(htf_sc, 1), "side": "SHORT", "gate": gate_label})
                    self._record_block(ts_ms, sym, "MTF_NO_CONFIRM_SHORT", price, regime, score, side="SHORT")
                    return None
                try:
                    packet.extra["short_gate_type"] = gate_label
                except Exception:
                    pass
                if is_weakness_short:
                    rel_size = sg.get("size_mult")
                    if rel_size is not None:
                        try:
                            packet.size_mult = float(rel_size)
                        except Exception:
                            pass
                    # NOT: max_hold_hours override burada UYGULANMADI — mevcut
                    # adaptive_exit policy (ae.policy.max_hold_hours -> pos["max_hold_bars_override"])
                    # ile çakışma riski net olmadan eklenmedi. Bkz. patch raporu.
            return {
                "symbol": sym, "score": score, "result": result, "packet": packet,
                "price": price, "adx_val": adx_val, "atr_pct": atr_pct, "regime": regime,
                "ts_str": ts_str, "date_str": date_str, "ts_ms": ts_ms,
                "prices": prices, "highs": highs, "lows": lows, "vols": vols,
                "htf_score": htf_sc, "rank_score": round(rank_score, 3),
                "quality_score": float((packet.extra.get("quality_score_report") or {}).get("score", 50.0) or 50.0),
                "symbol_mult": self.sym_mgr.size_multiplier(sym) if hasattr(self, "sym_mgr") else 1.0,
                "boa_feedback": packet.extra.get("boa_feedback_report", {}),
                "regime_router": packet.extra.get("regime_router_report", {}),
                "relative_strength": packet.extra.get("relative_strength_report", {}),
                "pump_context": pump_info,
            }

        open_votes_list = [v for v in packet.branch_votes.values() if v.action == Action.OPEN]
        if open_votes_list or is_long_candidate:
            cause = "PRESIDENT_" + (packet.reason.split()[0] if packet.reason else "BLOCK")
            intended_side = (packet.side.value if packet.side.value != "NONE" else (open_votes_list[0].side.value if open_votes_list else "LONG"))
            self._record_block(ts_ms, sym, cause, price, regime, score, side=intended_side)
        if self.ghost_en and score >= self.ghost_min_score:
            self._log_ghost(sym, score, result, prices, ts_str, "PRESIDENT_BLOCK")
        return None

    def _apply_v10_phase1_controls(self, sym: str, base_score: float, rank_score: float, packet,
                                    regime_packet, rs_result, ts_str: str, ts_ms: int,
                                    price: float, legacy_regime: str):
        """V10 Phase-1 Department Advice scoring.

        MİMARİ KURAL:
        - Regime Router ve Relative Strength final gate değildir.
        - İkisi de President'e bağlı departman raporu üretir.
        - OPEN / BLOCK / REDUCE final kararını yalnızca President bu fonksiyonda yazar.
        - 0-100 puan sistemi şişmesin diye pozitif/negatif score adjustment cap'lenir.
        - Shadow/disabled modda trade kararı değişmez.
        """
        rr = regime_packet.to_dict() if hasattr(regime_packet, "to_dict") else dict(regime_packet or {})
        rs = rs_result.to_dict() if hasattr(rs_result, "to_dict") else dict(rs_result or {})
        rr_mode = str(rr.get("mode", "disabled")).lower()
        rs_mode = str(rs.get("mode", "disabled")).lower()
        active = rr_mode in ("soft", "hard") or rs_mode in ("soft", "hard")
        if not active:
            return rank_score, packet

        pol = ((self.cfg.get("president", {}) or {}).get("department_policy", {}) or {})
        pol_enabled = bool(pol.get("enabled", True))
        use_rr = bool(pol.get("use_regime_router", True))
        use_rs = bool(pol.get("use_relative_strength", True))
        if not pol_enabled:
            return rank_score, packet

        def _cap(x, lo, hi):
            try:
                return max(float(lo), min(float(hi), float(x)))
            except Exception:
                return 0.0

        # Regime department: iyi rejim küçük bonus, kötü rejim kontrollü penalty üretir.
        regime_adj = 0.0
        regime_veto = ""
        regime_reason_parts = []
        regime_size_mult = 1.0
        if use_rr and rr_mode in ("soft", "hard"):
            regime_size_mult = float(rr.get("long_size_mult", 1.0) or 1.0)
            # Eski min_score_offset artık President score adjustment olarak okunur.
            regime_adj -= float(rr.get("min_score_offset", 0.0) or 0.0)
            btc_macro = str(rr.get("btc_macro_regime", "UNKNOWN")).upper()
            sym_micro = str(rr.get("symbol_micro_regime", "UNKNOWN")).upper()
            if btc_macro == "BULL" and sym_micro in ("TREND_UP", "BREAKOUT"):
                regime_adj += 2.0
                regime_reason_parts.append("REGIME_SUPPORTS_LONG")
            if rr.get("block_long"):
                regime_veto = rr.get("block_reason") or "REGIME_RECOMMENDED_VETO"
            if rr.get("require_breakout") and sym_micro not in ("BREAKOUT", "TREND_UP"):
                regime_adj -= 2.0
                regime_reason_parts.append("REGIME_RECOMMENDS_BREAKOUT_REQUIRED")
                if rr_mode == "hard":
                    regime_veto = regime_veto or "REGIME_RECOMMENDED_VETO_CHOP_NO_BREAKOUT"
            if rr.get("require_strong_rs") and str(rs.get("rs_state", "UNKNOWN")).upper() != "STRONG":
                regime_adj -= 2.0
                regime_reason_parts.append("REGIME_RECOMMENDS_STRONG_RS_REQUIRED")
                if rr_mode == "hard":
                    regime_veto = regime_veto or "REGIME_RECOMMENDED_VETO_CHOP_WEAK_RS"

        rs_adj = 0.0
        rs_veto = ""
        rs_size_mult = 1.0
        rs_reason_parts = []
        if use_rs and rs_mode in ("soft", "hard"):
            rs_adj = float(rs.get("score_adjustment", 0.0) or 0.0)
            rs_size_mult = float(rs.get("size_mult", 1.0) or 1.0)
            if rs.get("block_long"):
                rs_veto = rs.get("block_reason") or "RS_RECOMMENDED_VETO_WEAK"
            # CHOP'ta güçlü RS isteniyorsa bu bir departman tavsiyesidir; final kararı President verir.
            if bool((self.cfg.get("relative_strength", {}) or {}).get("require_strong_in_chop", True)):
                if rr.get("require_strong_rs") and str(rs.get("rs_state", "UNKNOWN")).upper() != "STRONG":
                    rs_adj -= 2.0
                    rs_reason_parts.append("RS_RECOMMENDS_CHOP_NOT_STRONG")
                    if rs_mode == "hard":
                        rs_veto = rs_veto or "RS_RECOMMENDED_VETO_CHOP_NOT_STRONG"

        # Departman katkılarını ayrı ve toplam cap ile kontrol et: bonus pump yok.
        max_reg_pos = float(pol.get("max_regime_positive_adjustment", 2.0))
        max_reg_neg = float(pol.get("max_regime_negative_adjustment", 10.0))
        max_rs_pos = float(pol.get("max_rs_positive_adjustment", 3.0))
        max_rs_neg = float(pol.get("max_rs_negative_adjustment", 5.0))
        regime_adj_c = _cap(regime_adj, -max_reg_neg, max_reg_pos)
        rs_adj_c = _cap(rs_adj, -max_rs_neg, max_rs_pos)
        raw_total_adj = regime_adj + rs_adj
        capped_total = regime_adj_c + rs_adj_c
        max_pos_total = float(pol.get("max_positive_score_adjustment", 4.0))
        max_neg_total = float(pol.get("max_negative_score_adjustment", 14.0))
        dept_adj = _cap(capped_total, -max_neg_total, max_pos_total)

        final_score = max(0.0, min(100.0, float(rank_score) + dept_adj))
        required = float(pol.get("min_final_score_to_open", self.score_long_open))
        allow_override = bool(pol.get("allow_override", True))
        # BUG-002 fix: 105.0 varsayılanı final_score 0-100 bandında asla ulaşılamayan bir
        # eşikti (override mekanizması fiilen ölü kodtu). Ulaşılabilir bir varsayılana
        # çekildi; config'te override_min_score açıkça tanımlıysa o değer kullanılır.
        override_min = float(pol.get("override_min_score", 99.0))
        accept_regime_veto = bool(pol.get("accept_regime_veto", True))
        accept_rs_veto = bool(pol.get("accept_rs_veto", True))
        base_for_override = float(rank_score)
        override_active = bool(allow_override and base_for_override >= override_min)
        accepted_veto = ""
        overridden_veto = ""
        if regime_veto and accept_regime_veto and not override_active:
            accepted_veto = "PRESIDENT_ACCEPTED_REGIME_VETO__" + regime_veto
        elif regime_veto and accept_regime_veto and override_active:
            overridden_veto = "PRESIDENT_OVERRIDED_DEPARTMENT_VETO__" + regime_veto
        if not accepted_veto and rs_veto and accept_rs_veto and not override_active:
            accepted_veto = "PRESIDENT_ACCEPTED_RS_VETO__" + rs_veto
        elif not accepted_veto and rs_veto and accept_rs_veto and override_active and not overridden_veto:
            overridden_veto = "PRESIDENT_OVERRIDED_DEPARTMENT_VETO__" + rs_veto

        size_mult = float(getattr(packet, "size_mult", 1.0) or 1.0) * regime_size_mult * rs_size_mult
        min_size = float(pol.get("min_final_size_mult", 0.20))
        max_size = float(pol.get("max_final_size_mult", 1.00))
        final_size = _cap(size_mult, min_size, max_size)

        president_reason = []
        president_reason.extend(regime_reason_parts)
        president_reason.extend(rs_reason_parts)
        if overridden_veto:
            president_reason.append(overridden_veto)
        if raw_total_adj != dept_adj:
            president_reason.append("PRESIDENT_CAPPED_DEPARTMENT_SCORE")
        if final_size < float(getattr(packet, "size_mult", 1.0) or 1.0):
            president_reason.append("PRESIDENT_ACCEPTED_DEPARTMENT_SIZE_REDUCTION")

        log_extra = {
            "btc_macro_regime": rr.get("btc_macro_regime", ""),
            "symbol_micro_regime": rr.get("symbol_micro_regime", ""),
            "volatility_state": rr.get("volatility_state", ""),
            "rs_state": rs.get("rs_state", ""),
            "rs_score": rs.get("rs_score", ""),
            "president_base_score": round(float(rank_score), 3),
            "president_final_score": round(final_score, 3),
            "department_score_adjustment": round(dept_adj, 3),
            "raw_department_score_adjustment": round(raw_total_adj, 3),
            "regime_score_adjustment": round(regime_adj_c, 3),
            "rs_score_adjustment": round(rs_adj_c, 3),
            "required_score": required,
            "final_size_mult": round(final_size, 4),
            "regime_veto_recommendation": regime_veto,
            "rs_veto_recommendation": rs_veto,
        }

        if accepted_veto:
            self._log_filter(accepted_veto, sym, base_score, ts_str, extra=log_extra)
            self._record_block(ts_ms, sym, accepted_veto, price, legacy_regime, base_score, side="LONG")
            try:
                packet.extra["president_department_reason"] = accepted_veto
            except Exception:
                pass
            return None

        if final_score < required:
            cause = "PRESIDENT_SCORE_AFTER_DEPARTMENT_ADVICE_REJECTED"
            self._log_filter(cause, sym, base_score, ts_str, extra=log_extra)
            self._record_block(ts_ms, sym, cause, price, legacy_regime, base_score, side="LONG")
            try:
                packet.extra["president_department_reason"] = cause
            except Exception:
                pass
            return None

        packet.size_mult = final_size
        packet.final_score = final_score
        try:
            packet.extra["v10_phase1_adjusted_rank_score"] = round(final_score, 3)
            packet.extra["v10_phase1_size_mult"] = round(packet.size_mult, 4)
            packet.extra["president_final_score"] = round(final_score, 3)
            packet.extra["department_score_adjustment"] = round(dept_adj, 3)
            packet.extra["raw_department_score_adjustment"] = round(raw_total_adj, 3)
            packet.extra["regime_score_adjustment"] = round(regime_adj_c, 3)
            packet.extra["rs_score_adjustment"] = round(rs_adj_c, 3)
            packet.extra["president_department_reason"] = ";".join(president_reason)[:300]
            packet.extra["regime_veto_recommendation"] = regime_veto
            packet.extra["rs_veto_recommendation"] = rs_veto
        except Exception:
            pass
        return final_score, packet

    def _rank_reject_reason(self, cand: dict, selected_min_score: float = 0.0) -> str:
        q = float(cand.get("quality_score", 50.0) or 50.0)
        sym_mult = float(cand.get("symbol_mult", 1.0) or 1.0)
        label = str(getattr(cand.get("packet"), "label", "") or "").upper()
        if q < self.rank_bad_quality_below:
            reason = "RANK_REJECTED_BAD_QUALITY"
        elif sym_mult < 0.70:
            reason = "RANK_REJECTED_SYMBOL_PENALTY"
        elif label in self.rank_chop_labels:
            reason = "RANK_REJECTED_CHOP_RISK"
        else:
            reason = "RANK_REJECTED_LOWER_SCORE"
        # V9.0.5 FIX (ghost-config temizliği): president.global_ranking.reject_reasons
        # artık gerçekten kullanılıyor — kod ile config arasında senkron bozulursa
        # (örn. biri yeni bir reason ekler ama config'i güncellemezse) burada
        # sessizce değil, açıkça uyarı basılır. Sinyal/karar mantığını DEĞİŞTİRMEZ.
        if self.rank_reject_reasons_allowed and reason not in self.rank_reject_reasons_allowed:
            print(f"[WARN] _rank_reject_reason: '{reason}' config'teki president.global_ranking."
                  f"reject_reasons listesinde yok — config güncellenmeli.")
        return reason

    def _open_ranked_candidates(self, candidates: list, ts_str: str, date_str: str, ts_ms: int):
        """Aynı timestamp adaylarını birlikte sıralar ve kapasiteye göre en iyileri açar.

        V8.5.7 kuralı:
        - Eski kaba MAX_POSITIONS sebebi aynı-candle ranking içinde kullanılmaz.
        - Eğer test daha önceki mumlardan dolayı zaten tamamen doluysa: MAX_POSITIONS_ALREADY_FULL.
        - Eğer aynı mumda adaylar arası seçim yapıldıysa: RANK_SELECTED / RANK_REJECTED_* yazılır.
        """
        if not candidates:
            return
        candidates = sorted(
            candidates,
            key=lambda c: (float(c.get("rank_score", 0.0)), float(c.get("quality_score", 0.0))),
            reverse=True,
        )
        if self.rank_max_candidates_per_bar > 0:
            candidates = candidates[:self.rank_max_candidates_per_bar]

        start_active = len(self.open_positions)
        available = max(0, self.max_open_pos - start_active)
        opened = []

        # Kapasite bu mum başlamadan zaten doluysa bunu ranking reddi gibi değil, gerçek portföy doluluğu gibi yaz.
        if available <= 0:
            for rank, cand in enumerate(candidates, start=1):
                self._log_filter("MAX_POSITIONS_ALREADY_FULL", cand["symbol"], cand["rank_score"], ts_str, extra={
                    "rank": rank, "side": cand["packet"].side.value, "label": cand["packet"].label,
                    "quality_score": round(cand.get("quality_score", 0.0), 2),
                    "active_positions": start_active, "max_positions": self.max_open_pos,
                    "boa_adj": (cand.get("boa_feedback") or {}).get("adjustment", 0.0),
                })
                self._record_block(cand["ts_ms"], cand["symbol"], "MAX_POSITIONS_ALREADY_FULL", cand["price"], cand["regime"], cand["score"], side=cand["packet"].side.value)
            return

        for cand in candidates:
            if available <= 0:
                break
            # V9 FIX: aynı mum içinde art arda açılan adaylar günlük limiti
            # aşabiliyordu çünkü bu kontrol sadece _evaluate_candidate_for_ranking
            # anında (tek seferlik snapshot) yapılıyordu. Burada da tekrar kontrol et.
            if self._daily_trade_count.get(cand["date_str"], 0) >= self.max_trades_day:
                self._log_filter("DAILY_TRADE_LIMIT", cand["symbol"], cand["rank_score"], cand["ts_str"], extra={
                    "rank": candidates.index(cand) + 1, "reason": "batch_recheck",
                })
                self._record_block(cand["ts_ms"], cand["symbol"], "DAILY_TRADE_LIMIT", cand["price"], cand["regime"], cand["score"], side=cand["packet"].side.value)
                continue
            sym = cand["symbol"]
            if sym in self.open_positions:
                continue
            packet = cand["packet"]
            branch_scores = {k: round(v.score, 2) for k, v in packet.branch_votes.items()}
            self._open_from_decision(
                sym, packet.side.value, cand["price"], cand["score"], cand["adx_val"], cand["atr_pct"],
                cand["regime"], cand["ts_str"], cand["date_str"], packet.sl_pct, packet.size_mult,
                label=packet.label, decision_id=packet.decision_id, branch_scores=branch_scores,
                htf_score=cand.get("htf_score", 50.0), prices=cand["prices"], highs=cand["highs"], lows=cand["lows"], vols=cand["vols"],
                packet_extra=getattr(packet, "extra", {}), score_components=(cand["result"].get("components", {}) or {}),
                rank_context={
                    "rank_score": cand.get("rank_score", ""),
                    "candidate_count": len(candidates),
                    "rank_position": candidates.index(cand) + 1,
                    "boa_feedback": cand.get("boa_feedback") or {},
                    "regime_router": cand.get("regime_router") or {},
                    "relative_strength": cand.get("relative_strength") or {},
                },
                pump_context=cand.get("pump_context"),
                setup_type=getattr(packet, "setup_type", ""),
                selected_engine=getattr(packet, "selected_engine", ""),
                sl_profile=getattr(packet, "sl_profile", ""),
                tp_profile=getattr(packet, "tp_profile", ""),
                trail_profile=getattr(packet, "trail_profile", ""),
            )
            opened.append(sym)
            available -= 1

        if not self.rank_reject_log:
            return
        selected_min = min([c.get("rank_score", 0.0) for c in candidates if c["symbol"] in opened], default=0.0)
        for rank, cand in enumerate(candidates, start=1):
            if cand["symbol"] in opened:
                self._log_ranking_event("RANK_SELECTED", cand, ts_str, rank, selected_min, opened_count=len(opened), total_candidates=len(candidates))
                continue
            reason = self._rank_reject_reason(cand, selected_min)
            self._log_ranking_event(reason, cand, ts_str, rank, selected_min, opened_count=len(opened), total_candidates=len(candidates))
            self._record_block(cand["ts_ms"], cand["symbol"], reason, cand["price"], cand["regime"], cand["score"], side=cand["packet"].side.value)
            if self.ghost_en and cand["score"] >= self.ghost_min_score:
                self._log_ghost(cand["symbol"], cand["score"], cand["result"], cand["prices"], ts_str, reason)

    def _log_ranking_event(self, cause: str, cand: dict, ts_str: str, rank: int, selected_min: float, opened_count: int, total_candidates: int):
        """Hem filter_events.csv hem ayrı candidate_ranking_events.csv için zengin ranking logu."""
        pkt = cand.get("packet")
        ev_extra = {
            "rank": rank,
            "side": pkt.side.value if pkt else "",
            "label": pkt.label if pkt else "",
            "quality_score": round(cand.get("quality_score", 0.0), 2),
            "selected_min_rank_score": round(selected_min, 3),
            "opened_count": opened_count,
            "total_candidates": total_candidates,
            "active_positions_after": len(self.open_positions),
            "max_positions": self.max_open_pos,
            "boa_adj": (cand.get("boa_feedback") or {}).get("adjustment", 0.0),
        }
        self._log_filter(cause, cand["symbol"], cand.get("rank_score", cand.get("score", 0.0)), ts_str, extra=ev_extra)
        row = {"ts": ts_str, "symbol": cand["symbol"], "cause": cause, "rank_score": round(cand.get("rank_score", 0.0), 3)}
        row.update(ev_extra)
        self.ranking_events.append(row)

    def _maybe_rotate_for_candidate(self, sym: str, score: float, price: float,
                                    ts_str: str, date_str: str, ts_ms: int) -> bool:
        """
        SAFETY PATCH V8.4.1:
        Rotation artık President kararından ÖNCE fiziksel pozisyon kapatmaz.
        Burada sadece aday loglanır. Yer açma/kapama kararı ayrı President aksiyonu
        haline getirilene kadar return False kalır.
        """
        if not self.rotation_enabled or len(self.open_positions) < self.max_open_pos:
            return False
        if score < self.rotation_min_score:
            return False
        if self._daily_rotations.get(date_str, 0) >= self.rotation_max_per_day:
            return False

        weakest_sym, weakest_pos = None, None
        weakest_score = 999.0
        weakest_change = 0.0
        for osym, pos in self.open_positions.items():
            last_price = float(pos.get("last_price", pos.get("entry", price)))
            entry = float(pos.get("entry", last_price))
            mult = 1 if pos.get("side") == "LONG" else -1
            change = ((last_price - entry) / entry * mult) if entry else 0.0
            # Kârdaki pozisyonlar varsayılan olarak rotasyon dışıdır.
            if change > 0 and not self.rotation_allow_close_profitable:
                continue
            ps = float(pos.get("score", 0.0))
            # Basit zayıflık: düşük açılış skoru + mevcut zarar
            weakness = ps + max(change * 100, -20)
            if weakness < weakest_score:
                weakest_sym, weakest_pos, weakest_score, weakest_change = osym, pos, weakness, change

        if not weakest_sym or score < float(weakest_pos.get("score", 0.0)) + self.rotation_min_delta:
            return False

        self._log_filter("ROTATION_CANDIDATE_SHADOW", sym, score, ts_str, extra={
            "candidate": sym,
            "candidate_score": round(score, 2),
            "would_replace": weakest_sym,
            "old_score": round(float(weakest_pos.get("score", 0.0)), 2),
            "old_unrealized_pct": round(weakest_change * 100, 3),
            "shadow_only": True,
        })
        # Güvenlik: President ROTATE_AND_OPEN aksiyonu yazılana kadar fiziksel kapatma yok.
        return False

    # ── Pozisyon Yonetimi ─────────────────────────────────────────────
    def _tp1_prog_htf_score(self, htf_p: list) -> float:
        """Güncel HTF skoru — MTF filtresindeki aynı hesap, nötr varsayılan 50.0."""
        if not htf_p or len(htf_p) < 50:
            return 50.0
        try:
            return score_symbol(htf_p)["final_score"]
        except Exception:
            return 50.0

    def _tp1_prog_volume_ratio(self, vols: list) -> float:
        """Son N mum hacim ortalaması / önceki M mumun ortalaması (pump_filter.py
        ile aynı oran kuralı). Yetersiz veri varsa 1.0 (nötr) döner."""
        if not vols:
            return 1.0
        recent_n = self.tp1_prog_c_vol_recent
        base_n   = self.tp1_prog_c_vol_lookback
        if len(vols) < base_n:
            return 1.0
        recent = vols[-recent_n:]
        base   = vols[-base_n:-recent_n] if base_n > recent_n else vols[-base_n:]
        if not recent or not base:
            return 1.0
        base_avg = sum(base) / len(base)
        if base_avg <= 0:
            return 1.0
        return (sum(recent) / len(recent)) / base_avg

    def _tp1_prog_btc_change_pct(self, btc_prices: list) -> float:
        """BTC'nin son N mumdaki yüzde değişimi (BTC filtresiyle aynı formül)."""
        n = self.tp1_prog_c_btc_lookback
        if not btc_prices or len(btc_prices) < n + 1:
            return 0.0
        start = btc_prices[-(n + 1)]
        end   = btc_prices[-1]
        return (end - start) / start * 100 if start > 0 else 0.0

    def _tp1_progress_market_confirm(self, side: str, change: float, mfe_r: float,
                                     components: dict, htf_sc: float,
                                     btc_change_pct: float, ae_class: str,
                                     vol_ratio: float) -> dict:
        """
        V9 FIX: TP1 Progress Manager artık sadece "kaç bar geçti + TP1'e ne
        kadar ilerledi" sorusuna değil, bağımsız piyasa teyitlerine de bakarak
        reduction/early-exit kararı veriyor. weakness_score basit, okunabilir,
        config-driven bir oy sayımıdır; LONG/SHORT side-aware yorumlanır.
        Bu fonksiyon SADECE self.tp1_prog_mc_enabled=True iken çağrılır —
        kapalıyken eski (saf süre+ilerleme) davranış birebir korunur.
        """
        reasons: list = []
        weak = 0
        rsi_val = components.get("rsi", 50.0)
        adx_val = components.get("adx", 0.0)

        if self.tp1_prog_c_rsi_en:
            if side == "LONG" and rsi_val >= self.tp1_prog_c_rsi_long_max:
                weak += 1; reasons.append("RSI_HIGH_LONG")
            elif side == "SHORT" and rsi_val <= self.tp1_prog_c_rsi_short_min:
                weak += 1; reasons.append("RSI_LOW_SHORT")

        if self.tp1_prog_c_adx_en and 0 < adx_val < self.tp1_prog_c_adx_min:
            weak += 1; reasons.append("ADX_WEAK")

        if self.tp1_prog_c_vol_en and vol_ratio < self.tp1_prog_c_vol_min_ratio:
            weak += 1; reasons.append("VOLUME_WEAK")

        if self.tp1_prog_c_htf_en:
            if side == "LONG" and htf_sc < self.tp1_prog_c_htf_long_min:
                weak += 1; reasons.append("HTF_WEAK_LONG")
            elif side == "SHORT" and htf_sc > self.tp1_prog_c_htf_short_max:
                weak += 1; reasons.append("HTF_WEAK_SHORT")

        if self.tp1_prog_c_btc_en:
            if side == "LONG" and btc_change_pct <= -self.tp1_prog_c_btc_drop_pct:
                weak += 1; reasons.append("BTC_AGAINST_LONG")
            elif side == "SHORT" and btc_change_pct >= self.tp1_prog_c_btc_rise_pct:
                weak += 1; reasons.append("BTC_AGAINST_SHORT")

        if self.tp1_prog_c_ae_en and ae_class in self.tp1_prog_c_ae_bad:
            weak += 1; reasons.append(f"AE_CLASS_{ae_class}")

        if self.tp1_prog_c_mfe_en and mfe_r < self.tp1_prog_c_mfe_min_r:
            weak += 1; reasons.append("MFE_WEAK")

        return {
            "allow_reduce": weak >= self.tp1_prog_min_conf_reduce,
            "allow_early_exit": weak >= self.tp1_prog_min_conf_early,
            "weakness_score": float(weak),
            "reasons": reasons,
        }

    def _manage_position(self, sym: str, prices: list, result: dict,
                         ts_str: str, date_str: str, ts_ms: int = 0,
                         vols: list = None, htf_p: list = None, btc_prices: list = None):
        pos   = self.open_positions[sym]
        price = prices[-1]
        mult  = 1 if pos["side"] == "LONG" else -1
        change = (price - pos["entry"]) / pos["entry"] * mult
        bars_held = pos.get("bars_held", 0) + 1
        pos["bars_held"]  = bars_held
        pos["last_price"] = price   # force_close için son bilinen fiyat
        pos_sl_pct = pos.get("sl_pct", self.sl_pct)
        reason = None
        # V9 FIX: MFE (en iyi lehte ilerleme) takibi — market confirm'in
        # "fiyat hic olumlu ilerlememis" teyidi icin gerekli.
        pos["mfe"] = max(pos.get("mfe", 0.0), change)

        # Partial TP
        if self.partial_tp_en and not pos.get("tp1_done", False):
            tp1_target = pos_sl_pct * self.tp1_r_mult
            if change >= tp1_target:
                partial_qty = pos["qty"] * float(pos.get("tp1_close_pct", self.tp1_close_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, partial_qty)
                exit_cost = self._fee_cost(price, partial_qty)
                pnl = gross - exit_cost
                self._pnl_running += pnl
                pos["qty"] -= partial_qty
                pos["tp1_done"] = True
                pos["tp1_pnl"]  = round(pnl, 4)
                pos["tp1_exit_cost"] = round(exit_cost, 6)

        # V8.5 TP1 Progress Manager — TP1'e ilerlemeyen trade'de riski azalt
        if self.tp1_prog_enabled and self.partial_tp_en and not pos.get("tp1_done", False):
            tp1_target = max(pos_sl_pct * self.tp1_r_mult, 0.0001)
            progress_to_tp1 = change / tp1_target

            # TP1 yoksa trail'i daha erken sıkılaştır
            # değişiklik başlangıcı — MARKET-CONFIRM SARTI EKLENDI (eski: sadece bar
            # sayisi yeterliydi, "Trail cikislarinin sistematik olarak erken oldugu"
            # bulgusuna dayanarak). Simdi sadece bar sayisi DEGIL, ayrica gercek
            # zayiflik kaniti (RSI/ADX/hacim/HTF/BTC/AE_class/MFE oylamasi) da
            # gerekiyor. Bu fonksiyon zaten asagida 'reduce' icin cagriliyordu;
            # ayni teyidi burada da kullaniyoruz (kod tekrari yok, ayni cagri).
            if bars_held >= self.tp1_prog_tighten_bars and self.use_trailing:
                _tighten_confirm = True  # varsayilan: eski davranis (mc kapaliysa)
                if getattr(self, "tp1_prog_mc_enabled", False):
                    _mc_probe = self._tp1_progress_market_confirm(
                        pos["side"], change, pos.get("mfe", 0.0) / tp1_target,
                        result.get("components", {}) or {},
                        self._tp1_prog_htf_score(htf_p),
                        self._tp1_prog_btc_change_pct(btc_prices),
                        pos.get("ae_class", ""),
                        self._tp1_prog_volume_ratio(vols or []))
                    _tighten_confirm = _mc_probe["weakness_score"] >= 2
                if _tighten_confirm:
                    original_trail = pos.get("original_trail_step", pos.get("trail_step", self.trail_step))
                    pos["trail_step"] = max(0.0015, min(pos.get("trail_step", original_trail), original_trail * self.tp1_prog_tighten_mult))
            # değişiklik bitişi

            # Pozisyon TP1 yönünde ilerlemiyorsa tek seferlik risk azalt
            # (V9 FIX: bu artik "base" kosul — piyasa teyidi asagida ekleniyor)
            base_should_reduce = (
                bars_held >= self.tp1_prog_check_bars
                and not pos.get("tp1_progress_reduced", False)
                and progress_to_tp1 < self.tp1_prog_min_progress
                and (not self.tp1_prog_only_if_not_profitable or change <= 0)
            )
            should_reduce = base_should_reduce
            if base_should_reduce and self.tp1_prog_mc_enabled:
                mc_reduce = self._tp1_progress_market_confirm(
                    pos["side"], change, pos["mfe"] / tp1_target,
                    result.get("components", {}) or {},
                    self._tp1_prog_htf_score(htf_p),
                    self._tp1_prog_btc_change_pct(btc_prices),
                    pos.get("ae_class", ""),
                    self._tp1_prog_volume_ratio(vols or []),
                )
                should_reduce = base_should_reduce and mc_reduce["allow_reduce"]
                pos["tp1_progress_weakness_score"] = mc_reduce["weakness_score"]
                pos["tp1_progress_reasons"] = ",".join(mc_reduce["reasons"])
                pos["tp1_progress_market_confirmed"] = mc_reduce["allow_reduce"]
            pos["tp1_progress_progress_to_tp1"] = round(progress_to_tp1, 4)

            if should_reduce and pos.get("qty", 0) > 0:
                reduce_qty = pos["qty"] * max(0.0, min(0.95, self.tp1_prog_reduce_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, reduce_qty)
                exit_cost = self._fee_cost(price, reduce_qty)
                pnl = gross - exit_cost
                self._pnl_running += pnl
                pos["qty"] -= reduce_qty
                pos["tp1_progress_reduced"] = True
                pos["tp1_progress_pnl"] = round(pos.get("tp1_progress_pnl", 0.0) + pnl, 4)
                pos["tp1_progress_exit_cost"] = round(pos.get("tp1_progress_exit_cost", 0.0) + exit_cost, 6)
                pos["tp1_progress_exit_price"] = round(price, 6)
                pos["tp1_progress_reduce_qty"] = round(reduce_qty, 8)

            # Hâlâ TP1 yok ve R bazında fazla geri gittiyse erken çıkış
            base_early_exit = (bars_held >= self.tp1_prog_early_exit_bars
                                and change <= pos_sl_pct * self.tp1_prog_early_exit_r)
            early_exit = base_early_exit
            if base_early_exit and self.tp1_prog_mc_enabled:
                mc_early = self._tp1_progress_market_confirm(
                    pos["side"], change, pos["mfe"] / tp1_target,
                    result.get("components", {}) or {},
                    self._tp1_prog_htf_score(htf_p),
                    self._tp1_prog_btc_change_pct(btc_prices),
                    pos.get("ae_class", ""),
                    self._tp1_prog_volume_ratio(vols or []),
                )
                early_exit = base_early_exit and mc_early["allow_early_exit"]
                pos["early_exit_weakness_score"] = mc_early["weakness_score"]
                pos["early_exit_reasons"] = ",".join(mc_early["reasons"])
                pos["early_exit_market_confirmed"] = mc_early["allow_early_exit"]
            if early_exit:
                reason = "EarlyNoTP1"

        # Trail
        if self.use_trailing and change > 0:
            pos_trail = pos.get("trail_step", self.trail_step)
            locked    = pos.get("trail_locked", None)
            if locked is None or change > locked + pos_trail:
                pos["trail_locked"] = change

        # Exit kontrolu
        if reason is None and change <= -pos_sl_pct:
            reason = "SL"
        elif reason is None and bars_held >= int(pos.get("max_hold_bars_override") or self.max_hold_bars):
            reason = "MaxHold"
        elif reason is None and change >= self.tp_pct and change >= self.min_profit_cls:
            reason = "TP"
        elif reason is None and change >= self.min_profit_cls:
            score = result.get("final_score", 50.0)
            # V9 FIX (SHORT parity): engine.py _exit_reason() (satır ~489-492)
            # LONG ve SHORT icin simetrik kontrol eder, ayni 'score' (final_score)
            # kaynagini kullanir. backtest.py'de SHORT karsiligi yoktu, eklendi.
            if pos["side"] == "LONG" and score < self.score_close:
                reason = "ScoreClose"
            elif pos["side"] == "SHORT" and score > self.score_close:
                reason = "ScoreClose"
        elif reason is None and self.use_trailing:
            locked    = pos.get("trail_locked")
            pos_trail = pos.get("trail_step", self.trail_step)
            if locked is not None and change < locked - pos_trail and bars_held >= self.min_hold_bars:
                reason = "Trail"

        # Convex pyramid (sadece kazanan pozisyona ekle — side-aware)
        if self.runtime and not reason:
            add_mult = self.runtime.check_pyramid(sym, price)
            if add_mult:
                extra = pos["qty"] * add_mult
                pos["qty"] += extra
                pos["pyramid_adds"] = pos.get("pyramid_adds", 0) + 1

        if reason:
            self._close_position(sym, price, change, reason, ts_str, date_str, ts_ms)

    def _try_open(self, sym: str, score: float, result: dict,
                  prices: list, highs: list, lows: list, vols: list,
                  htf_p: list, regime: str, ts_str: str, date_str: str,
                  ts_ms: int = 0, btc_prices: list = None):

        price   = prices[-1] if prices else 0.0
        atr_pct = result.get("components", {}).get("atr_pct", 0.0)
        adx_val = result.get("components", {}).get("adx", 0.0)
        rsi_val = result.get("components", {}).get("rsi", 50.0)
        # Bu skor bir "aday sinyal" mi? (yalniz LONG esigine bakar — sert kapilar
        # President'tan ONCE calistigi icin henuz hangi taraf onerildigini bilmeyiz;
        # bu erken bloklar varsayilan olarak LONG kabul edilir — sistem LONG-agirlikli)
        is_candidate = score >= self.score_long_open
        # V9 FIX (mimari düzeltme): "score <= score_short_open" proxy'si SHORT
        # özel filtreler (RSI_TOO_LOW, MTF_NO_CONFIRM_SHORT) için KALDIRILDI.
        # President aktif path'te (aşağıda) bu filtreler artık GERÇEK
        # packet.side == "SHORT" kararından sonra uygulanıyor. Sadece tamamen
        # legacy (president_enabled=False, fonksiyonun en sonu) yolda, side
        # zaten doğrudan ve kesin biçimde score_short_open ile belirleniyor —
        # orada proxy değil, fiili karar mekanizması olduğu için dokunulmadı.

        # ── Sert kapı (portföy/limit) ───────────────────────────────────
        # NOT: _maybe_rotate_for_candidate() HER ZAMAN False döner (shadow-only
        # güvenlik tasarımı, V8.4.1) — yani bu çağrı hiçbir zaman fiziksel pozisyon
        # kapatmaz, sadece ROTATION_CANDIDATE_SHADOW logu üretir. Bu nedenle
        # sıralama (önce/sonra olması) şu an pratik bir fark yaratmıyor; yine de
        # ileride gerçek rotasyon eklenirse güvenli olsun diye fonksiyon
        # PORTFÖY DOLU kontrolünün içinde, en erken noktada çağrılır.
        portfolio_full = len(self.open_positions) >= self.max_open_pos
        if portfolio_full:
            self._maybe_rotate_for_candidate(sym, score, price, ts_str, date_str, ts_ms)
            # V8.5.7: kapasite doluluğu artık açıkça gerçek aktif pozisyon sayısıyla loglanır.
            # Aktif pozisyon < limit iken bu sebep yazılamaz; o durum rank/rejection mantığına bırakılır.
            self._log_filter("MAX_POSITIONS_ALREADY_FULL", sym, score, ts_str, extra={
                "active_positions": len(self.open_positions),
                "max_positions": self.max_open_pos,
            })
            if is_candidate:
                self._record_block(ts_ms, sym, "MAX_POSITIONS_ALREADY_FULL", price, regime, score)
            if self.ghost_en and score >= self.ghost_min_score:
                self._log_ghost(sym, score, result, prices, ts_str, "MAX_POSITIONS_ALREADY_FULL")
            return

        if sym != "BTCUSDT" and self.bl_enabled and sym in self.bl_symbols:
            self._log_filter("SYMBOL_BLACKLIST", sym, score, ts_str)
            if is_candidate:
                self._record_block(ts_ms, sym, "SYMBOL_BLACKLIST", price, regime, score)
            return

        daily_trades = self._daily_trade_count.get(date_str, 0)
        if daily_trades >= self.max_trades_day:
            return

        # V8.5.9 FIX: Günlük hedef/kayıp kapısı (_try_open legacy yolu için de parity).
        equity_now = self.equity + self._pnl_running
        day_pnl    = self._daily_pnl.get(date_str, 0.0)
        if self.daily_target > 0 and day_pnl >= equity_now * self.daily_target:
            self._log_filter("DAILY_TARGET_HIT", sym, score, ts_str,
                             extra={"day_pnl": round(day_pnl, 2)})
            return
        if self.daily_loss_lim > 0 and day_pnl <= -(equity_now * self.daily_loss_lim):
            self._log_filter("DAILY_LOSS_LIMIT", sym, score, ts_str,
                             extra={"day_pnl": round(day_pnl, 2)})
            if is_candidate:
                self._record_block(ts_ms, sym, "DAILY_LOSS_LIMIT", price, regime, score)
            return

        # ADX filtresi (BOA adayi)
        # require_adx_strict=False (varsayılan): ADX=0 (hesaplanamadı) ise bypass
        # require_adx_strict=True: ADX=0 da bloklanır (strict mod)
        # V9 FIX (SHORT parity): engine.py'de ADX side'dan bağımsız uygulanır —
        # is_candidate gate'i kaldırıldı.
        adx_blocks = self.adx_filter_en and (
            (adx_val > 0 and adx_val < self.adx_thr) or
            (adx_val <= 0 and self.require_adx_strict)
        )
        if adx_blocks:
            self._log_filter("ADX_TOO_LOW", sym, score, ts_str, extra={"adx": round(adx_val, 1)})
            self._record_block(ts_ms, sym, "ADX_TOO_LOW", price, regime, score)
            return

        # BTC genel düşüş filtresi (varsayılan KAPALI) — açıksa ve BTC son N
        # mumda drop_pct'ten fazla düştüyse, TÜM adaylar (LONG+SHORT) bloklanır.
        # V9 FIX (SHORT parity): engine.py'de bu filtre de side'dan bağımsızdır —
        # is_candidate gate'i kaldırıldı.
        if self.btc_filter_en and btc_prices and \
           len(btc_prices) >= self.btc_filter_candles + 1:
            _b_start = btc_prices[-(self.btc_filter_candles + 1)]
            _b_end   = btc_prices[-1]
            _b_drop  = (_b_end - _b_start) / _b_start * 100 if _b_start > 0 else 0.0
            if _b_drop <= -self.btc_filter_drop:
                self._log_filter("BTC_FILTER_DROP", sym, score, ts_str,
                                 extra={"btc_drop_pct": round(_b_drop, 2)})
                self._record_block(ts_ms, sym, "BTC_FILTER_DROP", price, regime, score)
                return

        # MTF (BOA adayi)
        # MTF kapalıysa CoreLong HTF gate'i bypass edebilmek için nötr-altı 50 değil, 100 kullanılır.
        htf_sc = 100.0 if not self.mtf_enabled else 50.0
        if self.mtf_enabled and len(htf_p) >= 50:
            try:
                htf_sc = score_symbol(htf_p)["final_score"]
            except Exception:
                htf_sc = 50.0
            if is_candidate and htf_sc < self.mtf_long_min:
                self._log_filter("MTF_NO_CONFIRM", sym, score, ts_str, extra={"htf": round(htf_sc, 1), "side": "LONG"})
                self._record_block(ts_ms, sym, "MTF_NO_CONFIRM", price, regime, score)
                return

        if price <= 0:
            return

        # V8.5.8 Pump/Manipülasyon Filtresi — SERT BLOK DEĞİL (puan + boyut cezası).
        pump_info = compute_pump_risk(prices, vols, self.cfg)
        if pump_info.get("is_pump"):
            score = max(0.0, score - pump_info["score_penalty"])
            self._log_filter("PUMP_RISK_SOFT", sym, score, ts_str, extra={
                "vol_ratio": pump_info["vol_ratio"], "price_chg_pct": pump_info["price_chg_pct"],
                "score_penalty": pump_info["score_penalty"], "size_mult": pump_info["size_mult"],
            })

        # ── KARAR: President (varsayilan) veya Legacy ──────────────────────
        if self.president_enabled and self.runtime:
            ts_sec = (ts_ms / 1000) if ts_ms else time.time()
            sentiment = "BEARISH" if regime == "BEARISH" else ("BULLISH" if regime in ("BULL", "TREND") else "NEUTRAL")
            packet = self.runtime.evaluate(
                sym, ts_sec, score, result, regime, htf_sc, sentiment,
                prices, highs, lows, vols, btc_prices,
            )
            # V9 FIX (mimari düzeltme): SHORT özel filtreler artık GERÇEK
            # President kararına (packet.side == "SHORT") bağlı.
            if packet.action == Action.OPEN and packet.side.value == "SHORT":
                sg = ((self.cfg.get("short_surgeon", {}) or {}).get("weakness_short_gate", {}) or {})
                sg_enabled = bool(sg.get("enabled", False))
                ss_vote = (packet.branch_votes or {}).get("short_surgeon") if hasattr(packet, "branch_votes") else None
                ss_weakness = float(((getattr(ss_vote, "debug", None) or {}).get("weakness", 0.0)) or 0.0)
                is_weakness_short = bool(
                    sg_enabled and ss_vote is not None and
                    getattr(ss_vote, "action", None) == Action.OPEN and
                    getattr(ss_vote, "side", None) == Side.SHORT and
                    ss_weakness >= float(sg.get("min_weakness_score", 75.0))
                )
                eff_htf_max = float(sg.get("htf_short_max", self.mtf_short_max)) if is_weakness_short else self.mtf_short_max
                eff_rsi_min = float(sg.get("rsi_min_short", self.rsi_min_short)) if is_weakness_short else self.rsi_min_short
                gate_label = "SHORT_GATE_WEAKNESS_RELAXED" if is_weakness_short else "SHORT_GATE_DEFAULT"
                if self.rsi_filter_en and rsi_val < eff_rsi_min:
                    self._log_filter("RSI_TOO_LOW", sym, score, ts_str, extra={"rsi": round(rsi_val, 1), "side": "SHORT", "gate": gate_label})
                    self._record_block(ts_ms, sym, "RSI_TOO_LOW", price, regime, score, side="SHORT")
                    return
                if self.mtf_enabled and htf_sc > eff_htf_max:
                    self._log_filter("MTF_NO_CONFIRM_SHORT", sym, score, ts_str, extra={"htf": round(htf_sc, 1), "side": "SHORT", "gate": gate_label})
                    self._record_block(ts_ms, sym, "MTF_NO_CONFIRM_SHORT", price, regime, score, side="SHORT")
                    return
                try:
                    packet.extra["short_gate_type"] = gate_label
                except Exception:
                    pass
                if is_weakness_short and sg.get("size_mult") is not None:
                    try:
                        packet.size_mult = float(sg.get("size_mult"))
                    except Exception:
                        pass
            if packet.action == Action.OPEN:
                branch_scores = {k: round(v.score,2) for k,v in packet.branch_votes.items()}
                self._open_from_decision(sym, packet.side.value, price, score, adx_val, atr_pct,
                                         regime, ts_str, date_str, packet.sl_pct, packet.size_mult,
                                         label=packet.label, decision_id=packet.decision_id,
                                         branch_scores=branch_scores, htf_score=htf_sc,
                                         prices=prices, highs=highs, lows=lows, vols=vols,
                                         packet_extra=getattr(packet, "extra", {}),
                                         score_components=(result.get("components", {}) or {}),
                                         pump_context=pump_info,
                                         setup_type=getattr(packet, "setup_type", ""),
                                         selected_engine=getattr(packet, "selected_engine", ""),
                                         sl_profile=getattr(packet, "sl_profile", ""),
                                         tp_profile=getattr(packet, "tp_profile", ""),
                                         trail_profile=getattr(packet, "trail_profile", ""))
            else:
                # Acik oy vardiysa ama President acmadiysa -> BOA blok adayi
                open_votes_list = [v for v in packet.branch_votes.values() if v.action == Action.OPEN]
                had_open = len(open_votes_list) > 0
                if had_open or is_candidate:
                    cause = "PRESIDENT_" + (packet.reason.split()[0] if packet.reason else "BLOCK")
                    # Bloklanan sinyalin yönü: President'ın seçtiği taraf, yoksa ilk açık oy, yoksa LONG
                    intended_side = (packet.side.value if packet.side.value != "NONE"
                                     else (open_votes_list[0].side.value if open_votes_list else "LONG"))
                    self._record_block(ts_ms, sym, cause, price, regime, score, side=intended_side)
                if self.ghost_en and score >= self.ghost_min_score:
                    self._log_ghost(sym, score, result, prices, ts_str, "PRESIDENT_BLOCK")
            return

        # ── Legacy (president_enabled=False) — A/B karsilastirma icin ──────
        adsl = adaptive_sl_compute(regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.score_long_open, base_atr_multiplier=self.atr_multiplier,
            base_trail_step=self.trail_step, cfg=self.cfg)
        eff_thr, sl_pct = adsl["score_threshold"], adsl["sl_pct"]
        side = "LONG" if score >= eff_thr else ("SHORT" if score <= self.score_short_open else None)
        if side is None:
            if self.ghost_en and score >= self.ghost_min_score:
                self._log_ghost(sym, score, result, prices, ts_str, "BELOW_THRESHOLD")
            return
        # V9 FIX: legacy_short_proxy — Bu, President'ın DEVRE DIŞI olduğu
        # (president_enabled=False) tek modda çalışır. Burada 'side' bir
        # tahmin değil, sistemin fiili/tek karar mekanizmasıdır (President
        # path'inde olduğu gibi gerçek bir packet.side YOKTUR). Bu nedenle
        # SHORT özel filtreler burada güvenle 'side' üzerinden uygulanabilir.
        if side == "SHORT":
            if self.rsi_filter_en and rsi_val < self.rsi_min_short:
                self._log_filter("RSI_TOO_LOW", sym, score, ts_str, extra={"rsi": round(rsi_val, 1), "side": "SHORT"})
                self._record_block(ts_ms, sym, "RSI_TOO_LOW", price, regime, score, side="SHORT")
                return
            if self.mtf_enabled and htf_sc > self.mtf_short_max:
                self._log_filter("MTF_NO_CONFIRM_SHORT", sym, score, ts_str, extra={"htf": round(htf_sc, 1), "side": "SHORT"})
                self._record_block(ts_ms, sym, "MTF_NO_CONFIRM_SHORT", price, regime, score, side="SHORT")
                return
        self._open_from_decision(sym, side, price, score, adx_val, atr_pct, regime,
                                 ts_str, date_str, sl_pct, 1.0, label="LEGACY", htf_score=htf_sc,
                                 prices=prices, highs=highs, lows=lows, vols=vols,
                                 score_components=(result.get("components", {}) or {}))

    def _open_from_decision(self, sym, side, price, score, adx_val, atr_pct, regime,
                            ts_str, date_str, sl_pct, size_mult, label="",
                            decision_id="", branch_scores=None, htf_score=50.0,
                            prices=None, highs=None, lows=None, vols=None, packet_extra=None,
                            score_components=None, rank_context=None, pump_context=None,
                            setup_type="", selected_engine="",
                            sl_profile="", tp_profile="", trail_profile=""):
        """Karar paketinden pozisyon ac (President veya Legacy ortak yolu)."""
        adsl = adaptive_sl_compute(regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.score_long_open, base_atr_multiplier=self.atr_multiplier,
            base_trail_step=self.trail_step, cfg=self.cfg)
        trail = adsl["trail_step"]
        symbol_mult = self.sym_mgr.size_multiplier(sym) if hasattr(self, "sym_mgr") else 1.0
        # V8.5.2 Adaptive Exit: policy üretir; President kararını baypas etmez.
        symbol_stats = None
        try:
            symbol_stats = self.sym_mgr.get_all_stats().get(sym, {}) if hasattr(self, "sym_mgr") else None
        except Exception:
            symbol_stats = None
        ae = classify_trade(symbol=sym, side=side, score=score, htf_score=htf_score,
                            regime=regime, components={"adx": adx_val, "atr_pct": atr_pct},
                            cfg=self.cfg, prices=prices or [], highs=highs or [], lows=lows or [],
                            volumes=vols or [], symbol_stats=symbol_stats,
                            setup_type=setup_type, selected_engine=selected_engine)
        # V9.0.1 FIX (App/GUI denetimi): packet_extra üzerinden gelen
        # risk_mult/entry_confidence/entry_reasons/rejected_reports/conflict_info
        # ve packet'in sl/tp/trail_profile alanları artık pozisyona kaydediliyor —
        # önceden bunlar HİÇBİR ZAMAN trade kaydına ulaşmıyordu (sessiz veri kaybı).
        pe = packet_extra or {}
        if ae.enabled and not ae.shadow_mode:
            size_mult *= float(ae.policy.size_mult or 1.0)
            trail = max(0.001, float(ae.policy.trail_step_pct) / 100.0)
        # V8.5.8 Pump/Manipülasyon Filtresi — pozisyon boyutu küçültme (sert blok değil).
        pump_context = pump_context or {}
        if pump_context.get("is_pump"):
            size_mult *= float(pump_context.get("size_mult", 1.0))
        mr = self.cfg.get("market_regime", {})
        regime_mult = 1.0
        if str(regime).upper() == "NEUTRAL":
            regime_mult = float(mr.get("neutral_size_mult", 1.0))
        elif str(regime).upper() == "KONSOL":
            regime_mult = float(mr.get("konsol_size_mult", 1.0))
        final_size_mult = max(0.05, size_mult * symbol_mult * regime_mult)
        current_equity = self.equity + self._pnl_running
        risk_usdt = current_equity * self.risk_per_trade * max(0.1, final_size_mult)
        qty       = max(0.0001, risk_usdt / (price * max(sl_pct, 0.001)))
        entry_cost = self._fee_cost(price, qty)
        self._pnl_running -= entry_cost

        self.open_positions[sym] = {
            "side": side, "entry": price, "qty": qty, "sl_pct": sl_pct,
            "trail_step": trail, "open_ts": ts_str, "open_date": date_str,
            "score": score, "adx": adx_val, "atr_pct": atr_pct, "regime": regime,
            "label": label, "bars_held": 0, "tp1_done": False, "tp1_pnl": 0.0,
            "tp1_progress_reduced": False, "tp1_progress_pnl": 0.0, "mfe": 0.0,
            "trail_locked": None, "pyramid_adds": 0, "original_trail_step": trail,
            "entry_cost": round(entry_cost, 6),
            "decision_id": decision_id,              # President kararı zinciri
            "branch_scores": branch_scores or {},    # Dal skorları
            "symbol_size_mult": round(symbol_mult, 4),
            "regime_size_mult": round(regime_mult, 4),
            "final_size_mult": round(final_size_mult, 4),
            "raw_score": (score_components or {}).get("raw_score", score),
            "normalized_score": (score_components or {}).get("normalized_score", score),
            "long_score": (score_components or {}).get("long_score", score),
            "short_feature_score": (score_components or {}).get("short_score", ""),
            "score_model": (score_components or {}).get("score_model", ""),
            "president_score": (packet_extra or {}).get("president_score", ""),
            "rank_score": (rank_context or {}).get("rank_score", ""),
            "rank_position": (rank_context or {}).get("rank_position", ""),
            "rank_candidate_count": (rank_context or {}).get("candidate_count", ""),
            "boa_feedback_adj": ((rank_context or {}).get("boa_feedback", {}) or {}).get("adjustment", ""),
            # V10 Phase-1: Regime Router + Relative Strength context
            "regime_router_report": (packet_extra or {}).get("regime_router_report", (rank_context or {}).get("regime_router", {})),
            "relative_strength_report": (packet_extra or {}).get("relative_strength_report", (rank_context or {}).get("relative_strength", {})),
            "module_name": (packet_extra or {}).get("module_name", "core_long" if side == "LONG" else "short_surgeon"),
            "module_decision_reason": (packet_extra or {}).get("module_decision_reason", "v10_phase1"),
            "president_final_score": (packet_extra or {}).get("president_final_score", ""),
            "department_score_adjustment": (packet_extra or {}).get("department_score_adjustment", ""),
            "raw_department_score_adjustment": (packet_extra or {}).get("raw_department_score_adjustment", ""),
            "regime_score_adjustment": (packet_extra or {}).get("regime_score_adjustment", ""),
            "rs_score_adjustment": (packet_extra or {}).get("rs_score_adjustment", ""),
            "president_department_reason": (packet_extra or {}).get("president_department_reason", ""),
            "regime_veto_recommendation": (packet_extra or {}).get("regime_veto_recommendation", ""),
            "rs_veto_recommendation": (packet_extra or {}).get("rs_veto_recommendation", ""),
            "pump_risk": int(bool(pump_context.get("is_pump"))),
            "pump_vol_ratio": pump_context.get("vol_ratio", ""),
            "pump_price_chg_pct": pump_context.get("price_chg_pct", ""),
            "pump_score_penalty": pump_context.get("score_penalty", ""),
            "ae_class": ae.trade_class,
            "ae_policy": ae.policy_name,
            "ae_continuation_score": ae.continuation_score,
            "ae_confidence": ae.confidence,
            "ae_reasons": ae.reasons[:240],
            "selected_engine": selected_engine,
            "setup_type": setup_type,
            "sl_profile": sl_profile,
            "tp_profile": tp_profile,
            "trail_profile": trail_profile,
            "risk_mult": pe.get("risk_mult", ""),
            "entry_confidence": pe.get("entry_confidence", ""),
            "entry_reasons": pe.get("entry_reasons", ""),
            "rejected_reports": pe.get("rejected_reports", []),
            "conflict_info": pe.get("conflict_info", ""),
            "tp1_close_pct": float(ae.policy.tp1_close_pct),
            "max_hold_bars_override": (int(float(ae.policy.max_hold_hours)) if ae.policy.max_hold_hours else None),
            "quality_score_report": (packet_extra or {}).get("quality_score_report", {}),
            "adaptive_risk_report": (packet_extra or {}).get("adaptive_risk_report", {}),
        }
        if self.runtime:
            self.runtime.on_open(sym, side, price, sl_pct)
            self.runtime.confirm_open(side)  # filtreler zaten geçildi, risk sayacı artır
        self._daily_trade_count[date_str] = self._daily_trade_count.get(date_str, 0) + 1

    def _close_position(self, sym: str, price: float, change: float,
                        reason: str, ts_str: str, date_str: str, ts_ms: int = 0):
        pos   = self.open_positions.pop(sym, None)
        if not pos:
            return
        entry = pos["entry"]
        qty   = pos["qty"]
        pnl_raw = self._gross_pnl(pos["side"], entry, price, qty)
        exit_cost = round(self._fee_cost(price, qty), 6)
        pnl = pnl_raw - exit_cost
        self._pnl_running += pnl
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + pnl

        # Net_PnL = partial net + final net - entry cost
        tp1_pnl      = pos.get("tp1_pnl", 0.0)
        tp1_prog_pnl = pos.get("tp1_progress_pnl", 0.0)
        entry_cost   = pos.get("entry_cost", 0.0)
        total_net = round(pnl + tp1_pnl + tp1_prog_pnl - entry_cost, 4)

        trade = {
            "Sembol":        sym,
            "Yon":           pos["side"],
            "Giris":         pos.get("open_ts", ts_str),
            "Cikis":         ts_str,
            "GirisFiyati":   round(entry, 6),
            "CikisFiyati":   round(price, 6),
            "KarPct":        round(change * 100, 3),
            "Final_PnL":     round(pnl, 4),       # çıkış PnL (exit_cost dahil)
            "TP1_PnL":       round(tp1_pnl, 4),   # partial TP PnL
            "TP1_Progress_PnL": round(tp1_prog_pnl, 4) if self.write_tp1_progress_fields else "",  # V9.0.5: backtest_output_integrity.write_tp1_progress_fields
            "Net_PnL":       total_net,            # GERÇEK TOPLAM — summary bunu kullanır
            "Giris_Komisyon": round(entry_cost, 4),
            "Cikis_Komisyon": round(exit_cost, 4),
            "Sebep":         reason,
            "Skor":          round(pos.get("score", 0), 2),
            "ADX":           round(pos.get("adx", 0), 2),
            "ATR_Pct":       round(pos.get("atr_pct", 0), 4),
            "Rejim":         pos.get("regime", ""),
            "Label":         pos.get("label", ""),
            "AE_Class":      pos.get("ae_class", ""),
            "AE_Policy":     pos.get("ae_policy", ""),
            "AE_ContinuationScore": pos.get("ae_continuation_score", ""),
            "AE_Confidence": pos.get("ae_confidence", ""),
            "AE_Reasons":    pos.get("ae_reasons", ""),
            "SymbolSizeMult": pos.get("symbol_size_mult", ""),
            "RegimeSizeMult": pos.get("regime_size_mult", ""),
            "FinalSizeMult": pos.get("final_size_mult", ""),
            "BTCMacroRegime": (pos.get("regime_router_report", {}) or {}).get("btc_macro_regime", ""),
            "SymbolMicroRegime": (pos.get("regime_router_report", {}) or {}).get("symbol_micro_regime", ""),
            "VolatilityState": (pos.get("regime_router_report", {}) or {}).get("volatility_state", ""),
            "RegimeConfidence": (pos.get("regime_router_report", {}) or {}).get("regime_confidence", ""),
            "RegimeReason": (pos.get("regime_router_report", {}) or {}).get("regime_reason", ""),
            "AllowedModules": ",".join((pos.get("regime_router_report", {}) or {}).get("allowed_modules", []) or []),
            "RegimeLongSizeMult": (pos.get("regime_router_report", {}) or {}).get("long_size_mult", ""),
            "RegimeMinScoreOffset": (pos.get("regime_router_report", {}) or {}).get("min_score_offset", ""),
            "RegimeMode": (pos.get("regime_router_report", {}) or {}).get("mode", ""),
            "RSGroup": (pos.get("relative_strength_report", {}) or {}).get("rs_group", ""),
            "RSScore": (pos.get("relative_strength_report", {}) or {}).get("rs_score", ""),
            "RSRankPct": (pos.get("relative_strength_report", {}) or {}).get("rs_rank_pct", ""),
            "RSState": (pos.get("relative_strength_report", {}) or {}).get("rs_state", ""),
            "RSReason": (pos.get("relative_strength_report", {}) or {}).get("rs_reason", ""),
            "RSMode": (pos.get("relative_strength_report", {}) or {}).get("mode", ""),
            "ModuleName": pos.get("module_name", ""),
            "ModuleDecisionReason": pos.get("module_decision_reason", ""),
            "PresidentFinalScore": pos.get("president_final_score", ""),
            "DepartmentScoreAdjustment": pos.get("department_score_adjustment", ""),
            "RawDepartmentScoreAdjustment": pos.get("raw_department_score_adjustment", ""),
            "RegimeScoreAdjustment": pos.get("regime_score_adjustment", ""),
            "RSScoreAdjustment": pos.get("rs_score_adjustment", ""),
            "PresidentDepartmentReason": pos.get("president_department_reason", ""),
            "RegimeVetoRecommendation": pos.get("regime_veto_recommendation", ""),
            "RSVetoRecommendation": pos.get("rs_veto_recommendation", ""),
            "RawScore":      pos.get("raw_score", ""),
            "NormalizedScore": pos.get("normalized_score", ""),
            "LongScore":     pos.get("long_score", ""),
            "ShortFeatureScore": pos.get("short_feature_score", ""),
            "ScoreModel":    pos.get("score_model", ""),
            "EntryScore":    round(pos.get("score", 0), 2),
            "PresidentScore": pos.get("president_score", ""),
            "RankScore":     pos.get("rank_score", ""),
            "RankPosition":  pos.get("rank_position", ""),
            "RankCandidateCount": pos.get("rank_candidate_count", ""),
            "BOAFeedbackAdj": pos.get("boa_feedback_adj", ""),
            "PumpRisk":      pos.get("pump_risk", 0),
            "PumpVolRatio":  pos.get("pump_vol_ratio", ""),
            "PumpPriceChgPct": pos.get("pump_price_chg_pct", ""),
            "PumpScorePenalty": pos.get("pump_score_penalty", ""),
            "BarsHeld":      pos.get("bars_held", 0),
            "TP1_Done":      int(pos.get("tp1_done", False)),
            "TP1_Progress_Reduced": int(pos.get("tp1_progress_reduced", False)),
            "TP1_Progress_ExitPrice": pos.get("tp1_progress_exit_price", ""),
            "TP1_Progress_ReduceQty": pos.get("tp1_progress_reduce_qty", ""),
            "PyramidAdds":   pos.get("pyramid_adds", 0),
            "DecisionID":    pos.get("decision_id", ""),
            "CoreScore":     pos.get("branch_scores", {}).get("core_long", ""),
            "ShortScore":    pos.get("branch_scores", {}).get("short_surgeon", ""),
            "CascadeScore":  pos.get("branch_scores", {}).get("cascade_hunter", ""),
            "QualityScore":  (pos.get("quality_score_report", {}) or {}).get("score", ""),
            "AdaptiveRiskMult": (pos.get("adaptive_risk_report", {}) or {}).get("risk_mult", ""),
            # ── V9.0.1 FIX (App/GUI denetimi sonrası): bu alanlar açılışta
            # pozisyona kaydediliyordu ama kapanışta trade kaydına HİÇ
            # taşınmıyordu — sessiz veri kaybıydı. Artık CSV'de görünür.
            "SelectedEngine": pos.get("selected_engine", ""),
            "SetupType":     pos.get("setup_type", ""),
            "SLProfile":     pos.get("sl_profile", ""),
            "TPProfile":     pos.get("tp_profile", ""),
            "TrailProfile":  pos.get("trail_profile", ""),
            "RiskMult":      pos.get("risk_mult", ""),
            "EntryConfidence": pos.get("entry_confidence", ""),
            "EntryReasons":  pos.get("entry_reasons", ""),
            "RejectedReports": json.dumps(pos.get("rejected_reports", []), ensure_ascii=False)[:600],
            "ConflictInfo":  pos.get("conflict_info", ""),
            # V9 FIX: TP1 Progress Manager piyasa-teyit raporlaması (sona eklendi,
            # mevcut kolonlar kırılmadı).
            "TP1ProgressWeaknessScore": pos.get("tp1_progress_weakness_score", ""),
            "TP1ProgressReasons":       pos.get("tp1_progress_reasons", ""),
            "TP1ProgressMarketConfirmed": pos.get("tp1_progress_market_confirmed", ""),
            "TP1ProgressToTP1":         pos.get("tp1_progress_progress_to_tp1", ""),
            "EarlyExitWeaknessScore":   pos.get("early_exit_weakness_score", ""),
            "EarlyExitReasons":         pos.get("early_exit_reasons", ""),
        }

        # ── V9 FIX: Post-Exit Analytics (SADECE gerçekleşen trade'ler) ──
        # Bu blok hiçbir karar mantığına dahil değildir; trade dict ZATEN
        # tamamlanmış/append edilecek haldeyken, sona EK bilgi olarak yazılır.
        # Bloklanan sinyallere/ghost event'lere UYGULANMAZ.
        for _h in (4, 8, 12, 24):
            _snap = self._future_indicator_snapshot(sym, ts_ms, _h)
            trade[f"PostExit_{_h}h_Pct"]         = _snap["pct"]
            trade[f"PostExit_{_h}h_RSI"]         = _snap["rsi"]
            trade[f"PostExit_{_h}h_ADX"]         = _snap["adx"]
            trade[f"PostExit_{_h}h_VolumeRatio"] = _snap["volume_ratio"]

        self.trades.append(trade)

        # ── SL_DOGRU: pending kaydı — 4h SONRA verdict üretilir (look-ahead önleme) ──
        if self.runtime and reason == "SL" and pos["side"] == "LONG":
            ts_sec = (ts_ms / 1000) if ts_ms else time.time()
            # Gerçek hayatta 4h sonra bilinir; backtest'te de aynı şekilde geciktirilir
            if not hasattr(self, "_pending_sl_bt"):
                self._pending_sl_bt = {}
            self._pending_sl_bt[sym] = {"ts_ms": ts_ms, "ts_sec": ts_sec}

        # President risk governor'a GERÇEK toplam PnL gönder (TP1 + kapanış − giriş maliyeti)
        if self.runtime:
            self.runtime.on_close(sym, pos["side"], total_net, candle_ts=(ts_ms/1000 if ts_ms else 0.0))
            self.runtime.update_equity(self.equity + self._pnl_running)
        if hasattr(self, "sym_mgr"):
            self.sym_mgr.record_trade(sym, total_net)
            self.sym_mgr.update_equity(self.equity + self._pnl_running)

        # block_outcomes (exit reason ozeti — eski "BOA" artik exit ozeti olarak kalir)
        self.block_outcomes.append({
            "ts":     ts_str,
            "symbol": sym,
            "reason": reason,
            "pnl":    round(total_net, 4),
            "side":   pos["side"],
            "score":  round(pos.get("score", 0), 2),
        })

    def _force_close_all(self, last_candle_ts: int = 0):
        """Dönem sonu — her açık pozisyonu son bilinen fiyat VE gerçek mum zamanıyla kapat."""
        exit_ts_str = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(last_candle_ts // 1000))
                      if last_candle_ts else "EOT")
        for sym in list(self.open_positions.keys()):
            pos     = self.open_positions.pop(sym)
            entry   = pos["entry"]
            # Son bilinen fiyat: equity_curve son noktası değil, sembolün son kapanışı
            last_p  = pos.get("last_price", entry)   # _manage_position her barı günceller
            mult    = 1 if pos["side"] == "LONG" else -1
            change  = (last_p - entry) / entry * mult
            pnl_raw = self._gross_pnl(pos["side"], entry, last_p, pos["qty"])
            tp1_pnl   = pos.get("tp1_pnl", 0.0)
            tp1_prog_pnl = pos.get("tp1_progress_pnl", 0.0)
            entry_cost_fc = pos.get("entry_cost", 0.0)
            exit_cost_fc  = round(self._fee_cost(last_p, pos["qty"]), 6)
            pnl     = pnl_raw - exit_cost_fc
            total   = round(pnl + tp1_pnl + tp1_prog_pnl - entry_cost_fc, 4)
            self._pnl_running += pnl
            # V9 FIX: Post-Exit Analytics — EndOfTest da "gerceklesen trade"
            # kapsaminda (kullanicinin talebine gore), ayni 16 kolon eklenir.
            _pe = {h: self._future_indicator_snapshot(sym, last_candle_ts, h) for h in (4, 8, 12, 24)}
            self.trades.append({
                "Sembol": sym, "Yon": pos["side"],
                "Giris": pos.get("open_ts",""), "Cikis": exit_ts_str,
                "GirisFiyati": round(entry, 6), "CikisFiyati": round(last_p, 6),
                "KarPct": round(change * 100, 3),
                "Final_PnL": round(pnl, 4), "TP1_PnL": round(tp1_pnl, 4),
                "TP1_Progress_PnL": round(tp1_prog_pnl, 4) if self.write_tp1_progress_fields else "",  # V9.0.5: backtest_output_integrity.write_tp1_progress_fields
                "Net_PnL": total,
                "Giris_Komisyon": round(entry_cost_fc, 4),
                "Cikis_Komisyon": round(exit_cost_fc, 4),
                "Sebep": "EndOfTest",
                "Skor": pos.get("score",0), "ADX": 0, "ATR_Pct": 0,
                "Rejim": pos.get("regime",""), "Label": pos.get("label",""),
                "SymbolSizeMult": pos.get("symbol_size_mult", ""),
                "RegimeSizeMult": pos.get("regime_size_mult", ""),
                "FinalSizeMult": pos.get("final_size_mult", ""),
                "RawScore": pos.get("raw_score", ""),
                "NormalizedScore": pos.get("normalized_score", ""),
                "LongScore": pos.get("long_score", ""),
                "ShortFeatureScore": pos.get("short_feature_score", ""),
                "ScoreModel": pos.get("score_model", ""),
                "EntryScore": round(pos.get("score", 0), 2),
                "PresidentScore": pos.get("president_score", ""),
                "RankScore": pos.get("rank_score", ""),
                "RankPosition": pos.get("rank_position", ""),
                "RankCandidateCount": pos.get("rank_candidate_count", ""),
                "BOAFeedbackAdj": pos.get("boa_feedback_adj", ""),
                "PumpRisk": pos.get("pump_risk", 0),
                "PumpVolRatio": pos.get("pump_vol_ratio", ""),
                "PumpPriceChgPct": pos.get("pump_price_chg_pct", ""),
                "PumpScorePenalty": pos.get("pump_score_penalty", ""),
                "BarsHeld": pos.get("bars_held",0),
                "TP1_Done": int(pos.get("tp1_done",False)),
                "TP1_Progress_Reduced": int(pos.get("tp1_progress_reduced",False)),
                "TP1_Progress_ExitPrice": pos.get("tp1_progress_exit_price", ""),
                "TP1_Progress_ReduceQty": pos.get("tp1_progress_reduce_qty", ""),
                "PyramidAdds": pos.get("pyramid_adds",0),
                "DecisionID": pos.get("decision_id",""),
                "CoreScore": pos.get("branch_scores",{}).get("core_long",""),
                "ShortScore": pos.get("branch_scores",{}).get("short_surgeon",""),
                "CascadeScore": pos.get("branch_scores",{}).get("cascade_hunter",""),
                "QualityScore": (pos.get("quality_score_report", {}) or {}).get("score", ""),
                "PostExit_4h_Pct":          _pe[4]["pct"],
                "PostExit_8h_Pct":          _pe[8]["pct"],
                "PostExit_12h_Pct":         _pe[12]["pct"],
                "PostExit_24h_Pct":         _pe[24]["pct"],
                "PostExit_4h_RSI":          _pe[4]["rsi"],
                "PostExit_8h_RSI":          _pe[8]["rsi"],
                "PostExit_12h_RSI":         _pe[12]["rsi"],
                "PostExit_24h_RSI":         _pe[24]["rsi"],
                "PostExit_4h_ADX":          _pe[4]["adx"],
                "PostExit_8h_ADX":          _pe[8]["adx"],
                "PostExit_12h_ADX":         _pe[12]["adx"],
                "PostExit_24h_ADX":         _pe[24]["adx"],
                "PostExit_4h_VolumeRatio":  _pe[4]["volume_ratio"],
                "PostExit_8h_VolumeRatio":  _pe[8]["volume_ratio"],
                "PostExit_12h_VolumeRatio": _pe[12]["volume_ratio"],
                "PostExit_24h_VolumeRatio": _pe[24]["volume_ratio"],
            })
            if self.runtime:
                self.runtime.on_close(sym, pos["side"], total, candle_ts=(last_candle_ts/1000 if last_candle_ts else 0.0))
                self.runtime.update_equity(self.equity + self._pnl_running)
            if hasattr(self, "sym_mgr"):
                self.sym_mgr.record_trade(sym, total)
                self.sym_mgr.update_equity(self.equity + self._pnl_running)

    # ── SL_DOGRU pending çözücü (look-ahead önleme) ────────────────────
    def _resolve_pending_sl_bt(self, sym: str, ts_ms: int):
        """
        SL_DOGRU: SL anında geleceği görmek yerine 4h bekler.
        Her barda çağrılır; 4h geçtiyse o anki fiyatla verdict üretir.
        """
        if not self.runtime: return
        pend = getattr(self, "_pending_sl_bt", {})
        rec  = pend.get(sym)
        if not rec: return
        if ts_ms - rec["ts_ms"] < 4 * 3600 * 1000:
            return  # Henüz 4h geçmedi
        chg_4h = self._future_change(sym, rec["ts_ms"], 4)
        if chg_4h is None:
            verdict, chg_4h = "BELIRSIZ", 0.0
        elif chg_4h <= -0.01:
            verdict = "SL_DOGRU"
        elif chg_4h >= 0.01:
            verdict = "ERKEN_SL"
        else:
            verdict = "BELIRSIZ"
        self.runtime.on_sl(sym, verdict, rec["ts_sec"], chg_4h)
        del pend[sym]

    # ── GERCEK BOA: blok kaydi + ileriye donuk sonuc ──────────────────
    def _record_block(self, ts_ms, sym, cause, price, regime, score, side="LONG"):
        """Bloklanan bir aday sinyali kaydeder (BOA post-analizi icin)."""
        self.block_events.append({
            "ts_ms": ts_ms, "symbol": sym, "cause": cause,
            "price": price, "regime": regime, "score": round(score, 2),
            "side": side,
        })

    def _future_change(self, sym, ts_ms, hours) -> Optional[float]:
        """ts_ms anindan 'hours' saat sonraki fiyat degisimi (ondalik)."""
        if not ts_ms:
            return None
        candles = self._cbs.get(sym, [])
        if not candles:
            return None
        target = ts_ms + int(hours * 3600 * 1000)
        base = None
        fut  = None
        for c in candles:
            if c["open_time"] >= ts_ms and base is None:
                base = float(c["close"])
            if c["open_time"] >= target:
                fut = float(c["close"]); break
        if base is None or base <= 0:
            return None
        if fut is None:  # yeterli ileri veri yok
            return None
        return (fut - base) / base

    # ── V9 FIX: Post-Exit Analytics — SADECE gerçekleşen (executed) trade'ler
    # için offline analiz kolonu üretir. Bloklanan sinyallere/ghost event'lere
    # UYGULANMAZ (onlar zaten _future_change/_record_block ile ayrı bir
    # mekanizmaya sahip). Bu fonksiyon hiçbir entry/exit/President/ranking/
    # PnL/equity kararına dahil edilmez — sadece trade kapandıktan SONRA,
    # CSV'ye yazılacak ek bilgi üretir. ──────────────────────────────────
    def _future_indicator_snapshot(self, sym: str, ts_ms: int, hours: float) -> dict:
        """
        Çıkış (exit) anından 'hours' saat sonraki bar'a kadar:
          - pct: fiyat % değişimi (mevcut _future_change ile aynı formül)
          - rsi: o ana kadarki mumlarla RSI(14)
          - adx: o ana kadarki mumlarla ADX(14)
          - volume_ratio: o barın hacmi / önceki 20 mumun ortalama hacmi
        Yeterli ileri veri yoksa (test penceresi sonu) tüm alanlar None döner —
        hata fırlatmaz.
        """
        out = {"pct": None, "rsi": None, "adx": None, "volume_ratio": None}
        if not ts_ms:
            return out
        candles = self._cbs.get(sym, [])
        if not candles:
            return out
        target = ts_ms + int(hours * 3600 * 1000)
        base_close = None
        idx_target = None
        for i, c in enumerate(candles):
            if c["open_time"] >= ts_ms and base_close is None:
                base_close = float(c["close"])
            if c["open_time"] >= target:
                idx_target = i
                break
        if base_close is None or base_close <= 0 or idx_target is None:
            return out  # yeterli ileri veri yok (test penceresi sonu) — guvenli None

        fut_close = float(candles[idx_target]["close"])
        out["pct"] = round((fut_close - base_close) / base_close * 100, 4)

        # RSI/ADX icin idx_target'a kadarki (dahil) son ~100 mum
        window = candles[max(0, idx_target - 99):idx_target + 1]
        closes = [float(c["close"]) for c in window]
        highs  = [float(c["high"])  for c in window]
        lows   = [float(c["low"])   for c in window]

        try:
            if len(closes) >= 16:
                rsi_series = _post_rsi(np.array(closes, dtype=float))
                rsi_val = float(rsi_series.iloc[-1])
                if not (rsi_val != rsi_val):  # NaN check (NaN != NaN -> True)
                    out["rsi"] = round(rsi_val, 2)
        except Exception:
            pass

        try:
            if len(closes) >= 30:
                adx_val = float(_post_adx(np.array(highs, dtype=float),
                                          np.array(lows, dtype=float),
                                          np.array(closes, dtype=float)))
                out["adx"] = round(adx_val, 2)
        except Exception:
            pass

        try:
            if idx_target >= 20:
                base_vols = [float(candles[j]["volume"]) for j in range(idx_target - 20, idx_target)]
                base_avg = sum(base_vols) / 20
                cur_vol  = float(candles[idx_target]["volume"])
                if base_avg > 0:
                    out["volume_ratio"] = round(cur_vol / base_avg, 4)
        except Exception:
            pass

        return out

    def _future_outcome(self, sym, ts_ms, entry, hours, side="LONG") -> Tuple[str, float]:
        """
        Side-aware BOA: LONG için high>=TP, LOW için low<=SL (kazanan).
        SHORT için ters: low<=TP, high>=SL.
        """
        candles = self._cbs.get(sym, [])
        if not candles or entry <= 0:
            return ("BELIRSIZ", 0.0)
        end_ms = ts_ms + int(hours * 3600 * 1000)
        if side == "SHORT":
            tp = entry * (1 - self.tp_pct)   # short TP: fiyat düşer
            sl = entry * (1 + self.sl_pct)   # short SL: fiyat artar
        else:
            tp = entry * (1 + self.tp_pct)
            sl = entry * (1 - self.sl_pct)
        last_close = entry
        for c in candles:
            if c["open_time"] < ts_ms: continue
            if c["open_time"] > end_ms: break
            last_close = float(c["close"])
            if side == "SHORT":
                if float(c["low"])  <= tp: return ("GEREKSIZ_ENGEL",  self.tp_pct * 100)
                if float(c["high"]) >= sl: return ("DOGRU_ENGEL",    -self.sl_pct * 100)
            else:
                if float(c["high"]) >= tp: return ("GEREKSIZ_ENGEL",  self.tp_pct * 100)
                if float(c["low"])  <= sl: return ("DOGRU_ENGEL",    -self.sl_pct * 100)
        chg = (last_close - entry) / entry * (1 if side == "LONG" else -1) * 100
        return ("BELIRSIZ", round(chg, 3))

    def _post_boa_analysis(self):
        """Her bloklanan sinyal icin 4h/12h/24h hipotetik sonuc + ozetler."""
        self.boa_4h:  List[dict] = []
        self.boa_12h: List[dict] = []
        self.boa_24h: List[dict] = []
        for ev in self.block_events:
            for hours, bucket in ((4, self.boa_4h), (12, self.boa_12h), (24, self.boa_24h)):
                verdict, chg = self._future_outcome(ev["symbol"], ev["ts_ms"], ev["price"], hours, ev.get("side","LONG"))
                bucket.append({
                    "ts_ms": ev["ts_ms"], "symbol": ev["symbol"], "side": ev.get("side","LONG"),
                    "cause": ev["cause"], "regime": ev["regime"], "score": ev["score"],
                    "verdict": verdict, "hyp_pnl_pct": chg,
                })

    # ── Filter / Ghost Loglama ────────────────────────────────────────
    def _log_filter(self, cause: str, sym: str, score: float,
                    ts: str, extra: dict = None):
        ev = {
            "ts": ts, "symbol": sym, "cause": cause, "score": round(score, 2),
            "active_positions": len(self.open_positions),
            "max_positions": self.max_open_pos,
        }
        if extra:
            ev.update(extra)
        self.filter_events.append(ev)

    def _log_ghost(self, sym: str, score: float, result: dict,
                   prices: list, ts: str, cause: str):
        self.ghost_signals.append({
            "ts":    ts,
            "symbol": sym,
            "score": round(score, 2),
            "cause": cause,
            "atr":   round(result.get("components", {}).get("atr_pct", 0), 4),
        })

    # ── Rapor Olustur ─────────────────────────────────────────────────
    def _max_drawdown(self) -> float:
        """Equity egrisinden maksimum dususu (%) hesaplar."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 2)

    def _sharpe(self) -> float:
        """Equity getirilerinden basit Sharpe orani."""
        import math
        if len(self.equity_curve) < 2:
            return 0.0
        vals = [eq for _, eq in self.equity_curve]
        rets = [(vals[i] - vals[i-1]) / vals[i-1]
                for i in range(1, len(vals)) if vals[i-1] > 0]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var  = sum((r - mean) ** 2 for r in rets) / len(rets)
        std  = math.sqrt(var)
        return round(mean / std * math.sqrt(len(rets)), 3) if std > 0 else 0.0

    def _generate_report(self) -> dict:
        trades = self.trades
        n       = len(trades)
        wins    = [t for t in trades if t.get("Net_PnL", t.get("KarUSD", 0)) > 0]
        losses  = [t for t in trades if t.get("Net_PnL", t.get("KarUSD", 0)) <= 0]
        net_pnl = sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in trades)
        win_rate= len(wins) / n * 100 if n > 0 else 0.0
        avg_win = sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in wins) / len(wins) if wins else 0.0
        avg_loss= sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in losses) / len(losses) if losses else 0.0

        # Exit sebep ozeti
        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in trades:
            r = t["Sebep"]
            by_reason[r]["count"] += 1
            by_reason[r]["pnl"]   += t.get("Net_PnL", t.get("KarUSD", 0))

        max_dd  = self._max_drawdown()
        sharpe  = self._sharpe()

        summary = {
            "Toplam_Islem":   n,
            "Kazanma_Orani":  f"{win_rate:.2f}%",
            "Net_PnL_USD":    f"{net_pnl:.4f}",
            "Max_DD_Pct":     f"{max_dd:.2f}%",
            "Sharpe":         f"{sharpe:.2f}",
            "Ort_Kazanc_USD": f"{avg_win:.4f}",
            "Ort_Kayip_USD":  f"{avg_loss:.4f}",
            "Baslangic_Equity": f"{self.equity:.2f}",
            "Bitis_Equity":   f"{self.equity + self._pnl_running:.4f}",
            "Getiri_Pct":     f"{net_pnl / self.equity * 100:.3f}%",
        }
        for reas, data in by_reason.items():
            summary[f"Exit_{reas}_Sayi"]= data["count"]
            summary[f"Exit_{reas}_PnL"] = f"{data['pnl']:.4f}"

        # President karar sayaclari
        summary["President_Enabled"] = int(self.president_enabled)
        summary["Bloklanan_Sinyal"]  = len(self.block_events)
        if self.runtime:
            try:
                st = self.runtime.get_state()
                summary["President_Gunluk_PnL"] = f"{st.get('daily_pnl', 0):.2f}"
            except Exception:
                pass

        # Ranking / universe denetim metrikleri
        rank_causes = [r.get("cause", "") for r in getattr(self, "ranking_events", [])]
        summary["Ranking_Event_Count"] = len(rank_causes)
        summary["Rank_Selected_Count"] = sum(1 for c in rank_causes if c == "RANK_SELECTED")
        summary["Rank_Rejected_Count"] = sum(1 for c in rank_causes if str(c).startswith("RANK_REJECTED"))
        summary["MaxPositionsAlreadyFull_Count"] = sum(1 for e in self.filter_events if e.get("cause") == "MAX_POSITIONS_ALREADY_FULL")
        summary["TradedSymbolCount"] = len(set(t.get("Sembol") for t in trades)) if trades else 0
        summary["ActiveUniverseSize"] = len(getattr(self, "_run_symbols", []) or [])

        # CSV dosyalari yaz
        self._write_trades_csv()
        self._write_summary_csv(summary)
        self._write_equity_csv()
        self._write_filter_csv()
        self._write_ghost_csv()
        self._write_boa_csv()
        self._write_real_boa_csv()       # GERCEK BOA (4h/12h/24h + by_reason/symbol/regime)
        self._write_v10_phase1_reports(summary)
        # V9.0.5 FIX (ghost-config temizliği): write_boa_feedback_memory_placeholder
        # artık gerçekten okunuyor (varsayılan True -> davranış DEĞİŞMEDİ).
        if self.write_boa_feedback_memory_placeholder and not (self.out_dir / "boa_feedback_memory.json").exists():
            (self.out_dir / "boa_feedback_memory.json").write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_universe_audit_files()
        self._write_config_snapshot()

        return {
            "summary":      summary,
            "trades":       trades,
            "equity_curve": self.equity_curve,
        }

    # ── CSV Yazma ─────────────────────────────────────────────────────
    def _write_trades_csv(self):
        path = self.out_dir / "backtest_trades.csv"
        if not self.trades:
            return
        keys = []
        for row in self.trades:
            for k in row.keys():
                if k not in keys:
                    keys.append(k)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(self.trades)

    def _write_summary_csv(self, summary: dict):
        path = self.out_dir / "backtest_summary.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            for k, v in summary.items():
                w.writerow([k, v])

    def _write_equity_csv(self):
        path = self.out_dir / "equity_curve.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Timestamp", "Equity"])
            for ts, eq in self.equity_curve:
                w.writerow([ts, round(eq, 4)])

    def _write_filter_csv(self):
        path = self.out_dir / "filter_events.csv"
        if self.filter_events:
            keys = []
            for row in self.filter_events:
                for k in row.keys():
                    if k not in keys:
                        keys.append(k)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(self.filter_events)

        # V8.5.7: ranking olayları ayrı ve denetlenebilir dosyaya da yazılır.
        # V9.0.5 FIX: president.global_ranking.write_candidate_ranking_csv VE
        # backtest_output_integrity.write_candidate_ranking_events artık gerçekten
        # okunuyor (ikisi de varsayılan True -> davranış DEĞİŞMEDİ).
        if self.ranking_events and self.write_candidate_ranking_csv and self.write_candidate_ranking_events:
            rpath = self.out_dir / "candidate_ranking_events.csv"
            keys = []
            for row in self.ranking_events:
                for k in row.keys():
                    if k not in keys:
                        keys.append(k)
            with open(rpath, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(self.ranking_events)

    def _write_ghost_csv(self):
        path = self.out_dir / "ghost_signal_analysis.csv"
        if not self.ghost_signals:
            return
        keys = list(self.ghost_signals[0].keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, delimiter=";")
            w.writeheader()
            w.writerows(self.ghost_signals)

    def _write_boa_csv(self):
        """Exit sebep özeti — Net_PnL kolonunu kullanır."""
        path = self.out_dir / "block_outcome_summary_by_reason.csv"
        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
        for t in self.trades:
            r   = t.get("Sebep", "Unknown")
            pnl = t.get("Net_PnL", t.get("KarUSD", 0.0))
            by_reason[r]["count"] += 1
            by_reason[r]["pnl"]   += pnl
            if pnl > 0:
                by_reason[r]["wins"] += 1
        rows = []
        for r, d in sorted(by_reason.items(), key=lambda x: -x[1]["count"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] else 0
            rows.append({"Sebep": r, "Sayi": d["count"],
                         "WinRate": f"{wr:.1f}%", "ToplamPnL": f"{d['pnl']:.4f}",
                         "OrtPnL": f"{d['pnl']/d['count']:.4f}" if d["count"] else "0"})
        if not rows:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["Sebep","Sayi","WinRate","ToplamPnL","OrtPnL"],
                               delimiter=";")
            w.writeheader(); w.writerows(rows)

    def _write_boa_feedback_memory(self, rows_v2: list):
        """BOA sonuçlarından sonraki koşularda kullanılabilecek hafıza üretir.
        Bu dosya mevcut backtest içinde geçmişe uygulanmaz; lookahead yapmamak için sonraki koşularda okunur.
        """
        if not self.boa_feedback_enabled or not rows_v2:
            return
        agg = defaultdict(lambda: {"count": 0, "tp": 0, "sl": 0, "close_sum": 0.0})
        def add(key, r):
            d = agg[key]; d["count"] += 1
            fh = r.get("h24_first_hit", "")
            if fh == "TP_FIRST": d["tp"] += 1
            elif fh == "SL_FIRST": d["sl"] += 1
            try: d["close_sum"] += float(r.get("h24_close_return_pct", 0.0) or 0.0)
            except Exception: pass
        for r in rows_v2:
            sym = r.get("symbol", "")
            side = str(r.get("side", "LONG") or "LONG").upper()
            regime = r.get("regime", "")
            reason = r.get("reason", "")
            if sym: add(f"symbol:{sym}:{side}", r)
            if regime: add(f"regime:{regime}:{side}", r)
            if reason: add(f"reason:{reason}:{side}", r)
            add(f"side:{side}", r)
        mem = {}
        for k, d in agg.items():
            n = d["count"] or 1
            tp_rate = d["tp"] / n
            sl_rate = d["sl"] / n
            avg_close = d["close_sum"] / n
            # Edge puanı: TP-first pozitif, SL-first negatif, 24h kapanış yönü küçük ek etki.
            edge = (tp_rate - sl_rate) * self.boa_feedback_max_adj + max(-1.5, min(1.5, avg_close * 0.30))
            edge = max(-self.boa_feedback_max_adj, min(self.boa_feedback_max_adj, edge))
            mem[k] = {"count": d["count"], "tp_first": d["tp"], "sl_first": d["sl"],
                      "avg_close_return_pct": round(avg_close, 4), "edge": round(edge, 4)}
        try:
            path = self.boa_feedback_file
            if not path.is_absolute(): path = Path.cwd() / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.out_dir / "boa_feedback_memory.json").write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            try: (self.out_dir / "boa_feedback_error.txt").write_text(str(e), encoding="utf-8")
            except Exception: pass


    def _write_v10_phase1_reports(self, summary: dict = None):
        """Regime Router / Relative Strength aktif test raporları."""
        def _pnl(t):
            try: return float(t.get("Net_PnL", t.get("KarUSD", 0)) or 0.0)
            except Exception: return 0.0
        def _pf(rows):
            gp = sum(_pnl(r) for r in rows if _pnl(r) > 0)
            gl = -sum(_pnl(r) for r in rows if _pnl(r) <= 0)
            return round(gp / gl, 4) if gl > 0 else (round(gp, 4) if gp > 0 else 0.0)
        def _summ(rows):
            n=len(rows); wins=sum(1 for r in rows if _pnl(r)>0); pnl=sum(_pnl(r) for r in rows)
            return {
                "trades": n, "net_pnl": round(pnl,4), "win_rate": round(wins/n*100,2) if n else 0.0,
                "profit_factor": _pf(rows), "avg_trade": round(pnl/n,4) if n else 0.0,
                "max_loss": round(min([_pnl(r) for r in rows] or [0.0]),4),
                "max_win": round(max([_pnl(r) for r in rows] or [0.0]),4),
                "sl_count": sum(1 for r in rows if r.get("Sebep")=="SL"),
                "tp_count": sum(1 for r in rows if r.get("Sebep")=="TP"),
                "trail_count": sum(1 for r in rows if r.get("Sebep")=="Trail"),
            }
        def write_csv(name, rows, keys):
            with open(self.out_dir / name, "w", newline="", encoding="utf-8-sig") as f:
                w=csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(rows)
        trades = list(self.trades or [])
        # Regime summaries
        reg_rows=[]
        for col, rtype in [("BTCMacroRegime","btc_macro_regime"),("SymbolMicroRegime","symbol_micro_regime"),("VolatilityState","volatility_state")]:
            vals=sorted(set(str(t.get(col,"") or "UNKNOWN") for t in trades)) or ["UNKNOWN"]
            for v in vals:
                rows=[t for t in trades if str(t.get(col,"") or "UNKNOWN")==v]
                d=_summ(rows); d.update({"regime_type":rtype,"regime_value":v}); reg_rows.append(d)
        write_csv("regime_router_summary.csv", reg_rows, ["regime_type","regime_value","trades","net_pnl","win_rate","profit_factor","avg_trade","max_loss","max_win","sl_count","tp_count","trail_count"])
        # RS summaries
        rs_rows=[]
        pairs=sorted(set((str(t.get("RSGroup","") or "ungrouped"), str(t.get("RSState","") or "UNKNOWN")) for t in trades)) or [("ungrouped","UNKNOWN")]
        for g, st in pairs:
            rows=[t for t in trades if str(t.get("RSGroup","") or "ungrouped")==g and str(t.get("RSState","") or "UNKNOWN")==st]
            d=_summ(rows); d.update({"rs_group":g,"rs_state":st}); rs_rows.append(d)
        write_csv("relative_strength_summary.csv", rs_rows, ["rs_group","rs_state","trades","net_pnl","win_rate","profit_factor","avg_trade","sl_count","tp_count","trail_count"])
        # Module summary
        mod_rows=[]
        mods=sorted(set(str(t.get("ModuleName","") or "core_long") for t in trades)) or ["core_long"]
        for m in mods:
            rows=[t for t in trades if str(t.get("ModuleName","") or "core_long")==m]
            d=_summ(rows); d.update({"module_name":m,"max_dd_contribution":""}); mod_rows.append(d)
        write_csv("module_performance_summary.csv", mod_rows, ["module_name","trades","net_pnl","win_rate","profit_factor","avg_trade","max_dd_contribution"])
        # Block events subset. V10 department mimarisinde final reason çoğu zaman
        # PRESIDENT_* ile başlar; ancak içinde REGIME/RS/DEPARTMENT tavsiyesi taşır.
        # Bu yüzden yalnızca REGIME_ veya RS_ prefix'e bakmak raporu BOŞ bırakırdı.
        block_rows=[]
        def _is_v10_dept_block(cause: str) -> bool:
            c = str(cause or "")
            return (c.startswith("REGIME_") or c.startswith("RS_") or
                    c.startswith("PRESIDENT_ACCEPTED_REGIME_VETO") or
                    c.startswith("PRESIDENT_ACCEPTED_RS_VETO") or
                    c == "PRESIDENT_SCORE_AFTER_DEPARTMENT_ADVICE_REJECTED")
        for e in self.filter_events:
            cause=str(e.get("cause", ""))
            if _is_v10_dept_block(cause):
                block_rows.append({
                    "timestamp": e.get("ts",""), "symbol": e.get("symbol",""), "side": e.get("side","LONG"),
                    "block_reason": cause, "btc_macro_regime": e.get("btc_macro_regime",""),
                    "symbol_micro_regime": e.get("symbol_micro_regime",""), "volatility_state": e.get("volatility_state",""),
                    "rs_state": e.get("rs_state",""), "rs_score": e.get("rs_score",""),
                    "candidate_score": e.get("candidate_score", e.get("score", e.get("president_base_score", ""))), "required_score": e.get("required_score",""),
                    "final_size_mult": e.get("final_size_mult", ""),
                })
        write_csv("regime_block_events.csv", block_rows, ["timestamp","symbol","side","block_reason","btc_macro_regime","symbol_micro_regime","volatility_state","rs_state","rs_score","candidate_score","required_score","final_size_mult"])
        # Active test one-line summary
        rr_cfg=self.cfg.get("regime_router",{}) or {}; rs_cfg=self.cfg.get("relative_strength",{}) or {}
        def _flag_mode(sec):
            if not sec.get("enabled", False):
                return "disabled"
            m = str(sec.get("mode", "")).lower()
            if m in ("shadow", "soft", "hard"):
                return m
            if bool(sec.get("hard_mode", False)):
                return "hard"
            if bool(sec.get("soft_mode", False)):
                return "soft"
            return "shadow"
        chop_rows=[t for t in trades if str(t.get("BTCMacroRegime","")).upper() in ("CHOP","PANIC","BEAR") or str(t.get("SymbolMicroRegime","")).upper() in ("RANGE","BREAKDOWN") or str(t.get("Rejim","")).upper() in ("KONSOL",)]
        weak_rows=[t for t in trades if str(t.get("RSState","")).upper()=="WEAK"]
        allsum=_summ(trades)
        active=[{
            "test_mode": self.mode, "regime_mode": _flag_mode(rr_cfg),
            "rs_mode": _flag_mode(rs_cfg),
            "trades": allsum["trades"], "net_pnl": allsum["net_pnl"], "profit_factor": allsum["profit_factor"],
            "win_rate": allsum["win_rate"], "max_drawdown": self._max_drawdown(), "sl_count": allsum["sl_count"],
            "tp_count": allsum["tp_count"], "trail_count": allsum["trail_count"],
            "konsol_or_chop_trades": len(chop_rows), "konsol_or_chop_pnl": round(sum(_pnl(t) for t in chop_rows),4),
            "rs_weak_trades": len(weak_rows), "rs_weak_pnl": round(sum(_pnl(t) for t in weak_rows),4),
            "regime_blocks": sum(1 for e in self.filter_events if str(e.get("cause","")).startswith("REGIME_") or str(e.get("cause","")).startswith("PRESIDENT_ACCEPTED_REGIME_VETO") or str(e.get("cause","")).startswith("PRESIDENT_SCORE_AFTER_DEPARTMENT_ADVICE_REJECTED")),
            "rs_blocks": sum(1 for e in self.filter_events if str(e.get("cause","")).startswith("RS_") or str(e.get("cause","")).startswith("PRESIDENT_ACCEPTED_RS_VETO") or str(e.get("cause","")).startswith("PRESIDENT_SCORE_AFTER_DEPARTMENT_ADVICE_REJECTED")),
        }]
        write_csv("active_test_summary.csv", active, ["test_mode","regime_mode","rs_mode","trades","net_pnl","profit_factor","win_rate","max_drawdown","sl_count","tp_count","trail_count","konsol_or_chop_trades","konsol_or_chop_pnl","rs_weak_trades","rs_weak_pnl","regime_blocks","rs_blocks"])
        if summary is not None:
            summary["V10_RegimeRouter_Mode"] = active[0]["regime_mode"]
            summary["V10_RelativeStrength_Mode"] = active[0]["rs_mode"]
            summary["V10_Regime_Blocks"] = active[0]["regime_blocks"]
            summary["V10_RS_Blocks"] = active[0]["rs_blocks"]
            summary["V10_RS_Weak_Trades"] = active[0]["rs_weak_trades"]

    def _write_universe_audit_files(self):
        """Backtest output klasöründe aktif evren ve varsa meta/history dosyalarını zorunlu görünür yapar.

        Haftalık universe canlı/WF tarafında dinamik değişse bile tek backtest sonucu incelenirken
        en azından hangi aktif sembol listesiyle koşulduğu output içinde kalmalıdır.
        """
        # V9.0.5 FIX (ghost-config temizliği): backtest_output_integrity.write_active_universe
        # artık gerçekten okunuyor (varsayılan True -> davranış DEĞİŞMEDİ).
        if not self.write_active_universe:
            return
        try:
            symbols = list(getattr(self, "_run_symbols", []) or [])
            (self.out_dir / "active_universe_symbols.json").write_text(json.dumps(symbols, ensure_ascii=False, indent=2), encoding="utf-8")

            # V9 FIX: gercek rotasyon olduysa (self.rotation_events > 1 kayit)
            # her rotasyon noktasini ayri satir olarak yaz; yoksa eski STATIC davranisi koru.
            rot_events = getattr(self, "rotation_events", []) or []
            if rot_events:
                hist_rows = [{
                    "refresh_index": i,
                    "mode": "initial_universe" if i == 0 else "weekly_rotation",
                    "symbols": ",".join(ev["symbols"]),
                    "count": len(ev["symbols"]),
                } for i, ev in enumerate(rot_events)]
            else:
                hist_rows = [{"refresh_index": 0, "mode": "static_backtest_universe", "symbols": ",".join(symbols), "count": len(symbols)}]
            with open(self.out_dir / "symbol_universe_history.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["refresh_index", "mode", "symbols", "count"], delimiter=";")
                w.writeheader(); w.writerows(hist_rows)
            # Mevcut meta varsa output'a kopyala; yoksa placeholder üret.
            meta_src = Path("symbols_top70_meta.json")
            if meta_src.exists():
                try:
                    (self.out_dir / "symbols_top70_meta.json").write_text(meta_src.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
            if not (self.out_dir / "symbols_top70_meta.json").exists():
                (self.out_dir / "symbols_top70_meta.json").write_text(json.dumps({
                    "note": "symbols_top70_meta.json not found; active_universe_symbols.json records actual symbols used.",
                    "active_universe_size": len(symbols),
                    "symbols": symbols,
                }, ensure_ascii=False, indent=2), encoding="utf-8")

            if len(rot_events) > 1:
                lines = ["ts;event;detail"]
                for i, ev in enumerate(rot_events):
                    event_name = "INITIAL_UNIVERSE" if i == 0 else "WEEKLY_ROTATION"
                    lines.append(f"{ev['ts']};{event_name};{','.join(ev['symbols'])}")
                (self.out_dir / "weekly_universe_log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            else:
                (self.out_dir / "weekly_universe_log.csv").write_text("ts;event;detail\n;STATIC_BACKTEST_UNIVERSE;active_universe_symbols.json written\n", encoding="utf-8-sig")
        except Exception as e:
            try: (self.out_dir / "universe_audit_error.txt").write_text(str(e), encoding="utf-8")
            except Exception: pass

    def _write_config_snapshot(self):
        path = self.out_dir / "config_snapshot.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2, default=str)

    def _write_real_boa_csv(self):
        # V8.5.2: V7 Block Outcome Analyzer v2 — same-candle lookahead önlemeli, first_hit ve 4/8/12/24h raporları.
        try:
            boa_cfg = self.cfg.get("block_outcome_analysis", {}) or {}
            if boa_cfg.get("enabled", True) and getattr(self, "block_events", None):
                rows_v2 = build_block_outcome(
                    self.block_events, self._cbs,
                    tp_pct=float(boa_cfg.get("tp_pct", self.tp_pct)),
                    sl_pct=float(boa_cfg.get("sl_pct", self.sl_pct)),
                    horizons_hours=list(boa_cfg.get("horizons_hours", [4,8,12,24])),
                    cooldown_bars=int(boa_cfg.get("cooldown_bars", 12)),
                    bar_seconds=int(boa_cfg.get("bar_seconds", 3600)),
                )
                write_block_outcome_reports(self.out_dir, rows_v2, list(boa_cfg.get("horizons_hours", [4,8,12,24])))
                self._write_boa_feedback_memory(rows_v2)
        except Exception as e:
            try:
                (self.out_dir / "boa_v2_error.txt").write_text(str(e), encoding="utf-8")
            except Exception:
                pass
        buckets = {
            "block_outcome_4h.csv":  getattr(self, "boa_4h",  []),
            "block_outcome_12h.csv": getattr(self, "boa_12h", []),
            "block_outcome_24h.csv": getattr(self, "boa_24h", []),
        }
        keys = ["ts_ms", "symbol", "side", "cause", "regime", "score", "verdict", "hyp_pnl_pct"]
        for fname, rows in buckets.items():
            if not rows: continue
            with open(self.out_dir / fname, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(rows)

        rows24 = getattr(self, "boa_24h", [])
        if not rows24:
            return

        def _summarize(key):
            agg = defaultdict(lambda: {"n": 0, "gereksiz": 0, "dogru": 0, "pnl": 0.0})
            for r in rows24:
                k = r.get(key, "?")
                agg[k]["n"]   += 1
                agg[k]["pnl"] += r.get("hyp_pnl_pct", 0)
                if r.get("verdict") == "GEREKSIZ_ENGEL": agg[k]["gereksiz"] += 1
                elif r.get("verdict") == "DOGRU_ENGEL":  agg[k]["dogru"]    += 1
            out = []
            for k, d in sorted(agg.items(), key=lambda x: -x[1]["n"]):
                n = d["n"] or 1
                out.append({
                    key: k, "Blok_Sayisi": d["n"],
                    "Gereksiz_Engel": d["gereksiz"],
                    "Dogru_Engel":    d["dogru"],
                    "Gereksiz_Pct":   f"{d['gereksiz']/n*100:.1f}%",
                    "Ort_Hipotetik_PnL": f"{d['pnl']/n:.3f}%",
                })
            return out

        for key, fname, col in [
            ("cause",  "boa_summary_by_reason.csv", "Sebep"),
            ("symbol", "boa_summary_by_symbol.csv", "Sembol"),
            ("regime", "boa_summary_by_regime.csv", "Rejim"),
        ]:
            rows = _summarize(key)
            if not rows: continue
            for r in rows:
                r[col] = r.pop(key)
            fnames = [col, "Blok_Sayisi", "Gereksiz_Engel", "Dogru_Engel", "Gereksiz_Pct", "Ort_Hipotetik_PnL"]
            with open(self.out_dir / fname, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fnames, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(rows)


# ─── CLI Giris Noktasi ────────────────────────────────────────────────────────
def resolve_president_execution_mode(cfg: dict, cli_pmode: str = "") -> tuple:
    """
    V8.5.9 FIX: Bu mantık önceden SADECE backtest.py'nin CLI __main__ blogunda
    vardı. walk_forward.py / robustness_test.py / true_walk_forward.py
    Backtester'ı doğrudan Python içinde instantiate ettiği için bu override
    HİÇ uygulanmıyordu — config_online.yaml'daki statik president.shadow_mode
    değeri (ne olursa olsun) olduğu gibi kullanılıyordu. Bu yüzden
    president.shadow_mode: true iken WF/ROB/TWF'nin HER segmenti sessizce
    Action.SHADOW'a düşüyor, hiç trade açmıyordu (Toplam_Islem=0) — ama tekli
    backtest (subprocess+CLI üzerinden bu fonksiyonun eski hali çalıştığı
    için) normal çalışıyordu. Artık 4 script de AYNI fonksiyonu çağırıyor.

    Öncelik: CLI argümanı (cli_pmode) > config.backtest.president_execution_mode.
    cfg["president"]["shadow_mode"] İÇİNDE MUTATE EDİLİR (çağıran cfg dict'i
    elinde tutar). Dönüş: (president_enabled: bool, resolved_pmode: str)
    """
    president_enabled = True
    pmode = str(cli_pmode or "")
    if not pmode:
        _default_mode = str(cfg.get("backtest", {}).get("president_execution_mode", ""))
        if _default_mode == "shadow":
            pmode = "shadow"
        elif _default_mode in ("simulated_active", "live", "active"):
            pmode = "live"
        elif _default_mode == "legacy":
            pmode = "legacy"

    if pmode == "shadow":
        cfg.setdefault("president", {})["shadow_mode"] = True
    elif pmode == "live":
        cfg.setdefault("president", {})["shadow_mode"] = False
    elif pmode == "legacy":
        president_enabled = False
    return president_enabled, pmode


def main():
    parser = argparse.ArgumentParser(description="TRBOT V8 Backtest")
    parser.add_argument("--days",     type=int, default=30)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--top",      type=int, default=20)
    parser.add_argument("--out",      type=str, default="backtest_results/latest")
    parser.add_argument("--start",    type=str, default="")
    parser.add_argument("--end",      type=str, default="")
    parser.add_argument("--config",   type=str, default="config_online.yaml")
    parser.add_argument("--president-mode", type=str, default="",
                        choices=["", "shadow", "live", "legacy"],
                        help="shadow=emir yok, live=gercek acilis, legacy=President bypass")
    # değişiklik başlangıcı — DOMINANS FILTRESI CLI PARAMETRELERI
    # YAML'de "true" yapmayi unutma/yanlis dosya kullanma riskini ortadan
    # kaldirmak icin: bu parametreler verilirse YAML'deki degeri EZER (override).
    parser.add_argument("--dominance-filter", type=str, default="", choices=["", "on", "off"],
                        help="on=ac, off=kapat. Verilmezse config_online.yaml'daki deger kullanilir.")
    parser.add_argument("--dominance-csv", type=str, default="",
                        help="dominance.csv'nin TAM yolu. Verilmezse config'teki csv_path kullanilir.")
    parser.add_argument("--funding-filter", type=str, default="", choices=["", "on", "off"],
                        help="on=ac, off=kapat (funding-gated SHORT). Verilmezse YAML kullanilir.")
    parser.add_argument("--funding-csv", type=str, default="",
                        help="funding_history.csv'nin TAM yolu.")
    parser.add_argument("--trapped-filter", type=str, default="", choices=["", "on", "off"],
                        help="on=ac, off=kapat (tuzaklanmis pozisyon asimetrisi). Verilmezse YAML kullanilir.")
    parser.add_argument("--trapped-csv", type=str, default="",
                        help="OI verisi CSV'sinin TAM yolu (binance_metrics.csv tipi).")
    parser.add_argument("--trapped-mode", type=str, default="", choices=["", "cross", "level"],
                        help="cross=sadece taze gecis (guclu, nadir), level=her an o bolgedeyse (sik, zayif)")
    parser.add_argument("--trapped-price-csv", type=str, default="",
                        help="fetch_daily_close.py ciktisinin TAM yolu (offline on-isinma icin SART).")
    # değişiklik bitişi
    args = parser.parse_args()

    cfg = _load_cfg(args.config)
    # değişiklik başlangıcı — CLI override, YAML'i FIZIKSEL DEGISTIRMEDEN
    # (config dosyana hic dokunulmuyor, sadece bu calistirmada gecerli)
    if args.dominance_filter:
        cfg.setdefault("dominance_filter", {})
        cfg["dominance_filter"]["enabled"] = (args.dominance_filter == "on")
        print(f"[CLI OVERRIDE] dominance_filter.enabled = {cfg['dominance_filter']['enabled']} "
              f"(--dominance-filter {args.dominance_filter} ile YAML'i ezdi)")
    if args.dominance_csv:
        cfg.setdefault("dominance_filter", {})
        cfg["dominance_filter"]["csv_path"] = args.dominance_csv
        print(f"[CLI OVERRIDE] dominance_filter.csv_path = {args.dominance_csv}")
    if args.funding_filter:
        cfg.setdefault("funding_gate_filter", {})
        cfg["funding_gate_filter"]["enabled"] = (args.funding_filter == "on")
        print(f"[CLI OVERRIDE] funding_gate_filter.enabled = {cfg['funding_gate_filter']['enabled']} "
              f"(--funding-filter {args.funding_filter} ile YAML'i ezdi)")
    if args.funding_csv:
        cfg.setdefault("funding_gate_filter", {})
        cfg["funding_gate_filter"]["csv_path"] = args.funding_csv
        print(f"[CLI OVERRIDE] funding_gate_filter.csv_path = {args.funding_csv}")
    if args.trapped_filter:
        cfg.setdefault("trapped_distance_filter", {})
        cfg["trapped_distance_filter"]["enabled"] = (args.trapped_filter == "on")
        print(f"[CLI OVERRIDE] trapped_distance_filter.enabled = {cfg['trapped_distance_filter']['enabled']}")
    if args.trapped_csv:
        cfg.setdefault("trapped_distance_filter", {})
        cfg["trapped_distance_filter"]["csv_path"] = args.trapped_csv
        print(f"[CLI OVERRIDE] trapped_distance_filter.csv_path = {args.trapped_csv}")
    if args.trapped_mode:
        cfg.setdefault("trapped_distance_filter", {})
        cfg["trapped_distance_filter"]["mode"] = args.trapped_mode
        print(f"[CLI OVERRIDE] trapped_distance_filter.mode = {args.trapped_mode}")
    if args.trapped_price_csv:
        cfg.setdefault("trapped_distance_filter", {})
        cfg["trapped_distance_filter"]["price_csv_path"] = args.trapped_price_csv
        print(f"[CLI OVERRIDE] trapped_distance_filter.price_csv_path = {args.trapped_price_csv}")
    # değişiklik bitişi

    # Tarih hesapla (sembol seçimi look-ahead kontrolü için ÖNCE gerekli)
    if args.start and args.end:
        def _to_ms(s):
            return int(datetime.datetime.strptime(s, "%Y-%m-%d").timestamp() * 1000)
        start_ms = _to_ms(args.start)
        end_ms   = _to_ms(args.end)
    else:
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - args.days * 24 * 3600 * 1000

    # V9.0.3 FIX (sifir-mum teshisi): tarih araligi mantik kontrolu — yanlis
    # girilmis bir yil (orn. GUI'de "2005" yazilip "2025" kastedilmis olabilir)
    # tum sistemi sessizce "0 mum / 0 islem / PnL=0" sonucuna goturuyordu.
    # Artik calismaya BASLAMADAN ACIK VE NET bir hata ile durur.
    now_ms = int(time.time() * 1000)
    if start_ms >= end_ms:
        print(f"\n[TARIH_HATASI] start ({args.start}) >= end ({args.end}). Backtest BASLATILMIYOR.\n")
        sys.exit(1)
    if end_ms < _BINANCE_LAUNCH_MS:
        print(f"\n[TARIH_HATASI] Girilen tarih araligi ({args.start} .. {args.end}) Binance'in kurulus "
              f"tarihinden (2017-01-01) ONCE! Binance'te bu tarihte HICBIR sembol icin veri yoktur, "
              f"bu yuzden TUM semboller icin '0 mum / 0 islem / PnL=0' alirsiniz. "
              f"Yil yanlis girilmis olabilir (orn. '2005' yerine '2025' kastedilmis olabilir). "
              f"Backtest BASLATILMIYOR — lutfen tarihi duzeltip tekrar deneyin.\n")
        sys.exit(1)
    if start_ms > now_ms:
        print(f"\n[TARIH_HATASI] start tarihi ({args.start}) gelecekte! Backtest BASLATILMIYOR.\n")
        sys.exit(1)

    # V9.2 FIX: Universe Manager — V7 symbols_builder.py metodolojisi taşındı
    # (stable/fiat/wrapped eleme + 7g hacim stabilitesi + 30g momentum +
    # win_days_ratio + EMA5>EMA20). config'te açıksa, start_ms'ten ÖNCEKİ
    # (lookback_days) veriye bakarak look-ahead-safe sembol evreni seçer.
    # Aday havuzu (candidate pool) STATİK dosyadan gelir (canlı API'ye gidilmez,
    # tekrarlanabilirlik + anakronizm riski olmaması için) — canlı candidate
    # pool (simulator.py) ile backtest candidate pool TAMAMEN AYRI yollardır.
    um_cfg = cfg.get("weekly_symbol_rotation", {}) or {}
    rotation_schedule = None  # V9 FIX: dolu olursa bt.run() gercek rotasyonu uygular
    if bool(um_cfg.get("enabled", False)):
        import universe_manager as _um
        candidate_top = int(um_cfg.get("candidate_top", 120))
        lookback_days = int(um_cfg.get("lookback_days", 30))
        refresh_days  = max(1, int(um_cfg.get("refresh_days", 7)))
        candidate_file = str(um_cfg.get("backtest_candidate_file", "symbols_top120.json"))
        candidates = _um.load_candidate_pool_file(candidate_file, candidate_top)
        print(f"[UniverseManager] {len(candidates)} aday sembol (statik dosya: {candidate_file}), "
              f"look-ahead-safe HAFTALIK rotasyon hesaplanıyor (refresh_days={refresh_days})...")
        sel_start_ms = start_ms - lookback_days * 24 * 3600 * 1000

        # V9 FIX: rotasyonun TEST PENCERESI BOYUNCA gercekten calismasi icin
        # aday havuzu mumlari sadece start_ms'e kadar degil, end_ms'e kadar cekilir.
        # Boylece her refresh noktasinda o ana kadarki (look-ahead-safe) veriyle
        # universe yeniden hesaplanabilir.
        cand_candles = {}
        _net_before = get_network_error_count()
        for sym in candidates:
            cand_candles[sym] = _fetch_candles(sym, args.interval, sel_start_ms, end_ms)
        _net_delta = get_network_error_count() - _net_before

        refresh_ms = refresh_days * 24 * 3600 * 1000
        rotation_points = []  # [(effective_from_ms, [selected_symbols], uni_result), ...]
        boundary = start_ms
        while boundary < end_ms:
            uni_result = _um.build_universe_for_window(
                candidates, cand_candles, as_of_ms=boundary,
                lookback_days=lookback_days, top=int(um_cfg.get("top_n", args.top)),
                network_error_count=(_net_delta if boundary == start_ms else 0))
            rotation_points.append((boundary, list(uni_result["selected"]), uni_result))
            boundary += refresh_ms

        first_ms, first_symbols, first_result = rotation_points[0]
        if first_result.get("core_fallback_used"):
            print(f"[UniverseManager] ⚠️ CORE_FALLBACK_USED — DATA_QUALITY_FAIL! "
                  f"Temiz seçim boş çıktı, güvenli çekirdek evrene düşüldü: {first_symbols}")
        print(f"[UniverseManager] candidate={first_result['candidate_count']} "
              f"stable_filtered={first_result['stable_filtered_count']} "
              f"problematic_filtered={first_result['problematic_filtered_count']} "
              f"zero_candle={first_result['zero_candle_count']} "
              f"insufficient_history={first_result['insufficient_history_count']} "
              f"network_error_count={first_result.get('network_error_count', 0)} "
              f"selected={first_result['selected_count']} source={first_result['source']} "
              f"data_quality_ok={first_result['data_quality_ok']}")
        print(f"[UniverseManager] {len(rotation_points)} rotasyon noktasi hesaplandi "
              f"(her {refresh_days} günde bir). İlk seçilen {len(first_symbols)} sembol: "
              f"{first_symbols[:10]}{'...' if len(first_symbols) > 10 else ''}")
        for i in range(1, len(rotation_points)):
            _prev = set(rotation_points[i - 1][1])
            _cur  = set(rotation_points[i][1])
            if _cur != _prev:
                _added   = sorted(_cur - _prev)
                _dropped = sorted(_prev - _cur)
                _dt = time.strftime("%Y-%m-%d", time.gmtime(rotation_points[i][0] // 1000))
                print(f"[UniverseManager] Rotasyon #{i} ({_dt}): +{_added} -{_dropped}")
        # Geriye uyum: ilk secimi eskisi gibi _universe/ altina yaz (GUI bunu okuyor).
        _um.write_universe_files(first_result, str(Path(args.out) / "_universe"), mode="backtest", as_of_ms=start_ms, cfg=cfg)

        # V9 FIX: backtest.run() artik TUM rotasyon noktalarinda secilmis
        # sembollerin BIRLESIMINI gezer; hangi sembolun hangi ts'den itibaren
        # AKTIF (yeni aday acabilir) oldugunu rotation_schedule belirler.
        symbols = sorted(set(s for _, sel, _ in rotation_points for s in sel))
        rotation_schedule = [(ms, set(sel)) for ms, sel, _ in rotation_points]
    else:

        symbols = _load_symbols(args.top)

    # President modu override'i — CLI argümanı varsa o öncelikli (geriye uyumlu).
    # CLI argümanı VERİLMEDİYSE (--president-mode boş), config'teki
    # backtest.president_execution_mode okunur ve varsayılan olarak uygulanır.
    # Bu, dokümantasyonda (HYBRID_CONFIG_NOTES.md) bahsedilen ama önceden hiç
    # okunmayan alanı gerçek bir etkiye kavuşturur — CLI hâlâ her zaman üstün.
    president_enabled, pmode = resolve_president_execution_mode(cfg, args.president_mode)

    print(f"[Backtest] {len(symbols)} sembol | {args.interval} | {args.days} gun")
    print(f"[Backtest] Cikti klasoru: {args.out}")

    # Veri cek
    candles_by_sym = {}
    htf_candles    = {}
    _net_before_main = get_network_error_count()
    for sym in symbols:
        print(f"  Veri: {sym}", end=" ", flush=True)
        if rotation_schedule is not None and sym in cand_candles:
            # V9 FIX: rotasyon modunda bu sembol icin mumlar zaten cand_candles'da
            # (sel_start_ms..end_ms) var — tekrar fetch ETMEYELIM, sadece trade
            # penceresine (start_ms..end_ms) kirpip kullanalim.
            candles_by_sym[sym] = [c for c in cand_candles[sym] if c["open_time"] >= start_ms]
        else:
            candles_by_sym[sym] = _fetch_candles(sym, args.interval, start_ms, end_ms)
        htf_candles[sym] = _fetch_candles(sym, "1h", start_ms, end_ms)
        print(f"({len(candles_by_sym[sym])} mum)")

    # V9 FIX: BTCUSDT, trade evreninde olmasa bile rejim/btc_filter icin
    # her zaman ayrica cekilir. symbols/candles_by_sym icine KATILMAZ.
    print(f"  Veri: BTCUSDT (rejim/btc_filter icin, ayri kanal)", end=" ", flush=True)
    btc_candles = _fetch_candles("BTCUSDT", args.interval, start_ms, end_ms)
    print(f"({len(btc_candles)} mum)")

    _net_delta_main = get_network_error_count() - _net_before_main
    if _net_delta_main > 0:
        print(f"\n[DATA_QUALITY_WARNING] Ana veri çekimi sırasında {_net_delta_main} network hatası "
              f"(DNS/timeout/connection/429/5xx) oluştu — sonuçlar kısmen eksik veriye dayanıyor olabilir.\n")

    bt = Backtester(cfg, args.out, president_enabled=president_enabled, interval=args.interval)
    bt._write_config_snapshot()
    result = bt.run(symbols, candles_by_sym, htf_candles, btc_candles=btc_candles, rotation_schedule=rotation_schedule)

    summary = result["summary"]
    summary["NetworkErrorCount"] = _net_delta_main
    summary["DataQualityWarning"] = bool(_net_delta_main > 0)
    print("\n── Backtest Sonucu ──────────────────────────────────")
    for k, v in summary.items():
        if not k.startswith("Exit_"):
            print(f"  {k}: {v}")
    print(f"\nSonuclar: {args.out}")


if __name__ == "__main__":
    main()