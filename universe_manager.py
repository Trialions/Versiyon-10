# universe_manager.py — V9.2 Universe Manager (V7 symbols_builder.py metodolojisi taşındı)
#
# V7'nin GERÇEK symbols_builder.py mantığı (github.com/Trialions/Versiyon-7) bu modüle
# tasindi ve canli + backtest/WF/TWF ortak akisina baglandi:
#   1) Binance SPOT/TRADING USDT pariteleri (sadece canli yol — exchangeInfo)
#   2) Stable/fiat/emtia/wrapped/format-disi sembol temizligi (is_excluded_symbol)
#   3) 7 gunluk median/ortalama hacim stabilitesi + spike_ratio elemesi
#   4) 30 gunluk momentum: change_30d, win_days_ratio, daily_vol_std, EMA5>EMA20
#   5) CORE_SYMBOLS (BTC/ETH/BNB/SOL/XRP/...) momentum hard-eleme MUAFIYETI + skor primi
#   6) Zengin meta raporu (skor kirilimi, momentum verileri)
#
# KRITIK GUVENLIK KURALI: Eger temiz secim (selected) BOSSA, HAM aday listesine
# (candidate_symbols[:top]) SESSIZCE geri DUSULMEZ. Bunun yerine acikca tanimli
# CORE_FALLBACK (BTC/ETH/BNB/SOL/XRP) kullanilir ve rapora "CORE_FALLBACK_USED"
# / "DATA_QUALITY_FAIL" olarak yazilir.
from __future__ import annotations
import json
import math
import re
import time
from pathlib import Path
from statistics import median
from typing import Dict, List, Any, Optional, Tuple

from fetch_guard import guarded_get_json, classify_fetch_exception, NETWORK_ERROR_KINDS

# ───────────────────────── V7'den tasinan sabitler ──────────────────────────
_BLACKLIST = {
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT",
    "DAIUSDT", "USD1USDT", "RLUSDUSDT", "PYUSDUSDT", "USDEUSDT",
    "EURUSDT", "EURIUSDT", "AEURUSDT", "UUSDT", "TRYUSDT",
    "XAUTUSDT", "PAXGUSDT", "XAUUSDT", "WBETHUSDT",
}
_STABLE_OR_FIAT_KEYWORDS = (
    "USD", "EUR", "GBP", "TRY", "BRL", "AUD", "PAXG", "XAU", "XAUT", "WBETH",
)
_BAD_TOKENS = ("UP", "DOWN", "BULL", "BEAR")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LINKUSDT", "AVAXUSDT",
    "LTCUSDT", "BCHUSDT", "DOTUSDT", "NEARUSDT", "SUIUSDT",
]
# Bağlı kalınamayan durumda kullanılan AÇIKÇA TANIMLI son çare evren.
# Bu liste sadece DATA_QUALITY_FAIL durumunda devreye girer ve raporda
# her zaman "CORE_FALLBACK_USED" notuyla işaretlenir.
CORE_FALLBACK = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]

# V9.3: network-hatası sayacı (DNS/timeout/connection/429/5xx). backtest.py'daki
# ile AYNI mantık — fold/segment bazında "bu pencerede network sorunu oldu mu"
# sorusuna cevap vermek için. Modüle özel (backtest.py'nin kendi sayacı ayrı).
_NETWORK_ERROR_COUNT = [0]


def get_network_error_count() -> int:
    return _NETWORK_ERROR_COUNT[0]

