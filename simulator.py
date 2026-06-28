# simulator.py — Gerçek zamanlı bot orkestrasyonu
# V8.5 ENTEGRASYONU: Bu dosya GitHub Versiyon-7'den taşındı (V8.5 paketinde
# eksikti, bu yüzden canlı bot fiilen çalışmıyordu — app.py _SIM_OK=False
# fallback'ine düşüyordu). TradeEngine API'si (seed_from_candles,
# seed_from_candles_htf, on_candle, on_candle_htf, htf_last_time,
# add_to_blacklist, get_pnl, get_open_positions, stop) V8.5'te de mevcut
# olduğu doğrulandı — bu dosya değişiklik gerekmeden uyumlu çalışıyor.
import json
import time
import threading
import traceback
from pathlib import Path
from typing import List

import yaml

from engine import TradeEngine
from data_ws import WSFeedKline
from data_rest import preload_klines, sync_recent_klines, get_klines, parse_klines
from strategy_core import score_symbol
from agent import step_agent
from optimizer import compute_hourly_stats, compute_coin_stats
from logger import log_info, log_error
import universe_manager

# ── Global Durum ──────────────────────────────────────────────
_STATUS = {"ws": "-", "rest": "ok", "universe": 0,
           "shards": 0, "preload": False, "top5": "-"}
_OPEN     : list = []
_PNL      : dict = {"usd": 0.0, "pct": 0.0, "daily_usd": 0.0, "equity": 0.0}
_ENGINE   : TradeEngine = None
_FEED     : WSFeedKline = None
_started  : threading.Event = threading.Event()
_SYMS     : List[str] = []
_INTERVAL : str = "5m"
_INTERVAL_HTF: str = "1h"
_SHARD    : int = 20
_PRELOAD  : int = 1000
_PRELOAD_HTF : int = 500

_score_cache      : dict = {}   # {symbol: score}
_score_cache_tick : int = 0     # kaçıncı iterasyon
_SCORE_CACHE_TTL  : int = 30    # kaç iterasyonda bir yenile

_blacklist_store : dict = {}    # {symbol: expire_ts} — engine bağımsız kalıcı store
_BLACKLIST_FILE = Path(__file__).parent / "blacklist_store.json"


def _bl_load():
    """Disk'ten blacklist yükle, süresi dolmuşları temizle."""
    global _blacklist_store
    try:
        if _BLACKLIST_FILE.exists():
            data = json.loads(_BLACKLIST_FILE.read_text(encoding="utf-8"))
            now = time.time()
            _blacklist_store = {s: exp for s, exp in data.items() if exp > now}
    except Exception:
        _blacklist_store = {}


