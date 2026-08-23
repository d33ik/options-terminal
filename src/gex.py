"""
GEX — Gamma Exposure (експозиція гамми).

Що це означає для трейдера:
  ▲ Зелений бар (+ GEX): дилери лонг гамму → купують на падінні, продають на рості
      → ціна "притягується" до цього рівня, волатильність знижується
  ▼ Червоний бар (- GEX): дилери шорт гамму → продають на падінні, купують на рості
      → ціна "відштовхується" від рівня, волатильність зростає

Де переходить з + в −: "Gamma Flip" — зона зміни режиму ринку.

Формула (на страйк K):
  GEX_K = (CallGamma_K × CallOI_K − PutGamma_K × PutOI_K)
           × F² × multiplier × 0.01

  DAX multiplier = 5 EUR/пункт
"""

import logging
from datetime import date
from typing import Optional

from models import OptionStrikeRaw
from black76 import implied_vol, black76_gamma

logger = logging.getLogger(__name__)

DAX_MULTIPLIER  = 5.0     # EUR за пункт DAX
RISK_FREE_RATE  = 0.0375  # ~3.75% (ECB, 2026)
MIN_SETTLE      = 0.5     # мінімальна settlement ціна для надійного IV


def calc_gex(
    strikes: list[OptionStrikeRaw],
    futures_price: float,
    trade_date: date,
    expiry: date,
) -> list[dict]:
    """
    Розраховує GEX для кожного страйку.

    Повертає список:
      [{'strike': float, 'gex': float, 'call_iv': float|None, 'put_iv': float|None}, ...]

    gex в одиницях EUR (може бути мільярди — це нормально для DAX).
    """
    T = (expiry - trade_date).days / 365.0
    if T <= 0:
        logger.warning(f"GEX: T={T:.4f} — expiry вже минув, пропускаємо")
        return []

    F = futures_price
    r = RISK_FREE_RATE
    results = []

    for s in strikes:
        call_iv    = None
        put_iv     = None
        call_gamma = 0.0
        put_gamma  = 0.0

        # Call IV + Gamma
        if s.call_settlement and s.call_settlement >= MIN_SETTLE:
            call_iv = implied_vol(F, s.strike, T, r, s.call_settlement, is_call=True)
            if call_iv:
                call_gamma = black76_gamma(F, s.strike, T, r, call_iv)

        # Put IV + Gamma
        if s.put_settlement and s.put_settlement >= MIN_SETTLE:
            put_iv = implied_vol(F, s.strike, T, r, s.put_settlement, is_call=False)
            if put_iv:
                put_gamma = black76_gamma(F, s.strike, T, r, put_iv)

        # GEX на цьому страйку
        gex = (
            call_gamma * s.call_open_interest
            - put_gamma * s.put_open_interest
        ) * F * F * DAX_MULTIPLIER * 0.01

        results.append({
            "strike":  s.strike,
            "gex":     round(gex, 0),
            "call_iv": round(call_iv * 100, 2) if call_iv else None,
            "put_iv":  round(put_iv  * 100, 2) if put_iv  else None,
        })

    total = sum(r["gex"] for r in results)
    logger.info(
        f"GEX expiry={expiry}: {len(results)} страйків, "
        f"total={total/1e6:+.1f}M EUR"
    )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# GEX на готових греках із фіду (шлях Cboe: SPX / NDX)
# ─────────────────────────────────────────────────────────────────────────────

def calc_gex_from_greeks(
    strikes: list[OptionStrikeRaw],
    spot: float,
    multiplier: float = 100.0,
) -> list[dict]:
    """
    Те саме, що calc_gex, але gamma та IV беруться з фіду, а не рахуються
    з ціни через Black-76. Точніше: біржа рахує їх зі своєї моделі й реальної
    кривої ставок, а нам не треба вгадувати settlement.

    GEX_K = (CallGamma_K × CallOI_K − PutGamma_K × PutOI_K)
             × S² × multiplier × 0.01

    multiplier: SPX і NDX = 100 доларів на пункт індексу.
    Результат — у доларах на рух базового активу на 1 %.
    """
    if not spot or spot <= 0:
        return []

    out = []
    for s in strikes:
        cg = s.call_gamma or 0.0
        pg = s.put_gamma  or 0.0

        gex = (cg * s.call_open_interest - pg * s.put_open_interest) \
              * spot * spot * multiplier * 0.01

        out.append({
            "strike":  s.strike,
            "gex":     round(gex, 0),
            # у фіді IV — частка (0.15), у дашборді показуємо відсотки
            "call_iv": round(s.call_iv * 100, 2) if s.call_iv else None,
            "put_iv":  round(s.put_iv  * 100, 2) if s.put_iv  else None,
        })

    total = sum(r["gex"] for r in out)
    logger.info(f"GEX (греки з фіду): {len(out)} страйків, total={total/1e9:+.2f}B $")
    return out
