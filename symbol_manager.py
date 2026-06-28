# symbol_manager.py — Sembol performans yoneticisi (V8.5)
from __future__ import annotations
import threading
from collections import defaultdict, deque


class SymbolManager:
    """
    Sembol davranış hafızası.
    V8.5: Daha agresif ama kontrollü size penalty.
    - Hard blacklist yapmaz.
    - Kötü davranan sembolde pozisyon boyutunu düşürür.
    - Manuel soft penalty listesini destekler.
    """

    def __init__(self, cfg: dict = None, starting_equity: float = 1000.0):
        self.cfg = cfg or {}
        sq = self.cfg.get("symbol_quality_filter", {})
        self.enabled = bool(sq.get("enabled", True))
        self.weak_mult = float(sq.get("weak_symbol_multiplier", 0.35))
        self.min_trades = int(sq.get("min_trades_for_penalty", 2))
        self.soft_loss_pct = float(sq.get("soft_loss_pct", -0.012))
        self.hard_loss_pct = float(sq.get("hard_loss_pct", -0.025))
        self.loss_streak_after = int(sq.get("loss_streak_penalty_after", 2))
        self.loss_streak_mult = float(sq.get("loss_streak_multiplier", 0.50))
        self.low_wr_after = int(sq.get("low_winrate_penalty_after_trades", 4))
        self.low_wr_threshold = float(sq.get("low_winrate_threshold", 0.35))
        self.low_wr_mult = float(sq.get("low_winrate_multiplier", 0.65))
        self.recovery_mult = float(sq.get("recovery_multiplier", 0.85))
        self.manual_mults = {str(k).upper(): float(v) for k, v in (sq.get("manual_symbol_multipliers") or {}).items()}

        self._lock = threading.RLock()
        self._equity = float(starting_equity or 1000.0)
        self._records = defaultdict(lambda: {
            "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
            "loss_streak": 0, "win_streak": 0, "recent": deque(maxlen=12),
        })

    def record_trade(self, symbol: str, pnl_usd: float):
        symbol = str(symbol).upper()
        with self._lock:
            r = self._records[symbol]
            pnl_usd = float(pnl_usd or 0.0)
            r["pnl"] += pnl_usd
            r["trades"] += 1
            r["recent"].append(pnl_usd)
            if pnl_usd > 0:
                r["wins"] += 1
                r["win_streak"] += 1
                r["loss_streak"] = 0
            else:
                r["losses"] += 1
                r["loss_streak"] += 1
                r["win_streak"] = 0

    def update_equity(self, equity: float):
        with self._lock:
            self._equity = float(equity or self._equity or 1.0)

    def size_multiplier(self, symbol: str) -> float:
        """Sembol performansına göre pozisyon büyüklüğü çarpanı. 0.15-1.0 arası."""
        if not self.enabled:
            return 1.0
        symbol = str(symbol).upper()
        with self._lock:
            mult = float(self.manual_mults.get(symbol, 1.0))
            r = self._records.get(symbol)
            if not r:
                return self._clip(mult)

            trades = int(r.get("trades", 0))
            if trades < self.min_trades:
                return self._clip(mult)

            eq = max(float(self._equity or 1.0), 1.0)
            pnl_pct = float(r.get("pnl", 0.0)) / eq
            wins = int(r.get("wins", 0))
            wr = wins / max(trades, 1)
            recent = list(r.get("recent", []))
            recent_pnl = sum(recent[-5:]) if recent else 0.0
            loss_streak = int(r.get("loss_streak", 0))

            # Kümülatif zarar bazlı ceza
            if pnl_pct <= self.hard_loss_pct:
                mult *= self.weak_mult
            elif pnl_pct <= self.soft_loss_pct:
                mult *= max(self.weak_mult, 0.65)

            # Ardışık zarar cezası
            if loss_streak >= self.loss_streak_after:
                mult *= self.loss_streak_mult

            # Düşük winrate cezası
            if trades >= self.low_wr_after and wr < self.low_wr_threshold:
                mult *= self.low_wr_mult

            # Yakın dönem toparlanıyorsa cezayı biraz yumuşat, tamamen kaldırma
            if recent_pnl > 0 and mult < 1.0:
                mult = min(1.0, mult / max(self.recovery_mult, 0.1))

            return self._clip(mult)

    def _clip(self, v: float) -> float:
        return max(0.15, min(1.0, float(v)))

    def get_rolling_pnl(self, symbol: str) -> float:
        with self._lock:
            return float(self._records.get(str(symbol).upper(), {}).get("pnl", 0.0))

    def get_all_stats(self) -> dict:
        with self._lock:
            out = {}
            for sym, r in self._records.items():
                d = dict(r)
                d["recent"] = list(d.get("recent", []))
                d["size_mult"] = self.size_multiplier(sym)
                out[sym] = d
            return out
