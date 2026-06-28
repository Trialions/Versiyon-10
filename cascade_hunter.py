# branches/cascade_hunter.py — FIRSAT MOTORU (V9.0)
# Sikisma + konveksite skoru + derivatives-aware cascade/reversal tespiti.
from __future__ import annotations

import numpy as np
from typing import List, Optional
from modules.decision_packet import Action, BranchVote, Side


def derivatives_opportunity_helper(deriv_ctx: Optional[dict]) -> dict:
    """
    Funding asiri durumlar + OI hizli dususu (piyasa temizlenmesi) fırsat
    motoru perspektifinden yorumlanir: cascade sonrasi donus ihtimali.
    """
    if not deriv_ctx or not deriv_ctx.get("available"):
        return {"adj": 0.0, "used": False, "note": "derivatives_unavailable"}
    adj = 0.0
    notes = []
    bias = deriv_ctx.get("funding_bias")
    if bias in ("EXTREME_POS", "EXTREME_NEG"):
        adj += 8.0
        notes.append(f"funding_extreme_{bias.lower()}_watch_for_reversal")
    oi_chg = deriv_ctx.get("oi_chg_pct")
    if oi_chg is not None and oi_chg <= -5.0:
        adj += 10.0  # OI hızlı düşüşü = piyasa temizlenmesi = fırsat sinyali
        notes.append(f"oi_reset oi_chg={oi_chg:.1f}%")
    cvd_chg = deriv_ctx.get("cvd_proxy_chg")
    if cvd_chg is not None and cvd_chg > 0:
        adj += 4.0
        notes.append("cvd_reversal_up")
    return {"adj": adj, "used": True, "note": ";".join(notes) or "derivatives_neutral"}


