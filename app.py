# app.py — TRBOT President System V8 — PyWebView Koprusu
from __future__ import annotations

import csv as csv_mod
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import webview
import yaml

# ─── Dizin Ayarlari ──────────────────────────────────────────────────────────
_APP_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "config_online.yaml")
BT_BASE     = os.path.join(_APP_DIR, "backtest_results")
WF_BASE     = os.path.join(_APP_DIR, "walkforward_results")
TWF_BASE    = os.path.join(_APP_DIR, "truewf_results")
ROB_BASE    = os.path.join(_APP_DIR, "robustness_results")
DATA_DIR    = os.path.join(_APP_DIR, "data")

for d in [BT_BASE, WF_BASE, ROB_BASE, DATA_DIR]:
    os.makedirs(d, exist_ok=True)

# ─── Log Tamponu ─────────────────────────────────────────────────────────────
_log_buffer = []
_log_lock   = threading.Lock()

def _push_log(msg: str):
    ts   = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        _log_buffer.append(line)
        if len(_log_buffer) > 1000:
            _log_buffer.pop(0)

# ─── Proses Yoneticileri ─────────────────────────────────────────────────────
_bt_proc       = None
_bt_lock       = threading.Lock()
_bt_active_dir = ""

_wf_proc       = None
_wf_lock       = threading.Lock()
_wf_active_dir = ""
_twf_proc      = None
_twf_lock      = threading.Lock()
_twf_active_dir= ""

_rob_proc       = None
_rob_lock       = threading.Lock()
_rob_active_dir = ""

# ─── Simulator (canli bot) ───────────────────────────────────────────────────
try:
    from simulator import (get_status, get_open_status, get_pnl,
                           start_realtime, stop_realtime,
                           add_to_blacklist, remove_from_blacklist,
                           get_blacklist, get_hourly_stats, get_coin_stats)
    _SIM_OK = True
except ImportError:
    _SIM_OK = False
    def get_status():   return {}
    def get_open_status(): return []
    def get_pnl():      return {"usd": 0, "pct": 0, "daily_usd": 0, "equity": 0}
    def start_realtime(**kw): pass
    def stop_realtime(**kw):  pass
    def add_to_blacklist(s, h): pass
    def remove_from_blacklist(s): pass
    def get_blacklist(): return []
    def get_hourly_stats(): return []
    def get_coin_stats():   return []

# ─── Klasor Yardimcilari ─────────────────────────────────────────────────────
def _list_folders(base: str) -> list:
    if not os.path.exists(base):
        return []
    return sorted(
        [d for d in os.listdir(base)
         if os.path.isdir(os.path.join(base, d))],
        reverse=True
    )

def _load_bt_folder(path: str) -> dict:
    result = {"summary": {}, "trades": [], "equity": [],
              "filter_events": [], "ghost": [], "boa": [], "boa_reason": [], "boa_symbol": [], "boa_regime": [],
              "config": {}, "folder": path,
              "president_decisions": [], "president_shadows": [], "president_votes": []}
    try:
        # Summary
        sp = os.path.join(path, "backtest_summary.csv")
        if os.path.exists(sp):
            with open(sp, newline="", encoding="utf-8-sig") as f:
                for row in csv_mod.reader(f, delimiter=";"):
                    if len(row) >= 2:
                        result["summary"][row[0]] = row[1]
        # Trades (V9 FIX: sinir kaldirildi — tam okunuyor)
        tp = os.path.join(path, "backtest_trades.csv")
        if os.path.exists(tp):
            with open(tp, newline="", encoding="utf-8-sig") as f:
                result["trades"] = list(csv_mod.DictReader(f, delimiter=";"))
        # Equity (V9 FIX: sinir kaldirildi — tum noktalar)
        ep = os.path.join(path, "equity_curve.csv")
        if os.path.exists(ep):
            with open(ep, newline="", encoding="utf-8-sig") as f:
                rows = list(csv_mod.DictReader(f, delimiter=";"))
                result["equity"] = [
                    {"t": r["Timestamp"], "v": float(r["Equity"])}
                    for r in rows
                ]
        # Filter events (V9 FIX: sinir kaldirildi)
        fp = os.path.join(path, "filter_events.csv")
        if os.path.exists(fp):
            with open(fp, newline="", encoding="utf-8-sig") as f:
                result["filter_events"] = list(csv_mod.DictReader(f, delimiter=";"))
        # Ghost (V9 FIX: sinir kaldirildi)
        gp = os.path.join(path, "ghost_signal_analysis.csv")
        if os.path.exists(gp):
            with open(gp, newline="", encoding="utf-8-sig") as f:
                result["ghost"] = list(csv_mod.DictReader(f, delimiter=";"))
        # BOA (exit reason ozeti — eski)
        bp = os.path.join(path, "block_outcome_summary_by_reason.csv")
        if os.path.exists(bp):
            with open(bp, newline="", encoding="utf-8-sig") as f:
                result["boa"] = list(csv_mod.DictReader(f, delimiter=";"))
        # GERCEK BOA (bloklanan sinyallerin 4h/12h/24h sonucu)
        for key, fname in [("boa_reason", "boa_summary_by_reason.csv"),
                           ("boa_symbol", "boa_summary_by_symbol.csv"),
                           ("boa_regime", "boa_summary_by_regime.csv")]:
            rp = os.path.join(path, fname)
            if os.path.exists(rp):
                with open(rp, newline="", encoding="utf-8-sig") as f:
                    result[key] = list(csv_mod.DictReader(f, delimiter=";"))
        # Config
        cp = os.path.join(path, "config_snapshot.json")
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                result["config"] = json.load(f)
        # V8.5.8: Bu backtest koşusuna ait President kararları/oyları —
        # önceden gui.html'de "TODO" olarak bırakılmıştı, artık okunuyor.
        # V9 FIX: satır sinirlari kaldirildi — tam okunuyor.
        pres_dir = os.path.join(path, "_president")
        for key, fname in [
            ("president_decisions", "president_decisions.csv"),
            ("president_shadows",   "shadow_opportunities.csv"),
            ("president_votes",     "branch_votes.csv"),
        ]:
            fp = os.path.join(pres_dir, fname)
            if os.path.exists(fp):
                with open(fp, newline="", encoding="utf-8-sig") as f:
                    result[key] = list(csv_mod.DictReader(f, delimiter=";"))
    except Exception as e:
        result["error"] = str(e)
    return result

