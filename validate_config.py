# validate_config.py — TRBOT V8.5.2 fast config sanity validator
from __future__ import annotations
import sys, yaml
from pathlib import Path

errors=[]; warnings=[]
cfg=yaml.safe_load(Path('config_online.yaml').read_text(encoding='utf-8')) or {}

def has(path):
    cur=cfg
    for p in path.split('.'):
        if not isinstance(cur, dict) or p not in cur: return False
        cur=cur[p]
    return True

def get(path, default=None):
    cur=cfg
    for p in path.split('.'):
        if not isinstance(cur, dict) or p not in cur: return default
        cur=cur[p]
    return cur

required=['president.enabled','president.shadow_mode','backtest.president_execution_mode','live.president_execution_mode','adaptive_exit.enabled','block_outcome_analysis.enabled','weekly_symbol_rotation.enabled','quality_score.enabled','adaptive_risk.enabled','mtf.enabled','mode.interval']
for r in required:
    if not has(r): errors.append(f'MISSING_CONFIG:{r}')

if not Path('president_runtime.py').exists(): errors.append('MISSING_FILE:president_runtime.py')
if get('adaptive_exit.enabled', False) and not Path('adaptive_exit.py').exists(): errors.append('MISSING_FILE:adaptive_exit.py')
if get('block_outcome_analysis.enabled', False) and not Path('block_outcome_analyzer.py').exists(): errors.append('MISSING_FILE:block_outcome_analyzer.py')
if get('weekly_symbol_rotation.enabled', False) and not Path('weekly_symbol_universe.py').exists(): errors.append('MISSING_FILE:weekly_symbol_universe.py')

live_mode=str(get('live.president_execution_mode','')).lower()
if live_mode not in ('shadow','paper','simulated_active','active','live'):
    errors.append(f'BAD_LIVE_MODE:{live_mode}')
if live_mode=='paper' and get('president.shadow_mode') is not True:
    warnings.append('NOTE:global president.shadow_mode can remain true; simulator.py overrides to false only for paper live runtime.')
if get('mtf.enabled') is False and float(get('core_long.htf_block_min', 0) or 0) > 50:
    errors.append('CONFLICT: mtf.enabled=false but core_long.htf_block_min > neutral score; can cause zero trades.')
if get('position_rotation.enabled', False) and not get('position_rotation.shadow_mode', True):
    warnings.append('RISK: physical rotation enabled. Keep shadow unless separately validated.')
if str(get('mode.interval','1h')) != '1h':
    warnings.append(f'LIVE_INTERVAL_NOT_1H:{get("mode.interval")} — backtests were mainly 1h; verify intentionally.')

print('CONFIG_VALIDATION')
for w in warnings: print('WARN', w)
for e in errors: print('ERROR', e)
if errors:
    sys.exit(1)
print('OK')
