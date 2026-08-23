"""
Eurex провайдер — отримує реальні дані опціонів з безкоштовного публічного API Eurex.
Ніякого ключа не потрібно.

§1 зі spec:
  Base: https://www.eurex.com/api/v1/overallstatistics/{statsId}
  Протокол: два запити на продукт — overview (список expiry) → detail (страйки).

⚠️  КРИТИЧНО: без заголовку Referer detail-запит повертає ПОРОЖНІЙ результат.
    Це головна пастка, яка мовчки ламає все.
"""

import logging
import time
from datetime import date, datetime
from typing import Optional

import requests

from models import OptionChainRaw, OptionStrikeRaw

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Константи
# ─────────────────────────────────────────────

EUREX_BASE_URL = "https://www.eurex.com/api/v1/overallstatistics/{stats_id}"

# БЕЗ цього Referer — detail-запит повертає порожній список. Перевірено.
REQUIRED_HEADERS = {
    "Referer": "https://www.eurex.com/ex-en/data/statistics/market-statistics-online",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.eurex.com",
}

# Відповідність коротких кодів фіду → людиночитабельні мітки
CONTRACT_TYPE_MAP = {
    "M":  "Monthly",
    "W":  "Weekly",
    "ME": "EndOfMonth",
    "D":  "Daily",
    "F":  "Flex",
}


# ─────────────────────────────────────────────
# Допоміжні функції
# ─────────────────────────────────────────────

def _map_contract_type(code: str) -> Optional[str]:
    """
    Перетворює код типу контракту Eurex на мітку.
    Невідомі коди зберігаємо як є (не викидаємо expiry через невідомий тип).
    Порожній рядок → None.
    """
    if not code:
        return None
    return CONTRACT_TYPE_MAP.get(code, code)  # невідомий → залишаємо verbatim


def _parse_ymd(s: str) -> Optional[date]:
    """Парсинг дати формату yyyyMMdd (як у полі 'date' рядків dataRows)."""
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _parse_trading_date(s: str) -> Optional[date]:
    """
    Парсинг дати з поля tradingDates заголовка overview.
    Формат: 'dd-MM-yyyy HH:mm', наприклад '19-06-2026 12:00'.
    """
    try:
        return datetime.strptime(s, "%d-%m-%Y %H:%M").date()
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    """Конвертація в int з округленням (числа в фіді можуть бути float)."""
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    """Конвертація в float; 0.0 вважається відсутнім (0.0 у settlement = не торгувався)."""
    try:
        f = float(val)
        return f if f != 0.0 else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────
# Головний клас
# ─────────────────────────────────────────────

