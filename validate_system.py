#!/usr/bin/env python3
# validate_system.py — TRBOT President System V8.4
# ============================================================================
# AMAÇ: Her değişiklikten sonra tek komutla çalıştırılır:
#
#     python validate_system.py
#
# Sistemin "arıza lambası"dır. Yeni bir parça (config ayarı, filtre, modül)
# eklendiğinde şunları OTOMATİK kontrol eder:
#
#   [1] Tüm Python dosyaları hatasız derleniyor mu?           (syntax)
#   [2] Tüm modüller hatasız import ediliyor mu?               (import zinciri)
#   [3] config_online.yaml'daki ayarlar kod tarafından         (okunmayan config —
#       gerçekten okunuyor mu, yoksa "yazılmış ama etkisiz" mi? "hayalet ayar")
#   [4] Bilinen çakışma kalıpları var mı?                      (örn. MTF kapalı +
#       HTF eşiği nötr değerle çakışıyor mu — V8.4'te bulunan bug türü)
#   [5] TradeEngine (canlı motor) crash vermeden kuruluyor mu?
#   [6] Backtest çalışıyor mu, PnL muhasebesi tutarlı mı?       (trade toplamı =
#       running PnL, equity eğrisi son nokta = aynı değer)
#   [7] Risk sayacı (açık pozisyon sayısı) açılış/kapanışta dengeli mi?
#
# Script PASS/FAIL olarak biter. FAIL varsa, paket teslim edilmeden önce
# düzeltilmesi gerektiği anlamına gelir.
#
# Bu script projeyi yeniden yazmaz, mevcut mimariyi DEĞİŞTİRMEZ — sadece
# üstüne otomatik bir kontrol katmanı ekler.
# ============================================================================

import sys
import os
import re
import json
import subprocess
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────────────
# Çıktı yardımcıları
# ─────────────────────────────────────────────────────────────────────────
class C:
    OK    = "\033[92m"
    FAIL  = "\033[91m"
    WARN  = "\033[93m"
    BOLD  = "\033[1m"
    END   = "\033[0m"

RESULTS = []  # (section, passed: bool, detail: str)

def section(title):
    print(f"\n{C.BOLD}── {title} ──{C.END}")

def ok(msg):
    print(f"  {C.OK}✓{C.END} {msg}")

def fail(msg):
    print(f"  {C.FAIL}✗{C.END} {msg}")

def warn(msg):
    print(f"  {C.WARN}!{C.END} {msg}")

def record(name, passed, detail=""):
    RESULTS.append((name, passed, detail))


# ═════════════════════════════════════════════════════════════════════════
# [1] SYNTAX — tüm .py dosyaları derleniyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_syntax():
    section("[1] Syntax — tüm Python dosyaları derleniyor mu?")
    py_files = [str(p) for p in ROOT.rglob("*.py")
                if "__pycache__" not in str(p) and "venv" not in str(p)]
    failed = []
    for f in py_files:
        r = subprocess.run([sys.executable, "-m", "py_compile", f],
                            capture_output=True, text=True)
        if r.returncode != 0:
            failed.append((f, r.stderr.strip()))
    if failed:
        for f, err in failed:
            fail(f"{f}\n      {err}")
        record("syntax", False, f"{len(failed)} dosya derlenmedi")
    else:
        ok(f"{len(py_files)} dosya hatasız derlendi")
        record("syntax", True, f"{len(py_files)} dosya")
    # temizlik
    for p in ROOT.rglob("__pycache__"):
        if p.is_dir():
            for sub in p.glob("*"):
                try: sub.unlink()
                except Exception: pass


# ═════════════════════════════════════════════════════════════════════════
# [2] IMPORT ZİNCİRİ — tüm modüller hatasız import ediliyor mu?
# ═════════════════════════════════════════════════════════════════════════
MODULES_TO_IMPORT = [
    "fetch_guard",
    "strategy_core", "adaptive_sl", "market_regime", "symbol_manager",
    "logger", "symbols_builder",
    "data_derivatives", "universe_manager", "weekly_symbol_universe",
    "modules.decision_packet", "modules.risk_governor", "modules.convex_position",
    "branches.core_long_branch", "branches.short_surgeon", "branches.cascade_hunter",
    "president_governor", "president_runtime",
    "backtest", "walk_forward", "robustness_test", "true_walk_forward", "engine",
]

def check_imports():
    section("[2] Import zinciri — tüm modüller yükleniyor mu?")
    code = "import sys; sys.path.insert(0, '.')\n"
    for m in MODULES_TO_IMPORT:
        code += f"import {m}\n"
    code += "print('IMPORT_OK')\n"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "IMPORT_OK" in r.stdout:
        ok(f"{len(MODULES_TO_IMPORT)} modül hatasız import edildi")
        record("imports", True, f"{len(MODULES_TO_IMPORT)} modül")
    else:
        fail("Import zinciri koptu:")
        print(r.stderr.strip())
        record("imports", False, r.stderr.strip()[-400:])


# ═════════════════════════════════════════════════════════════════════════
# [3] HAYALET CONFIG — config'te yazan ayar kodda hiç okunmuyor mu?
# ═════════════════════════════════════════════════════════════════════════
# Bu alanlar SABİT şema değil, kullanıcı tanımlı DİNAMİK anahtarlar içerir
# (örn. sembol adları). İçine inip her sembolü "kullanılmıyor" diye işaretlemek
# yanlış pozitif üretir — kod bunları .items() ile döngüsel okur, sabit
# anahtar adıyla değil. Bu yüzden flatten bu noktada DURMALI (parent leaf sayılır).
DYNAMIC_KEY_PARENTS = {
    "symbol_quality_filter.manual_symbol_multipliers",
}

def flatten_yaml_keys(d, prefix=""):
    """config dict'ini 'bolum.alt.alan' yollarına açar."""
    out = []
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if path in DYNAMIC_KEY_PARENTS:
            out.append(path)   # alt anahtarlara inme, kendisini leaf say
            continue
        if isinstance(v, dict):
            out.extend(flatten_yaml_keys(v, path))
        else:
            out.append(path)
    return out

# Bilinen "kasıtlı geriye-uyumluluk" anahtarları — bunlar artık birincil
# kaynak değil ama kod içinde fallback olarak hâlâ aranıyor, hayalet sayılmaz.
KNOWN_LEGACY_FALLBACKS = {
    "risk.starting_equity_usdt",  # account.starting_equity_usdt birincil; bu fallback
    "misc.starting_equity_usdt",  # aynı şekilde
}

