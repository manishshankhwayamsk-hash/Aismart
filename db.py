"""
db.py - PostgreSQL persistence layer for KRX AI Smart bot.
Uses asyncpg for non-blocking DB access. Nothing is ever deleted automatically;
only explicit owner commands remove rows.
"""

import asyncpg
import datetime
import os

DATABASE_URL = os.environ.get("DATABASE_URL")

_pool: asyncpg.Pool | None = None


async def init_db():
    """Create the connection pool and all required tables (idempotent)."""
    global _pool
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=10)

    async with _pool.acquire() as con:
        await con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                chat_type TEXT NOT NULL,
                title TEXT,
                added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                broadcast_enabled BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                api_key TEXT UNIQUE NOT NULL,
                provider TEXT NOT NULL,
                extra TEXT,
                added_by BIGINT,
                added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                daily_usage INTEGER NOT NULL DEFAULT 0,
                daily_limit INTEGER NOT NULL DEFAULT 1500,
                last_reset_date DATE NOT NULL DEFAULT CURRENT_DATE,
                last_alert_pct INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                exhausted BOOLEAN NOT NULL DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS active_polls (
                poll_id TEXT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                correct_option_id INTEGER NOT NULL,
                topic TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS topics (
                name TEXT PRIMARY KEY
            );
            """
        )

        # Defensive migration: if any of these tables already existed from an
        # earlier deploy attempt with a different schema, add whatever
        # columns are missing instead of failing. Never drops or renames.
        await con.execute(
            """
            ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS correct_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS wrong_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

            ALTER TABLE chats ADD COLUMN IF NOT EXISTS chat_type TEXT;
            ALTER TABLE chats ADD COLUMN IF NOT EXISTS title TEXT;
            ALTER TABLE chats ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ NOT NULL DEFAULT now();
            ALTER TABLE chats ADD COLUMN IF NOT EXISTS broadcast_enabled BOOLEAN NOT NULL DEFAULT TRUE;

            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS provider TEXT;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS extra TEXT;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS added_by BIGINT;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ NOT NULL DEFAULT now();
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS daily_usage INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS daily_limit INTEGER NOT NULL DEFAULT 1500;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_reset_date DATE NOT NULL DEFAULT CURRENT_DATE;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_alert_pct INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS exhausted BOOLEAN NOT NULL DEFAULT FALSE;
            """
        )
        # seed default topics only if table is empty. Covers all subjects,
        # Class 1 through Graduation level (cybersecurity kept from the
        # previous bot theme as one topic among many).
        count = await con.fetchval("SELECT COUNT(*) FROM topics")
        if count == 0:
            defaults = [
                "Science (Physics/Chemistry/Biology)", "Mathematics",
                "General Knowledge", "History", "Geography",
                "Civics & Political Science", "English Grammar & Literature",
                "Hindi Grammar & Literature", "Computer Science & IT",
                "Reasoning & Aptitude", "Environmental Science", "Economics",
                "Arts & Culture", "Sports", "Current Affairs",
                "Cybersecurity & Ethical Hacking",
            ]
            await con.executemany(
                "INSERT INTO topics(name) VALUES($1) ON CONFLICT DO NOTHING",
                [(t,) for t in defaults],
            )


def pool() -> asyncpg.Pool:
    return _pool


# ---------- users / scoring ----------

async def upsert_user(user_id: int, username: str | None, first_name: str | None):
    async with _pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO users(user_id, username, first_name)
            VALUES($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
            """,
            user_id, username, first_name,
        )


async def add_score(user_id: int, delta: int, correct: bool):
    async with _pool.acquire() as con:
        if correct:
            await con.execute(
                "UPDATE users SET score = score + $1, correct_count = correct_count + 1 WHERE user_id=$2",
                delta, user_id,
            )
        else:
            await con.execute(
                "UPDATE users SET score = score + $1, wrong_count = wrong_count + 1 WHERE user_id=$2",
                delta, user_id,
            )


async def get_score(user_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def top_scores(limit: int = 10):
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM users ORDER BY score DESC LIMIT $1", limit
        )


# ---------- chats (groups/channels/dms) ----------

async def register_chat(chat_id: int, chat_type: str, title: str | None):
    async with _pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO chats(chat_id, chat_type, title)
            VALUES($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
            """,
            chat_id, chat_type, title,
        )


async def all_broadcast_chats():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM chats WHERE broadcast_enabled = TRUE"
        )


async def all_group_chats():
    async with _pool.acquire() as con:
        return await con.fetch(
            "SELECT * FROM chats WHERE chat_type IN ('group', 'supergroup')"
        )


# ---------- api keys ----------

async def add_api_key(api_key: str, provider: str, added_by: int, extra: str | None = None):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            """
            INSERT INTO api_keys(api_key, provider, added_by, extra)
            VALUES($1, $2, $3, $4)
            ON CONFLICT (api_key) DO UPDATE SET active = TRUE, exhausted = FALSE
            RETURNING *
            """,
            api_key, provider, added_by, extra,
        )


async def list_api_keys():
    async with _pool.acquire() as con:
        return await con.fetch("SELECT * FROM api_keys ORDER BY id")


async def remove_api_key(key_id: int):
    async with _pool.acquire() as con:
        await con.execute("DELETE FROM api_keys WHERE id=$1", key_id)


async def get_usable_keys():
    """Reset daily counters if the date rolled over, then return active/non-exhausted keys."""
    async with _pool.acquire() as con:
        await con.execute(
            """
            UPDATE api_keys
            SET daily_usage = 0, exhausted = FALSE, last_alert_pct = 0, last_reset_date = CURRENT_DATE
            WHERE last_reset_date < CURRENT_DATE
            """
        )
        return await con.fetch(
            "SELECT * FROM api_keys WHERE active = TRUE AND exhausted = FALSE ORDER BY daily_usage ASC"
        )


async def bump_usage(key_id: int):
    async with _pool.acquire() as con:
        return await con.fetchrow(
            """
            UPDATE api_keys SET daily_usage = daily_usage + 1
            WHERE id=$1 RETURNING *
            """,
            key_id,
        )


async def mark_exhausted(key_id: int):
    async with _pool.acquire() as con:
        await con.execute("UPDATE api_keys SET exhausted = TRUE WHERE id=$1", key_id)


async def set_alert_pct(key_id: int, pct: int):
    async with _pool.acquire() as con:
        await con.execute("UPDATE api_keys SET last_alert_pct=$1 WHERE id=$2", pct, key_id)


# ---------- topics ----------

async def get_topics():
    async with _pool.acquire() as con:
        rows = await con.fetch("SELECT name FROM topics ORDER BY name")
        return [r["name"] for r in rows]


# ---------- polls ----------

async def save_poll(poll_id: str, chat_id: int, correct_option_id: int, topic: str):
    async with _pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO active_polls(poll_id, chat_id, correct_option_id, topic)
            VALUES($1, $2, $3, $4)
            ON CONFLICT (poll_id) DO NOTHING
            """,
            poll_id, chat_id, correct_option_id, topic,
        )


async def get_poll(poll_id: str):
    async with _pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM active_polls WHERE poll_id=$1", poll_id)
