# weekly_symbol_universe.py — V9.0 GERIYE UYUM SHIM
# Gercek mantik artik universe_manager.py icinde (konsolide Universe Manager).
# Bu dosya sadece eski import yollarini (walk_forward.py, true_walk_forward.py,
# robustness_test.py, validate_config.py) kirmamak icin tutuluyor.
from __future__ import annotations
from universe_manager import (   # noqa: F401
    load_current_symbols,
    score_symbol_universe as score_symbol_from_candles,
    select_universe_for_window,
    write_universe_history,
)