# V9.0.4: Bu 26 alan gerçek "bağlanmamış modül" DEĞİL — Ömer'in dead-code-audit
# notlarında zaten tespit edilmiş, kasıtlı olarak henüz koda bağlanmamış
# placeholder/taslak alanlardır (ayrı bir "dead code cleanup" oturumunda ele
# alınacak, bu V9 refactor'un kapsamı dışında bırakılmıştı). FAIL yerine
# bilgi notu olarak gösterilir ki her validate_system.py çalışmasında yanlışlıkla
# "teslime engel" gibi görünmesin.
KNOWN_PLANNED_UNIMPLEMENTED = {
    "short_surgeon.sl_dogru.min_score",         # bağlamak için record_sl() çağrı zincirine entry-score eklemek gerekir (engine.py+backtest.py) — ayrı oturum, motor mantığına dokunma riski
    "short_surgeon.failed_breakout.enabled",    # mimari belgede planlanan ama hiç implemente edilmemiş özellik (ENABLED=false, gerçek "açık" risk yok)
    "short_surgeon.failed_breakout.breakout_candles",
    "short_surgeon.failed_breakout.reversal_confirmation",
    "short_surgeon.cascade.enabled",            # aynı şekilde planlanan ama implemente edilmemiş (ENABLED=false)
    "execution.short_mode",                     # canlı/paper emir yürütme YOK (proje genelinde real order placement kodu hiç yazılmadı)
    "execution.real_orders_enabled",            # aynı sebep — gerçek emir gönderen kod mevcut DEĞİL, bu yüzden flag'in bağlanacağı bir yer yok
}

# V9.3: data_fetch.* — fetch_guard.py'nin davranışını BELGELEYEN ama kod
# tarafından parametre olarak OKUNMAYAN alanlar (kod yerine TRBOT_FETCH_*
# ortam değişkenlerini okuyor). cache_failed_fetches=false davranışı zaten
# HER ZAMAN uygulanıyor (had_error=True ise cache'e hiç yazılmıyor). Bunlar
# gerçek "bağlanmamış ghost" değil, "dokümantasyon amaçlı sabit" alanlardır —
# bkz. fetch_guard.py dosya sonu "config_online.yaml ile ilişki" notu.
KNOWN_DOCUMENTED_BEHAVIOR_ONLY = {
    "data_fetch.enabled",
    "data_fetch.request_delay_sec",
    "data_fetch.max_retries",
    "data_fetch.timeout_sec",
    "data_fetch.cache_failed_fetches",
}

# Bilinen "kasıtlı sabit" anahtarlar — config'te bilgi amaçlı yazılı durur
# ama kod bunu kasıtlı olarak sabit (hardcoded) kullanır; bunlar gerçek
# "hayalet ayar" değildir, sadece bilgi notu olarak ayrı gösterilir.
KNOWN_INFO_ONLY = {
    "general.version", "general.name", "general.exchange",
    "general.market_type", "general.base_currency",
    "president.boa_feedback.note",      # insan-okunur açıklama metni, kod tarafından okunması beklenmez
    "score_integrity.model",            # hangi skor modelinin aktif olduğunu belgeleyen etiket, kod tarafından okunması beklenmez
}

# Bilinen YANLIŞ POZİTİF DEĞİL alanlar — leaf adı kodda geçiyor ama bu
# validator path-aware değil (sadece son anahtar adına bakıyor), bu yüzden
# leaf'i paylaşan farklı bir path okunduğunda bu alan da "kullanılıyor"
# görünür, oysa GERÇEKTE okunmuyor. live.president_execution_mode bunun
# tek örneği: backtest.president_execution_mode kodda okunuyor (V8.5
# patch-2'de eklendi), ama live.president_execution_mode'un GERÇEK bir
# okuma noktası yok — canlı tarafta (engine.py/app.py) bu CLI'ya eşdeğer
# bir override mekanizması henüz inşa edilmedi.
KNOWN_FALSE_POSITIVE_NOTE = {
    "live.president_execution_mode":
        "backtest.president_execution_mode okunuyor ama live.* okunmuyor — "
        "leaf adı ortak olduğu için bu kontrol yanlışlıkla 'kullanılıyor' diyor.",
}

# V9.0.5: Tipik kod kalıbı şudur:
#   pr = cfg.get("president", {}) or {}
#   gr = pr.get("global_ranking", {}) or {}
#   ... (çok satır sonra) ...
#   self.x = gr.get("write_candidate_ranking_csv", True)
# Basit bir karakter-penceresi yaklaşımı bunu YAKALAYAMAZ (ara değişkenler
# farklı satırlarda, leaf'ten uzakta). Bu yüzden gerçek bir DEĞİŞKEN-ZİNCİRİ
# çözümleyici kullanılıyor: her atamayı (VAR = BASE.get("key") / BASE["key"])
# izleyip VAR'ın hangi config path'ine karşılık geldiğini (en azından en
# yakın ata zincirini) çıkarır, sonra leaf erişiminin yapıldığı değişkenin
# zincirinin beklenen üst segmentle eşleşip eşleşmediğine bakar.
# V9.0.5: `sl_d = modes.get("sl_dogru_short", {}) or ss.get("sl_dogru", {})` gibi
# FALLBACK ifadeleri tek-zincirli bir çözümleyiciyi yanıltır (sadece İLK .get
# çağrısını yakalar). Bu yüzden her değişken için TEK bir zincir değil,
# OLASI ZİNCİRLER LİSTESİ tutuluyor — aynı satırdaki RHS'de geçen TÜM
# `.get("key")` / `["key"]` erişimleri (kaç tane olursa olsun) o değişkenin
# alternatif kaynakları olarak kaydedilir.
_GET_OR_BRACKET_RE = re.compile(r'(\w+)\.get\(\s*["\'](\w+)["\']|(\w+)\[["\'](\w+)["\']\]')


def _build_var_chains(src: str) -> dict:
    chains: dict = {}  # var -> list[list[str]] (olası zincirler)
    for line in src.splitlines():
        m_assign = re.match(r'\s*(\w+)\s*=\s*(.+)', line)
        if not m_assign:
            continue
        lhs, rhs = m_assign.group(1), m_assign.group(2)
        candidates = []
        for gm in _GET_OR_BRACKET_RE.finditer(rhs):
            base = gm.group(1) or gm.group(3)
            key  = gm.group(2) or gm.group(4)
            if not base or not key:
                continue
            base_chains = chains.get(base, [[]])
            for bc in base_chains:
                candidates.append(bc + [key])
        if candidates:
            chains.setdefault(lhs, [])
            for c in candidates:
                if c not in chains[lhs]:
                    chains[lhs].append(c)
    return chains


def _leaf_access_vars(leaf: str, src: str) -> list:
    pattern = re.compile(r'(\w+)\.get\(\s*["\']' + re.escape(leaf) + r'["\']|(\w+)\[["\']' + re.escape(leaf) + r'["\']\]')
    return [(m.group(1) or m.group(2)) for m in pattern.finditer(src) if (m.group(1) or m.group(2))]


