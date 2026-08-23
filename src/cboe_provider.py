"""
Провайдер Cboe — індексні опціони SPX та NDX.

Один публічний JSON на актив, без ключа:
    https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json

Чому SPX/NDX, а не ES/NQ:
  CME закрив безкоштовний доступ до Open Interest (FTP вимкнено, дані
  тільки через платний DataMine). Публічний API CME віддає ціни й обсяги,
  але без OI — а на ньому тримається вся аналітика стінок.
  SPX і NDX — ті самі індекси, страйки на тій самій шкалі, OI більший,
  і фід одразу віддає IV та gamma, які для DAX доводиться рахувати самому.

Формат відповіді:
    {"timestamp": "2026-08-21 03:00:56",
     "data": {"current_price": 6800.5,
              "options": [{"option": "SPX260821C00200000",
                           "open_interest": 2969.0, "volume": 0.0,
                           "iv": 0.0, "gamma": 0.0, "delta": 1.0,
                           "bid": ..., "ask": ..., ...}, ...]}}

Символ контракту (OCC): SPX 260821 C 00200000
    корінь · YYMMDD експірація · C/P · страйк × 1000 у 8 знаках
"""

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from models import OptionChainRaw, OptionStrikeRaw

logger = logging.getLogger(__name__)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{symbol}.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# SPX260821C00200000 → корінь / YYMMDD / C|P / страйк×1000
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

# Корені тижневих/денних серій — решта вважається місячною
WEEKLY_ROOTS = {"SPXW", "NDXP", "XSP", "RUTW"}

# Множник контракту (доларів на пункт індексу)
MULTIPLIER = {"SPX": 100.0, "NDX": 100.0}


def _parse_occ(symbol: str):
    """
    Розбирає OCC-символ. Повертає (root, expiry, is_call, strike) або None.
    """
    m = OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return root, expiry, (cp == "C"), int(strike_raw) / 1000.0


def _num(v) -> float:
    try:
        f = float(v)
        return f if f == f else 0.0   # NaN → 0
    except (TypeError, ValueError):
        return 0.0


def _pos(v) -> Optional[float]:
    """Додатне значення або None (0 у фіді = «немає даних»)."""
    f = _num(v)
    return f if f > 0 else None


