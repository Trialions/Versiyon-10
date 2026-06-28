# modules/risk_governor.py — v8.2
# KRİTİK DÜZELTİ: reset artık bilgisayar saatine değil candle zamanına göre
from __future__ import annotations
import json, os, threading, time


class RiskGovernor:
    def __init__(self, cfg: dict, persist_path: str = None):
        p    = cfg.get("president", {})
        port = p.get("portfolio", {})
        self.max_long         = int(port.get("max_long_positions", 3))
        self.max_short        = int(port.get("max_short_positions", 1))
        self.max_total        = int(port.get("max_total_positions", 3))
        self.daily_loss_pct   = float(port.get("max_daily_loss_pct", 3.0)) / 100
        self.monthly_loss_pct = float(port.get("max_monthly_loss_pct", 10.0)) / 100

        self._lock         = threading.Lock()
        self._daily_pnl    = 0.0
        self._monthly_pnl  = 0.0
        self._equity       = float(cfg.get('account', {}).get('starting_equity_usdt', cfg.get('misc', {}).get('starting_equity_usdt', 1000.0)))
        self._last_day     = ""   # candle bazlı — başlangıçta boş
        self._last_month   = ""
        self._open_longs   = 0
        self._open_shorts  = 0
        self._persist_path = persist_path
        self._load()

    # ── Candle zamanıyla reset (backtest güvenilirliği) ───────────────
    def _day_str(self, ts: float) -> str:
        """ts = epoch saniye (0 ise bilgisayar saati — sadece canlı)"""
        return time.strftime("%Y-%m-%d", time.gmtime(ts if ts > 0 else time.time()))

    def _month_str(self, ts: float) -> str:
        return time.strftime("%Y-%m", time.gmtime(ts if ts > 0 else time.time()))

    def _reset_if_needed(self, candle_ts: float = 0.0):
        day   = self._day_str(candle_ts)
        month = self._month_str(candle_ts)
        if self._last_day and day != self._last_day:
            self._daily_pnl  = 0.0
        if self._last_month and month != self._last_month:
            self._monthly_pnl = 0.0
        self._last_day   = day
        self._last_month = month

    # ── Güncelleme ────────────────────────────────────────────────────
    def record_trade_close(self, pnl_usd: float, candle_ts: float = 0.0):
        with self._lock:
            self._reset_if_needed(candle_ts)
            self._daily_pnl   += pnl_usd
            self._monthly_pnl += pnl_usd
        self._save()

    def record_open(self, side: str):
        with self._lock:
            if side == "LONG":   self._open_longs  += 1
            elif side == "SHORT": self._open_shorts += 1

    def record_close(self, side: str):
        with self._lock:
            if side == "LONG":   self._open_longs  = max(0, self._open_longs - 1)
            elif side == "SHORT": self._open_shorts = max(0, self._open_shorts - 1)

    def update_equity(self, equity: float):
        with self._lock:
            self._equity = equity

    # ── Kontrol ───────────────────────────────────────────────────────
    def can_open(self, side: str, candle_ts: float = 0.0) -> tuple:
        with self._lock:
            self._reset_if_needed(candle_ts)
            total = self._open_longs + self._open_shorts
            if total >= self.max_total:
                return False, f"MAX_TOTAL_POS={self.max_total}"
            if side == "LONG"  and self._open_longs  >= self.max_long:
                return False, f"MAX_LONG_POS={self.max_long}"
            if side == "SHORT" and self._open_shorts >= self.max_short:
                return False, f"MAX_SHORT_POS={self.max_short}"
            eq = max(self._equity, 1.0)
            if self._daily_pnl   <= -(eq * self.daily_loss_pct):
                return False, f"DAILY_LOSS_LIMIT pnl={self._daily_pnl:.2f}"
            if self._monthly_pnl <= -(eq * self.monthly_loss_pct):
                return False, f"MONTHLY_LOSS_LIMIT pnl={self._monthly_pnl:.2f}"
            return True, "OK"

    def get_state(self) -> dict:
        with self._lock:
            return {
                "open_longs":   self._open_longs,
                "open_shorts":  self._open_shorts,
                "daily_pnl":    round(self._daily_pnl, 2),
                "monthly_pnl":  round(self._monthly_pnl, 2),
                "equity":       round(self._equity, 2),
                "last_day":     self._last_day,
            }

    # ── Kalıcılık ─────────────────────────────────────────────────────
    def _save(self):
        if not self._persist_path: return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._persist_path)), exist_ok=True)
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump({"daily_pnl": self._daily_pnl, "monthly_pnl": self._monthly_pnl,
                    "equity": self._equity, "open_longs": self._open_longs,
                    "open_shorts": self._open_shorts, "last_day": self._last_day,
                    "last_month": self._last_month}, f)
        except Exception: pass

    def _load(self):
        if not self._persist_path or not os.path.exists(self._persist_path): return
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                d = json.load(f)
            self._daily_pnl   = d.get("daily_pnl", 0.0)
            self._monthly_pnl = d.get("monthly_pnl", 0.0)
            self._equity      = d.get("equity", self._equity)
            self._open_longs  = d.get("open_longs", 0)
            self._open_shorts = d.get("open_shorts", 0)
            self._last_day    = d.get("last_day", "")
            self._last_month  = d.get("last_month", "")
        except Exception: pass