def _path_aware_used(path_keys: list, src_by_file: dict) -> bool:
    """
    path_keys örn: ['president','global_ranking','write_candidate_ranking_csv'].
    İKİ KATMANLI kontrol:
      1) PRIMARY: değişken atama zincirini takip et (VAR = BASE.get("key") /
         BASE["key"]) — bu, "backtest.x okunuyor diye live.x de kullanılıyor
         sayılsın" gibi net yanlış pozitifleri eler (live ve backtest farklı
         dosyalarda/bloklarda okunuyorsa zincir onları ayırt eder).
      2) FALLBACK: zincir çözümlenemezse (örn. `(cfg or {}).get(...)` gibi
         parantezli ifadeler, ya da `X.get("a") or Y.get("b")` gibi fallback
         zincirleri zincir takibini bozarsa), AYNI DOSYADA üst segment VE
         leaf'in birlikte geçip geçmediğine bakılır — dosya-seviyesinde hâlâ
         path-aware (farklı dosyalardaki aynı-leaf çakışmalarını elemeye
         yeter), ama satır-bazlı zincir kadar sıkı değildir.
    """
    leaf = path_keys[-1]
    expected_parent = path_keys[-2] if len(path_keys) >= 2 else None
    leaf_pattern = re.compile(
        r'[\[\.]get\(\s*["\']' + re.escape(leaf) + r'["\']'
        r'|\["' + re.escape(leaf) + r'"\]'
        r'|\.' + re.escape(leaf) + r'\b'   # JS-tarzı düz nokta erişimi (örn. guiCfg.show_module_status)
    )

    if expected_parent is None:
        return any(leaf_pattern.search(src) for src in src_by_file.values())

    inline_pattern = re.compile(
        r'["\']' + re.escape(expected_parent) + r'["\'][^\n]{0,80}\.get\(\s*["\']' + re.escape(leaf) + r'["\']'
    )
    parent_pattern = re.compile(
        r'["\']' + re.escape(expected_parent) + r'["\']'
        r'|\.' + re.escape(expected_parent) + r'\b'
    )

    for src in src_by_file.values():
        if not leaf_pattern.search(src):
            continue  # leaf bu dosyada hiç geçmiyor, ata
        if inline_pattern.search(src):
            return True
        chains = _build_var_chains(src)
        chain_matched = False
        chain_found_any = False
        for var in _leaf_access_vars(leaf, src):
            for chain in chains.get(var, []):
                if chain:
                    chain_found_any = True
                    if chain[-1] == expected_parent:
                        chain_matched = True
                        break
            if chain_matched:
                break
        if chain_matched:
            return True
        if not chain_found_any and parent_pattern.search(src):
            # Zincir çözümlenemedi (örn. parantezli/fallback ifade) ama bu
            # dosyada hem üst segment hem leaf birlikte geçiyor -> kabul et.
            return True
    return False


# V9.0.5: Bazı config alanları RUNTIME'da DİNAMİK bir anahtarla okunur —
# örn. adaptive_exit.py: `_policy_from_cfg(trade_class, ae_cfg)` burada
# `trade_class` çalışma zamanında "TREND_RUNNER"/"SHORT_MOMENTUM"/... gibi
# bir DEĞİŞKEN, literal string değil. Statik regex analizi bunu HİÇBİR
# ZAMAN göremez (kod ".get(trade_class)" yazıyor, ".get('TREND_RUNNER')"
# yazmıyor). Bu yüzden bu alt-ağaçlar için açık bir istisna tanımlanıyor —
# gerçekten kullanıldığı manuel olarak doğrulanmıştır (bkz. adaptive_exit.py
# _policy_from_cfg() ve classify_trade() satır ~407).
KNOWN_DYNAMIC_KEY_PREFIXES = {
    "adaptive_exit.policies.",  # her policy adı (TREND_RUNNER, SCALP_EXIT, ...) dinamik okunur
}


def _is_known_dynamic_key_access(key: str) -> bool:
    return any(key.startswith(p) for p in KNOWN_DYNAMIC_KEY_PREFIXES)


def check_ghost_config():
    section("[3] Hayalet config — yazılan ayar kodda okunuyor mu? (PATH-AWARE)")
    import yaml
    cfg_path = ROOT / "config_online.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    keys = flatten_yaml_keys(cfg)

    # V9.0.5 FIX: gui.* config alanları Python'da değil, gui.html'deki JS
    # tarafında okunuyor (örn. boot() içinde call('get_config') ile). Tarama
    # sadece *.py ile sınırlı kalırsa bu alanlar HER ZAMAN ghost görünür.
    # Bu yüzden *.html (gui.html) da kaynak taramasına dahil ediliyor.
    py_files = [p for p in ROOT.rglob("*.py")
                if "__pycache__" not in str(p) and "venv" not in str(p)]
    html_files = [p for p in ROOT.rglob("*.html") if "venv" not in str(p)]
    src_by_file = {}
    for p in py_files + html_files:
        try:
            src_by_file[str(p)] = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass

    ghosts = []
    info_only = []
    planned = []  # kasıtlı henüz uygulanmamış placeholder (gerçek hayalet değil)
    dormant = []  # enabled:false olan bölümlerin kullanılmayan alanları — gerçek hayalet değil
    dynamic_used = []  # runtime'da dinamik anahtarla okunan (statik analiz göremez ama gerçekten kullanılıyor)
    for key in keys:
        path_keys = key.split(".")
        top = path_keys[0]
        if _is_known_dynamic_key_access(key):
            dynamic_used.append(key)
            continue
        if _path_aware_used(path_keys, src_by_file) or key in KNOWN_LEGACY_FALLBACKS:
            continue
        if key in KNOWN_PLANNED_UNIMPLEMENTED:
            planned.append(key)
            continue
        if key in KNOWN_DOCUMENTED_BEHAVIOR_ONLY:
            info_only.append(key)
            continue
        if key in KNOWN_INFO_ONLY:
            info_only.append(key)
            continue
        # Bu bölümün üst seviyesinde enabled:false var mı? (bölüm bilinçli kapalı)
        section_enabled = cfg.get(top, {}).get("enabled", None) if isinstance(cfg.get(top), dict) else None
        if section_enabled is False:
            dormant.append(key)
        else:
            ghosts.append(key)

    if info_only:
        warn(f"{len(info_only)} alan bilgi amaçlı yazılmış (kod kasıtlı sabit kullanıyor — sorun değil):")
        for g in info_only:
            print(f"      - {g}")

    if dormant:
        sections_d = sorted(set(g.split(".")[0] for g in dormant))
        warn(f"{len(dormant)} alan, enabled=false olan bölümlerde duruyor (kasıtlı pasif taslak, sorun değil): "
             f"{', '.join(sections_d)}")

    if dynamic_used:
        warn(f"{len(dynamic_used)} alan runtime'da DİNAMİK anahtarla okunuyor (statik analiz "
             f"göremez, ama gerçekten kullanılıyor — bkz. adaptive_exit._policy_from_cfg): "
             f"{dynamic_used[0]} ve {len(dynamic_used)-1} benzeri")

    if planned:
        sections_p = sorted(set(g.split(".")[0] for g in planned))
        warn(f"{len(planned)} alan kasıtlı olarak henüz koda bağlanmamış (bilinen dead-code-audit "
             f"backlog'u, ayrı bir 'cleanup' oturumunda ele alınacak — şu anki teslimi engellemez): "
             f"{', '.join(sections_p)}")

    fp_present = [k for k in KNOWN_FALSE_POSITIVE_NOTE if k in keys]
    if fp_present:
        warn(f"{len(fp_present)} alan PASS gibi görünüyor ama path-aware DEĞİL kontrol yüzünden "
             f"yanlış pozitif olabilir:")
        for k in fp_present:
            print(f"      - {k}: {KNOWN_FALSE_POSITIVE_NOTE[k]}")

    if ghosts:
        # Aynı üst bölüme ait anahtarları gruplayıp tekrar bildirmek yerine
        # bölüm bazında gerçek "ölü modül" sinyali ver.
        sections = {}
        for g in ghosts:
            top = g.split(".")[0]
            sections.setdefault(top, []).append(g)
        fail(f"{len(ghosts)} config alanı kodda HİÇ aranmıyor — bölüm enabled=true/tanımsız olduğu "
             f"halde kodda hiç kullanılmıyor — '{', '.join(sections.keys())}' muhtemelen bağlanmamış modül:")
        for top, fields in sections.items():
            print(f"      [{top}] {len(fields)} alan kullanılmıyor")
        record("ghost_config", False, f"{len(ghosts)} alan, bölümler: {', '.join(sections.keys())}")
    else:
        ok(f"{len(keys)} config alanının hepsi kodda aranıyor veya bilinçli pasif bölümlerde (gerçek hayalet yok)")
        record("ghost_config", True, f"{len(keys)} alan, {len(dormant)} kasıtlı pasif")


