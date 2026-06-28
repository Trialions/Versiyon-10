# branches/short_surgeon.py — SHORT MOTORU (V9.0 bagimsiz motor)
#
# V9.0 DEGISIKLIK: Eskiden short kararinin TEK kaynagi SL_DOGRU (post-hoc SL
# dogrulama) idi. Artik gercek bagimsiz bir zayiflik/risk analizi yapiliyor:
# weakness_helper (RSI/MACD/Bollinger/Supertrend kirilim sinyalleri),
# btc_risk_helper, funding_pressure_helper (derivatives), taker_sell_helper.
# SL_DOGRU ve BTC_RISK_OFF artik ANA tetikleyici degil, EK TEYIT/confidence
# boost olarak kullaniliyor.
from __future__ import annotations

import time
from typing import Dict, Optional
from modules.decision_packet import Action, BranchVote, Side


# ───────────────────────── Helper'lar ──────────────────────────────────────
def weakness_helper(components: dict) -> float:
    """
    Fiyat zayıflığı / trend kırılımı / rejection / momentum kaybı.
    RSI yüksekten dönüş, MACD negatife geçiş, Bollinger üst-bandı reddi,
    Supertrend down-flip, trend yapısı zayıflıyor (trend skoru düşük),
    Çift Tepe/breakdown formasyonu (confirmation, tek başına tetiklemez).
    """
    rsi = float(components.get("rsi", 50.0))
    macd = float(components.get("macd", 50.0))
    bb = float(components.get("bollinger", 50.0))
    trend = float(components.get("trend", 50.0))
    supertrend = float(components.get("supertrend", 0.0))
    divergence = float(components.get("divergence", 0.0))
    patterns = float(components.get("patterns", 0.0))

    weak = 0.0
    if rsi > 68:
        weak += (rsi - 68) * 1.2          # overbought -> short fırsatı
    if macd < 45:
        weak += (45 - macd) * 0.6
    if bb > 75:
        weak += (bb - 75) * 0.5           # üst banda yakın -> rejection riski
    if trend < 40:
        weak += (40 - trend) * 0.5
    if supertrend < 0:
        weak += 15.0
    if divergence < 0:                    # negatif RSI uyuşmazlığı
        weak += 10.0
    if patterns < 0:                      # Çift Tepe / bearish formasyon (strategy_core._patterns)
        weak += min(8.0, abs(patterns) * 0.8)
    return max(0.0, min(100.0, weak))


def btc_risk_helper(btc_prices: Optional[list], drop_candles: int, drop_pct: float, boost: float = 20.0) -> dict:
    if not btc_prices or len(btc_prices) < drop_candles + 1:
        return {"boost": 0.0, "note": "btc_no_data"}
    start, end = btc_prices[-(drop_candles + 1)], btc_prices[-1]
    chg = (end - start) / start * 100 if start > 0 else 0.0
    if chg <= -drop_pct:
        return {"boost": boost, "note": f"btc_risk_off drop={chg:.2f}%"}
    return {"boost": 0.0, "note": f"btc_ok chg={chg:.2f}%"}


def funding_pressure_helper(deriv_ctx: Optional[dict]) -> dict:
    """Funding aşırı pozitif -> aşağı yönlü fırsat ihtimali (short için bonus)."""
    if not deriv_ctx or not deriv_ctx.get("available"):
        return {"adj": 0.0, "used": False, "note": "derivatives_unavailable"}
    bias = deriv_ctx.get("funding_bias")
    if bias == "EXTREME_POS":
        return {"adj": 12.0, "used": True, "note": "funding_extreme_pos_short_opportunity"}
    if bias == "EXTREME_NEG":
        return {"adj": -8.0, "used": True, "note": "funding_extreme_neg_avoid_short"}
    return {"adj": 0.0, "used": True, "note": "funding_neutral"}


def taker_sell_helper(deriv_ctx: Optional[dict]) -> dict:
    """OI artarken fiyat yükselmekte zorlanıyor + taker_sell dominance -> short teyidi."""
    if not deriv_ctx or not deriv_ctx.get("available"):
        return {"adj": 0.0, "used": False, "note": "derivatives_unavailable"}
    taker_ratio = deriv_ctx.get("taker_buy_ratio")
    oi_chg = deriv_ctx.get("oi_chg_pct")
    if taker_ratio is None:
        return {"adj": 0.0, "used": False, "note": "no_taker_data"}
    adj = 0.0
    note = []
    if taker_ratio < 0.42:
        adj += 8.0
        note.append(f"taker_sell_dominant={1-taker_ratio:.2f}")
    if oi_chg is not None and oi_chg > 3.0 and taker_ratio < 0.48:
        adj += 6.0
        note.append("oi_up_price_struggle")
    return {"adj": adj, "used": True, "note": ";".join(note) or "taker_neutral"}