def _bl_save():
    """Blacklist'i diske yaz."""
    try:
        _BLACKLIST_FILE.write_text(
            json.dumps(_blacklist_store, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ── Yardımcılar ───────────────────────────────────────────────
def _load_symbols(limit: int = 20, file: str = "symbols_top70.json",
                   cfg: dict = None, data_dir: str = "data") -> list:
    """
    V9.2: Universe Manager entegrasyonu (V7 symbols_builder.py metodolojisi).
    weekly_symbol_rotation.enabled=True ise refresh_days periyoduna göre
    candidate_top adaydan top_n sembol seçer (stable/fiat/wrapped eleme +
    7g hacim stabilitesi + 30g momentum + win_days_ratio + EMA5>EMA20).
    Kapalıysa veya ağ yoksa eski statik symbols_top70.json fallback'ine
    düşer — sistem ASLA durmaz. KRİTİK: build_live_universe() içinde zaten
    ham-candidate fallback YOK — boş seçim CORE_FALLBACK ile garantili.
    """
    cfg = cfg or {}
    um_cfg = cfg.get("weekly_symbol_rotation", {}) or {}
    if bool(um_cfg.get("enabled", False)):
        refresh_days = int(um_cfg.get("refresh_days", 7))
        try:
            # V9.2 FIX (ghost-config): live_refresh_requires_no_open_positions artık
            # gerçekten kontrol ediliyor. Bu fonksiyon bot başlangıcında (_ENGINE henüz
            # yokken) çağrıldığı için şu an her zaman "açık pozisyon yok" durumudur —
            # ama ileride periyodik canlı refresh eklenirse bu kontrol gerçek bir engel
            # haline gelecektir (global _ENGINE referansı kontrol edilir).
            requires_no_open = bool(um_cfg.get("live_refresh_requires_no_open_positions", True))
            has_open_positions = False
            if requires_no_open:
                try:
                    eng = globals().get("_ENGINE")
                    if eng is not None:
                        has_open_positions = len(eng.get_open_positions()) > 0
                except Exception:
                    has_open_positions = False
            if requires_no_open and has_open_positions:
                log_info("[UniverseManager] Açık pozisyon var, refresh ATLANDI "
                         "(live_refresh_requires_no_open_positions=true) — mevcut evren korunuyor.")
                cached = universe_manager.load_current_universe(data_dir)
                if cached:
                    return cached[:limit]
            elif universe_manager.should_refresh(data_dir, refresh_days):
                def _fetch_candles(sym: str):
                    raw = get_klines(sym, interval="1h", limit=24 * int(um_cfg.get("lookback_days", 30)))
                    return parse_klines(raw)
                log_info("[UniverseManager] Evren yenileniyor (refresh_days doldu)...")
                result = universe_manager.build_live_universe(cfg, _fetch_candles, data_dir=data_dir)
                selected = result["selected"]
                if result.get("core_fallback_used"):
                    log_error(f"[UniverseManager] ⚠️ CORE_FALLBACK_USED — DATA_QUALITY_FAIL! "
                              f"Temiz seçim boş çıktı, güvenli çekirdek evrene düşüldü: {selected}")
                log_info(f"[UniverseManager] candidate={result.get('candidate_count',0)} "
                         f"stable_filtered={result.get('stable_filtered_count',0)} "
                         f"problematic_filtered={result.get('problematic_filtered_count',0)} "
                         f"selected={result.get('selected_count',0)} source={result.get('source','?')} "
                         f"data_quality_ok={result.get('data_quality_ok', True)}")
                return selected[:limit]
            cached = universe_manager.load_current_universe(data_dir)
            if cached:
                return cached[:limit]
        except Exception as e:
            log_error(f"[UniverseManager] hata, statik listeye düşülüyor: {e}")

    try:
        return json.loads(Path(file).read_text(encoding="utf-8"))[:limit]
    except Exception:
        log_error("symbols_top70.json okunamadı")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]


def _compute_top5(force: bool = False) -> str:
    global _score_cache, _score_cache_tick
    if not _ENGINE:
        return "Motor kapalı"

    _score_cache_tick += 1
    if not force and _score_cache_tick % _SCORE_CACHE_TTL != 0:
        # Cache'den oku — sadece sıralama yap, hesaplama yok
        items = [(s, sc) for s, sc in _score_cache.items()]
        if not items:
            return "Veri toplanıyor..."
        items.sort(key=lambda x: abs(x[1] - 50), reverse=True)
        return "\n".join(
            f"{r}. {s:<12} score={sc:5.2f} {'LONG' if sc >= 50 else 'SHORT'}"
            for r, (s, sc) in enumerate(items[:5], 1)
        )

    # TTL doldu — yeniden hesapla
    for s in _SYMS:
        prices = list(_ENGINE.close_series.get(s, []))
        volumes = list(_ENGINE.vol_series.get(s, []))
        if not prices or len(prices) < 50:
            continue
        try:
            highs = list(_ENGINE.high_series.get(s, []))
            lows = list(_ENGINE.low_series.get(s, []))
            sc = score_symbol(prices, highs, lows, volumes).get("final_score")
            if sc is not None:
                _score_cache[s] = sc
        except Exception:
            continue

    items = [(s, sc) for s, sc in _score_cache.items()]
    if not items:
        return "Veri toplanıyor..."
    items.sort(key=lambda x: abs(x[1] - 50), reverse=True)
    return "\n".join(
        f"{r}. {s:<12} score={sc:5.2f} {'LONG' if sc >= 50 else 'SHORT'}"
        for r, (s, sc) in enumerate(items[:5], 1)
    )


# ── Arka Plan Döngüleri ───────────────────────────────────────
def _step_loop():
    global _OPEN, _PNL, _STATUS
    while _started.is_set():
        try:
            if _ENGINE:
                _STATUS["top5"] = _compute_top5()
                _PNL = _ENGINE.get_pnl()
                _OPEN = _ENGINE.get_open_positions()
                step_agent()
        except Exception:
            traceback.print_exc()
        time.sleep(1)


_INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400
}


