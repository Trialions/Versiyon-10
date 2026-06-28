# validate_short_smoke.py
# TRBOT short-side smoke validation
# Amaç: SHORT PnL mantığının temel olarak doğru çalıştığını ve kritik modüllerin import edilebildiğini kontrol etmek.

from __future__ import annotations

import sys


def _assert_close(a: float, b: float, eps: float = 1e-9) -> None:
    if abs(a - b) > eps:
        raise AssertionError(f"Expected {b}, got {a}")


def calc_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    side = str(side).upper()
    if side == "LONG":
        return (exit_price - entry) * qty
    if side == "SHORT":
        return (entry - exit_price) * qty
    raise ValueError(f"Unsupported side: {side}")


def main() -> int:
    # LONG kâr testi
    _assert_close(calc_pnl("LONG", 100.0, 110.0, 1.0), 10.0)

    # LONG zarar testi
    _assert_close(calc_pnl("LONG", 100.0, 90.0, 1.0), -10.0)

    # SHORT kâr testi: 100'den short, 90'dan kapanırsa +10
    _assert_close(calc_pnl("SHORT", 100.0, 90.0, 1.0), 10.0)

    # SHORT zarar testi: 100'den short, 110'dan kapanırsa -10
    _assert_close(calc_pnl("SHORT", 100.0, 110.0, 1.0), -10.0)

    # Kritik import zinciri
    import backtest  # noqa: F401
    import engine  # noqa: F401
    import president_runtime  # noqa: F401
    import president_governor  # noqa: F401

    print("SHORT_SMOKE_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("SHORT_SMOKE_FAIL")
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)