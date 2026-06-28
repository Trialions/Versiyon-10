# adaptive_sl.py — Rejim bazli adaptif Stop-Loss hesaplayici
# V7 ile tam uyumlu, V8 config yapisiyla genisletildi.
from __future__ import annotations


def compute(regime: str,
            atr_pct: float,
            base_score_threshold: float,
            base_atr_multiplier: float,
            base_trail_step: float,
            cfg: dict = None) -> dict:
    """
    Piyasa rejimine gore SL, trail ve esik degerlerini hesaplar.

    Donus: {
        "score_threshold": float,
        "sl_pct":          float,
        "trail_step":      float,
        "regime":          str,
    }
    """
    cfg = cfg or {}
    asl = cfg.get("adaptive_sl", {})

    # Rejim bazli ATR carpanlari
    regime_upper = regime.upper() if regime else "NEUTRAL"

    if regime_upper == "TREND":
        atr_mult   = float(asl.get("trend_atr_mult",   base_atr_multiplier))
        trail_mult = float(asl.get("trend_trail_mult",  1.0))
        score_thr  = base_score_threshold
    elif regime_upper == "KONSOL":
        atr_mult   = float(asl.get("konsol_atr_mult",  1.5))
        trail_mult = float(asl.get("konsol_trail_mult", 0.7))
        score_thr  = base_score_threshold + 2.0  # Daha katı esik
    elif regime_upper == "BEARISH":
        atr_mult   = float(asl.get("bearish_atr_mult", 1.2))
        trail_mult = 0.5
        score_thr  = 999.0  # Bearishde giris yok
    else:  # NEUTRAL
        atr_mult   = base_atr_multiplier
        trail_mult = 1.0
        score_thr  = base_score_threshold

    # Dynamic trail cfg
    dt = cfg.get("dynamic_trail", {})
    dt_enabled = bool(dt.get("enabled", True))
    dt_min     = float(dt.get("min_pct",   0.5)) / 100
    dt_max     = float(dt.get("max_pct",   2.5)) / 100
    dt_atr_m   = float(dt.get("atr_mult",  0.5))

    # SL hesapla
    if atr_pct > 0:
        raw_sl = (atr_pct / 100) * atr_mult
    else:
        raw_sl = 0.015  # Fallback: %1.5

    max_sl = float(cfg.get("risk", {}).get("max_stop_pct", 4.5)) / 100
    sl_pct = max(0.005, min(max_sl, raw_sl))

    # Trail hesapla
    if dt_enabled and atr_pct > 0:
        raw_trail = (atr_pct / 100) * dt_atr_m * trail_mult
        trail_step = max(dt_min, min(dt_max, raw_trail))
    else:
        trail_step = base_trail_step * trail_mult

    return {
        "score_threshold": round(score_thr, 2),
        "sl_pct":          round(sl_pct, 4),
        "trail_step":      round(trail_step, 4),
        "regime":          regime_upper,
    }