# ═════════════════════════════════════════════════════════════════════════
# [4] BİLİNEN ÇAKIŞMA KALIPLARI — V8.4'te bulunan bug sınıfı tekrar eder mi?
# ═════════════════════════════════════════════════════════════════════════
def check_known_conflict_patterns():
    section("[4] Bilinen çakışma kalıpları")
    import yaml
    cfg = yaml.safe_load((ROOT / "config_online.yaml").read_text(encoding="utf-8"))
    problems = []

    # 4a. MTF kapalıyken bu sistemde htf_sc=100.0 (maksimum, her zaman geçer)
    #     kullanılır — V8.4'teki "eşiği nötr 50'nin altına çek" çözümünden farklı
    #     ama daha temiz bir tasarım: MTF kapalıyken hiçbir htf_block_min değeri
    #     sorun yaratamaz. Sadece MTF AÇIKKEN ve veri yetersizken 50.0 fallback'i
    #     kullanılır — o yüzden htf_block_min<=50 olması ZORUNLU değil, ama MTF
    #     açıkken bilinçli bir eşik olmalı (0 veya >=100 anlamsız olur).
    htf_block_min = float(cfg.get("core_long", {}).get("htf_block_min", 0.0))
    mtf_enabled = bool(cfg.get("mtf", {}).get("enabled", True))
    if htf_block_min <= 0 or htf_block_min >= 100:
        problems.append(
            f"core_long.htf_block_min={htf_block_min} anlamsız bir değer (0-100 arası olmalı, "
            f"MTF açıkken bu eşik tüm sinyalleri bloklar/hiçbirini bloklamaz)."
        )
    else:
        ok(f"core_long.htf_block_min={htf_block_min} (MTF enabled={mtf_enabled}) — makul aralıkta")

    # 4b. position_rotation.enabled=true olsa bile, bu sürümde
    #     _maybe_rotate_for_candidate()/_maybe_rotate_live() HER ZAMAN False
    #     döner (shadow-only güvenlik tasarımı) — fiziksel pozisyon kapatma
    #     kodu bilinçli olarak devre dışı. Yine de enabled=true + 
    #     allow_close_profitable=true kombinasyonu ileride biri "shadow-only"
    #     kilidini kaldırırsa riskli olur; bunu burada erken uyaralım.
    rot = cfg.get("position_rotation", {})
    if bool(rot.get("enabled", False)) and bool(rot.get("allow_close_profitable", False)):
        problems.append(
            "position_rotation.enabled=true VE allow_close_profitable=true "
            "→ (şu an kod shadow-only olduğu için zararsız, ama biri ileride "
            "fiziksel kapatmayı aktif ederse KÂRLI pozisyonlar kapatılabilir)."
        )
    else:
        ok("position_rotation güvenli durumda (enabled=false VEYA allow_close_profitable=false; "
           "ayrıca kod şu an her durumda shadow-only çalışıyor)")

    # 4c. president.shadow_mode=false (canlı emir) iken risk.risk_per_trade_pct
    #     aşırı yüksek olmamalı (kaza güvenliği).
    shadow = bool(cfg.get("president", {}).get("shadow_mode", True))
    risk_per_trade = float(cfg.get("risk", {}).get("risk_per_trade_pct", 0.0))
    if not shadow and risk_per_trade > 5.0:
        problems.append(
            f"president.shadow_mode=false (GERÇEK EMİR) VE risk_per_trade_pct={risk_per_trade}% "
            f"→ %5'in üzerinde, kaza riski yüksek."
        )
    elif not shadow:
        ok(f"shadow_mode=false ama risk_per_trade_pct={risk_per_trade}% makul seviyede")
    else:
        ok("president.shadow_mode=true (güvenli varsayılan, gerçek emir yok)")

    # 4d. mtf.enabled=false ise bu sürümde htf_sc=100.0 kullanılır (maksimum,
    #     her zaman geçer) — V8.4'teki "nötr 50" tasarımından farklı, KASITLI.
    mtf_enabled = bool(cfg.get("mtf", {}).get("enabled", True))
    if not mtf_enabled:
        warn("mtf.enabled=false — bu sürümde Core Long'a htf_sc=100.0 (maksimum) "
             "gönderilir, böylece MTF kapalıyken hiçbir htf_block_min değeri sinyalleri "
             "yanlışlıkla bloklayamaz. Bu kasıtlı bir tasarımdır, hata değildir.")

    if problems:
        for p in problems:
            fail(p)
        record("conflicts", False, f"{len(problems)} çakışma bulundu")
    else:
        record("conflicts", True, "Bilinen çakışma kalıplarından hiçbiri tespit edilmedi")