class EurexProvider:
    """
    Провайдер даних Eurex.

    Один екземпляр = один продукт (напр. DAX / statsId=70044).
    Щоб підтримати кілька продуктів — створіть кілька екземплярів.

    Відомі statsId (зі spec):
        ODAX  (DAX)          = 70044
        OESX  (EuroStoxx 50) = 69660
        OSMI  (SMI)          = 69710
    """

    def __init__(
        self,
        stats_id: str,
        asset: str,
        *,
        delay_between_requests: float = 0.5,
        session: Optional[requests.Session] = None,
    ):
        """
        Параметри:
            stats_id   — числовий id статистики Eurex (не id продукту!)
            asset      — коротка назва, напр. "DAX"
            delay_between_requests — пауза між detail-запитами (щоб не спамити сервер)
            session    — можна передати свою requests.Session (для тестів)
        """
        self.stats_id = stats_id
        self.asset = asset
        self.delay = delay_between_requests
        self._base_url = EUREX_BASE_URL.format(stats_id=stats_id)

        self._session = session or requests.Session()
        self._session.headers.update(REQUIRED_HEADERS)

    # ── Публічний API ──────────────────────────────────────────────────────────

    def fetch(self, trade_date: Optional[date] = None) -> list[OptionChainRaw]:
        """
        Головна точка входу.
        Повертає список OptionChainRaw — по одному на кожен expiry.
        Якщо trade_date не передано — автоматично знаходить останню доступну сесію.

        Eurex публікує T+1 ~опівдні за CET, тому "остання" сесія = зазвичай вчора.
        """
        if trade_date is None:
            trade_date = self._latest_trade_date()
            if trade_date is None:
                logger.error(f"[{self.asset}] Не вдалося визначити дату останньої сесії")
                return []

        logger.info(f"[{self.asset}] Завантажую ланцюжки за сесію {trade_date}")

        # 1) Отримуємо список expiry
        try:
            expiries = self._fetch_expiries(trade_date)
        except Exception as exc:
            logger.error(f"[{self.asset}] Помилка overview на {trade_date}: {exc}")
            return []

        if not expiries:
            logger.warning(f"[{self.asset}] Нема expiry для сесії {trade_date}")
            return []

        logger.info(f"[{self.asset}] Знайдено {len(expiries)} expiry")

        # 2) Для кожного expiry — завантажуємо страйки
        chains: list[OptionChainRaw] = []
        for i, (expiry, contract_code) in enumerate(expiries):
            try:
                if i > 0:
                    time.sleep(self.delay)  # ввічливо до сервера

                strikes = self._fetch_strikes(trade_date, expiry, contract_code)
                chain = OptionChainRaw(
                    asset=self.asset,
                    exchange="EUREX",
                    trade_date=trade_date,
                    expiry=expiry,
                    contract_type=_map_contract_type(contract_code),
                    strikes=strikes,
                )
                chains.append(chain)
                logger.info(f"  {chain}")

            except Exception as exc:
                # Один зламаний expiry не повинен зупиняти решту
                logger.error(
                    f"[{self.asset}] Помилка для expiry={expiry} "
                    f"contractType='{contract_code}': {exc}"
                )
                continue

        logger.info(f"[{self.asset}] Готово: {len(chains)}/{len(expiries)} ланцюжків")
        return chains

    # ── Приватні методи ────────────────────────────────────────────────────────

    def _latest_trade_date(self, reference: Optional[date] = None) -> Optional[date]:
        """
        Зондуємо overview, щоб знайти реально доступну останню сесію.
        Фід публікує T+1 ~12:00 CET → "остання" = зазвичай вчора.
        """
        ref = (reference or date.today()).strftime("%Y%m%d")
        data = self._get({"busdate": ref, "filtertype": "overview"})

        trading_dates = data.get("header", {}).get("tradingDates", [])
        if not trading_dates:
            logger.warning(f"[{self.asset}] tradingDates порожній у відповіді overview")
            return None

        # Перший запис — найновіший
        latest = _parse_trading_date(trading_dates[0])
        logger.info(f"[{self.asset}] Остання доступна сесія: {latest} (з {len(trading_dates)} дат)")
        return latest

    def _fetch_expiries(self, trade_date: date) -> list[tuple[date, str]]:
        """
        Повертає [(expiry_date, contract_code), ...] для заданої сесії.
        contract_code — короткий код із фіду ("M", "W", "ME" тощо).
        """
        data = self._get({"busdate": trade_date.strftime("%Y%m%d"), "filtertype": "overview"})

        result = []
        for row in data.get("dataRows", []):
            expiry = _parse_ymd(row.get("date", ""))
            code = row.get("contractType", "") or ""
            if expiry:
                result.append((expiry, code))
            else:
                logger.warning(f"[{self.asset}] Пропускаю рядок з невалідною датою: {row}")

        return result

    def _fetch_strikes(
        self, trade_date: date, expiry: date, contract_code: str
    ) -> list[OptionStrikeRaw]:
        """
        Завантажує по-страйкові дані для ОДНОГО expiry.

        Повертає відфільтрований список страйків:
        вилучаємо страйки де і call_oi=0 і put_oi=0 (неторговані хвости).
        """
        data = self._get({
            "filtertype":    "detail",
            "productdate":   expiry.strftime("%Y%m%d"),
            "busdate":       trade_date.strftime("%Y%m%d"),
            "contracttype":  contract_code,
        })

        # Збираємо call і put в єдиний словник за страйком
        strike_map: dict[float, dict] = {}

        for row in data.get("dataRowsCall", []):
            k = _safe_float(row.get("strike"))
            if k is not None:
                strike_map.setdefault(k, {})["call"] = row

        for row in data.get("dataRowsPut", []):
            k = _safe_float(row.get("strike"))
            if k is not None:
                strike_map.setdefault(k, {})["put"] = row

        strikes: list[OptionStrikeRaw] = []
        for k in sorted(strike_map.keys()):
            sides = strike_map[k]
            call = sides.get("call", {})
            put  = sides.get("put",  {})

            call_oi = _safe_int(call.get("openInterest")) or 0
            put_oi  = _safe_int(put.get("openInterest"))  or 0

            # ── Фільтр: прибираємо неторговані хвости (OI=0 з обох боків)
            if call_oi == 0 and put_oi == 0:
                continue

            strikes.append(OptionStrikeRaw(
                strike              = k,
                call_open_interest  = call_oi,
                put_open_interest   = put_oi,
                call_volume         = _safe_int(call.get("volume")),
                put_volume          = _safe_int(put.get("volume")),
                call_settlement     = _safe_float(call.get("dSettle")),
                put_settlement      = _safe_float(put.get("dSettle")),
            ))

        return strikes

    def _get(self, params: dict) -> dict:
        """
        Базовий GET з обробкою помилок.
        Кидає виняток при мережевій помилці або не-200 відповіді.
        """
        resp = self._session.get(self._base_url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
