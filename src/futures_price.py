"""
Spot-ціна DAX (Xetra GER40 cash index) з Yahoo Finance.
Тікер: ^GDAXI — це саме той індекс проти якого settles ODAX (Eurex).

Логіка:
  - trade_date = сьогодні або None  → live ринкова ціна (fast_info)
  - trade_date = минула дата        → ціна закриття за той день
"""

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TICKER = "^GDAXI"   # Xetra DAX Performance Index


def fetch_dax_spot(trade_date: Optional[date] = None) -> Optional[float]:
    """
    Повертає spot-ціну Xetra DAX.
    При помилці повертає None — caller використає put-call parity як резерв.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(TICKER)
        today  = date.today()

        if trade_date is None or trade_date >= today:
            # ── Live ціна (під час торгів) ──────────────────────────────
            try:
                price = ticker.fast_info.last_price   # найшвидший метод
                if price and price > 0:
                    logger.info(f"DAX Xetra (live): {price:,.1f}")
                    return round(float(price), 1)
            except Exception:
                pass

            # Fallback: остання свічка
            hist = ticker.history(period="5d")
            if not hist.empty:
                price = round(float(hist["Close"].iloc[-1]), 1)
                logger.info(f"DAX Xetra (last close): {price:,.1f}")
                return price

        else:
            # ── Ціна закриття за конкретну дату (для історії в БД) ─────
            hist = ticker.history(
                start=str(trade_date - timedelta(days=1)),
                end=str(trade_date   + timedelta(days=3)),
            )
            if not hist.empty:
                date_strs = hist.index.strftime("%Y-%m-%d").tolist()
                target    = trade_date.strftime("%Y-%m-%d")
                idx = date_strs.index(target) if target in date_strs else -1
                price = round(float(hist["Close"].iloc[idx]), 1)
                logger.info(f"DAX Xetra ({trade_date}): {price:,.1f}")
                return price

        logger.warning("Yahoo Finance: порожня відповідь")
        return None

    except ImportError:
        logger.warning("yfinance не встановлено: pip install yfinance")
        return None
    except Exception as e:
        logger.warning(f"Yahoo Finance помилка: {e}")
        return None


def fetch_dax_ohlc(days: int = 60):
    """
    OHLC дані DAX (^GDAXI) за останні N торгових днів.
    Повертає DataFrame або None при помилці.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("^GDAXI").history(period=f"{days}d", interval="1d")
        if hist.empty:
            return None
        hist.index = hist.index.tz_localize(None)   # прибираємо timezone
        logger.info(f"OHLC: {len(hist)} свічок завантажено")
        return hist[["Open", "High", "Low", "Close"]]
    except Exception as e:
        logger.warning(f"OHLC помилка: {e}")
        return None
