# branches/core_long_branch.py — LONG MOTORU (V9.0 bagimsiz motor)
#
# V9.0 DEGISIKLIK: Bu dal artik disaridan gelen tek merkezi 'score'u esikle
# karsilastirip acip/acmiyan bir FILTRE degil. Ortak feature katmanindan
# (result["components"]: rsi, macd, bollinger, trend, adx, supertrend,
# divergence, atr_pct) KENDI agirliklandirmasiyla bagimsiz bir long_score
# uretir; ayrica HTF, BTC, hacim ve (varsa) derivatives verisini kendi
# helper'lariyla yorumlayip bu skoru ayarlar. Merkezi 'score' (strategy_core
# ciktisi) yalniz bir ON-FILTRE / sagliklilik kontrolu olarak kullanilir,
# tek karar kaynagi degildir.
from __future__ import annotations
from typing import Optional

from modules.decision_packet import Action, BranchVote, Side
import adaptive_sl


# ───────────────────────── Helper'lar ──────────────────────────────────────
def trend_helper(components: dict) -> float:
    """Trend/momentum devamı: trend yapısı + ADX + Supertrend + MACD histogramı."""
    trend = float(components.get("trend", 50.0))
    adx = float(components.get("adx", 0.0))
    supertrend = float(components.get("supertrend", 0.0))
    macd = float(components.get("macd", 50.0))
    score = trend * 0.45 + macd * 0.25
    score += min(20.0, adx * 0.5)        # ADX güçlüyse bonus
    score += supertrend                  # +20 / -20 / 0
    return max(0.0, min(100.0, score))


def htf_helper(htf_score: float, htf_gate_enabled: bool,
               block_min: float, penalty_min: float, boost_min: float) -> dict:
    """HTF (üst zaman dilimi) trend uyumu — confidence çarpanı + olası blok."""
    if not htf_gate_enabled:
        return {"block": False, "conf_mult": 1.0, "note": "htf=BYPASS"}
    if htf_score < block_min:
        return {"block": True, "conf_mult": 0.0, "note": f"htf_weak={htf_score:.1f}"}
    if htf_score < penalty_min:
        return {"block": False, "conf_mult": 0.70, "note": f"htf_soft={htf_score:.1f}"}
    if htf_score >= boost_min:
        return {"block": False, "conf_mult": 1.12, "note": f"htf_strong={htf_score:.1f}"}
    return {"block": False, "conf_mult": 1.0, "note": f"htf_ok={htf_score:.1f}"}


def btc_helper(btc_prices: Optional[list], drop_candles: int = 3, drop_pct: float = 2.0) -> dict:
    """BTC genel yönü long için risk taşıyor mu (kalabalık piyasa riski)."""
    if not btc_prices or len(btc_prices) < drop_candles + 1:
        return {"penalty": 0.0, "note": "btc_no_data"}
    start, end = btc_prices[-(drop_candles + 1)], btc_prices[-1]
    chg = (end - start) / start * 100 if start > 0 else 0.0
    if chg <= -drop_pct:
        return {"penalty": 15.0, "note": f"btc_drop={chg:.2f}%"}
    if chg >= drop_pct:
        return {"penalty": -5.0, "note": f"btc_rally={chg:.2f}%"}  # negatif = bonus
    return {"penalty": 0.0, "note": f"btc_flat={chg:.2f}%"}


def volume_helper(components: dict) -> float:
    """Hacim desteği — trend devamına eşlik eden hacim artışı bonus verir."""
    vol_score = float(components.get("volume", 50.0))
    return (vol_score - 50.0) * 0.3  # -15..+15 aralığında etki


def derivatives_helper(deriv_ctx: Optional[dict]) -> dict:
    """
    Funding/OI/taker verisini LONG perspektifinden yorumlar.
    Funding aşırı pozitif -> kalabalık long riski (skoru düşür, risk azalt).
    Funding aşırı negatif -> short squeeze fırsatı (skoru hafif artır).
    OI artışı + taker_buy dominance -> trend devamı teyidi.
    """
    if not deriv_ctx or not deriv_ctx.get("available"):
        return {"adj": 0.0, "risk_mult": 1.0, "used": False, "note": "derivatives_unavailable"}

    adj = 0.0
    risk_mult = 1.0
    notes = []
    bias = deriv_ctx.get("funding_bias")
    if bias == "EXTREME_POS":
        adj -= 8.0
        risk_mult *= 0.80
        notes.append("funding_extreme_pos_crowded_long")
    elif bias == "EXTREME_NEG":
        adj += 5.0
        notes.append("funding_extreme_neg_squeeze_potential")

    oi_chg = deriv_ctx.get("oi_chg_pct")
    taker_ratio = deriv_ctx.get("taker_buy_ratio")
    if oi_chg is not None and taker_ratio is not None:
        if oi_chg > 3.0 and taker_ratio > 0.55:
            adj += 6.0
            notes.append(f"oi_up_taker_buy_dom oi_chg={oi_chg:.1f}% taker_buy={taker_ratio:.2f}")
        elif oi_chg > 3.0 and taker_ratio < 0.45:
            adj -= 6.0
            notes.append("oi_up_taker_sell_dom_divergence")

    return {"adj": adj, "risk_mult": max(0.5, min(1.2, risk_mult)),
            "used": True, "note": ";".join(notes) or "derivatives_neutral"}