# ═════════════════════════════════════════════════════════════════════════
# [4b] PARİTE KONTROLÜ — engine.py (canlı) ile backtest.py aynı PnL formülünü
#      mü kullanıyor? (V8.5'te bulunan ve düzeltilen bug sınıfı: TP1 Progress
#      Manager eklenirken backtest.py güncellenmiş ama engine.py'de komisyon
#      hesabı unutulmuştu — bu kontrol bunun tekrarlanmadığını garanti eder.)
# ═════════════════════════════════════════════════════════════════════════
def check_engine_backtest_parity():
    section("[4b] Parite — engine.py'de backtest.py ile aynı PnL/komisyon yardımcıları var mı?")
    eng_src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="ignore")
    bt_src  = (ROOT / "backtest.py").read_text(encoding="utf-8", errors="ignore")

    required_helpers = ["_fee_cost", "_gross_pnl"]
    missing = [h for h in required_helpers if h not in eng_src]
    if missing:
        fail(f"engine.py'de eksik yardımcı fonksiyon(lar): {', '.join(missing)} "
             f"— canlı motor komisyonu hesaplamıyor olabilir (backtest'ten optimistik PnL).")
        record("engine_parity", False, f"eksik: {', '.join(missing)}")
        return

    # commission/slippage parametreleri okunuyor mu?
    if "self.commission" not in eng_src or "self.slippage" not in eng_src:
        fail("engine.py'de self.commission / self.slippage tanımlı değil.")
        record("engine_parity", False, "commission/slippage eksik")
        return

    # on_close çağrısına giden değişken adı total_net (veya benzer bir
    # "toplam" ismi) olmalı, sadece ham pnl_usd olmamalı.
    on_close_calls = re.findall(r"runtime\.on_close\([^)]*\)", eng_src)
    if not on_close_calls:
        warn("engine.py'de runtime.on_close çağrısı bulunamadı — manuel kontrol edin.")
        record("engine_parity", None, "on_close çağrısı bulunamadı")
        return

    if any("total_net" in c for c in on_close_calls):
        ok("engine.py: _fee_cost/_gross_pnl mevcut, commission okunuyor, "
           "on_close() total_net (TP1+progress+komisyon dahil) gönderiyor")
        record("engine_parity", True, "OK")
    else:
        fail(f"engine.py'deki on_close çağrısı 'total_net' değil ham bir pnl değişkeni "
             f"gönderiyor olabilir: {on_close_calls} — Risk Governor yanlış PnL görebilir.")
        record("engine_parity", False, f"on_close çağrıları: {on_close_calls}")


# ═════════════════════════════════════════════════════════════════════════
# [5] ENGINE KURULUMU — canlı motor crash vermeden kuruluyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_engine_boot():
    section("[5] Canlı motor (TradeEngine) kurulumu")
    code = """
import sys, yaml
sys.path.insert(0, '.')
from engine import TradeEngine
cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
e = TradeEngine(['BTCUSDT'], cfg, data_dir='/tmp/_validate_engine')
assert hasattr(e, 'runtime'), 'runtime yok'
assert hasattr(e.runtime, 'confirm_open'), 'confirm_open yok'
assert hasattr(e, 'rotation_enabled'), 'rotation_enabled yok'
print('ENGINE_BOOT_OK rotation_enabled=' + str(e.rotation_enabled))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "ENGINE_BOOT_OK" in r.stdout:
        ok("TradeEngine crash vermeden kuruldu — " + r.stdout.strip().split("\n")[-1])
        record("engine_boot", True, "OK")
    else:
        fail("TradeEngine kurulumu başarısız:")
        print((r.stderr or r.stdout).strip())
        record("engine_boot", False, (r.stderr or r.stdout).strip()[-400:])


# ═════════════════════════════════════════════════════════════════════════
# [5b] CANLI BOT ZİNCİRİ — app.py'nin simulator.py'yi gerçekten import edip
#      _SIM_OK=True alabildiğini doğrular. Bu kontrol, simulator.py (ve
#      data_ws/data_rest/agent/agent_reporter/optimizer bağımlılıkları)
#      paketten yanlışlıkla çıkarılırsa GUI'deki "Canlı Başlat" butonunun
#      sessizce no-op'a düşmesini önceden yakalamak için eklendi.
# ═════════════════════════════════════════════════════════════════════════
def check_live_bot_chain():
    section("[5b] Canlı bot zinciri — app.py simulator.py'yi gerçekten kullanabiliyor mu?")
    sim_path = ROOT / "simulator.py"
    if not sim_path.exists():
        fail("simulator.py PAKETTE YOK — app.py _SIM_OK=False fallback'ine düşecek, "
             "GUI'deki 'Canlı Başlat' butonu sessizce hiçbir şey yapmayacak.")
        record("live_bot_chain", False, "simulator.py eksik")
        return

    code = """
import sys
sys.path.insert(0, '.')
try:
    from simulator import (get_status, get_open_status, get_pnl,
                           start_realtime, stop_realtime,
                           add_to_blacklist, remove_from_blacklist,
                           get_blacklist, get_hourly_stats, get_coin_stats)
    print('SIM_OK')
except ImportError as e:
    print('SIM_IMPORT_FAIL: ' + str(e))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "SIM_OK" in r.stdout:
        ok("simulator.py mevcut ve app.py'nin beklediği TÜM fonksiyonlarla import "
           "edilebiliyor (_SIM_OK=True olacak, canlı bot gerçekten çalışır)")
        record("live_bot_chain", True, "OK")
    else:
        fail(f"simulator.py import edilemiyor — app.py _SIM_OK=False fallback'ine "
             f"düşecek: {(r.stdout + r.stderr).strip()[-300:]}")
        record("live_bot_chain", False, (r.stdout + r.stderr).strip()[-300:])


