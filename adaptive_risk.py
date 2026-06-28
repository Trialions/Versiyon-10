# adaptive_risk.py — V8.5.2 sizing-hint only risk report
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Mapping, Any

@dataclass(frozen=True)
class AdaptiveRiskReport:
    enabled: bool
    risk_mult: float
    reason: str
    mode: str = "sizing_hint_only"
    def to_dict(self): return asdict(self)


def _f(v, d=0.0):
    try: return float(v if v is not None else d)
    except Exception: return d


def compute_adaptive_risk_hint(result: Mapping[str, Any], regime: str, quality_score: float, cfg: Mapping[str, Any]) -> AdaptiveRiskReport:
    rcfg = (cfg or {}).get("adaptive_risk", {}) or {}
    enabled = bool(rcfg.get("enabled", True))
    mode = str(rcfg.get("mode", "sizing_hint_only"))
    if not enabled:
        return AdaptiveRiskReport(False, 1.0, "adaptive_risk_disabled", mode)
    comp = dict((result or {}).get("components", {}) or {})
    adx = _f(comp.get("adx"), 0.0)
    atr = _f(comp.get("atr_pct"), 0.0)
    rsi = _f(comp.get("rsi"), 50.0)
    regime_u = str(regime or "NEUTRAL").upper()
    mult = 1.0
    reasons = []
    if quality_score < 45: mult *= 0.70; reasons.append("quality<45")
    elif quality_score > 75: mult *= 1.05; reasons.append("quality>75")
    if regime_u in ("KONSOL", "CHOP", "RANGE"): mult *= 0.80; reasons.append("chop")
    if regime_u in ("BEAR", "BEARISH", "RISK_OFF"): mult *= 0.60; reasons.append("risk_off")
    if atr > 5.0: mult *= 0.70; reasons.append("atr_high")
    elif 0 < atr < 0.7: mult *= 0.85; reasons.append("atr_low")
    if adx >= 35 and 48 <= rsi <= 68: mult *= 1.05; reasons.append("trend_quality")
    floor = _f(rcfg.get("min_mult"), 0.35)
    cap = _f(rcfg.get("max_mult"), 1.10)
    mult = max(floor, min(cap, mult))
    return AdaptiveRiskReport(True, round(mult, 3), ";".join(reasons) or "neutral", mode)