def funding_oi_confirm_helper(deriv_ctx: Optional[dict], foi_cfg: dict) -> dict:
    """Funding aşırı pozitif TEK BAŞINA yeterli bir long-squeeze teyidi değildir;
    OI'nin de (yeni kaldıraçlı long birikimi) artıyor olması gerekir. Bu fonksiyon
    short_score'a SINIRLI bir confirmation boost'u verir, tek başına OPEN üretmez.
    Config'te 'enabled' yoksa/false ise davranış tamamen nötrdür (adj=0) — eski
    davranış (funding_pressure_helper tek başına) değişmez.
    """
    foi_cfg = foi_cfg or {}
    if not bool(foi_cfg.get("enabled", False)):
        return {"adj": 0.0, "note": "funding_oi_confirm_disabled"}
    if not deriv_ctx or not deriv_ctx.get("available"):
        return {"adj": 0.0, "note": "derivatives_unavailable"}

    require_both = bool(foi_cfg.get("require_both", True))
    min_oi_chg = float(foi_cfg.get("min_oi_change_pct", 2.0))
    min_funding = float(foi_cfg.get("positive_funding_bias_min", 0.01))
    missing_policy = str(foi_cfg.get("missing_oi_policy", "neutral")).lower()

    funding_rate = deriv_ctx.get("funding_rate")
    oi_chg = deriv_ctx.get("oi_chg_pct")

    if funding_rate is None or funding_rate < min_funding:
        return {"adj": 0.0, "note": "funding_not_positive_enough"}

    if oi_chg is None:
        if missing_policy == "ignore":
            return {"adj": 6.0, "note": "OI_MISSING_IGNORED_FUNDING_ONLY"}
        if missing_policy == "block":
            return {"adj": 0.0, "note": "OI_MISSING_CONFIRM_FAILED"}
        return {"adj": 0.0, "note": "OI_MISSING"}  # missing_oi_policy=neutral (varsayılan)

    if require_both and oi_chg < min_oi_chg:
        return {"adj": 0.0, "note": f"OI_NOT_RISING oi_chg={oi_chg:.1f}%"}

    return {"adj": 8.0, "note": f"funding_oi_confirm funding={funding_rate:.4f} oi_chg={oi_chg:.1f}%"}


