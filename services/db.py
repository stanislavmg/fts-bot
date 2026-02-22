import json
from datetime import date

import aiosqlite

from config import DB_PATH


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                fs_access_token TEXT,
                fs_access_secret TEXT,
                fs_request_token TEXT,
                fs_request_secret TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                meal_json TEXT NOT NULL,
                logged_to_fs INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
            """
        )
        await db.commit()


async def save_request_tokens(
    telegram_id: int, token: str, secret: str
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, fs_request_token, fs_request_secret)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                fs_request_token = excluded.fs_request_token,
                fs_request_secret = excluded.fs_request_secret
            """,
            (telegram_id, token, secret),
        )
        await db.commit()


async def get_request_tokens(telegram_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT fs_request_token, fs_request_secret FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] and row[1]:
            return row[0], row[1]
        return None


async def save_access_tokens(
    telegram_id: int, token: str, secret: str
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, fs_access_token, fs_access_secret)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                fs_access_token = excluded.fs_access_token,
                fs_access_secret = excluded.fs_access_secret,
                fs_request_token = NULL,
                fs_request_secret = NULL
            """,
            (telegram_id, token, secret),
        )
        await db.commit()


async def get_access_tokens(telegram_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT fs_access_token, fs_access_secret FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] and row[1]:
            return row[0], row[1]
        return None


async def is_user_authorized(telegram_id: int) -> bool:
    return await get_access_tokens(telegram_id) is not None


async def save_meal(
    telegram_id: int, meal_json: dict, logged_to_fs: bool = False
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO meal_log (telegram_id, date, meal_json, logged_to_fs)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, date.today().isoformat(), json.dumps(meal_json, ensure_ascii=False), int(logged_to_fs)),
        )
        await db.commit()
        return cursor.lastrowid


async def mark_meal_logged(meal_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meal_log SET logged_to_fs = 1 WHERE id = ?",
            (meal_id,),
        )
        await db.commit()