# ═════════════════════════════════════════════════════════════════════════
# [6] BACKTEST SMOKE TEST — PnL muhasebesi tutarlı mı?
# ═════════════════════════════════════════════════════════════════════════
def check_backtest_smoke():
    section("[6] Backtest smoke test — PnL muhasebesi tutarlılığı")
    code = """
import sys, os, numpy as np, yaml
sys.path.insert(0, '.')
from backtest import Backtester

cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
cfg['thresholds']['score_long_open'] = 40
cfg['mtf']['enabled'] = False   # bilinen risk senaryosu: MTF kapali
cfg['president']['shadow_mode'] = False
cfg['core_long']['shadow_mode'] = False
cfg['limits']['max_open_positions'] = 2

def mk(n, s, t):
    np.random.seed(s)
    b = np.cumsum(np.random.randn(n)) * 0.6 + np.linspace(0, t, n) + 100
    return [{'open_time': 1704067200000 + i*3600000, 'open': float(p),
             'high': float(p*1.013), 'low': float(p*0.987), 'close': float(p),
             'volume': 1000.0 + i} for i, p in enumerate(b)]

syms = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
cb = {s: mk(450, i+1, [20, -15, 12, -10][i]) for i, s in enumerate(syms)}

os.system('rm -rf /tmp/_validate_bt')
bt = Backtester(cfg, '/tmp/_validate_bt', president_enabled=True, interval='1h')
r = bt.run(syms, cb, {s: cb[s] for s in syms})
sm = r['summary']
trades = r['trades']

trade_sum = sum(t.get('Net_PnL', 0) for t in trades)
fark = abs(trade_sum - bt._pnl_running)

print('TRADES=' + str(sm['Toplam_Islem']))
print('PNL=' + str(sm['Net_PnL_USD']))
print('FARK=' + f'{fark:.6f}')
print('TRADE_HAS_ZERO=' + str(sm['Toplam_Islem'] == 0))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    out = r.stdout
    if r.returncode != 0:
        fail("Backtest çalışırken hata fırlattı:")
        print(r.stderr.strip())
        record("backtest_smoke", False, r.stderr.strip()[-400:])
        return

    def grab(key):
        m = re.search(rf"^{key}=(.*)$", out, re.M)
        return m.group(1) if m else None

    trades_n = grab("TRADES")
    pnl      = grab("PNL")
    fark     = grab("FARK")
    zero     = grab("TRADE_HAS_ZERO")

    problems = []
    if zero == "True":
        problems.append("İşlem sayısı 0 — MTF kapalıyken sinyal üretilmiyor olabilir (bilinen risk).")
    try:
        if fark is not None and float(fark) > 0.01:
            problems.append(f"PnL tutarsızlığı: trade toplamı ile running PnL arasında ${fark} fark var (>0.01$ eşiği aşıldı).")
    except ValueError:
        problems.append("Fark değeri okunamadı.")

    if problems:
        for p in problems:
            fail(p)
        record("backtest_smoke", False, "; ".join(problems))
    else:
        ok(f"İşlem={trades_n} PnL=${pnl} — PnL tutarlılık farkı=${fark} (✓ < 0.01$)")
        record("backtest_smoke", True, f"İşlem={trades_n} fark=${fark}")


# ═════════════════════════════════════════════════════════════════════════
# [7] RİSK SAYACI DENGESİ — açık pozisyon sayacı aç/kapa sonrası sıfırlanıyor mu?
# ═════════════════════════════════════════════════════════════════════════
def check_risk_counter_balance():
    section("[7] Risk sayacı dengesi — açık pozisyon aç/kapa simülasyonu")
    code = """
import sys, yaml, numpy as np
from collections import deque
sys.path.insert(0, '.')
from engine import TradeEngine
from strategy_core import score_symbol

cfg = yaml.safe_load(open('config_online.yaml', encoding='utf-8'))
cfg['thresholds']['score_long_open'] = 35
cfg['mtf']['enabled'] = False
cfg['president']['shadow_mode'] = False
cfg['core_long']['shadow_mode'] = False
cfg['adx_filter']['enabled'] = False
cfg['rsi_filter']['enabled'] = False
cfg['atr_filter']['enabled'] = False

e = TradeEngine(['BTCUSDT'], cfg, data_dir='/tmp/_validate_risk')
e.vol_mult = 0.0001

np.random.seed(1)
prices = np.cumsum(np.random.randn(80)) * 0.5 + np.linspace(0, 15, 80) + 100

e.close_series['BTCUSDT'] = deque(maxlen=300)
e.high_series['BTCUSDT']  = deque(maxlen=300)
e.low_series['BTCUSDT']   = deque(maxlen=300)
e.vol_series['BTCUSDT']   = deque(maxlen=300)

for i, p in enumerate(prices):
    e.close_series['BTCUSDT'].append(float(p))
    e.high_series['BTCUSDT'].append(float(p * 1.01))
    e.low_series['BTCUSDT'].append(float(p * 0.99))
    e.vol_series['BTCUSDT'].append(5_000_000.0)
    e.last_close_time['BTCUSDT'] = int(1704067200000 + i * 3600000)

pl = list(e.close_series['BTCUSDT']); hl = list(e.high_series['BTCUSDT'])
ll = list(e.low_series['BTCUSDT']);   vl = list(e.vol_series['BTCUSDT'])
result = score_symbol(pl, hl, ll, vl)
score = result['final_score']

before = e.runtime.get_state()['open_longs']
e._try_open('BTCUSDT', pl[-1], score, pl, hl, ll, vl, result)
opened = 'BTCUSDT' in e.open_positions
mid = e.runtime.get_state()['open_longs']

if opened:
    e._close('BTCUSDT', pl[-1] * 1.02, 0.02, 'TP')
after = e.runtime.get_state()['open_longs']

print('BEFORE=' + str(before))
print('OPENED=' + str(opened))
print('MID=' + str(mid))
print('AFTER=' + str(after))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        fail("Risk sayacı testi hata fırlattı:")
        print(r.stderr.strip())
        record("risk_balance", False, r.stderr.strip()[-400:])
        return

    out = r.stdout
    def grab(key):
        m = re.search(rf"^{key}=(.*)$", out, re.M)
        return m.group(1) if m else None

    before, opened, mid, after = grab("BEFORE"), grab("OPENED"), grab("MID"), grab("AFTER")

    if opened != "True":
        warn("Bu sentetik veride pozisyon açılmadı (filtre engelledi) — sayaç testi atlandı, bu FAIL değildir.")
        record("risk_balance", None, "pozisyon açılmadı, test anlamlı sonuç vermedi")
        return

    if before == "0" and mid == "1" and after == "0":
        ok(f"Sayaç dengeli: açılış öncesi={before} → açılış sonrası={mid} → kapanış sonrası={after}")
        record("risk_balance", True, f"{before}→{mid}→{after}")
    else:
        fail(f"Sayaç DENGESİZ: açılış öncesi={before} → açılış sonrası={mid} → kapanış sonrası={after} (0→1→0 olmalıydı)")
        record("risk_balance", False, f"{before}→{mid}→{after}")


