# modules/convex_position.py — Sadece kazanana pyramid (SIDE-AWARE)
# v8.1: long/short ayrimi eklendi. Short'ta kar = (entry - price)/entry.
from __future__ import annotations
from typing import Dict, Optional


class ConvexPosition:
    def __init__(self, cfg: dict):
        cp = cfg.get("convex_position", {})
        self.enabled        = bool(cp.get("enabled", False))
        self.min_profit_pct = float(cp.get("min_profit_pct_to_add", cp.get("add_1_at_r", 1.0)))
        self.max_levels     = int(cp.get("max_pyramid_levels", cp.get("max_pyramid_adds", 2)))
        self.size_mult      = float(cp.get("pyramid_size_mult", cp.get("add_1_size_mult", 0.5)))
        self.min_r          = float(cp.get("min_r_value", 1.0))
        self.never_if_losing = bool(cp.get("never_add_if_losing", True))
        self._state: Dict[str, dict] = {}

    def on_open(self, symbol, side, entry, sl_pct):
        self._state[symbol] = {"side": side, "entry": entry, "sl_pct": sl_pct, "level": 0, "adds": []}

    def on_close(self, symbol):
        self._state.pop(symbol, None)

    def check_add(self, symbol, price) -> Optional[float]:
        """Pyramid ek lot carpani veya None. Long/short uyumlu."""
        if not self.enabled:
            return None
        s = self._state.get(symbol)
        if not s or s["level"] >= self.max_levels:
            return None
        entry, sl_pct, side = s["entry"], s["sl_pct"], s["side"]
        # SIDE-AWARE kar hesabi
        if side == "SHORT":
            change = (entry - price) / entry
        else:
            change = (price - entry) / entry
        if self.never_if_losing and change <= 0:
            return None
        r_value = change / sl_pct if sl_pct > 0 else 0
        if change >= (self.min_profit_pct / 100 if self.min_profit_pct > 1 else self.min_profit_pct) \
           and r_value >= self.min_r:
            s["level"] += 1
            s["adds"].append(price)
            return max(0.1, self.size_mult * (0.5 ** (s["level"] - 1)))
        return None

    def get_state(self, symbol):
        return self._state.get(symbol, {})
