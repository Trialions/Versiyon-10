# modules/decision_packet.py — v8.2
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any
import time, uuid


class Action(Enum):
    OPEN   = "OPEN"
    BLOCK  = "BLOCK"
    WATCH  = "WATCH"
    SHADOW = "SHADOW"
    CLOSE  = "CLOSE"
    ROTATE = "ROTATE"


class Side(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"


@dataclass
class BranchVote:
    """
    V9.0: BranchVote artık tam bir EngineReport'tur. Yeni alanlar opsiyonel
    (default'lu) eklendi — eski çağıranlar (sadece ilk 6 pozisyonel alanı
    veren) hiçbir değişiklik yapmadan çalışmaya devam eder.
    """
    branch_name: str
    action: Action          # OPEN | BLOCK | WATCH (asla SHADOW değil)
    side: Side
    score: float            # 0-100 — bu dalın KENDİ bağımsız analizinden üretilen skor
    confidence: float       # 0.0-1.0 (zorunlu standart)
    reason: str
    shadow: bool = False    # True → bu dal güvenilmez; karar shadow kalır
    params: Dict[str, Any] = field(default_factory=dict)
    candle_ts: float = 0.0  # mum zamanı (epoch saniye)

    # ── EngineReport genişletmesi (V9.0) ───────────────────────────────
    engine: str = ""             # "LONG" | "SHORT" | "OPPORTUNITY"
    setup_type: str = ""         # orn. "TREND_CONTINUATION", "MOMENTUM_BREAKDOWN", "CASCADE_REVERSAL"
    risk_mult: float = 1.0       # bu dalın önerdiği risk çarpanı
    sl_profile: str = ""         # orn. "ATR_TREND", "TIGHT_REJECTION", "CASCADE_TIGHT"
    tp_profile: str = ""         # orn. "WIDE_TREND", "FAST_SCALP"
    trail_profile: str = ""      # orn. "LATE_TREND_FOLLOW", "AGGRESSIVE_LOCK", "EARLY_BREAKEVEN"
    open_intent: bool = False    # bu dal gerçekten açmak mı istiyor (BLOCK olsa da niyet sinyali tutulur)
    block_reason: str = ""       # action=BLOCK ise insan-okunur sebep (reason'dan ayrı, yapısal)
    derivatives_used: bool = False  # bu rapor derivatives verisi kullandı mı
    debug: Dict[str, Any] = field(default_factory=dict)  # alt-skor kırılımı (trend/htf/btc/volume/derivatives)

    def __post_init__(self):
        # confidence her zaman 0-1 aralığında
        self.confidence = float(max(0.0, min(1.0, self.confidence)))
        self.score      = float(max(0.0, min(100.0, self.score)))
        if not self.engine:
            # branch_name'den otomatik türet (geriye uyum)
            nm = (self.branch_name or "").lower()
            if "long" in nm:
                self.engine = "LONG"
            elif "short" in nm:
                self.engine = "SHORT"
            elif "cascade" in nm or "opportunity" in nm:
                self.engine = "OPPORTUNITY"


@dataclass
class DecisionPacket:
    symbol: str
    action: Action
    side: Side
    final_score: float
    size_mult: float
    sl_pct: float
    reason: str
    branch_votes: Dict[str, BranchVote] = field(default_factory=dict)
    is_shadow: bool = False
    label: str = ""
    candle_ts: float = 0.0   # mum zamanı — bilgisayar saati DEĞİL
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── V9.0: Başkan'ın seçtiği motorun setup karakteri — Adaptive Exit
    # bu alanları okuyup pozisyon yönetimini buna göre uyarlar. ──────────
    selected_engine: str = ""     # "LONG" | "SHORT" | "OPPORTUNITY"
    setup_type: str = ""
    sl_profile: str = ""
    tp_profile: str = ""
    trail_profile: str = ""

    def winning_votes(self):
        return {k: v for k, v in self.branch_votes.items()
                if v.action == Action.OPEN and v.side == self.side}

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "side": self.side.value,
            "final_score": round(self.final_score, 2),
            "size_mult": round(self.size_mult, 3),
            "sl_pct": round(self.sl_pct, 4),
            "reason": self.reason,
            "label": self.label,
            "is_shadow": self.is_shadow,
            "candle_ts": self.candle_ts,
            "votes": {
                k: {"action": v.action.value, "side": v.side.value,
                    "score": round(v.score, 2), "confidence": round(v.confidence, 3),
                    "shadow": v.shadow, "reason": v.reason,
                    "engine": v.engine, "setup_type": v.setup_type,
                    "risk_mult": round(v.risk_mult, 3),
                    "sl_profile": v.sl_profile, "tp_profile": v.tp_profile,
                    "trail_profile": v.trail_profile, "open_intent": v.open_intent}
                for k, v in self.branch_votes.items()
            }
        }