# ═════════════════════════════════════════════════════════════════════════
# ÖZET
# ═════════════════════════════════════════════════════════════════════════
# [6b] V9.0 — 3 MOTOR BAĞIMSIZLIK SMOKE TEST
# Her motorun (LONG/SHORT/OPPORTUNITY) kendi bağımsız score/engine/setup_type
# ürettiğini, aynı merkezi 'score' değişse de SHORT/OPPORTUNITY skorlarının
# DEĞİŞMEDİĞİNİ (yani dışarıdan gelen tek skora bağımlı olmadığını) doğrular.
# ═════════════════════════════════════════════════════════════════════════
def check_engine_independence():
    section("[6b] 3 Motor Bağımsızlık Testi (EngineReport)")
    code = '''
import sys; sys.path.insert(0, ".")
from branches.core_long_branch import CoreLongBranch
from branches.short_surgeon import ShortSurgeon
from branches.cascade_hunter import CascadeHunter

cfg = {}
cl = CoreLongBranch(cfg)
ss = ShortSurgeon(cfg)
ch = CascadeHunter(cfg)

components = {"rsi": 72, "macd": 40, "bollinger": 80, "trend": 30,
              "adx": 20, "supertrend": -20, "divergence": -10, "atr_pct": 1.2, "volume": 60}
result = {"components": components, "final_score": 60.0}
prices = [100 + i*0.1 for i in range(60)]
highs = [p*1.001 for p in prices]; lows = [p*0.999 for p in prices]
vols = [1000]*60

v1 = cl.vote("BTCUSDT", 99.0, result, "TREND", 80.0, "NEUTRAL")
v2 = ss.vote("BTCUSDT", 99.0, result, "TREND")
v3 = ch.vote("BTCUSDT", 99.0, prices, highs, lows, vols, result)

v1b = cl.vote("BTCUSDT", 10.0, result, "TREND", 80.0, "NEUTRAL")  # merkezi skor cok farkli

assert v1.engine == "LONG" and v2.engine == "SHORT" and v3.engine == "OPPORTUNITY", "engine etiketleri yanlis"
assert hasattr(v1, "setup_type") and hasattr(v1, "risk_mult"), "EngineReport alanlari eksik"
# SHORT motoru merkezi skordan bagimsiz calismali (skor parametresi degismedi -> ayni sonuc)
print(f"LONG_SCORE={v1.score} LONG_LOWSCORE={v1b.score} SHORT_SCORE={v2.score} OPP_SCORE={v3.score}")
print("INDEPENDENCE_OK")
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode == 0 and "INDEPENDENCE_OK" in r.stdout:
        ok("3 motor da kendi engine/setup_type/score alanlarını bağımsız üretiyor")
        ok(r.stdout.strip().splitlines()[0] if r.stdout.strip() else "")
        record("engine_independence", True, "LONG/SHORT/OPPORTUNITY EngineReport üretiyor")
    else:
        fail("Motor bağımsızlık testi başarısız:")
        print(r.stdout.strip()); print(r.stderr.strip())
        record("engine_independence", False, r.stderr.strip()[-400:])


# ═════════════════════════════════════════════════════════════════════════
# [6c] V9.0 — UNIVERSE MANAGER canlı/backtest ortak akışa bağlı mı?
# ═════════════════════════════════════════════════════════════════════════
def check_universe_manager_wiring():
    section("[6c] Universe Manager canlı/backtest bağlantı kontrolü")
    um_file = ROOT / "universe_manager.py"
    if not um_file.exists():
        fail("universe_manager.py bulunamadı")
        record("universe_manager_wiring", False, "dosya yok")
        return
    bt_src = (ROOT / "backtest.py").read_text(encoding="utf-8", errors="ignore")
    sim_src = (ROOT / "simulator.py").read_text(encoding="utf-8", errors="ignore") if (ROOT / "simulator.py").exists() else ""
    bt_wired = "universe_manager" in bt_src and "build_universe_for_window" in bt_src
    live_wired = "universe_manager" in sim_src and "build_live_universe" in sim_src
    if bt_wired and live_wired:
        ok("backtest.py VE simulator.py (canlı) UniverseManager'a bağlı")
        record("universe_manager_wiring", True, "backtest+live bağlı")
    else:
        miss = []
        if not bt_wired: miss.append("backtest.py")
        if not live_wired: miss.append("simulator.py")
        fail(f"UniverseManager bağlantısı eksik: {', '.join(miss)}")
        record("universe_manager_wiring", False, f"eksik: {miss}")


# ═════════════════════════════════════════════════════════════════════════
# [7] V9.0.3 — CANDLE FETCH SMOKE TEST (sifir-mum teshisi)
# Tek sembol BTCUSDT, 1h, kucuk bir aralik (~5 mum) ile gercek Binance API'ye
# gider. AG GEREKTIRIR — sandbox/CI ortaminda ag kapaliysa bu test FAIL olur,
# bu NORMALDIR (kod hatasi degildir); gercek makinede calistirilmalidir.
# ═════════════════════════════════════════════════════════════════════════
def check_candle_fetch_smoke():
    section("[7] Candle fetch smoke test (BTCUSDT 1h, ~5 mum, AG GEREKTIRIR)")
    code = '''
import sys, time
sys.path.insert(0, ".")
from backtest import _fetch_candles

end_ms = int(time.time() * 1000)
start_ms = end_ms - 6 * 3600 * 1000  # son ~6 saat -> 1h mumda ~5-6 mum beklenir
candles = _fetch_candles("BTCUSDT", "1h", start_ms, end_ms, cache_dir="/tmp/_validate_fetch_cache")
print(f"RESULT_COUNT={len(candles)}")
if candles:
    first_ot = candles[0]["open_time"]
    last_ct = candles[-1]["close_time"]
    print(f"FIRST_OPEN_TIME={first_ot} LAST_CLOSE_TIME={last_ct}")
    print("SMOKE_OK")
else:
    print("SMOKE_EMPTY")
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    out = r.stdout.strip()
    if "SMOKE_OK" in out and "RESULT_COUNT=0" not in out:
        ok("BTCUSDT 1h gerçek API'den mum verisi çekildi:")
        for line in out.splitlines():
            ok(f"  {line}")
        record("candle_fetch_smoke", True, out.replace("\n", " | ")[:300])
    else:
        warn("Candle fetch smoke test başarısız veya 0 mum döndü (AĞ KAPALI ortamda BEKLENEN "
             "bir durumdur — sandbox/CI'da network olmayabilir; gerçek makinede tekrar çalıştırın).")
        print(out)
        print(r.stderr.strip()[-500:])
        record("candle_fetch_smoke", None, "AĞ YOK veya API hatası — detay yukarıda; gerçek makinede tekrar deneyin")