class ShortSurgeon:
    """SHORT MOTORU — bağımsız weakness/funding/taker analizi + SL_DOGRU ek teyit."""

    NAME = "short_surgeon"

    def __init__(self, cfg: dict):
        ss  = cfg.get("short_surgeon", {})
        self.enabled = bool(ss.get("enabled", True))
        self.shadow  = bool(ss.get("shadow_mode", True))

        self.weakness_min_score = float(ss.get("weakness_min_score", 55.0))
        self.weakness_min_confidence = float(ss.get("weakness_min_confidence", 0.35))

        modes = ss.get("modes", {})
        sl_d  = modes.get("sl_dogru_short", {}) or ss.get("sl_dogru", {})
        self.sld_enabled      = bool(sl_d.get("enabled", True))
        self.sld_lookback_hrs = float(sl_d.get("max_hold_hours", sl_d.get("lookback_hours", 18)))
        self.sld_min_drop     = float(sl_d.get("min_drop_4h_pct", 1.0))
        self.sld_size_mult    = float(sl_d.get("size_mult", 0.5))

        bro = modes.get("btc_risk_off_short", {}) or ss.get("btc_risk_off", {})
        self.bro_enabled   = bool(bro.get("enabled", False))
        self.bro_drop_pct  = float(bro.get("btc_drop_pct", 2.0))
        self.bro_candles   = int(bro.get("lookback_candles", 3))
        self.bro_size_mult = float(bro.get("size_mult", bro.get("short_size_mult", 0.5)))
        # V9 FIX: boost önceden kodda sabit 20.0 idi (config'ten okunmuyordu).
        # Artık config-driven; varsayılan 12.0'a çekildi (weak_s tipik aralığı
        # 0-30 olduğundan +20 BTC'nin tek başına eşiği aşırması riski taşıyordu).
        self.bro_boost     = float(bro.get("boost", 12.0))

        # symbol -> {ts, verdict, chg_4h}
        self._sl_records: Dict[str, dict] = {}

        # funding+OI birlikte teyit (BUG/öneri: funding tek başına yeterli
        # değil). Config'te yoksa enabled=False -> davranış değişmez.
        self.funding_oi_cfg = ss.get("funding_oi_confirm", {}) or {}

    # ── Besleme: engine/backtest SL kapanışını bildirir (EK TEYİT kaynağı) ──
    def record_sl(self, symbol: str, verdict: str, ts: float, chg_4h: float = 0.0):
        self._sl_records[symbol] = {"ts": float(ts), "verdict": verdict, "chg_4h": float(chg_4h)}

    def get_sl_records(self) -> dict:
        return dict(self._sl_records)

    # ── Oy ───────────────────────────────────────────────────────────
    def vote(self, symbol, score, result, regime, btc_prices=None, now=None,
             derivatives_ctx=None) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")

        now = now if now is not None else time.time()
        components = (result or {}).get("components", {}) or {}

        # ── Bağımsız zayıflık analizi (ANA tetikleyici) ─────────────
        weak_s = weakness_helper(components)
        btc_r  = btc_risk_helper(btc_prices, self.bro_candles, self.bro_drop_pct, self.bro_boost) \
            if self.bro_enabled else {"boost": 0.0, "note": "btc_risk_off_disabled"}
        fund_r = funding_pressure_helper(derivatives_ctx)
        sell_r = taker_sell_helper(derivatives_ctx)
        foi_r  = funding_oi_confirm_helper(derivatives_ctx, self.funding_oi_cfg)

        # SL_DOGRU bilgisi varsa EK TEYİT (skor artırır, tek kaynak değildir)
        sld_boost, sld_note = self._sl_dogru_confirmation(symbol, now)

        short_score = weak_s + btc_r["boost"] + fund_r["adj"] + sell_r["adj"] + sld_boost + foi_r["adj"]
        short_score = max(0.0, min(100.0, short_score))

        confidence = min(1.0, short_score / 100.0 + 0.10)
        derivatives_used = bool(fund_r["used"] or sell_r["used"] or foi_r["adj"] != 0.0)

        if short_score < self.weakness_min_score or confidence < self.weakness_min_confidence:
            return self._block(
                f"NO_SHORT_SIGNAL weak={weak_s:.1f} short_score={short_score:.1f} "
                f"thr={self.weakness_min_score:.1f} btc={btc_r['note']} fund={fund_r['note']} "
                f"sell={sell_r['note']} sld={sld_note} foi={foi_r['note']}")

        setup_type = "MOMENTUM_BREAKDOWN" if weak_s >= 60 else "FUNDING_PRESSURE_SHORT"
        size_mult = 0.5
        if sld_boost > 0:
            size_mult = max(size_mult, self.sld_size_mult)
        if btc_r["boost"] > 0:
            size_mult = max(size_mult, self.bro_size_mult)

        reason = (f"SHORT_OK weak={weak_s:.1f} short_score={short_score:.1f} "
                  f"btc={btc_r['note']} fund={fund_r['note']} sell={sell_r['note']} sld={sld_note} foi={foi_r['note']}")

        return BranchVote(
            branch_name=self.NAME, action=Action.OPEN, side=Side.SHORT,
            score=round(short_score, 2), confidence=round(confidence, 3), shadow=self.shadow,
            reason=reason,
            params={"sl_pct": 0.018, "size_mult": size_mult, "mode": setup_type},
            engine="SHORT", setup_type=setup_type,
            risk_mult=round(max(0.4, min(1.1, size_mult)), 3),
            sl_profile="TIGHT_REJECTION", tp_profile="FAST_SCALP",
            trail_profile="AGGRESSIVE_LOCK",
            open_intent=True, derivatives_used=derivatives_used,
            debug={"weakness": round(weak_s, 2), "btc_boost": btc_r["boost"],
                   "funding_adj": fund_r["adj"], "taker_sell_adj": sell_r["adj"],
                   "sld_boost": sld_boost, "funding_oi_confirm_adj": foi_r["adj"],
                   "funding_oi_confirm_note": foi_r["note"]},
        )

    def _sl_dogru_confirmation(self, symbol, now: float) -> tuple:
        """SL_DOGRU artık ana tetikleyici değil; mevcutsa skor BOOST eder."""
        if not self.sld_enabled:
            return 0.0, "sld_disabled"
        rec = self._sl_records.get(symbol)
        if not rec:
            return 0.0, "sld_no_record"
        elapsed_h = (now - rec["ts"]) / 3600
        if elapsed_h > self.sld_lookback_hrs:
            self._sl_records.pop(symbol, None)
            return 0.0, "sld_expired"
        if rec["verdict"] == "SL_DOGRU" and rec["chg_4h"] <= -(self.sld_min_drop / 100):
            boost = min(25.0, abs(rec["chg_4h"]) * 1000)
            return boost, f"sld_confirm chg4h={rec['chg_4h']:.3f}"
        return 0.0, "sld_no_confirm"

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason,
                           shadow=self.shadow, engine="SHORT", block_reason=reason)