class CascadeHunter:
    NAME = "cascade_hunter"

    def __init__(self, cfg: dict):
        ch = cfg.get("cascade_hunter", {})
        self.enabled        = bool(ch.get("enabled", True))
        self.shadow         = bool(ch.get("shadow_mode", True))
        self.comp_bars      = int(ch.get("compression_lookback", ch.get("compression_bars", 10)))
        self.min_comp_bars  = int(ch.get("min_compression_bars", 8))
        self.comp_atr_mult  = float(ch.get("compression_atr_mult", 0.5))
        self.min_conv_score = float(ch.get("min_convexity_score", 60.0))
        self.breakout_vol   = float(ch.get("breakout_vol_mult", 1.5))

        self.price_volume_only = bool(ch.get("price_volume_only", True))
        ff = ch.get("futures_flow", {})
        self.futures_flow_en        = bool(ff.get("enabled", False)) and not self.price_volume_only
        self.futures_oi_change_thr  = float(ff.get("oi_change_threshold", 0.05))

        # Yön düzeltmesi (cascade_hunter SHORT yönü artık tek mum yerine
        # merkezi indikatör bileşenlerine bakıyor). Config'te yoksa True
        # (yeni, doğru davranış); eski davranışa dönmek için config'ten
        # use_component_direction: false yapılabilir.
        self.use_component_direction = bool(ch.get("use_component_direction", True))
        self.min_rsi_for_short = float(ch.get("min_rsi_for_short", 35.0))

    def vote(self, symbol, score, prices, highs, lows, volumes, result,
             futures_oi: Optional[List[float]] = None,
             derivatives_ctx: Optional[dict] = None) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")
        if len(prices) < self.comp_bars + 5:
            return self._block("INSUFFICIENT_DATA")

        compression = self._detect_compression(highs, lows, prices)
        conv_score  = self._convexity_score(prices, volumes)

        oi_note = ""
        if self.futures_flow_en and futures_oi and len(futures_oi) >= 2:
            oi_chg = (futures_oi[-1] - futures_oi[0]) / futures_oi[0] if futures_oi[0] else 0.0
            if abs(oi_chg) >= self.futures_oi_change_thr:
                conv_score = float(np.clip(conv_score + (15.0 if oi_chg > 0 else -15.0), 0, 100))
                oi_note = f" oi_chg={oi_chg:+.1%}"

        # V9.0: yeni derivatives katmanı (funding/OI/CVD) — shadow/log destekli,
        # config kapalıysa veya veri yoksa devre dışı, sistemi bozmaz.
        deriv_r = derivatives_opportunity_helper(derivatives_ctx)
        conv_score = float(np.clip(conv_score + deriv_r["adj"], 0, 100))

        if not compression:
            return self._block(f"NO_COMPRESSION conv={conv_score:.1f}{oi_note} deriv={deriv_r['note']}")
        if conv_score < self.min_conv_score:
            return self._block(f"LOW_CONVEXITY conv={conv_score:.1f}{oi_note} deriv={deriv_r['note']}")

        side = self._resolve_side(prices, result)
        return BranchVote(
            self.NAME, Action.OPEN, side, score=conv_score,
            confidence=round(min(1.0, conv_score / 100), 3), shadow=self.shadow,
            reason=f"CASCADE_OK conv={conv_score:.1f}{oi_note} deriv={deriv_r['note']}",
            params={"sl_pct": 0.020, "size_mult": 0.6, "mode": "CASCADE"},
            engine="OPPORTUNITY", setup_type="CASCADE_REVERSAL",
            risk_mult=0.6, sl_profile="CASCADE_TIGHT", tp_profile="FAST_TP",
            trail_profile="EARLY_BREAKEVEN", open_intent=True,
            derivatives_used=deriv_r["used"],
            debug={"compression": compression, "deriv_adj": deriv_r["adj"]},
        )

    def _resolve_side(self, prices, result) -> "Side":
        """Sıkışma yönünü belirler.

        Eski davranış: sadece son mum bir önceki mumdan yüksek/düşük diye karar
        veriyordu (tek mumluk, yöne dair gerçek bilgi taşımayan bir sinyal —
        V9/V10 backtest verisinde SHORT tarafında medyan hipotetik getiri ~0
        çıktı). Bu, merkezi strategy_core bileşenlerini (macd, trend yapısı,
        supertrend, formasyon, divergence — zaten hesaplanıp result["components"]
        içinde geliyor) çoklu-oy ile birleştirip yön tespiti yapar. Bileşenler
        yoksa veya oylar belirsiz/eşitse eski (son mum) davranışına düşer —
        geriye dönük davranış regresyonu yok.
        """
        if not self.use_component_direction:
            return Side.LONG if prices[-1] > prices[-2] else Side.SHORT
        comp = (result or {}).get("components", {}) or {}
        if not comp:
            return Side.LONG if prices[-1] > prices[-2] else Side.SHORT
        bearish = 0
        bullish = 0
        macd_c = float(comp.get("macd", 50.0) or 50.0)
        trend_c = float(comp.get("trend", 50.0) or 50.0)
        st_c = float(comp.get("supertrend", 0.0) or 0.0)
        pat_c = float(comp.get("patterns", 0.0) or 0.0)
        div_c = float(comp.get("divergence", 0.0) or 0.0)
        rsi_c = float(comp.get("rsi", 50.0) or 50.0)
        if macd_c < 48: bearish += 1
        elif macd_c > 52: bullish += 1
        if trend_c < 40: bearish += 1
        elif trend_c > 60: bullish += 1
        if st_c < 0: bearish += 1
        elif st_c > 0: bullish += 1
        if pat_c < 0: bearish += 1
        elif pat_c > 0: bullish += 1
        if div_c < 0: bearish += 1
        elif div_c > 0: bullish += 1
        # Zaten aşırı dipte bir sembolü short'lamayı engelle (short_surgeon'daki
        # aynı güvenlik mantığıyla tutarlı).
        if bearish > bullish and rsi_c >= self.min_rsi_for_short:
            return Side.SHORT
        if bullish > bearish:
            return Side.LONG
        return Side.LONG if prices[-1] > prices[-2] else Side.SHORT

    def _detect_compression(self, highs, lows, closes) -> bool:
        n = self.comp_bars
        if len(closes) < n + 5:
            return False
        seg_h, seg_l, seg_c = np.array(highs[-n:]), np.array(lows[-n:]), np.array(closes[-n:])
        prev_c = seg_c[:-1]
        tr = np.maximum(seg_h[1:] - seg_l[1:],
                        np.maximum(np.abs(seg_h[1:] - prev_c), np.abs(seg_l[1:] - prev_c)))
        atr_recent = float(np.mean(tr[-3:])) if len(tr) >= 3 else 0.0
        atr_old    = float(np.mean(tr[:3]))  if len(tr) >= 6 else atr_recent
        if atr_old <= 0:
            return False
        return (atr_recent / atr_old) < self.comp_atr_mult

    def _convexity_score(self, prices, volumes) -> float:
        score = 50.0
        if len(prices) >= 10:
            seg = prices[-10:]
            price_range = (max(seg) - min(seg)) / (min(seg) + 1e-9) * 100
            score += max(0, 25 - price_range * 5)
        if len(volumes) >= 10:
            recent_vol = np.mean(volumes[-3:]); old_vol = np.mean(volumes[-10:-3])
            if old_vol > 0:
                score += min(25, (recent_vol / old_vol - 1.0) * 20)
        return float(np.clip(score, 0, 100))

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason,
                           shadow=self.shadow, engine="OPPORTUNITY", block_reason=reason)
