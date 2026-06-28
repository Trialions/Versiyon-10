"""V8.5.7 ranking output smoke.
Checks that ranking/rejection events are written to both filter_events.csv and candidate_ranking_events.csv.
This is an output-integrity test, not a PnL strategy test.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import yaml

from backtest import Backtester

cfg_path = Path('config_online.yaml')
cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
out = Path(tempfile.mkdtemp(prefix='trbot_rank_smoke_'))
bt = Backtester(cfg, str(out), president_enabled=True, interval='1h')
packet = SimpleNamespace(side=SimpleNamespace(value='LONG'), label='NORMAL')
cand = {
    'symbol': 'TESTUSDT', 'rank_score': 82.5, 'score': 80.0,
    'packet': packet, 'quality_score': 70.0, 'boa_feedback': {'adjustment': 1.2},
}
bt._log_ranking_event('RANK_SELECTED', cand, '2026-01-01 00:00', 1, 82.5, opened_count=1, total_candidates=2)
bt._log_ranking_event('RANK_REJECTED_LOWER_SCORE', cand, '2026-01-01 00:00', 2, 82.5, opened_count=1, total_candidates=2)
bt._write_filter_csv()
rank_file = out / 'candidate_ranking_events.csv'
filter_file = out / 'filter_events.csv'
if not rank_file.exists():
    raise SystemExit('candidate_ranking_events.csv not written')
if not filter_file.exists():
    raise SystemExit('filter_events.csv not written')
text = rank_file.read_text(encoding='utf-8-sig')
if 'RANK_SELECTED' not in text or 'RANK_REJECTED_LOWER_SCORE' not in text:
    raise SystemExit('ranking causes missing from candidate_ranking_events.csv')
print('RANKING_OUTPUT_SMOKE_OK', out)
