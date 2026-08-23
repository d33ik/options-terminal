"""
Black-76 модель для ф'ючерсних опціонів.
Розраховує IV (implied volatility) та Gamma для кожного страйку.
Не потребує сторонніх бібліотек — тільки вбудований math.
"""

import math
from typing import Optional


# ── Стандартний нормальний розподіл ─────────────────────────────────────────

def _npdf(x: float) -> float:
    """Щільність стандартного нормального розподілу."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _ncdf(x: float) -> float:
    """Кумулятивна функція стандартного нормального розподілу."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ── Black-76 ціна ────────────────────────────────────────────────────────────

def black76_price(F: float, K: float, T: float, r: float,
                  sigma: float, is_call: bool) -> float:
    """
    Теоретична ціна опціону за моделлю Black-76.

    F     — ф'ючерсна ціна базового активу
    K     — страйк
    T     — час до закінчення (роки)
    r     — безризикова ставка (напр. 0.0375 = 3.75%)
    sigma — волатильність (напр. 0.15 = 15%)
    """
    if T <= 0 or sigma <= 0:
        return max(0.0, (F - K) if is_call else (K - F))

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T)

    if is_call:
        return disc * (F * _ncdf(d1) - K * _ncdf(d2))
    else:
        return disc * (K * _ncdf(-d2) - F * _ncdf(-d1))


# ── Black-76 Gamma ───────────────────────────────────────────────────────────

def black76_gamma(F: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Gamma — однакова для call і put.
    Показує наскільки швидко змінюється Delta при русі ціни на 1 пункт.
    """
    if T <= 0 or sigma <= 0 or F <= 0:
        return 0.0

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrt_T)
    return math.exp(-r * T) * _npdf(d1) / (F * sigma * sqrt_T)


# ── Implied Volatility (метод бісекції) ──────────────────────────────────────

def implied_vol(F: float, K: float, T: float, r: float,
                market_price: float, is_call: bool,
                tol: float = 1e-6, max_iter: int = 120) -> Optional[float]:
    """
    Розраховує implied volatility методом бісекції.
    Повертає None якщо:
      - ціна занадто мала (< 0.5 пункту) — ненадійні дані
      - рішення не знайдено в діапазоні 0.01%–500%
    """
    if T <= 0 or market_price < 0.5:
        return None

    lo, hi = 0.0001, 5.0
    f_lo = black76_price(F, K, T, r, lo, is_call) - market_price
    f_hi = black76_price(F, K, T, r, hi, is_call) - market_price

    # Якщо обидва кінці одного знаку — рішення немає в цьому діапазоні
    if f_lo * f_hi > 0:
        return None

    for _ in range(max_iter):
        mid = (lo + hi) * 0.5
        f_mid = black76_price(F, K, T, r, mid, is_call) - market_price

        if abs(f_mid) < tol or (hi - lo) < tol:
            return round(mid, 6)

        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid

    return round((lo + hi) * 0.5, 6)