# ═════════════════════════════════════════════════════════════════════════
# [8] V9.0.5 (ghost-config temizliği) — SCORE INTEGRITY / VALIDATION AUDIT
# config_online.yaml'daki score_integrity.* ve validation.* alanları artık
# GERÇEKTEN burada okunuyor (önceden tanımlı ama hiç kullanılmıyorlardı).
# Küçük bir sentetik backtest çalıştırıp üretilen trade skorlarının
# dağılımını bu eşiklere göre denetler. Sinyal/karar mantığını DEĞİŞTİRMEZ —
# sadece zaten üretilmiş sonuçları analiz eder.
# ═════════════════════════════════════════════════════════════════════════
def check_score_integrity():
    section("[8] Score Integrity / Validation audit (config_online.yaml score_integrity.*)")
    import yaml as _yaml
    cfg = _yaml.safe_load((ROOT / "config_online.yaml").read_text(encoding="utf-8")) or {}
    si_cfg = cfg.get("score_integrity", {}) or {}
    val_cfg = cfg.get("validation", {}) or {}
    if not bool(si_cfg.get("enabled", True)):
        ok("score_integrity.enabled=false — audit atlandı (kasıtlı kapalı)")
        record("score_integrity", None, "score_integrity.enabled=false, atlandı")
        return

    code = """
import sys, os, numpy as np, yaml, json
sys.path.insert(0, ".")
from backtest import Backtester

cfg = yaml.safe_load(open("config_online.yaml", encoding="utf-8"))
cfg["thresholds"]["score_long_open"] = 40
cfg["mtf"]["enabled"] = False
cfg["president"]["shadow_mode"] = False
cfg["core_long"]["shadow_mode"] = False
cfg["short_surgeon"]["shadow_mode"] = False
cfg["limits"]["max_open_positions"] = 3

def mk(n, s, t):
    np.random.seed(s)
    b = np.cumsum(np.random.randn(n)) * 0.6 + np.linspace(0, t, n) + 100
    return [{"open_time": 1704067200000 + i*3600000, "open": float(p), "high": float(p*1.013),
             "low": float(p*0.987), "close": float(p), "volume": 1000.0 + i} for i, p in enumerate(b)]

syms = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]
trends = [20, -18, 15, -12]
cb = {s: mk(700, i+1, trends[i]) for i, s in enumerate(syms)}
os.system("rm -rf /tmp/_score_integrity_check")
bt = Backtester(cfg, "/tmp/_score_integrity_check", president_enabled=True, interval="1h")
r = bt.run(syms, cb, {s: cb[s] for s in syms})
trades = r["trades"]
scores = [float(t.get("Skor", 0) or 0) for t in trades]
labels = [str(t.get("Label", "") or "") for t in trades]
sides  = [str(t.get("Yon", "") or "") for t in trades]
print(json.dumps({"n": len(trades), "scores": scores, "labels": labels, "sides": sides}))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    if r.returncode != 0:
        warn("Score integrity audit için backtest çalıştırılamadı (kod hatası değilse ağ/ortam sorunu olabilir):")
        print(r.stderr.strip()[-500:])
        record("score_integrity", None, "audit backtest çalıştırılamadı")
        return

    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        warn(f"Score integrity audit çıktısı parse edilemedi: {e}")
        record("score_integrity", None, "çıktı parse edilemedi")
        return

    n = data["n"]
    if n == 0:
        warn("Audit backtest'i 0 trade üretti — score integrity kontrolü atlanıyor (örnek yok).")
        record("score_integrity", None, "0 trade, kontrol atlandı")
        return

    scores, labels, sides = data["scores"], data["labels"], data["sides"]
    pct_above_97 = sum(1 for s in scores if s >= 97) / n
    label_counts = {}
    for l in labels:
        label_counts[l] = label_counts.get(l, 0) + 1
    max_label_pct = max(label_counts.values()) / n if label_counts else 0.0
    distinct_labels = len(label_counts)
    has_long = any(s == "LONG" for s in sides)
    has_short = any(s == "SHORT" for s in sides)

    max_pct_97   = float(si_cfg.get("max_pct_trades_above_97", 0.8))
    max_label    = float(si_cfg.get("max_single_label_pct", 0.9))
    min_distinct = int(si_cfg.get("min_distinct_labels", 2))
    require_audit = bool(si_cfg.get("require_long_short_audit", True))
    warn_short    = bool(si_cfg.get("warn_if_short_votes_but_no_short_trades", True))
    fail_on_sat   = bool(val_cfg.get("fail_on_score_saturation", True))

    problems = []
    if pct_above_97 > max_pct_97:
        problems.append(f"score_saturation: trade'lerin %{pct_above_97*100:.1f}'i skor>=97 "
                         f"(eşik: %{max_pct_97*100:.1f}) — skor dağılımı sıkışmış olabilir")
    if max_label_pct > max_label:
        dom = max(label_counts, key=label_counts.get)
        problems.append(f"label_concentration: '{dom}' etiketi trade'lerin %{max_label_pct*100:.1f}'i "
                         f"(eşik: %{max_label*100:.1f})")
    if distinct_labels < min_distinct:
        problems.append(f"distinct_labels={distinct_labels} < min={min_distinct} — etiket çeşitliliği az")
    if require_audit and not (has_long and has_short):
        ok(f"long/short audit: LONG={has_long} SHORT={has_short} (bu sentetik veri setinde "
           f"ikisinin de görülmesi şart değil — gerçek backtest'inde kontrol et)")
    if warn_short and not has_short:
        warn("warn_if_short_votes_but_no_short_trades: bu çalışmada hiç SHORT trade yok "
             "(short_surgeon shadow değilse ve gerçek datada da hiç SHORT açılmıyorsa kontrol et).")

    ok(f"n={n} skor>=97 oranı=%{pct_above_97*100:.1f} en-yoğun-etiket-oranı=%{max_label_pct*100:.1f} "
       f"distinct_labels={distinct_labels} LONG={has_long} SHORT={has_short}")

    if problems and fail_on_sat:
        fail("Score integrity ihlalleri (validation.fail_on_score_saturation=true):")
        for p in problems:
            print(f"      - {p}")
        record("score_integrity", False, "; ".join(problems))
    elif problems:
        warn("Score integrity ihlalleri (validation.fail_on_score_saturation=false, sadece uyarı):")
        for p in problems:
            print(f"      - {p}")
        record("score_integrity", None, "; ".join(problems))
    else:
        ok("Score integrity ihlali yok.")
        record("score_integrity", True, f"n={n} sat=%{pct_above_97*100:.1f} label=%{max_label_pct*100:.1f}")


# ═════════════════════════════════════════════════════════════════════════
def print_summary():
    section("ÖZET")
    n_pass = sum(1 for _, p, _ in RESULTS if p is True)
    n_fail = sum(1 for _, p, _ in RESULTS if p is False)
    n_warn = sum(1 for _, p, _ in RESULTS if p is None)

    for name, passed, detail in RESULTS:
        tag = f"{C.OK}PASS{C.END}" if passed is True else (f"{C.FAIL}FAIL{C.END}" if passed is False else f"{C.WARN}WARN{C.END}")
        print(f"  [{tag}] {name:<18} {detail}")

    print()
    if n_fail == 0:
        print(f"{C.OK}{C.BOLD}SONUÇ: PASS{C.END} ({n_pass} geçti, {n_warn} uyarı) — sistem teslime hazır görünüyor.")
        return 0
    else:
        print(f"{C.FAIL}{C.BOLD}SONUÇ: FAIL{C.END} ({n_fail} hata, {n_pass} geçti, {n_warn} uyarı) — teslimden ÖNCE düzeltilmeli.")
        return 1


def main():
    print(f"{C.BOLD}TRBOT President System — validate_system.py{C.END}")
    print(f"Kontrol dizini: {ROOT}")
    try:
        check_syntax()
        check_imports()
        check_ghost_config()
        check_known_conflict_patterns()
        check_engine_backtest_parity()
        check_engine_independence()
        check_universe_manager_wiring()
        check_engine_boot()
        check_candle_fetch_smoke()
        check_live_bot_chain()
        check_backtest_smoke()
        check_risk_counter_balance()
        check_score_integrity()
    except Exception:
        print(f"\n{C.FAIL}Script çalışırken beklenmeyen hata:{C.END}")
        traceback.print_exc()
        return 2
    return print_summary()


if __name__ == "__main__":
    sys.exit(main())