def _load_wf_folder(path: str) -> dict:
    result = {"summary": {}, "monthly": [], "folder": path}
    try:
        sp = os.path.join(path, "wf_summary.json")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                result["summary"] = json.load(f)
        mp = os.path.join(path, "wf_monthly.csv")
        if os.path.exists(mp):
            with open(mp, newline="", encoding="utf-8-sig") as f:
                result["monthly"] = list(csv_mod.DictReader(f, delimiter=";"))
    except Exception as e:
        result["error"] = str(e)
    return result

def _load_rob_folder(path: str) -> dict:
    result = {"summary": {}, "weekly": [], "folder": path}
    try:
        sp = os.path.join(path, "robustness_summary.json")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                result["summary"] = json.load(f)
        wp = os.path.join(path, "robustness_weekly.csv")
        if os.path.exists(wp):
            with open(wp, newline="", encoding="utf-8-sig") as f:
                result["weekly"] = list(csv_mod.DictReader(f, delimiter=";"))
    except Exception as e:
        result["error"] = str(e)
    return result

# ─── Config Yardimci ─────────────────────────────────────────────────────────
def _make_bt_dir(params: dict) -> str:
    stamp    = time.strftime("%Y-%m-%d_%H-%M")
    interval = params.get("interval", "1h")
    start    = str(params.get("start", "") or "")
    end      = str(params.get("end", "") or "")
    if start and end:
        # V9 FIX: start/end verildiğinde klasör adı gerçek gün sayısını yansıtmalı,
        # GUI'deki (muhtemelen eski/varsayılan) "days" alanına göre değil.
        try:
            _d0 = datetime.datetime.strptime(start, "%Y-%m-%d")
            _d1 = datetime.datetime.strptime(end, "%Y-%m-%d")
            days = (_d1 - _d0).days
        except Exception:
            days = params.get("days", 30)
    else:
        days = params.get("days", 30)
    folder   = f"{stamp}_{interval}_{days}d"
    path     = os.path.join(BT_BASE, folder)
    os.makedirs(path, exist_ok=True)
    # Config snapshot
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        with open(os.path.join(path, "config_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass
    return path

# ─── API Sinifi ──────────────────────────────────────────────────────────────
class API:

    # ── Bot Kontrolu ──────────────────────────────────────────────────
    def start_bot(self, mode: str = ""):
        """Canlı veri modunu başlat.
        mode="shadow"  -> sadece karar/log
        mode="paper"   -> gerçek veri + sanal pozisyon, gerçek emir yok
        Boş bırakılırsa config_online.yaml içindeki live.president_execution_mode kullanılır.
        """
        mode = str(mode or "").strip().lower()
        if mode in ("shadow", "paper"):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                cfg.setdefault("live", {})["president_execution_mode"] = mode
                # simulator.py live.president_execution_mode'u okuyup president.shadow_mode'u runtime'da ayarlıyor.
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                _push_log(f"[LIVE] Mod ayarlandı: {mode}")
            except Exception as e:
                _push_log(f"[LIVE] Mod yazılamadı: {e}")
                return {"ok": False, "error": str(e)}
        def _run():
            if _SIM_OK:
                start_realtime(log_callback=_push_log)
            else:
                _push_log("[HATA] simulator.py import edilemedi; canlı/paper mod başlatılamadı.")
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "mode": mode or "config"}

    def stop_bot(self):
        if _SIM_OK:
            stop_realtime(log_callback=_push_log)
        return {"ok": True}

    def get_state(self):
        st  = get_status()
        pnl = get_pnl()
        return {
            "ws":       st.get("ws", "-"),
            "universe": st.get("universe", 0),
            "pnl_usd":  pnl.get("usd", 0.0),
            "pnl_pct":  pnl.get("pct", 0.0),
            "daily":    pnl.get("daily_usd", 0.0),
            "equity":   pnl.get("equity", 0.0),
            "universe_manager": self._get_universe_manager_status(),
        }

    def _get_universe_manager_status(self):
        """V9.0.1 (App/GUI denetimi): Universe Manager aktif/pasif, son refresh
        zamanı ve seçilen sembol sayısını döner. Hiçbir ağ çağrısı yapmaz —
        sadece config + diskteki symbols_current_meta.json'u okur. Dosya/anahtar
        yoksa güvenli varsayılanlarla devam eder (crash etmez)."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
        wu = cfg.get("weekly_symbol_rotation", {}) or {}
        enabled = bool(wu.get("enabled", False))
        result = {"enabled": enabled, "candidate_top": wu.get("candidate_top", ""),
                  "top_n": wu.get("top_n", ""), "lookback_days": wu.get("lookback_days", ""),
                  "refresh_days": wu.get("refresh_days", ""),
                  "last_refresh": None, "selected_count": 0, "source": "static_fallback"}
        # Canlı modda meta dosyası logs/paper_live veya logs/shadow_live altında olur;
        # backtest modunda <out_dir>/_universe altında. En güncelini bul.
        candidates = [
            os.path.join(_APP_DIR, "logs", "paper_live", "symbols_current_meta.json"),
            os.path.join(_APP_DIR, "logs", "shadow_live", "symbols_current_meta.json"),
            os.path.join(DATA_DIR, "symbols_current_meta.json"),
        ]
        newest = None
        for p in candidates:
            if os.path.exists(p):
                try:
                    mtime = os.path.getmtime(p)
                    if newest is None or mtime > newest[1]:
                        newest = (p, mtime)
                except Exception:
                    continue
        if newest:
            try:
                with open(newest[0], encoding="utf-8") as f:
                    meta = json.load(f)
                result["last_refresh"] = meta.get("generated_at")
                result["selected_count"] = len(meta.get("selected", []))
                # V9.2 FIX: artık meta'daki GERÇEK source alanı yansıtılıyor —
                # önceden burada her zaman sabit "universe_manager" yazıyordu,
                # live_weekly_rotation/historical_weekly_rotation/core_fallback
                # ayrımı GUI'de hiç görünmüyordu.
                result["source"] = meta.get("source", "universe_manager")
                result["data_quality_ok"] = meta.get("data_quality_ok", True)
                result["core_fallback_used"] = meta.get("core_fallback_used", False)
                result["candidate_count"] = meta.get("candidate_count", 0)
                result["stable_filtered_count"] = meta.get("stable_filtered_count", 0)
            except Exception:
                pass
        else:
            # Hiç meta yoksa (UniverseManager hiç çalışmadı) statik fallback sayısı
            try:
                with open(os.path.join(_APP_DIR, "symbols_top70.json"), encoding="utf-8") as f:
                    result["selected_count"] = len(json.load(f) or [])
            except Exception:
                pass
        return result

    def get_positions(self):
        return get_open_status()

    def get_logs(self, since_index: int = 0):
        with _log_lock:
            return {"lines": _log_buffer[since_index:], "total": len(_log_buffer)}

    # ── Klasör Aç (V8.5.8 — önceden hiç bağlanmamıştı) ──────────────────
    def open_folder(self, kind: str, folder: str):
        """OS dosya yöneticisinde klasörü açar. kind: bt|wf|rob|twf."""
        base_map = {"bt": BT_BASE, "wf": WF_BASE, "rob": ROB_BASE, "twf": TWF_BASE}
        base = base_map.get(str(kind or "bt"), BT_BASE)
        target = str(folder or "").strip()
        path = target if os.path.isabs(target) else os.path.join(base, target)
        if not path or not os.path.isdir(path):
            return {"ok": False, "error": "Klasör bulunamadı."}
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── WF / ROB / TWF segment derinlemesine analiz (V8.5.8) ────────────
    # Her segment (ay/hafta/fold) zaten kendi bağımsız Backtester çıktısı
    # (backtest_trades.csv, filter_events.csv, _president/ vb.) ile
    # tamamlanmış durumda; sadece _load_bt_folder() ile okunup aynı
    # Analiz panelinde gösterilir. Hiçbir karar/hesap mantığına dokunulmaz.
    def get_wf_segment_results(self, wf_folder: str, segment: str):
        path = os.path.join(WF_BASE, str(wf_folder or ""), str(segment or ""))
        if not os.path.isdir(path):
            return {"error": "Segment bulunamadı."}
        return _load_bt_folder(path)

    def get_rob_segment_results(self, rob_folder: str, segment: str):
        path = os.path.join(ROB_BASE, str(rob_folder or ""), str(segment or ""))
        if not os.path.isdir(path):
            return {"error": "Segment bulunamadı."}
        return _load_bt_folder(path)

    def get_twf_segment_results(self, twf_folder: str, segment: str):
        path = os.path.join(TWF_BASE, str(twf_folder or ""), str(segment or ""))
        if not os.path.isdir(path):
            return {"error": "Segment bulunamadı."}
        return _load_bt_folder(path)

    def get_hourly_stats(self):
        return get_hourly_stats()

    def get_coin_stats(self):
        return get_coin_stats()

    # ── Sembol Guncelle ───────────────────────────────────────────────
    def update_symbols(self):
        def _run():
            try:
                from symbols_builder import build_top_usdt
                build_top_usdt()
                _push_log("[OK] Sembol listesi guncellendi.")
            except Exception as e:
                _push_log(f"[HATA] Sembol: {e}")
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    # ── Kara Liste ────────────────────────────────────────────────────
    def get_blacklist(self):
        return get_blacklist()

    def blacklist_add(self, symbol: str, hours: float):
        sym = symbol.strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        add_to_blacklist(sym, hours)
        _push_log(f"[Kara Liste] {sym} eklendi ({hours:.0f}h)")
        return {"ok": True}

    def blacklist_remove(self, symbol: str):
        sym = symbol.strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        remove_from_blacklist(sym)
        _push_log(f"[Kara Liste] {sym} cikarildi")
        return {"ok": True}

    # ── Config ────────────────────────────────────────────────────────
    def get_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                raw  = f.read()
                data = yaml.safe_load(raw) or {}
            return {"ok": True, "data": data, "raw": raw}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_config(self, data):
        try:
            if isinstance(data, str):
                # Ham YAML string
                yaml.safe_load(data)  # Syntax kontrolu
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(data)
            else:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True,
                              default_flow_style=False, sort_keys=False)
            _push_log("[Config] Kaydedildi.")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Backtest ──────────────────────────────────────────────────────
    def start_backtest(self, params: dict):
        global _bt_proc, _bt_active_dir
        with _bt_lock:
            if _bt_proc and _bt_proc.poll() is None:
                return {"ok": False, "error": "Backtest zaten calisiyor."}
            out_dir = _make_bt_dir(params)
            _bt_active_dir = out_dir
            days     = str(params.get("days", 30))
            interval = str(params.get("interval", "1h"))
            top      = str(params.get("top", 20))
            start    = str(params.get("start", ""))
            end      = str(params.get("end", ""))
            cmd = [sys.executable, "backtest.py",
                   "--days", days, "--interval", interval,
                   "--top", top, "--out", out_dir, "--config", CONFIG_PATH]
            if start and end:
                cmd += ["--start", start, "--end", end]
            pmode = str(params.get("president_mode", ""))
            if pmode in ("shadow", "live", "legacy"):
                cmd += ["--president-mode", pmode]

        def _run():
            global _bt_proc
            _push_log(f"[BT] Basladi: {out_dir}")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            _bt_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8",
                                        errors="replace", bufsize=1, env=env)
            for line in _bt_proc.stdout:
                line = line.rstrip()
                if line:
                    _push_log(f"[BT] {line}")
            _bt_proc.wait()
            code = _bt_proc.returncode
            _push_log("[BT] Tamamlandi." if code == 0 else f"[BT] Hata: {code}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "dir": out_dir}

    def stop_backtest(self):
        global _bt_proc
        with _bt_lock:
            if _bt_proc and _bt_proc.poll() is None:
                _bt_proc.terminate()
                _push_log("[BT] Durduruldu.")
                return {"ok": True}
        return {"ok": False, "error": "Calisan backtest yok."}

    def get_backtest_status(self):
        return {"running": bool(_bt_proc and _bt_proc.poll() is None),
                "dir": _bt_active_dir}

    def get_backtest_results(self, folder: str = ""):
        global _bt_active_dir
        if folder:
            path = folder if os.path.isabs(folder) else os.path.join(BT_BASE, folder)
        else:
            path = _bt_active_dir or ""
        if not path or not os.path.exists(path):
            return {"summary": {}, "trades": [], "equity": [], "error": "Klasor bulunamadi."}
        return _load_bt_folder(path)

    def get_backtest_history(self):
        folders = _list_folders(BT_BASE)
        result  = []
        for folder in folders[:30]:
            path  = os.path.join(BT_BASE, folder)
            meta  = {"folder": folder, "summary": {}, "config": {}}
            sp    = os.path.join(path, "backtest_summary.csv")
            if os.path.exists(sp):
                try:
                    with open(sp, newline="", encoding="utf-8-sig") as f:
                        for row in csv_mod.reader(f, delimiter=";"):
                            if len(row) >= 2:
                                meta["summary"][row[0]] = row[1]
                except Exception:
                    pass
            cp = os.path.join(path, "config_snapshot.json")
            if os.path.exists(cp):
                try:
                    with open(cp, encoding="utf-8") as f:
                        meta["config"] = json.load(f)
                except Exception:
                    pass
            result.append(meta)
        return result

    def delete_backtest(self, folder: str):
        path = os.path.join(BT_BASE, folder)
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
                _push_log(f"[BT] Silindi: {folder}")
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Bulunamadi."}

    # ── Walk-Forward ──────────────────────────────────────────────────
    def start_walkforward(self, params: dict):
        global _wf_proc, _wf_active_dir
        with _wf_lock:
            if _wf_proc and _wf_proc.poll() is None:
                return {"ok": False, "error": "WF zaten calisiyor."}
            stamp    = time.strftime("%Y-%m-%d_%H-%M")
            interval = str(params.get("interval", "1h"))
            folder   = f"{stamp}_wf_{interval}"
            out_dir  = os.path.join(WF_BASE, folder)
            os.makedirs(out_dir, exist_ok=True)
            _wf_active_dir = out_dir
            start = str(params.get("start", ""))
            end   = str(params.get("end", ""))
            top   = str(params.get("top", 20))
            cmd   = [sys.executable, "walk_forward.py",
                     "--start", start, "--end", end,
                     "--interval", interval, "--top", top,
                     "--out", out_dir, "--config", CONFIG_PATH]

        def _run():
            global _wf_proc
            _push_log(f"[WF] Basladi: {out_dir}")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            _wf_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        bufsize=1, env=env)
            for line in _wf_proc.stdout:
                line = line.rstrip()
                if line:
                    _push_log(f"[WF] {line}")
            _wf_proc.wait()
            code = _wf_proc.returncode
            _push_log("[WF] Tamamlandi." if code == 0 else f"[WF] Hata: {code}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "dir": out_dir}

    def stop_walkforward(self):
        global _wf_proc
        with _wf_lock:
            if _wf_proc and _wf_proc.poll() is None:
                _wf_proc.terminate()
                _push_log("[WF] Durduruldu.")
                return {"ok": True}
        return {"ok": False}

    def get_wf_status(self):
        return {"running": bool(_wf_proc and _wf_proc.poll() is None),
                "dir": _wf_active_dir}

    def get_wf_results(self, folder: str = ""):
        global _wf_active_dir
        if folder:
            path = folder if os.path.isabs(folder) else os.path.join(WF_BASE, folder)
        else:
            path = _wf_active_dir or ""
        if not path or not os.path.exists(path):
            return {"summary": {}, "monthly": [], "error": "Bulunamadi."}
        return _load_wf_folder(path)

    def get_wf_history(self):
        folders = _list_folders(WF_BASE)
        result  = []
        for folder in folders[:20]:
            path = os.path.join(WF_BASE, folder)
            meta = {"folder": folder, "summary": {}}
            sp   = os.path.join(path, "wf_summary.json")
            if os.path.exists(sp):
                try:
                    with open(sp, encoding="utf-8") as f:
                        meta["summary"] = json.load(f)
                except Exception:
                    pass
            result.append(meta)
        return result

    def delete_wf(self, folder: str):
        path = os.path.join(WF_BASE, folder)
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
                _push_log(f"[WF] Silindi: {folder}")
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Bulunamadi."}

    # ── GERCEK Walk-Forward (train/optimize/test/roll) ─────────────────
    def start_true_walkforward(self, params: dict):
        global _twf_proc, _twf_active_dir
        with _twf_lock:
            if _twf_proc and _twf_proc.poll() is None:
                return {"ok": False, "error": "True-WF zaten calisiyor."}
            stamp    = time.strftime("%Y-%m-%d_%H-%M")
            interval = str(params.get("interval", "1h"))
            folder   = f"{stamp}_truewf_{interval}"
            out_dir  = os.path.join(TWF_BASE, folder)
            os.makedirs(out_dir, exist_ok=True)
            _twf_active_dir = out_dir
            cmd = [sys.executable, "true_walk_forward.py",
                   "--start", str(params.get("start","")), "--end", str(params.get("end","")),
                   "--interval", interval, "--top", str(params.get("top",20)),
                   "--train-days", str(params.get("train_days",60)),
                   "--test-days", str(params.get("test_days",30)),
                   "--roll-days", str(params.get("roll_days",30)),
                   "--out", out_dir, "--config", CONFIG_PATH]

        def _run():
            global _twf_proc
            _push_log(f"[TWF] Basladi: {out_dir}")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            _twf_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
            for line in _twf_proc.stdout:
                line = line.rstrip()
                if line: _push_log(f"[TWF] {line}")
            _twf_proc.wait()
            _push_log("[TWF] Tamamlandi." if _twf_proc.returncode == 0 else f"[TWF] Hata: {_twf_proc.returncode}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "dir": out_dir}

    def stop_true_walkforward(self):
        global _twf_proc
        with _twf_lock:
            if _twf_proc and _twf_proc.poll() is None:
                _twf_proc.terminate(); _push_log("[TWF] Durduruldu."); return {"ok": True}
        return {"ok": False}

    def get_true_wf_status(self):
        return {"running": bool(_twf_proc and _twf_proc.poll() is None), "dir": _twf_active_dir}

    def get_true_wf_results(self, folder: str = ""):
        path = (folder if os.path.isabs(folder) else os.path.join(TWF_BASE, folder)) if folder else (_twf_active_dir or "")
        if not path or not os.path.exists(path):
            return {"summary": {}, "folds": [], "error": "Bulunamadi."}
        res = {"summary": {}, "folds": [], "folder": path}
        sp = os.path.join(path, "true_wf_summary.json")
        if os.path.exists(sp):
            try: res["summary"] = json.load(open(sp, encoding="utf-8"))
            except Exception: pass
        fp = os.path.join(path, "true_wf_folds.csv")
        if os.path.exists(fp):
            try:
                res["folds"] = list(csv_mod.DictReader(open(fp, encoding="utf-8-sig"), delimiter=";"))
            except Exception: pass
        return res

    def get_true_wf_history(self):
        result = []
        for folder in _list_folders(TWF_BASE)[:20]:
            meta = {"folder": folder, "summary": {}}
            sp = os.path.join(TWF_BASE, folder, "true_wf_summary.json")
            if os.path.exists(sp):
                try: meta["summary"] = json.load(open(sp, encoding="utf-8"))
                except Exception: pass
            result.append(meta)
        return result

    def delete_true_wf(self, folder: str):
        path = os.path.join(TWF_BASE, folder)
        try:
            if os.path.exists(path):
                shutil.rmtree(path); return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Bulunamadi."}

    # ── Robustluk Testi ───────────────────────────────────────────────
    def start_robustness(self, params: dict):
        global _rob_proc, _rob_active_dir
        with _rob_lock:
            if _rob_proc and _rob_proc.poll() is None:
                return {"ok": False, "error": "Robustluk testi zaten calisiyor."}
            stamp   = time.strftime("%Y-%m-%d_%H-%M")
            folder  = f"{stamp}_rob"
            out_dir = os.path.join(ROB_BASE, folder)
            os.makedirs(out_dir, exist_ok=True)
            _rob_active_dir = out_dir
            start = str(params.get("start", ""))
            end   = str(params.get("end", ""))
            top   = str(params.get("top", 20))
            intvl = str(params.get("interval", "1h"))
            cmd   = [sys.executable, "robustness_test.py",
                     "--start", start, "--end", end,
                     "--interval", intvl, "--top", top,
                     "--out", out_dir, "--config", CONFIG_PATH]

        def _run():
            global _rob_proc
            _push_log(f"[ROB] Basladi: {out_dir}")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            _rob_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace",
                                         bufsize=1, env=env)
            for line in _rob_proc.stdout:
                line = line.rstrip()
                if line:
                    _push_log(f"[ROB] {line}")
            _rob_proc.wait()
            code = _rob_proc.returncode
            _push_log("[ROB] Tamamlandi." if code == 0 else f"[ROB] Hata: {code}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "dir": out_dir}

    def stop_robustness(self):
        global _rob_proc
        with _rob_lock:
            if _rob_proc and _rob_proc.poll() is None:
                _rob_proc.terminate()
                _push_log("[ROB] Durduruldu.")
                return {"ok": True}
        return {"ok": False}

    def get_rob_status(self):
        return {"running": bool(_rob_proc and _rob_proc.poll() is None),
                "dir": _rob_active_dir}

    def get_rob_results(self, folder: str = ""):
        global _rob_active_dir
        if folder:
            path = folder if os.path.isabs(folder) else os.path.join(ROB_BASE, folder)
        else:
            path = _rob_active_dir or ""
        if not path or not os.path.exists(path):
            return {"summary": {}, "weekly": [], "error": "Bulunamadi."}
        return _load_rob_folder(path)

    def get_rob_history(self):
        folders = _list_folders(ROB_BASE)
        result  = []
        for folder in folders[:20]:
            path = os.path.join(ROB_BASE, folder)
            meta = {"folder": folder, "summary": {}}
            sp   = os.path.join(path, "robustness_summary.json")
            if os.path.exists(sp):
                try:
                    with open(sp, encoding="utf-8") as f:
                        meta["summary"] = json.load(f)
                except Exception:
                    pass
            result.append(meta)
        return result

    def delete_rob(self, folder: str):
        path = os.path.join(ROB_BASE, folder)
        try:
            if os.path.exists(path):
                shutil.rmtree(path)
                return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False}

    # ── President Verileri ────────────────────────────────────────────
    def get_president_state(self):
        try:
            import json as _json
            from president_governor import PresidentGovernor
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            pg = PresidentGovernor(cfg, DATA_DIR)
            # Risk: canli motorun kalici state'ini oku (sifirlanmasin)
            risk = pg.get_state()
            rpath = os.path.join(DATA_DIR, "risk_state.json")
            if os.path.exists(rpath):
                try:
                    d = _json.load(open(rpath, encoding="utf-8"))
                    risk = {
                        "open_longs":  d.get("open_longs", 0),
                        "open_shorts": d.get("open_shorts", 0),
                        "daily_pnl":   round(d.get("daily_pnl", 0), 2),
                        "monthly_pnl": round(d.get("monthly_pnl", 0), 2),
                        "equity":      round(d.get("equity", 0), 2),
                    }
                except Exception:
                    pass
            return {
                "ok":        True,
                "decisions": pg.load_decisions(200),
                "shadows":   pg.load_shadows(200),
                "votes":     pg.load_votes(300),
                "risk":      risk,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ── Validation / Audit / Durum ───────────────────────────────────
    def run_validate(self):
        """GUI'den sistem validasyonu çalıştırır."""
        cmd = [sys.executable, "validate_config.py"]
        try:
            proc = subprocess.run(cmd, cwd=_APP_DIR, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
            out = (proc.stdout or "") + (proc.stderr or "")
            _push_log("[VALIDATE] " + ("OK" if proc.returncode == 0 else f"FAIL {proc.returncode}"))
            for line in out.splitlines()[-30:]:
                if line.strip(): _push_log("[VALIDATE] " + line.strip())
            return {"ok": proc.returncode == 0, "code": proc.returncode, "output": out[-12000:]}
        except Exception as e:
            _push_log(f"[VALIDATE] HATA: {e}")
            return {"ok": False, "error": str(e), "output": ""}

    def run_decision_audit(self, folder: str = ""):
        """Seçili veya son backtest klasörü için decision_integrity_audit çalıştırır."""
        target = folder or _bt_active_dir or BT_BASE
        if folder and not os.path.isabs(folder):
            target = os.path.join(BT_BASE, folder)
        out_path = os.path.join(target if os.path.isdir(target) else BT_BASE, "decision_integrity_audit.md")
        cmd = [sys.executable, "decision_integrity_audit.py", target, "--out", out_path]
        try:
            proc = subprocess.run(cmd, cwd=_APP_DIR, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=90)
            out = (proc.stdout or "") + (proc.stderr or "")
            _push_log("[AUDIT] " + ("OK" if proc.returncode == 0 else f"FAIL {proc.returncode}"))
            for line in out.splitlines()[-30:]:
                if line.strip(): _push_log("[AUDIT] " + line.strip())
            audit_txt = ""
            if os.path.exists(out_path):
                try:
                    audit_txt = open(out_path, encoding="utf-8").read()[-20000:]
                except Exception:
                    audit_txt = ""
            return {"ok": proc.returncode == 0, "code": proc.returncode, "output": out[-12000:], "audit": audit_txt, "path": out_path}
        except Exception as e:
            _push_log(f"[AUDIT] HATA: {e}")
            return {"ok": False, "error": str(e), "output": "", "audit": ""}

    def get_module_status(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            return {"ok": False, "error": str(e), "modules": []}
        def exists(name): return os.path.exists(os.path.join(_APP_DIR, name))
        mods = []
        def add(name, state, detail=""):
            mods.append({"name": name, "state": state, "detail": detail})
        add("PresidentRuntime", "ACTIVE" if exists("president_runtime.py") else "MISSING", "Nihai karar zinciri")
        add("Core Long", "ACTIVE", "Ana long skor/vote motoru")
        ss = cfg.get("short_surgeon", {}) or {}
        add("Short Surgeon", "SHADOW" if ss.get("shadow_mode", True) else "ACTIVE/PAPER", f"enabled={ss.get('enabled', False)}")
        ch = cfg.get("cascade_hunter", {}) or {}
        add("Cascade Hunter", "SHADOW" if ch.get("shadow_mode", True) else "ACTIVE/PAPER", f"enabled={ch.get('enabled', False)}")
        ae = cfg.get("adaptive_exit", {}) or {}
        add("Adaptive Exit", "ACTIVE" if ae.get("enabled", False) and exists("adaptive_exit.py") else "OFF/MISSING", "Policy üretir, emir vermez")
        boa = cfg.get("block_outcome_analysis", {}) or {}
        add("BOA v2", "ACTIVE" if boa.get("enabled", False) and exists("block_outcome_analyzer.py") else "OFF/MISSING", "Blok sonrası first-hit/outcome")
        gr = (cfg.get("president", {}) or {}).get("global_ranking", {}) or {}
        add("President Global Ranking", "ACTIVE" if gr.get("enabled", False) else "OFF", "Aynı mum aday sıralama")
        bf = (cfg.get("president", {}) or {}).get("boa_feedback", {}) or {}
        add("BOA Feedback", "FEATURE_ON" if bf.get("enabled", False) else "OFF", f"max_adj={bf.get('max_adjustment', '?')}")
        qs = cfg.get("quality_score", {}) or {}
        add("Quality Score", "FEATURE_ONLY" if qs.get("enabled", False) and exists("quality_score.py") else "OFF/MISSING", "President feature")
        ar = cfg.get("adaptive_risk", {}) or {}
        add("Adaptive Risk", "SIZING_HINT" if ar.get("enabled", False) and exists("adaptive_risk.py") else "OFF/MISSING", "Risk önerisi, hard limit değil")
        wu = cfg.get("weekly_symbol_rotation", {}) or {}
        add("Weekly Universe", "ACTIVE" if wu.get("enabled", False) and exists("weekly_symbol_universe.py") else "OFF/MISSING", f"refresh={wu.get('refresh_days', '?')}d")
        rot = cfg.get("position_rotation", {}) or {}
        add("Position Rotation", "SHADOW" if rot.get("shadow_mode", True) else ("ACTIVE" if rot.get("enabled", False) else "OFF"), "Varsayılan güvenli kapalı/shadow")
        live = cfg.get("live", {}) or {}
        add("Live Mode", str(live.get("president_execution_mode", "shadow")).upper(), "shadow=karar, paper=sanal pozisyon")
        return {"ok": True, "modules": mods, "version": cfg.get("general", {}).get("version", "?")}


    def _resolve_backtest_folder(self, folder: str = ""):
        """GUI yardımcı: boşsa son aktif/son backtest klasörünü döndürür."""
        target = str(folder or "").strip()
        if target:
            return target if os.path.isabs(target) else os.path.join(BT_BASE, target)
        if _bt_active_dir and os.path.isdir(_bt_active_dir):
            return _bt_active_dir
        folders = _list_folders(BT_BASE)
        if folders:
            return os.path.join(BT_BASE, folders[0])
        return ""

    def _read_csv_rows(self, path: str, limit: int = 5000):
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = list(csv_mod.DictReader(f, delimiter=";"))
            return rows[:limit] if limit else rows
        except Exception:
            return []

    def get_ranking_summary(self, folder: str = ""):
        """V8.5.5 ranking/rejection olaylarını GUI için özetler. Claude core koduna dokunmadan sadece okuma yapar."""
        path = self._resolve_backtest_folder(folder)
        if not path or not os.path.isdir(path):
            return {"ok": False, "error": "Backtest klasörü bulunamadı.", "folder": path}
        # V8.5.7: önce ayrı candidate_ranking_events.csv okunur; yoksa geriye uyumluluk için filter_events.csv kullanılır.
        rank_file = os.path.join(path, "candidate_ranking_events.csv")
        if os.path.exists(rank_file):
            rank_rows = self._read_csv_rows(rank_file, limit=0)
        else:
            rows = self._read_csv_rows(os.path.join(path, "filter_events.csv"), limit=0)
            rank_rows = [r for r in rows if str(r.get("cause", "")).startswith("RANK_")]
        causes = {}
        sides = {}
        labels = {}
        ts_counts = {}
        for r in rank_rows:
            c = str(r.get("cause", "")) or "?"
            causes[c] = causes.get(c, 0) + 1
            side = str(r.get("side", "")) or "?"
            sides[side] = sides.get(side, 0) + 1
            lab = str(r.get("label", "")) or "?"
            labels[lab] = labels.get(lab, 0) + 1
            ts = str(r.get("ts", ""))
            if ts:
                ts_counts[ts] = ts_counts.get(ts, 0) + 1
        selected = causes.get("RANK_SELECTED", 0)
        rejected = sum(v for k, v in causes.items() if k != "RANK_SELECTED")
        last_ts = ""
        latest = []
        if rank_rows:
            last_ts = str(rank_rows[-1].get("ts", ""))
            latest = [r for r in rank_rows if str(r.get("ts", "")) == last_ts]
            def _rank_key(x):
                try: return int(float(x.get("rank", 999999)))
                except Exception: return 999999
            latest = sorted(latest, key=_rank_key)[:50]
        reason_rows = [{"cause": k, "count": v} for k, v in sorted(causes.items(), key=lambda kv: kv[1], reverse=True)]
        side_rows = [{"side": k, "count": v} for k, v in sorted(sides.items(), key=lambda kv: kv[1], reverse=True)]
        label_rows = [{"label": k, "count": v} for k, v in sorted(labels.items(), key=lambda kv: kv[1], reverse=True)]
        avg_candidates = (sum(ts_counts.values()) / len(ts_counts)) if ts_counts else 0.0
        max_candidates = max(ts_counts.values()) if ts_counts else 0
        return {
            "ok": True,
            "folder": path,
            "folder_name": os.path.basename(path),
            "selected": selected,
            "rejected": rejected,
            "total_rank_events": len(rank_rows),
            "rank_timestamps": len(ts_counts),
            "avg_candidates_per_rank_ts": round(avg_candidates, 2),
            "max_candidates_per_rank_ts": max_candidates,
            "reason_rows": reason_rows,
            "side_rows": side_rows,
            "label_rows": label_rows,
            "latest_ts": last_ts,
            "latest_rows": latest,
        }

    def get_boa_feedback_status(self, folder: str = ""):
        """BOA feedback hafızasını GUI için okur; karar motoruna yazmaz."""
        path = self._resolve_backtest_folder(folder)
        candidates = []
        if path:
            candidates.append(os.path.join(path, "boa_feedback_memory.json"))
        candidates.append(os.path.join(DATA_DIR, "boa_feedback_memory.json"))
        mem_path = next((x for x in candidates if os.path.exists(x)), "")
        if not mem_path:
            return {"ok": True, "exists": False, "path": "", "entries": 0, "top_positive": [], "top_negative": []}
        try:
            with open(mem_path, encoding="utf-8") as f:
                mem = json.load(f) or {}
        except Exception as e:
            return {"ok": False, "exists": True, "path": mem_path, "error": str(e)}
        items = []
        for key, val in mem.items():
            if not isinstance(val, dict):
                continue
            try:
                edge = float(val.get("edge", val.get("adjustment", 0.0)) or 0.0)
            except Exception:
                edge = 0.0
            items.append({
                "key": key,
                "edge": round(edge, 4),
                "count": val.get("count", val.get("n", "")),
                "tp_rate": val.get("tp_rate", ""),
                "sl_rate": val.get("sl_rate", ""),
                "avg_close": val.get("avg_close", val.get("avg_close_return", "")),
            })
        items_sorted = sorted(items, key=lambda x: float(x.get("edge", 0.0)), reverse=True)
        return {
            "ok": True,
            "exists": True,
            "path": mem_path,
            "entries": len(items),
            "top_positive": items_sorted[:25],
            "top_negative": list(reversed(items_sorted[-25:])),
        }

    def get_ranking_boa_dashboard(self, folder: str = ""):
        """Tek çağrıda Ranking + BOA feedback durumunu döndürür."""
        return {"ranking": self.get_ranking_summary(folder), "boa_feedback": self.get_boa_feedback_status(folder)}

    # ── Sistem Bilgisi ────────────────────────────────────────────────
    def get_sys_info(self):
        version = "8.5.4"
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            version = str(cfg.get("general", {}).get("version", version))
        except Exception:
            pass
        return {
            "version":   version,
            "name":      "TRBOT President System",
            "python":    sys.version,
            "app_dir":   _APP_DIR,
            "sim_ok":    _SIM_OK,
        }


# ─── PyWebView Baslat ────────────────────────────────────────────────────────
if __name__ == "__main__":
    api    = API()
    window = webview.create_window(
        title    = "TRBOT President System — V8",
        url      = "gui.html",
        js_api   = api,
        width    = 1500,
        height   = 900,
        min_size = (1100, 750),
        resizable= True,
    )
    webview.start(debug=False)