def _sync_loop():
    """LTF senkronizasyonu — mum aralığına göre dinamik bekleme."""
    while _started.is_set():
        sleep_sec = _INTERVAL_SECONDS.get(_INTERVAL, 300)
        time.sleep(sleep_sec)
        try:
            if _ENGINE:
                for s in _SYMS:
                    last_ts = _ENGINE.last_close_time.get(s, 0)
                    if last_ts > 0:
                        new = sync_recent_klines(s, last_ts, interval=_INTERVAL)
                        for c in new:
                            _ENGINE.on_candle(s, c)
                    time.sleep(0.05)
        except Exception:
            traceback.print_exc()


def _sync_htf_loop():
    """HTF senkronizasyonu — mtf.htf_interval'e göre periyodik."""
    while _started.is_set():
        wait_sec = _INTERVAL_SECONDS.get(_INTERVAL_HTF, 3600)
        time.sleep(wait_sec)
        try:
            if _ENGINE:
                log_info("HTF sync başlıyor...")
                for s in _SYMS:
                    last_ts = _ENGINE.htf_last_time.get(s, 0)
                    if last_ts > 0:
                        new = sync_recent_klines(s, last_ts, interval=_INTERVAL_HTF)
                        for c in new:
                            _ENGINE.on_candle_htf(s, c)
                    time.sleep(0.05)
                log_info("HTF sync tamamlandı.")
        except Exception:
            traceback.print_exc()


# ── GUI API ───────────────────────────────────────────────────
def get_status() -> dict:
    if _FEED:
        _STATUS["ws"] = "open" if _FEED.is_open() else "closed"
    return dict(_STATUS)


def get_open_status() -> list:
    return list(_OPEN)


def get_pnl() -> dict:
    return dict(_PNL)


# ── Kara Liste API ────────────────────────────────────────────
def add_to_blacklist(symbol: str, hours: float = 24.0):
    _blacklist_store[symbol] = float("inf") if hours <= 0 else time.time() + hours * 3600
    _bl_save()
    if _ENGINE:
        _ENGINE.add_to_blacklist(symbol, hours)


def remove_from_blacklist(symbol: str):
    _blacklist_store.pop(symbol, None)
    _bl_save()
    if _ENGINE:
        _ENGINE.remove_from_blacklist(symbol)


def get_blacklist() -> list:
    now = time.time()
    expired = [s for s, exp in list(_blacklist_store.items()) if exp != float("inf") and now >= exp]
    for s in expired:
        del _blacklist_store[s]
    if expired:
        _bl_save()
    return [(s, -1 if exp == float("inf") else round((exp - now) / 3600, 1))
            for s, exp in _blacklist_store.items()]


# ── İstatistik API ────────────────────────────────────────────
def get_hourly_stats() -> list:
    return compute_hourly_stats()


def get_coin_stats() -> list:
    return compute_coin_stats()


# ── Başlat / Durdur ───────────────────────────────────────────
_bl_load()


