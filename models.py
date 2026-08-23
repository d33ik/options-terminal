"""
Моделі даних для options analytics.
Прості dataclass-и — не прив'язані до жодного фреймворку чи БД.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List


@dataclass
class OptionStrikeRaw:
    """
    Дані по одному страйку з Eurex-фіду.
    Одна така структура = один рядок call + відповідний рядок put.
    """
    strike: float

    # Відкритий інтерес (Open Interest) — кількість відкритих контрактів
    call_open_interest: int
    put_open_interest: int

    # Обсяг торгів за сесію (може бути відсутній)
    call_volume: Optional[int]
    put_volume: Optional[int]

    # Розрахункова ціна на кінець дня (settlement price; dSettle у фіді)
    call_settlement: Optional[float]
    put_settlement: Optional[float]

    # ── Греки з фіду (Cboe віддає готові; Eurex — ні) ────────────────────────
    # Якщо None — рахуємо самі через Black-76 (шлях DAX).
    # Якщо заповнені — беремо як є (шлях SPX/NDX), це точніше й швидше.
    call_iv: Optional[float]    = None   # частка, не відсоток: 0.15 = 15 %
    put_iv: Optional[float]     = None
    call_gamma: Optional[float] = None
    put_gamma: Optional[float]  = None


@dataclass
class OptionChainRaw:
    """
    Повний ланцюжок опціонів (один рядок = один страйк) для ОДНОГО терміну закінчення (expiry).
    Після парсингу — передається в математичний модуль та в БД.
    """
    asset: str           # напр. "DAX"
    exchange: str        # напр. "EUREX"
    trade_date: date     # дата торгової сесії (наприклад 2026-06-23)
    expiry: Optional[date]          # дата закінчення опціону
    contract_type: Optional[str]    # "Monthly", "Weekly", "EndOfMonth", "Daily", "Flex"

    strikes: List[OptionStrikeRaw] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.asset} | {self.trade_date} | expiry={self.expiry} "
            f"({self.contract_type}) | {len(self.strikes)} strikes"
        )