def _is_third_friday(d: date) -> bool:
    """Місячна експірація: п'ятниця, що припадає на 15–21 число."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def _select_expiries(expiries: list, today: date,
                     weeklies: int = 6, monthlies: int = 3) -> set:
    """
    Відбір за структурою, а не «перші N»:

      1. увесь найближчий тиждень — пн, вт, ср, чт, пт (денні + тижнева)
      2. далі тільки п'ятниці — тижневі експірації
      3. плюс місячні (третя п'ятниця) — навіть якщо далеко

    Проміжні буденні дні за межами першого тижня відкидаються: у SPX
    вони щоденні на місяці вперед і перетворюють список на кашу.
    """
    fut = [e for e in expiries if e >= today]
    if not fut:
        return set()

    # Найближчий тиждень = від сьогодні до найближчої п'ятниці включно.
    # Саме до п'ятниці, а не «7 днів»: інакше в список залазить понеділок
    # наступного тижня, який за алгоритмом там бути не повинен.
    to_friday = (4 - today.weekday()) % 7
    week_end  = today + timedelta(days=to_friday)

    near  = [e for e in fut if e <= week_end]
    fri   = [e for e in fut if e > week_end and e.weekday() == 4]
    month = [e for e in fut if _is_third_friday(e)]

    return set(near) | set(fri[:weeklies]) | set(month[:monthlies])


# Дні тижня українською — щоб у списку одразу було видно який це день
_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _label(root: str, expiry: date) -> str:
    """
    Мітка серії. Базовий корінь (SPX, NDX) — це AM-розрахунок, тобто
    класична місячна експірація. Тижневі корені (SPXW, NDXP) — PM.
    """
    if root not in WEEKLY_ROOTS and _is_third_friday(expiry):
        return "Місячна AM"
    if expiry.weekday() == 4:
        return "Тижнева Пт"
    return f"Денна {_WD[expiry.weekday()]}"


class CboeProvider:
    """
    Провайдер одного індексу.

        p = CboeProvider("SPX")
        chains, spot, ts = p.fetch()

    max_expiries обмежує кількість найближчих експірацій — у SPX вони
    щоденні, без обмеження HTML розростається до десятків мегабайт.
    """

    def __init__(self, symbol: str, *, max_expiries: int = 16,
                 weeklies: int = 6, monthlies: int = 3,
                 session: Optional[requests.Session] = None):
        self.symbol = symbol.upper()
        self.max_expiries = max_expiries   # жорстка стеля після відбору
        self.weeklies = weeklies           # скільки п'ятниць після 1-го тижня
        self.monthlies = monthlies         # скільки третіх п'ятниць
        self._session = session or requests.Session()
        self._session.headers.update(HEADERS)

    # ── Публічний API ────────────────────────────────────────────────────────

    def fetch(self) -> tuple[list[OptionChainRaw], Optional[float], Optional[datetime]]:
        """
        Повертає (chains, spot_price, timestamp).
        При будь-якій помилці — ([], None, None); дашборд це переживе.
        """
        url = CBOE_URL.format(symbol=self.symbol)
        logger.info(f"[{self.symbol}] Завантажую {url}")

        try:
            resp = self._session.get(url, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.error(f"[{self.symbol}] Помилка запиту: {exc}")
            return [], None, None

        return self.parse(payload)

    def parse(self, payload: dict) -> tuple[list[OptionChainRaw], Optional[float], Optional[datetime]]:
        """Розбір відповіді — винесено окремо, щоб тестувати без мережі."""
        data = payload.get("data") or {}
        rows = data.get("options") or []
        if not rows:
            logger.warning(f"[{self.symbol}] У відповіді немає options")
            return [], None, None

        spot = _pos(data.get("current_price"))
        ts   = self._parse_ts(payload.get("timestamp"))
        trade_date = ts.date() if ts else date.today()

        # (expiry, root) → strike → {call: row, put: row}
        buckets: dict[tuple, dict] = defaultdict(lambda: defaultdict(dict))
        skipped = 0

        for r in rows:
            parsed = _parse_occ(str(r.get("option", "")))
            if not parsed:
                skipped += 1
                continue
            root, expiry, is_call, strike = parsed
            side = "call" if is_call else "put"
            buckets[(expiry, root)][strike][side] = r

        if skipped:
            logger.info(f"[{self.symbol}] Пропущено {skipped} нерозпізнаних символів")

        # ── Відбір експірацій ────────────────────────────────────────────────
        # Не «N найближчих» (у SPX вони щоденні на місяці вперед), а за
        # структурою: увесь найближчий тиждень + далі тільки п'ятниці +
        # обов'язково місячні. Решта днів відкидається як шум.
        wanted = _select_expiries(
            sorted({k[0] for k in buckets}), trade_date,
            weeklies=self.weeklies, monthlies=self.monthlies,
        )
        keys = sorted((k for k in buckets if k[0] in wanted),
                      key=lambda k: (k[0], k[1]))
        if not keys:                       # усе в минулому — беремо як є
            keys = sorted(buckets.keys())[: self.max_expiries]
        keys = keys[: self.max_expiries]

        chains = []
        for expiry, root in keys:
            strikes = self._build_strikes(buckets[(expiry, root)])
            if not strikes:
                continue
            chains.append(OptionChainRaw(
                asset=self.symbol,
                exchange="CBOE",
                trade_date=trade_date,
                expiry=expiry,
                contract_type=_label(root, expiry),
                strikes=strikes,
            ))
            logger.info(f"  {chains[-1]}")

        total = sum(len(c.strikes) for c in chains)
        logger.info(f"[{self.symbol}] {len(chains)} експірацій, {total} страйків, spot={spot}")
        return chains, spot, ts

    # ── Внутрішнє ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ts(raw) -> Optional[datetime]:
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(raw), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _build_strikes(by_strike: dict) -> list[OptionStrikeRaw]:
        """Зшиває call і put у рядок на страйк, викидає порожні хвости."""
        out = []
        for strike in sorted(by_strike):
            call = by_strike[strike].get("call", {})
            put  = by_strike[strike].get("put", {})

            call_oi = int(_num(call.get("open_interest")))
            put_oi  = int(_num(put.get("open_interest")))
            if call_oi == 0 and put_oi == 0:
                continue                      # неторгований хвіст

            out.append(OptionStrikeRaw(
                strike             = float(strike),
                call_open_interest = call_oi,
                put_open_interest  = put_oi,
                call_volume        = int(_num(call.get("volume"))),
                put_volume         = int(_num(put.get("volume"))),
                # для індексних опціонів «settlement» — це остання теор. ціна
                call_settlement    = _pos(call.get("theo")) or _pos(call.get("last_trade_price")),
                put_settlement     = _pos(put.get("theo"))  or _pos(put.get("last_trade_price")),
                call_iv            = _pos(call.get("iv")),
                put_iv             = _pos(put.get("iv")),
                call_gamma         = _pos(call.get("gamma")),
                put_gamma          = _pos(put.get("gamma")),
            ))
        return out