class CoreLongBranch:
    """LONG MOTORU — bağımsız analiz + tam EngineReport (BranchVote) üretir."""

    NAME = "core_long"

    def __init__(self, cfg: dict):
        self.cfg     = cfg
        thr          = cfg.get("thresholds", {})
        risk         = cfg.get("risk", {})
        cl           = cfg.get("core_long", {})
        self.enabled = bool(cl.get("enabled", True))
        self.shadow  = bool(cl.get("shadow_mode", False))
        self.thr_long  = float(thr.get("score_long_open", 97.0))
        mtf = cfg.get("mtf", {})
        self.htf_gate_enabled = bool(mtf.get("enabled", True)) and bool(cl.get("htf_gate_enabled", True))
        self.htf_block_min = float(cl.get("htf_block_min", 55.0))
        self.htf_penalty_min = float(cl.get("htf_penalty_min", 70.0))
        self.htf_boost_min = float(cl.get("htf_boost_min", 85.0))
        self.sl_pct    = float(risk.get("hard_stop_pct", 1.5)) / 100
        self.atr_mult  = float(risk.get("atr_multiplier", 2.0))
        self.trail     = float(risk.get("trailing_step_pct", 0.7)) / 100
        # V9.0.1: merkezi skor SADECE bir ön-sağlık-filtresi (gate) — nihai
        # skora karışmaz (bkz. own_score_weight altı).
        self.central_score_min_gate = float(cl.get("central_score_min_gate", 50.0))
        # V9.0.1 FIX: merkezi skor (strategy_core.score_symbol) artık LONG
        # motorunun skoruna KARIŞMIYOR. own_score_weight varsayılanı 1.0 —
        # yani nihai skor TAMAMEN bağımsız trend/htf/btc/volume/derivatives
        # analizinden gelir. Merkezi skor SADECE central_score_min_gate ile
        # bir ön-sağlık-filtresi (gate) olarak kullanılır, ağırlıklı harmana
        # girmez. Config'te own_score_weight < 1.0 yapılırsa (deneysel/A-B
        # amaçlı) eski harman davranışına dönülebilir, ama varsayılan KATI.
        self.own_score_weight = float(cl.get("own_score_weight", 1.0))

        # V9 FIX: own_score (trend_helper+volume_helper bileşimi) rejimden
        # tamamen bağımsız hesaplanıyor — KONSOL'da bile ortalama ~94 çıkıyor
        # ve kazanan/kaybeden trade'i ayırt edemiyor (89 günlük gerçek backtest
        # kanıtı: KONSOL'da own_score TP grubunda 94.95, SL grubunda 94.71 —
        # pratik olarak aynı). trend_helper/volume_helper'ın KENDİSİNE
        # dokunulmuyor; bunun yerine eşik karşılaştırmasından ÖNCE, sadece
        # regime_score_penalty.enabled=true ise, ek/isteğe bağlı bir ceza
        # katmanı uygulanır. Varsayılan KAPALI — mevcut referans sonuçları
        # bu fix olmadan üretildi, geriye dönük davranışı bozmaz.
        rp = cl.get("regime_score_penalty", {}) or {}
        self.regime_penalty_enabled = bool(rp.get("enabled", False))
        self.regime_penalty_konsol  = float(rp.get("konsol", 8.0))
        self.regime_penalty_neutral = float(rp.get("neutral", 0.0))
        self.regime_penalty_bearish = float(rp.get("bearish", 0.0))

    def vote(self, symbol, score, result, regime, htf_score, sentiment,
             btc_prices=None, derivatives_ctx=None) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")

        components = (result or {}).get("components", {}) or {}
        atr_pct = components.get("atr_pct", 0.0)
        adsl    = adaptive_sl.compute(
            regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.thr_long,
            base_atr_multiplier=self.atr_mult,
            base_trail_step=self.trail, cfg=self.cfg,
        )
        sl_pct  = adsl["sl_pct"]

        if sentiment == "BEARISH" or regime == "BEARISH":
            return self._block(f"REGIME_BEARISH_NO_LONG regime={regime}")

        # Merkezi skor: sadece sağlık ön-filtresi (paylaşılan veri, tek karar değil)
        if score < self.central_score_min_gate:
            return self._block(f"CENTRAL_SCORE_TOO_LOW score={score:.1f}")

        # ── Bağımsız analiz ──────────────────────────────────────────
        trend_s = trend_helper(components)
        vol_adj = volume_helper(components)
        btc_r   = btc_helper(btc_prices)
        deriv_r = derivatives_helper(derivatives_ctx)

        own_score = max(0.0, min(100.0, trend_s + vol_adj - btc_r["penalty"] + deriv_r["adj"]))
        # V9.0.1: own_score_weight=1.0 (varsayılan) ise final_long_score == own_score
        # birebir; merkezi 'score' hiçbir şekilde nihai skora karışmaz.
        final_long_score = own_score * self.own_score_weight + score * (1 - self.own_score_weight)

        # V9 FIX: own_score rejimden bağımsız (KONSOL'da bile yüksek çıkıyor,
        # kazanan/kaybeden ayırt edemiyor). trend_helper/volume_helper'a
        # dokunmadan, sadece eşik kontrolünden ÖNCE, isteğe bağlı/config-driven
        # bir ceza uygulanır. regime_score_penalty.enabled=false (varsayılan)
        # ise hiçbir şey değişmez — bu blok no-op'tur.
        regime_penalty = 0.0
        if self.regime_penalty_enabled:
            if regime == "KONSOL":
                regime_penalty = self.regime_penalty_konsol
            elif regime == "NEUTRAL":
                regime_penalty = self.regime_penalty_neutral
            elif regime == "BEARISH":
                regime_penalty = self.regime_penalty_bearish
            if regime_penalty > 0:
                final_long_score = max(0.0, final_long_score - regime_penalty)

        eff_thr = adsl["score_threshold"]
        if final_long_score < eff_thr:
            return self._block(
                f"LONG_SCORE_BELOW_THR own={own_score:.1f} final={final_long_score:.1f} "
                f"(central_score={score:.1f} sadece gate, own_score_weight={self.own_score_weight}) "
                f"regime_penalty={regime_penalty:.1f} thr={eff_thr:.1f}")

        htf_r = htf_helper(htf_score, self.htf_gate_enabled,
                            self.htf_block_min, self.htf_penalty_min, self.htf_boost_min)
        if htf_r["block"]:
            return self._block(f"HTF_WEAK {htf_r['note']}")

        confidence = min(1.0, (final_long_score - eff_thr) / max(100 - eff_thr, 1) + 0.3)
        confidence *= htf_r["conf_mult"]
        confidence = max(0.0, min(1.0, confidence))

        # Setup tipi: trend gücüne göre ayrışım (Adaptive Exit bu alanı okuyacak)
        setup_type = "TREND_CONTINUATION" if trend_s >= 65 else "RANGE_BOUNCE"
        sl_profile = "ATR_TREND" if setup_type == "TREND_CONTINUATION" else "ATR_TIGHT"
        tp_profile = "WIDE_TREND" if setup_type == "TREND_CONTINUATION" else "STANDARD"
        trail_profile = "LATE_TREND_FOLLOW" if setup_type == "TREND_CONTINUATION" else "STANDARD"

        reason = (f"CORE_LONG_OK regime={regime} own_score={own_score:.1f} "
                  f"final_long_score={final_long_score:.1f} central_score_gate_only={score:.1f} "
                  f"regime_penalty={regime_penalty:.1f} "
                  f"{htf_r['note']} btc={btc_r['note']} deriv={deriv_r['note']}")

        return BranchVote(
            branch_name=self.NAME, action=Action.OPEN, side=Side.LONG,
            score=round(final_long_score, 2), confidence=round(confidence, 3), shadow=self.shadow,
            reason=reason,
            params={"sl_pct": sl_pct, "size_mult": 1.0, "trail_step": adsl["trail_step"]},
            engine="LONG", setup_type=setup_type,
            risk_mult=round(deriv_r["risk_mult"], 3),
            sl_profile=sl_profile, tp_profile=tp_profile, trail_profile=trail_profile,
            open_intent=True, derivatives_used=deriv_r["used"],
            debug={"trend_helper": round(trend_s, 2), "vol_adj": round(vol_adj, 2),
                   "btc_penalty": btc_r["penalty"], "deriv_adj": deriv_r["adj"],
                   "own_score": round(own_score, 2), "central_score_gate_only": round(score, 2),
                   "own_score_weight": self.own_score_weight, "regime_penalty": regime_penalty},
        )

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason,
                           shadow=self.shadow, engine="LONG", block_reason=reason)
