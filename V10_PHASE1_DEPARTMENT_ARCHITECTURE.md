# V10 Phase-1 — President Department Architecture

Bu paket tek config dosyası kullanır:

```text
config_online.yaml
```

Başka YAML preset dosyası yoktur. Sistem otomatik olarak sadece `config_online.yaml` okur.

## Hiyerarşi

```text
Risk Governor / Circuit Breaker
    ↓
President
    ↓
Regime Router Department + Relative Strength Department + Branch Votes
```

Regime Router ve Relative Strength final gate değildir. İkisi de President'e rapor verir. Final `OPEN / REDUCE / BLOCK` kararını President verir.

## Basit bayrak mantığı

`config_online.yaml` içinde sadece şu true/false bayrakları değiştirilir:

```yaml
regime_router:
  enabled: true
  shadow_mode: false
  soft_mode: true
  hard_mode: false

relative_strength:
  enabled: true
  shadow_mode: false
  soft_mode: true
  hard_mode: false
```

Öncelik sırası: `hard_mode > soft_mode > shadow_mode`. Yanlışlıkla birden fazla true olursa sistem bu sıraya göre karar verir.

## Test modları

Baseline için:

```yaml
regime_router:
  enabled: false
relative_strength:
  enabled: false
```

Shadow için:

```yaml
regime_router:
  enabled: true
  shadow_mode: true
  soft_mode: false
  hard_mode: false
relative_strength:
  enabled: true
  shadow_mode: true
  soft_mode: false
  hard_mode: false
```

Soft için:

```yaml
regime_router:
  enabled: true
  shadow_mode: false
  soft_mode: true
  hard_mode: false
relative_strength:
  enabled: true
  shadow_mode: false
  soft_mode: true
  hard_mode: false
```

Hard için:

```yaml
regime_router:
  enabled: true
  shadow_mode: false
  soft_mode: false
  hard_mode: true
relative_strength:
  enabled: true
  shadow_mode: false
  soft_mode: false
  hard_mode: true
```

## Puan şişirme koruması

President departman katkılarını sınırlar:

```yaml
president:
  department_policy:
    max_positive_score_adjustment: 4.0
    max_negative_score_adjustment: 14.0
```

Böylece Regime/RS iyi diye puan aşırı pompalanmaz.

## Ana kontrol dosyaları

Backtest sonrası bakılacak dosyalar:

```text
active_test_summary.csv
regime_router_summary.csv
relative_strength_summary.csv
module_performance_summary.csv
regime_block_events.csv
backtest_trades.csv
```
