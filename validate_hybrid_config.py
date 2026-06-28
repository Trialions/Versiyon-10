# validate_hybrid_config.py — V8.5.2 compatibility check
from __future__ import annotations
import sys, yaml
from pathlib import Path

cfg = yaml.safe_load(Path('config_online.yaml').read_text(encoding='utf-8')) or {}
required = [
    'adaptive_exit', 'block_outcome_analysis', 'weekly_symbol_rotation',
    'quality_score', 'adaptive_risk', 'president', 'live', 'backtest',
]
missing = [k for k in required if k not in cfg]
if missing:
    print('MISSING', missing)
    sys.exit(1)
mode = str(cfg.get('live', {}).get('president_execution_mode', '')).lower()
if mode not in ('shadow','paper','simulated_active','active','live'):
    print('BAD_LIVE_MODE', mode)
    sys.exit(1)
if cfg.get('weekly_symbol_rotation', {}).get('enabled') and not Path('weekly_symbol_universe.py').exists():
    print('MISSING weekly_symbol_universe.py')
    sys.exit(1)
if cfg.get('adaptive_exit', {}).get('enabled') and not Path('adaptive_exit.py').exists():
    print('MISSING adaptive_exit.py')
    sys.exit(1)
if cfg.get('block_outcome_analysis', {}).get('enabled') and not Path('block_outcome_analyzer.py').exists():
    print('MISSING block_outcome_analyzer.py')
    sys.exit(1)
print('HYBRID_CONFIG_OK V8.5.2')
