"""
SQLite база даних — зберігає всі дані локально у файл options_data.db.
Не потребує встановлення сервера — вбудовано в Python.

Структура:
  sessions  — одна сесія = один торговий день × один актив
  chains    — один ланцюжок = один expiry
  strikes   — страйки з OI / volume / settlement
  analytics — max_pain, pcr, underlying, futures_px, total_gex
  gex       — GEX + IV по кожному страйку
"""

import sqlite3
import logging
from pathlib import Path
from datetime import date
from typing import Optional

from models import OptionChainRaw

logger = logging.getLogger(__name__)

# Файл бази лежить поряд з проєктом
DB_PATH = Path(__file__).parent.parent / "options_data.db"


# ── Підключення ──────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")   # швидший запис
    c.execute("PRAGMA foreign_keys=ON")
    return c


# ── Ініціалізація схеми ──────────────────────────────────────────────────────

def init_db() -> None:
    """Створює таблиці при першому запуску. Безпечно викликати повторно."""
    with _conn() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date  TEXT    NOT NULL,
            asset       TEXT    NOT NULL,
            fetched_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(trade_date, asset)
        );

        CREATE TABLE IF NOT EXISTS chains (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            expiry        TEXT    NOT NULL,
            contract_type TEXT,
            UNIQUE(session_id, expiry)
        );

        CREATE TABLE IF NOT EXISTS strikes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id    INTEGER NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
            strike      REAL    NOT NULL,
            call_oi     INTEGER DEFAULT 0,
            put_oi      INTEGER DEFAULT 0,
            call_vol    INTEGER,
            put_vol     INTEGER,
            call_settle REAL,
            put_settle  REAL,
            UNIQUE(chain_id, strike)
        );

        CREATE TABLE IF NOT EXISTS analytics (
            chain_id    INTEGER PRIMARY KEY REFERENCES chains(id) ON DELETE CASCADE,
            max_pain    REAL,
            pcr         REAL,
            underlying  REAL,
            futures_px  REAL,
            total_gex   REAL
        );

        CREATE TABLE IF NOT EXISTS gex (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id    INTEGER NOT NULL REFERENCES chains(id) ON DELETE CASCADE,
            strike      REAL    NOT NULL,
            gex         REAL,
            call_iv     REAL,
            put_iv      REAL,
            UNIQUE(chain_id, strike)
        );
        """)
    logger.info(f"БД: {DB_PATH}")


# ── Запис ────────────────────────────────────────────────────────────────────

def session_exists(trade_date: date, asset: str) -> bool:
    with _conn() as db:
        row = db.execute(
            "SELECT id FROM sessions WHERE trade_date=? AND asset=?",
            (trade_date.isoformat(), asset)
        ).fetchone()
    return row is not None


def save_chains(chains: list[OptionChainRaw]) -> None:
    """Зберігає список ланцюжків. Пропускає якщо вже є."""
    if not chains:
        return
    asset      = chains[0].asset
    trade_date = chains[0].trade_date

    with _conn() as db:
        db.execute(
            "INSERT OR IGNORE INTO sessions(trade_date, asset) VALUES(?,?)",
            (trade_date.isoformat(), asset)
        )
        sid = db.execute(
            "SELECT id FROM sessions WHERE trade_date=? AND asset=?",
            (trade_date.isoformat(), asset)
        ).fetchone()["id"]

        for ch in chains:
            if not ch.strikes:
                continue
            db.execute(
                "INSERT OR IGNORE INTO chains(session_id, expiry, contract_type) VALUES(?,?,?)",
                (sid, ch.expiry.isoformat(), ch.contract_type)
            )
            cid = db.execute(
                "SELECT id FROM chains WHERE session_id=? AND expiry=?",
                (sid, ch.expiry.isoformat())
            ).fetchone()["id"]

            db.executemany(
                """INSERT OR REPLACE INTO strikes
                   (chain_id, strike, call_oi, put_oi, call_vol, put_vol, call_settle, put_settle)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(cid, s.strike, s.call_open_interest, s.put_open_interest,
                  s.call_volume, s.put_volume, s.call_settlement, s.put_settlement)
                 for s in ch.strikes]
            )

    logger.info(f"Збережено: {len(chains)} ланцюжків для {asset} {trade_date}")


def save_analytics(chain_id: int, max_pain, pcr, underlying, futures_px, total_gex) -> None:
    with _conn() as db:
        db.execute(
            """INSERT OR REPLACE INTO analytics
               (chain_id, max_pain, pcr, underlying, futures_px, total_gex)
               VALUES(?,?,?,?,?,?)""",
            (chain_id, max_pain, pcr, underlying, futures_px, total_gex)
        )


def save_gex(chain_id: int, gex_rows: list[dict]) -> None:
    with _conn() as db:
        db.executemany(
            "INSERT OR REPLACE INTO gex(chain_id, strike, gex, call_iv, put_iv) VALUES(?,?,?,?,?)",
            [(chain_id, r["strike"], r["gex"], r["call_iv"], r["put_iv"]) for r in gex_rows]
        )


