# V10 Phase-1: Regime Router + Relative Strength

Bu paket V9 üzerine iki aktif test modülü ekler:

1. `regime_router.py`
2. `relative_strength.py`

## Modlar

Her modül üç modda çalışır:

- `shadow`: sadece hesaplar ve CSV kolonlarına/raporlara yazar; trade kararını değiştirmez.
- `soft`: skor/ranking ve pozisyon boyutunu değiştirir; gerekli hallerde offset sonrası reddeder.
- `hard`: kötü rejim veya zayıf RS koşullarında LONG sinyali bloklar.

## A/B configleri

`v10_phase1_configs/` içinde hazır config varyantları var:

- `01_baseline_disabled.yaml`
- `02_shadow_both.yaml`
- `03_regime_soft.yaml`
- `04_rs_soft.yaml`
- `05_combined_soft.yaml`
- `06_combined_hard.yaml`

## Yeni trade kolonları

- `BTCMacroRegime`
- `SymbolMicroRegime`
- `VolatilityState`
- `RegimeConfidence`
- `RegimeReason`
- `AllowedModules`
- `RegimeLongSizeMult`
- `RegimeMinScoreOffset`
- `RegimeMode`
- `RSGroup`
- `RSScore`
- `RSRankPct`
- `RSState`
- `RSReason`
- `RSMode`
- `ModuleName`
- `ModuleDecisionReason`

## Yeni raporlar

- `regime_router_summary.csv`
- `relative_strength_summary.csv`
- `module_performance_summary.csv`
- `regime_block_events.csv`
- `active_test_summary.csv`

## Ana doğrulama

- Baseline disabled sonuçları eski V9'a yakın olmalı.
- Shadow modda PnL değişmemeli; sadece yeni kolon/rapor oluşmalı.
- Soft/hard modda PnL değişirse `active_test_summary.csv`, `regime_block_events.csv`, `filter_events.csv` sebebi göstermeli.

## Not

Bu fazda range/grid, yeni trend_breakout stratejisi veya bear-short motoru eklenmedi. Sadece router/RS karar katmanı ve raporlama altyapısı eklendi.