def start_realtime(log_callback):
    global _ENGINE, _FEED, _SYMS
    global _INTERVAL, _INTERVAL_HTF, _SHARD, _PRELOAD, _PRELOAD_HTF

    if _started.is_set():
        log_callback("[UYARI] Bot zaten çalışıyor.")
        return

    try:
        with open("config_online.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        log_callback("[HATA] config_online.yaml bulunamadı.")
        return
    except Exception as e:
        log_callback(f"[HATA] Config okuma: {e}")
        return

    # V8.5.2 Paper Live Fix: live.president_execution_mode gerçek etki yaratır.
    # shadow = canlı veri + sadece karar/log; paper = canlı veri + sanal pozisyon, gerçek emir yok.
    live_cfg = cfg.get("live", {}) or {}
    exec_mode = str(live_cfg.get("president_execution_mode", "shadow")).lower()
    if exec_mode in ("paper", "simulated_active", "active", "live"):
        cfg.setdefault("president", {})["shadow_mode"] = False
        cfg.setdefault("live", {})["_paper_trade_enabled"] = True
    elif exec_mode == "shadow":
        cfg.setdefault("president", {})["shadow_mode"] = True
        cfg.setdefault("live", {})["_paper_trade_enabled"] = False
    else:
        log_callback(f"[UYARI] Bilinmeyen live.president_execution_mode={exec_mode}; shadow kabul edildi.")
        cfg.setdefault("president", {})["shadow_mode"] = True
        cfg.setdefault("live", {})["_paper_trade_enabled"] = False

    mode = cfg.get("mode", {}) or {}
    mtf = cfg.get("mtf", {}) or {}
    _INTERVAL = mode.get("interval", "1h")
    _INTERVAL_HTF = mtf.get("htf_interval", "1h")
    _SHARD = int(mode.get("shard_size", 20))
    _PRELOAD = int(mode.get("preload_candles", 1000))
    _PRELOAD_HTF = int(mtf.get("preload_candles_htf", 500))
    _data_dir = "logs/paper_live" if cfg.get("live", {}).get("_paper_trade_enabled") else "logs/shadow_live"
    _SYMS = _load_symbols(limit=int(mode.get("top_n", 20)), cfg=cfg, data_dir=_data_dir)

    shards = max(1, (len(_SYMS) + _SHARD - 1) // _SHARD)
    _STATUS.update({"universe": len(_SYMS), "shards": shards, "preload": False})

    _ENGINE = TradeEngine(symbols=_SYMS, cfg=cfg, data_dir=_data_dir)
    _ENGINE.on_event = lambda etype, payload: log_callback(
        f"[EVENT] {etype}: {payload}"
    )

    # Mevcut blacklist store'u engine'e aktar
    for _sym, _exp in list(_blacklist_store.items()):
        _remaining = (_exp - time.time()) / 3600
        if _remaining > 0:
            _ENGINE.add_to_blacklist(_sym, _remaining)

    # ── LTF Preload ───────────────────────────────────────────
    log_callback(f"LTF geçmiş veriler ({_INTERVAL}, {_PRELOAD} mum) yükleniyor...")
    try:
        preloaded = preload_klines(_SYMS, interval=_INTERVAL, limit=_PRELOAD)
        for s, candles in preloaded.items():
            _ENGINE.seed_from_candles(s, candles)
        log_callback(f"LTF yüklendi ({len(preloaded)} sembol).")
    except Exception as e:
        log_callback(f"[HATA] LTF Preload: {e}")
        return

    # ── HTF Preload ───────────────────────────────────────────
    if mtf.get("enabled", True):
        log_callback(f"HTF geçmiş veriler ({_INTERVAL_HTF}, {_PRELOAD_HTF} mum) yükleniyor...")
        try:
            preloaded_htf = preload_klines(_SYMS, interval=_INTERVAL_HTF,
                                            limit=_PRELOAD_HTF)
            for s, candles in preloaded_htf.items():
                _ENGINE.seed_from_candles_htf(s, candles)
            log_callback(f"HTF yüklendi ({len(preloaded_htf)} sembol).")
        except Exception as e:
            log_callback(f"[UYARI] HTF Preload başarısız, MTF devre dışı: {e}")

    _STATUS["preload"] = True

    # ── WebSocket (LTF) ───────────────────────────────────────
    def on_ws_connect():
        log_callback(f"WebSocket ({_INTERVAL}) bağlandı.")

    log_callback(f"WebSocket ({_INTERVAL}) kuruluyor...")
    _FEED = WSFeedKline(
        symbols=_SYMS, on_candle=_ENGINE.on_candle,
        on_connect=on_ws_connect, interval=_INTERVAL, shard_size=_SHARD
    )
    _FEED.start()

    _started.set()
    threading.Thread(target=_step_loop, daemon=True).start()
    threading.Thread(target=_sync_loop, daemon=True).start()
    threading.Thread(target=_sync_htf_loop, daemon=True).start()

    log_callback(f"[Simulator] Başarıyla başlatıldı. mode={exec_mode} interval={_INTERVAL} data_dir={_data_dir}")
    log_info("Simulator başlatıldı")


def stop_realtime(log_callback=None):
    global _FEED, _ENGINE

    if not _started.is_set():
        if log_callback:
            log_callback("[UYARI] Bot zaten durdurulmuş.")
        return

    _started.clear()

    if _ENGINE:
        _ENGINE.stop()
        if log_callback:
            log_callback("[Engine] Yeni işlem açma kapatıldı.")

    if _FEED:
        _FEED.stop()
        if log_callback:
            log_callback("[WebSocket] Bağlantı kapatıldı.")

    _ENGINE = None
    _FEED = None
    if log_callback:
        log_callback("[Simulator] Durduruldu.")
    log_info("Simulator durduruldu")