# ── Читання ──────────────────────────────────────────────────────────────────

def load_latest(asset: str) -> tuple[Optional[date], list[dict]]:
    """
    Завантажує останні доступні дані з БД.
    Повертає (trade_date, list_of_chain_dicts).
    Кожен chain_dict: {expiry, contract_type, strikes[], analytics{}, gex_data[]}
    """
    with _conn() as db:
        sess = db.execute(
            "SELECT id, trade_date FROM sessions WHERE asset=? ORDER BY trade_date DESC LIMIT 1",
            (asset,)
        ).fetchone()
        if not sess:
            return None, []

        trade_date = date.fromisoformat(sess["trade_date"])
        sid        = sess["id"]

        chain_rows = db.execute(
            "SELECT id, expiry, contract_type FROM chains WHERE session_id=? ORDER BY expiry",
            (sid,)
        ).fetchall()

        result = []
        for ch in chain_rows:
            cid = ch["id"]

            strikes = db.execute(
                """SELECT strike, call_oi, put_oi, call_vol, put_vol, call_settle, put_settle
                   FROM strikes WHERE chain_id=? ORDER BY strike""",
                (cid,)
            ).fetchall()

            analytics = db.execute(
                "SELECT max_pain, pcr, underlying, futures_px, total_gex FROM analytics WHERE chain_id=?",
                (cid,)
            ).fetchone()

            gex = db.execute(
                "SELECT strike, gex, call_iv, put_iv FROM gex WHERE chain_id=? ORDER BY strike",
                (cid,)
            ).fetchall()

            result.append({
                "chain_id":      cid,
                "expiry":        date.fromisoformat(ch["expiry"]),
                "contract_type": ch["contract_type"],
                "strikes":       [dict(r) for r in strikes],
                "analytics":     dict(analytics) if analytics else {},
                "gex_data":      [dict(r) for r in gex],
            })

    return trade_date, result


def get_chain_id(trade_date: date, asset: str, expiry: date) -> Optional[int]:
    """Повертає id ланцюжка за датою сесії та expiry."""
    with _conn() as db:
        row = db.execute(
            """SELECT c.id FROM chains c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.trade_date=? AND s.asset=? AND c.expiry=?""",
            (trade_date.isoformat(), asset, expiry.isoformat())
        ).fetchone()
    return row["id"] if row else None


def available_dates(asset: str, limit: int = 30) -> list[date]:
    """Список наявних торгових дат в БД (для майбутнього history-режиму)."""
    with _conn() as db:
        rows = db.execute(
            "SELECT trade_date FROM sessions WHERE asset=? ORDER BY trade_date DESC LIMIT ?",
            (asset, limit)
        ).fetchall()
    return [date.fromisoformat(r["trade_date"]) for r in rows]


def load_previous(asset: str) -> tuple[Optional[date], list[dict]]:
    """Друга за давністю сесія (для Δ OI). Повертає (date, chains) або (None, [])."""
    with _conn() as db:
        rows = db.execute(
            "SELECT id, trade_date FROM sessions WHERE asset=? ORDER BY trade_date DESC LIMIT 2",
            (asset,)
        ).fetchall()
        if len(rows) < 2:
            return None, []
        prev = rows[1]
        td   = date.fromisoformat(prev["trade_date"])
        sid  = prev["id"]
        chain_rows = db.execute(
            "SELECT id, expiry FROM chains WHERE session_id=? ORDER BY expiry", (sid,)
        ).fetchall()
        result = []
        for ch in chain_rows:
            strikes = db.execute(
                "SELECT strike, call_oi, put_oi FROM strikes WHERE chain_id=? ORDER BY strike",
                (ch["id"],)
            ).fetchall()
            result.append({
                "expiry":  date.fromisoformat(ch["expiry"]),
                "strikes": {r["strike"]: dict(r) for r in strikes},
            })
        return td, result


def prune_sessions(asset: str, keep: int = 20) -> int:
    """
    Лишає тільки `keep` найсвіжіших сесій активу, старіші видаляє.

    Для Δ OI достатньо попередньої сесії, а база на сервері зберігається
    між запусками — без обрізання вона росла б на кілька мегабайт щодня.
    Каскад по зовнішніх ключах прибирає chains/strikes/analytics/gex.

    Повертає кількість видалених сесій.
    """
    with _conn() as db:
        old = db.execute(
            """SELECT id FROM sessions WHERE asset=?
               ORDER BY trade_date DESC LIMIT -1 OFFSET ?""",
            (asset, keep),
        ).fetchall()
        if not old:
            return 0
        ids = [r["id"] for r in old]
        db.executemany("DELETE FROM sessions WHERE id=?", [(i,) for i in ids])
        db.execute("VACUUM")
    logger.info(f"Обрізано {len(ids)} старих сесій {asset}")
    return len(ids)
