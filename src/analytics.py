"""
Математика для опціонної аналітики (§5 зі spec).
Чисті функції — не залежать від бази даних чи мережі.
"""

from statistics import median
from typing import Optional
from models import OptionStrikeRaw


def calc_max_pain(strikes: list[OptionStrikeRaw]) -> Optional[float]:
    """
    Max Pain = страйк, де загальна виплата холдерам МІНІМАЛЬНА.

    Для кожного кандидата c рахуємо:
      payout = Σ (c − K) · CallOI  для всіх K < c   (колли в грошах)
             + Σ (K − c) · PutOI   для всіх K > c   (пути в грошах)
    Повертаємо c з найменшим payout.
    """
    if not strikes:
        return None

    min_payout = float("inf")
    result = None

    for candidate in strikes:
        c = candidate.strike
        payout = sum(
            (c - s.strike) * s.call_open_interest
            for s in strikes if s.strike < c
        ) + sum(
            (s.strike - c) * s.put_open_interest
            for s in strikes if s.strike > c
        )
        if payout < min_payout:
            min_payout = payout
            result = c

    return result


def calc_put_call_ratio(strikes: list[OptionStrikeRaw]) -> Optional[float]:
    """Put/Call ratio = сумарний Put OI / сумарний Call OI."""
    total_call = sum(s.call_open_interest for s in strikes)
    total_put  = sum(s.put_open_interest  for s in strikes)
    if total_call == 0:
        return None
    return round(total_put / total_call, 3)


def estimate_underlying_parity(strikes: list[OptionStrikeRaw]) -> Optional[float]:
    """
    Оцінка ф'ючерсної ціни через паритет put-call (§2 зі spec):
      F ≈ K + CallSettle − PutSettle

    Беремо медіану по всіх страйках де є обидва settlement-и.
    Точність < 1 пункту відносно реального ф'ючерсу.
    """
    forwards = [
        round(s.strike + s.call_settlement - s.put_settlement, 1)
        for s in strikes
        if s.call_settlement and s.put_settlement
    ]
    if not forwards:
        return None
    return round(median(forwards), 1)