MIN_24H_QUOTE_VOLUME = 5_000_000
MIN_7D_MEDIAN_QUOTE_VOLUME = 2_000_000
MAX_SPIKE_RATIO = 6.0
MAX_ABS_24H_CHANGE_PCT = 45.0
MOMENTUM_MIN_CHANGE_30D = -25.0
MOMENTUM_MIN_WIN_DAYS = 0.35


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def is_excluded_symbol(symbol: str) -> bool:
    """V7'den birebir tasindi: stable/fiat/emtia/wrapped/format-disi sembolleri eler."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return True
    if symbol in _BLACKLIST:
        return True
    if not _is_ascii(symbol):
        return True
    if not _SYMBOL_RE.match(symbol):
        return True
    if not symbol.endswith("USDT"):
        return True
    if any(x in symbol for x in _BAD_TOKENS):  # leveraged token'lar (UP/DOWN/BULL/BEAR)
        return True
    base = symbol[:-4]
    for kw in _STABLE_OR_FIAT_KEYWORDS:
        if kw in base:
            return True
    return False


# ───────────────────────── Momentum / skor hesaplama (ortak) ───────────────
def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _momentum_metrics(closes: List[float]) -> Dict[str, Any]:
    """V7._fetch_momentum_data() esdegeri — gunluk (veya periyodik) kapanislardan
    change_30d / win_days_ratio / daily_vol_std / ema_ok hesaplar."""
    if len(closes) < 10:
        return {}
    change_30d = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0.0
    daily_returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                      for i in range(1, len(closes)) if closes[i-1] > 0]
    win_days_ratio = (sum(1 for r in daily_returns if r > 0) / len(daily_returns)
                       if daily_returns else 0.5)
    if len(daily_returns) >= 2:
        mean_r = sum(daily_returns) / len(daily_returns)
        daily_vol_std = (sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
    else:
        daily_vol_std = 0.0
    ema5 = _ema(closes, 5) if len(closes) >= 5 else closes[-1]
    ema20 = _ema(closes, 20) if len(closes) >= 20 else closes[-1]
    return {
        "change_30d": round(change_30d, 2),
        "win_days_ratio": round(win_days_ratio, 3),
        "daily_vol_std": round(daily_vol_std, 2),
        "ema_ok": bool(ema5 > ema20),
    }


def _momentum_score(mdata: dict) -> float:
    """V7._calc_momentum_score() esdegeri. Aralik: -0.5..+0.5."""
    if not mdata:
        return 0.0
    ms = 0.0
    c30, wdr, std, ema = (mdata.get("change_30d", 0.0), mdata.get("win_days_ratio", 0.5),
                          mdata.get("daily_vol_std", 0.0), mdata.get("ema_ok", False))
    if c30 > 5.0: ms += 0.25
    if wdr > 0.55: ms += 0.15
    if ema: ms += 0.25
    if c30 < -10.0: ms -= 0.25
    if wdr < 0.40: ms -= 0.15
    if std > 5.0: ms -= 0.15
    return round(ms, 3)


def _excluded_by_momentum(mdata: dict, symbol: str) -> bool:
    """V7._is_excluded_by_momentum() esdegeri. CORE_SYMBOLS muaf (fail-open veri yoksa da muaf)."""
    if symbol in CORE_SYMBOLS:
        return False
    if not mdata:
        return False
    if mdata.get("change_30d", 0.0) < MOMENTUM_MIN_CHANGE_30D:
        return True
    if mdata.get("win_days_ratio", 0.5) < MOMENTUM_MIN_WIN_DAYS:
        return True
    return False


def _candidate_score(quote_volume_recent: float, median_quote_volume_7p: float,
                      avg_quote_volume_7p: float, spike_ratio: float,
                      change_abs: float, symbol: str, momentum_score: float = 0.0) -> float:
    """V7._candidate_score() esdegeri (degisken adlari periyot-agnostik yapildi)."""
    score = 0.0
    score += median_quote_volume_7p * 0.55
    score += avg_quote_volume_7p * 0.25
    score += quote_volume_recent * 0.20
    if spike_ratio > 2.5:
        score /= min(spike_ratio / 2.5, 4.0)
    if change_abs > 25:
        score *= 0.70
    if change_abs > 35:
        score *= 0.55
    if symbol in CORE_SYMBOLS:
        score *= 1.15
    score *= (1.0 + momentum_score * 0.6)
    return max(score, 0.0)


def score_symbol_universe(symbol: str, candles: list, recent_period_bars: int = 24 * 7,
                           momentum_period_bars: int = 24 * 30, bars_per_day: int = 24) -> Dict[str, Any]:
    """
    V7 metodolojisinin tek-sembol skorlama versiyonu. candles HER GRANULARITEDE
    (1h backtest mumu veya 1d canli mum) calisir. bars_per_day, candle interval'ina
    gore "gunluk" hacim kovalarini olusturmak icin kullanilir (1h mum -> 24,
    1d mum -> 1) — V9.0.5 BUG FIX: onceden 24-bar TOPLAMI ile 1-bar MEDYANI
    karsilastiriliyordu (olcek uyumsuzlugu, her sembol "spike" gibi gorunuyordu).
    Artik hacim ONCE gunluk kovalara toplaniyor, sonra median/ortalama/spike_ratio
    AYNI OLCEKTE (gun bazli) hesaplaniyor — V7'nin orijinal mantigiyla birebir.
    SADECE verilen candles listesine bakar — disaridan veri sizintisi YOK.
    """
    if not candles:
        return {"symbol": symbol, "score": -1e9, "reason": "NO_CANDLES", "zero_candle": True}

    closes = [float(c.get("close", 0) or 0) for c in candles if float(c.get("close", 0) or 0) > 0]
    vols   = [float(c.get("volume", 0) or 0) for c in candles]
    if len(closes) < min(30, recent_period_bars // 4):
        return {"symbol": symbol, "score": -1e9, "reason": "INSUFFICIENT_HISTORY",
                "insufficient_history": True, "bars": len(closes)}

    # ── Hacmi GÜNLÜK kovalara topla (V7 ile aynı ölçek: bir günün toplam hacmi) ──
    bpd = max(1, int(bars_per_day))
    daily_vols = [sum(vols[i:i + bpd]) for i in range(0, len(vols), bpd)] if bpd > 1 else list(vols)
    recent_days_n = max(1, recent_period_bars // bpd)
    recent_daily = daily_vols[-recent_days_n:] if daily_vols else []
    med_recent = median(recent_daily) if recent_daily else 0.0
    avg_recent = sum(recent_daily) / len(recent_daily) if recent_daily else 0.0
    quote_volume_recent = daily_vols[-1] if daily_vols else 0.0  # "24h-eşdeğeri" = en son günün hacmi
    spike_ratio = quote_volume_recent / max(med_recent, 1.0) if med_recent > 0 else 1.0

    # ── 30g-esdegeri momentum (gunluk kapanislar uzerinden) ──────────────
    daily_closes = closes[bpd - 1::bpd] if bpd > 1 else closes  # her gunun SON kapanisi
    if not daily_closes:
        daily_closes = closes
    mom_n = min(len(daily_closes), max(1, momentum_period_bars // bpd))
    mdata = _momentum_metrics(daily_closes[-mom_n:])
    if _excluded_by_momentum(mdata, symbol):
        return {"symbol": symbol, "score": -1e9, "reason": "MOMENTUM_EXCLUDED",
                "momentum_excluded": True, **mdata}
    mom_score = _momentum_score(mdata)

    # ── 24h-esdegeri degisim (son gun vs onceki gun) ─────────────────────
    chg_recent = ((daily_closes[-1] - daily_closes[-2]) / daily_closes[-2] * 100
                  if len(daily_closes) >= 2 and daily_closes[-2] > 0 else 0.0)

    reason = "OK"
    if med_recent > 0 and med_recent < MIN_7D_MEDIAN_QUOTE_VOLUME and symbol not in CORE_SYMBOLS:
        reason = "LOW_MEDIAN_VOLUME"
    if spike_ratio > MAX_SPIKE_RATIO and symbol not in CORE_SYMBOLS:
        return {"symbol": symbol, "score": -1e9, "reason": "SPIKE_RATIO_TOO_HIGH",
                "spike_ratio": round(spike_ratio, 2)}

    score = _candidate_score(quote_volume_recent, med_recent, avg_recent, spike_ratio,
                              abs(chg_recent), symbol, mom_score)

    return {
        "symbol": symbol, "score": round(score, 4), "reason": reason,
        "median_volume": round(med_recent, 2), "avg_volume": round(avg_recent, 2),
        "recent_volume": round(quote_volume_recent, 2), "spike_ratio": round(spike_ratio, 3),
        "change_pct": round(chg_recent, 2), "momentum_score": mom_score,
        "change_30d": mdata.get("change_30d", 0.0), "win_days_ratio": mdata.get("win_days_ratio", 0.0),
        "daily_vol_std": mdata.get("daily_vol_std", 0.0), "ema_ok": mdata.get("ema_ok", False),
        "bars": len(closes), "zero_candle": False, "insufficient_history": False,
        "momentum_excluded": False,
    }


# ───────────────────────── Ortak secim + GUVENLI fallback ──────────────────
def select_universe(candidate_symbols: List[str], candles_by_sym: Dict[str, list],
                     top: int = 20, recent_period_bars: int = 24 * 7,
                     momentum_period_bars: int = 24 * 30, bars_per_day: int = 24) -> Dict[str, Any]:
    """
    Aday sembolleri skorlar, secer. KRITIK: secim bossa ham candidate listesine
    SESSIZCE DUSULMEZ — acik CORE_FALLBACK + DATA_QUALITY_FAIL/CORE_FALLBACK_USED
    bayraklariyla doner. Cagiran taraf (backtest/live) bu bayraklari LOGLAMALIDIR.
    """
    stable_filtered, problematic_filtered = [], []
    pre_filtered = []
    for s in candidate_symbols:
        if is_excluded_symbol(s):
            stable_filtered.append(s)
        else:
            pre_filtered.append(s)

    rows = [score_symbol_universe(s, candles_by_sym.get(s, []), recent_period_bars,
                                   momentum_period_bars, bars_per_day) for s in pre_filtered]

    zero_candle = [r["symbol"] for r in rows if r.get("zero_candle")]
    insufficient = [r["symbol"] for r in rows if r.get("insufficient_history")]
    momentum_excl = [r["symbol"] for r in rows if r.get("momentum_excluded")]
    problematic_filtered = zero_candle + insufficient + momentum_excl

    valid_rows = [r for r in rows if r.get("score", -1e9) > -1e8]
    valid_rows.sort(key=lambda r: r["score"], reverse=True)
    selected_rows = valid_rows[:top]
    selected = [r["symbol"] for r in selected_rows]

    dropped_symbols_with_reason = {r["symbol"]: r.get("reason", "REJECTED")
                                    for r in rows if r["symbol"] not in selected}
    for s in stable_filtered:
        dropped_symbols_with_reason[s] = "STABLE_OR_PROBLEMATIC_FILTERED"

    data_quality_ok = bool(selected)
    source = "scored_selection"
    core_fallback_used = False

    if not selected:
        # ── KRITIK GUVENLIK KURALI: ham candidate listesine SESSIZCE DUSULMEZ ──
        selected = list(CORE_FALLBACK)
        selected_rows = [{"symbol": s, "score": 0.0, "reason": "CORE_FALLBACK_USED"} for s in selected]
        data_quality_ok = False
        core_fallback_used = True
        source = "core_fallback"
        for s in selected:
            dropped_symbols_with_reason.pop(s, None)

    return {
        "selected": selected,
        "selected_rows": selected_rows,
        "rejected_rows": [r for r in rows if r["symbol"] not in selected],
        "candidate_count": len(candidate_symbols),
        "stable_filtered_count": len(stable_filtered),
        "problematic_filtered_count": len(problematic_filtered),
        "historical_unavailable_count": len(zero_candle),
        "zero_candle_count": len(zero_candle),
        "insufficient_history_count": len(insufficient),
        "momentum_excluded_count": len(momentum_excl),
        "selected_count": len(selected),
        "dropped_symbols_with_reason": dropped_symbols_with_reason,
        "data_quality_ok": data_quality_ok,
        "core_fallback_used": core_fallback_used,
        "source": source,
    }


# ───────────────────────── CANLI yol (Binance exchangeInfo + ticker + kline) ─
def fetch_live_spot_usdt_candidates(candidate_top: int = 120) -> List[str]:
    """
    V7._load_spot_trading_symbols() + on-filtre esdegeri: SADECE Binance SPOT/
    TRADING durumundaki USDT pariteleri, stable/fiat/wrapped/format-disi/
    dusuk-hacim/asiri-24h-degisim elenmis sekilde, hacme gore siralanmis ilk
    candidate_top aday. Ag yoksa bos liste doner (cagiran taraf fallback'e duser).
    """
    try:
        ex = guarded_get_json("https://api.binance.com/api/v3/exchangeInfo",
                              timeout=20, label="exchangeInfo")
        valid = set()
        for item in ex.get("symbols", []):
            sym = item.get("symbol", "")
            if is_excluded_symbol(sym):
                continue
            if item.get("status") != "TRADING":
                continue
            if item.get("quoteAsset") != "USDT":
                continue
            valid.add(sym)

        tick = guarded_get_json("https://api.binance.com/api/v3/ticker/24hr",
                                timeout=20, label="ticker24hr")
        prelim = []
        for item in tick:
            sym = item.get("symbol", "")
            if sym not in valid:
                continue
            qv = float(item.get("quoteVolume", 0) or 0)
            chg = float(item.get("priceChangePercent", 0) or 0)
            if qv < MIN_24H_QUOTE_VOLUME and sym not in CORE_SYMBOLS:
                continue
            if abs(chg) > MAX_ABS_24H_CHANGE_PCT and sym not in CORE_SYMBOLS:
                continue
            prelim.append((sym, qv))
        prelim.sort(key=lambda x: x[1], reverse=True)
        out = [s for s, _ in prelim[:candidate_top]]
        for s in CORE_SYMBOLS:
            if s in valid and s not in out:
                out.append(s)
        return out[:candidate_top] if len(out) > candidate_top else out
    except Exception as e:
        kind = classify_fetch_exception(e)
        if kind in NETWORK_ERROR_KINDS:
            _NETWORK_ERROR_COUNT[0] += 1
        print(f"[FETCH_ERROR] universe_manager candidate_pool: {kind}: {type(e).__name__}: {e}")
        return []


def fetch_candidate_symbols(top: int = 120, quote: str = "USDT") -> List[str]:
    """GERİYE UYUM ad takma adı — bkz. fetch_live_spot_usdt_candidates()."""
    return fetch_live_spot_usdt_candidates(top)


def _fetch_klines_closes_vols(symbol: str, interval: str = "1d", limit: int = 30
                               ) -> Tuple[List[float], List[float]]:
    try:
        data = guarded_get_json(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20, label=f"universe {symbol} {interval}",
        )
        if not isinstance(data, list):
            return [], []
        closes = [float(k[4]) for k in data]
        vols   = [float(k[7]) for k in data]  # quote asset volume
        return closes, vols
    except Exception as e:
        kind = classify_fetch_exception(e)
        if kind in NETWORK_ERROR_KINDS:
            _NETWORK_ERROR_COUNT[0] += 1
        print(f"[FETCH_ERROR] universe_manager {symbol} {interval}: {kind}: {type(e).__name__}: {e}")
        return [], []


def build_live_universe(cfg: dict, fetch_candles_fn=None, data_dir: str = "data") -> Dict[str, Any]:
    """
    Canli evren uretimi — V7 metodolojisi (exchangeInfo + 24hr ticker + 1d kline
    ile 7g/30g analiz). fetch_candles_fn verilmezse dogrudan Binance'ten 1d kline
    ceker (gercek canli akis budur). Sonuc dict'i select_universe() ile aynı
    şemada (data_quality_ok, source, core_fallback_used, vb. dahil).
    """
    wu = (cfg or {}).get("weekly_symbol_rotation", {}) or {}
    candidate_top = int(wu.get("candidate_top", 120))
    top_n = int(wu.get("top_n", 20))

    candidates = fetch_live_spot_usdt_candidates(candidate_top)
    if not candidates:
        cur = load_current_universe(data_dir)
        result = {
            "selected": cur or list(CORE_FALLBACK), "selected_rows": [],
            "rejected_rows": [], "candidate_count": 0, "stable_filtered_count": 0,
            "problematic_filtered_count": 0, "historical_unavailable_count": 0,
            "zero_candle_count": 0, "insufficient_history_count": 0,
            "momentum_excluded_count": 0, "selected_count": len(cur or CORE_FALLBACK),
            "dropped_symbols_with_reason": {}, "data_quality_ok": bool(cur),
            "core_fallback_used": not bool(cur), "source": "static_fallback" if cur else "core_fallback",
        }
        write_universe_files(result, data_dir, mode="live", as_of_ms=int(time.time() * 1000), cfg=cfg)
        return result

    candles_by_sym = {}
    net_before = _NETWORK_ERROR_COUNT[0]
    for s in candidates:
        if fetch_candles_fn:
            try:
                candles_by_sym[s] = fetch_candles_fn(s)
                continue
            except Exception as e:
                kind = classify_fetch_exception(e)
                if kind in NETWORK_ERROR_KINDS:
                    _NETWORK_ERROR_COUNT[0] += 1
                print(f"[FETCH_ERROR] universe_manager {s}: {kind}: {type(e).__name__}: {e}")
                candles_by_sym[s] = []
                continue
        closes, vols = _fetch_klines_closes_vols(s, "1d", 30)
        candles_by_sym[s] = [{"close": c, "volume": v} for c, v in zip(closes, vols)]

    result = select_universe(candidates, candles_by_sym, top=top_n,
                              recent_period_bars=7, momentum_period_bars=30, bars_per_day=1)
    network_errors_this_run = _NETWORK_ERROR_COUNT[0] - net_before
    result["network_error_count"] = network_errors_this_run
    if network_errors_this_run > 0:
        # V9.3: network hatası ile gerçek 0-mum (sembol o tarihte yok) ayrımı —
        # network sorunu varsa data_quality_ok=False (CORE_FALLBACK_USED'dan
        # AYRI bir sebep) ve kaynak etiketine WARNING eklenir.
        result["data_quality_ok"] = False
        if not result.get("core_fallback_used"):
            result["source"] = result.get("source", "live_weekly_rotation") + "_DATA_QUALITY_WARNING"
        print(f"[UniverseManager] ⚠️ DATA_QUALITY_WARNING — {network_errors_this_run} network "
              f"hatası (DNS/timeout/connection/429/5xx) oluştu, seçim kısmen güvenilmez olabilir.")
    result["source"] = ("live_weekly_rotation" if (result["data_quality_ok"] and not result.get("core_fallback_used"))
                        else result["source"])
    write_universe_files(result, data_dir, mode="live", as_of_ms=int(time.time() * 1000), cfg=cfg)
    return result


def should_refresh(data_dir: str, refresh_days: int) -> bool:
    meta_path = Path(data_dir) / "symbols_current_meta.json"
    if not meta_path.exists():
        return True
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        generated_at = float(meta.get("generated_at", 0))
        return (time.time() - generated_at) >= refresh_days * 86400
    except Exception:
        return True


def load_current_universe(data_dir: str = "data") -> List[str]:
    p = Path(data_dir) / "symbols_current.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(s).upper() for s in data]
    except Exception:
        pass
    return []


# ───────────────────────── BACKTEST/WF/TWF yol (look-ahead-safe, historical) ─
def load_candidate_pool_file(path: str = "symbols_top120.json", top: int = 120) -> List[str]:
    """
    SADECE BACKTEST/WF/TWF icindir. Statik/dosya tabanli aday havuzu — canli
    API'ye HIC gitmez (tekrarlanabilirlik + "bugunun" hacim siralamasinin
    gecmisle karismamasi icin). Dosya yoksa/bozuksa fallback dosyaya, o da
    yoksa CORE_FALLBACK'e duser (sessiz ham-liste fallback YOK).
    """
    for candidate_path in (path, "symbols_top70.json"):
        try:
            data = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                cleaned = [str(s).upper() for s in data if not is_excluded_symbol(str(s).upper())]
                if cleaned:
                    return cleaned[:top]
        except Exception:
            continue
    print(f"[universe_manager] UYARI: {path} bulunamadi/bos — CORE_FALLBACK kullaniliyor.")
    return list(CORE_FALLBACK)


def build_universe_for_window(candidate_symbols: List[str], candles_by_sym_full: Dict[str, list],
                               as_of_ms: int, lookback_days: int = 30, top: int = 20,
                               interval_hours: float = 1.0, network_error_count: int = 0) -> Dict[str, Any]:
    """
    Belirli bir backtest/WF/TWF ani (as_of_ms) icin universe secer.
    candles_by_sym_full: sembol -> TUM mum verisi (close_time alani olan).
    SADECE close_time <= as_of_ms olan mumlar kullanilir -> LOOK-AHEAD BIAS YOK.
    Stable/fiat/problemli semboller candle fetch'ten ONCE elenir (select_universe
    icinde is_excluded_symbol ile). O tarihte yeterli gecmisi olmayan veya 0 mum
    donen semboller selected universe'e GIRMEZ (insufficient_history/zero_candle
    olarak raporlanir, dropped_symbols_with_reason'da görünür).

    network_error_count: ÇAĞIRAN TARAF (backtest.py/walk_forward.py/...) bu
    pencere için candle fetch sırasında kaç DNS/timeout/connection/429/5xx
    hatası oluştuğunu (backtest.get_network_error_count() delta'sı ile) buraya
    iletir. >0 ise data_quality_ok=False olur ve source'a _DATA_QUALITY_WARNING
    eklenir — "sembol o tarihte yok" (ZERO_CANDLES) ile "network sorunu
    yaşandı" (FETCH_NETWORK_ERROR) durumları böylece AÇIKÇA AYRILIR.
    """
    bars_lookback = max(30, int(lookback_days * 24 / max(interval_hours, 0.01)))
    bars_recent_7d = max(7, int(7 * 24 / max(interval_hours, 0.01)))
    bars_momentum_30d = max(30, int(lookback_days * 24 / max(interval_hours, 0.01)))
    bars_per_day = max(1, round(24 / max(interval_hours, 0.01)))

    candles_by_sym = {}
    for s in candidate_symbols:
        if is_excluded_symbol(s):
            continue  # candle fetch'e bile gerek yok — daha o asamada elendi
        full = candles_by_sym_full.get(s, []) or []
        past = [c for c in full if int(c.get("close_time", c.get("closeTime", 0)) or 0) <= as_of_ms]
        candles_by_sym[s] = past[-bars_lookback:] if past else []

    pre_filtered_candidates = [s for s in candidate_symbols if not is_excluded_symbol(s)]
    excluded_at_pool_level = [s for s in candidate_symbols if is_excluded_symbol(s)]
    result = select_universe(pre_filtered_candidates, candles_by_sym, top=top,
                              recent_period_bars=bars_recent_7d, momentum_period_bars=bars_momentum_30d,
                              bars_per_day=bars_per_day)
    # V9.2: stable/problemli semboller candle fetch'ten ÖNCE (havuz seviyesinde)
    # elendiği için select_universe() içindeki stable_filtered_count sıfır
    # görünür — bu sayıyı burada birleştiriyoruz ki rapor doğru yansısın.
    result["stable_filtered_count"] += len(excluded_at_pool_level)
    result["candidate_count"] = len(candidate_symbols)
    for s in excluded_at_pool_level:
        result["dropped_symbols_with_reason"].setdefault(s, "STABLE_OR_PROBLEMATIC_FILTERED")

    # V9.3: network hatası (DNS/timeout/connection/429/5xx) ile sembolün o
    # tarihte gerçekten yok olması (ZERO_CANDLES) AÇIKÇA AYRILIR. Network
    # sorunu varsa bu fold/segment DATA_QUALITY_WARNING alır — CORE_FALLBACK_USED
    # ile karıştırılmaz (farklı kök sebep).
    result["network_error_count"] = network_error_count
    if network_error_count > 0:
        result["data_quality_ok"] = False
        if not result.get("core_fallback_used"):
            result["source"] = result.get("source", "scored_selection") + "_DATA_QUALITY_WARNING"
        print(f"[UniverseManager] ⚠️ DATA_QUALITY_WARNING — {network_error_count} network hatası "
              f"bu pencerede oluştu (DNS/timeout/connection/429/5xx), seçim kısmen güvenilmez olabilir.")
    elif result["data_quality_ok"]:
        result["source"] = "historical_weekly_rotation"
    return result


def write_universe_files(result: Dict[str, Any], data_dir: str, mode: str = "live",
                          as_of_ms: Optional[int] = None, cfg: Optional[dict] = None) -> None:
    # V9.2 FIX (ghost-config): weekly_symbol_rotation.write_meta/meta_file/
    # write_history artık gerçekten okunuyor. write_meta=False ise meta dosyası
    # hiç yazılmaz (sadece symbols_current.json yazılır). meta_file verilmişse
    # symbols_current_meta.json'a EK OLARAK o isimle de (geriye uyum için,
    # örn. eski "symbols_top70_meta.json" okuyan araçlar için) yazılır.
    wcfg = (cfg or {}).get("weekly_symbol_rotation", {}) or {}
    write_meta = bool(wcfg.get("write_meta", True))
    meta_file_alias = wcfg.get("meta_file")
    write_history_flag = bool(wcfg.get("write_history", True))

    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "symbols_current.json").write_text(
        json.dumps(result["selected"], ensure_ascii=False, indent=2), encoding="utf-8")
    if not write_meta:
        return
    meta = {
        "mode": mode,
        "as_of_ms": as_of_ms,
        "generated_at": time.time(),
        "source": result.get("source", "unknown"),
        "data_quality_ok": result.get("data_quality_ok", True),
        "core_fallback_used": result.get("core_fallback_used", False),
        "candidate_count": result.get("candidate_count", 0),
        "stable_filtered_count": result.get("stable_filtered_count", 0),
        "problematic_filtered_count": result.get("problematic_filtered_count", 0),
        "historical_unavailable_count": result.get("historical_unavailable_count", 0),
        "zero_candle_count": result.get("zero_candle_count", 0),
        "insufficient_history_count": result.get("insufficient_history_count", 0),
        "selected_count": len(result["selected"]),
        "selected": result["selected"],
        "selected_rows": result.get("selected_rows", []),
        "dropped_symbols_with_reason": result.get("dropped_symbols_with_reason", {}),
    }
    (d / "symbols_current_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if meta_file_alias:
        try:
            (d / str(meta_file_alias)).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    if write_history_flag:
        _append_universe_event(d / "universe_events.csv", mode, as_of_ms, result)


def _append_universe_event(path: Path, mode: str, as_of_ms, result: Dict[str, Any]):
    import csv
    new = not path.exists()
    try:
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["ts", "mode", "as_of_ms", "candidate_count", "stable_filtered_count",
                            "problematic_filtered_count", "zero_candle_count", "insufficient_history_count",
                            "selected_count", "data_quality_ok", "core_fallback_used", "source",
                            "selected", "dropped_sample"])
            dropped_sample = ";".join(
                f"{k}:{v}" for k, v in list(result.get("dropped_symbols_with_reason", {}).items())[:10])
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), mode, as_of_ms,
                result.get("candidate_count", 0), result.get("stable_filtered_count", 0),
                result.get("problematic_filtered_count", 0), result.get("zero_candle_count", 0),
                result.get("insufficient_history_count", 0), result.get("selected_count", 0),
                result.get("data_quality_ok", True), result.get("core_fallback_used", False),
                result.get("source", ""), ",".join(result["selected"]), dropped_sample,
            ])
    except Exception:
        pass


# ───────────────────────── Geriye uyum (eski API isimleri) ─────────────────
def load_current_symbols(top: int = 20, path: str = "symbols_top120.json") -> List[str]:
    """Eski weekly_symbol_universe.load_current_symbols ile uyumlu yardimci."""
    return load_candidate_pool_file(path, top)


def select_universe_for_window(candidate_symbols: List[str], candles_by_sym: Dict[str, list],
                                top: int = 20, out_meta: Optional[str] = None,
                                as_of_ms: Optional[int] = None) -> List[str]:
    """
    GERIYE UYUM: walk_forward.py / true_walk_forward.py / robustness_test.py
    bu imzayi kullaniyor. KRITIK FIX: artik "selected veya ham candidate"
    fallback'i YOK — select_universe() icindeki guvenli CORE_FALLBACK kullanilir.
    """
    pre_filtered = [s for s in candidate_symbols if not is_excluded_symbol(s)]
    result = select_universe(pre_filtered, candles_by_sym, top=top)
    selected = result["selected"]  # select_universe zaten CORE_FALLBACK garantisi veriyor
    if out_meta:
        payload = {"as_of_ms": as_of_ms, "generated_at": int(time.time()), "top": top,
                   "selected": selected, "data_quality_ok": result["data_quality_ok"],
                   "core_fallback_used": result["core_fallback_used"], "source": result["source"],
                   "dropped_symbols_with_reason": result.get("dropped_symbols_with_reason", {}),
                   "rows": result.get("selected_rows", []) + result.get("rejected_rows", [])}
        Path(out_meta).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return selected


def write_universe_history(path: str, period_label: str, symbols: List[str], meta: Dict[str, Any] = None):
    """GERIYE UYUM: weekly_symbol_universe.write_universe_history ile ayni imza."""
    import csv
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["period", "symbols", "meta_json"])
        w.writerow([period_label, ",".join(symbols), json.dumps(meta or {}, ensure_ascii=False)])


# ───────────────────────── CLI: symbols_top120.json üretici ────────────────
def build_candidate_pool_file(n: int = 120, outfile: str = "symbols_top120.json") -> List[str]:
    """
    V7 build_top_usdt() esdegeri — CANLI Binance verisiyle candidate_top
    boyutunda bir aday havuzu dosyasi uretir (backtest_candidate_file icin).
    AG GEREKTIRIR. Ag yoksa bos liste doner ve dosya yazilmaz (mevcut dosyaya
    dokunulmaz) — boylece bir agsiz calistirma eldeki dosyayi BOZMAZ.
    """
    candidates = fetch_live_spot_usdt_candidates(n)
    if not candidates:
        print(f"[universe_manager] AG YOK/HATA — {outfile} ÜRETİLEMEDİ (mevcut dosyaya dokunulmadı).")
        return []
    Path(outfile).write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[universe_manager] {len(candidates)} sembol yazildi -> {outfile}")
    return candidates


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    out = sys.argv[2] if len(sys.argv) > 2 else "symbols_top120.json"
    build_candidate_pool_file(n, out)
