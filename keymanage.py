"""
keymanager.py - picks a usable API key on every AI call, tracks daily usage
per key, marks a key exhausted once it hits its daily_limit, and DMs the
owner alerts at 50/70/90/100% usage so a key never silently dies mid-use.
"""

import logging
import db
import providers

logger = logging.getLogger("keymanager")

ALERT_THRESHOLDS = [50, 70, 90, 100]


class NoUsableKeyError(Exception):
    pass


async def run_ai_call(coro_factory, bot=None, owner_id=None):
    """
    coro_factory(provider, key, extra) -> coroutine
    Tries usable keys one by one (least-used first) until one works.
    Raises NoUsableKeyError if none succeed.
    """
    keys = await db.get_usable_keys()
    if not keys:
        raise NoUsableKeyError("No active API keys available.")

    last_error = None
    for row in keys:
        try:
            result = await coro_factory(row["provider"], row["api_key"], row["extra"])
            updated = await db.bump_usage(row["id"])
            await _maybe_alert(updated, bot, owner_id)
            return result
        except Exception as e:  # noqa: BLE001 - provider errors vary widely
            logger.warning("Key id=%s provider=%s failed: %s", row["id"], row["provider"], e)
            last_error = e
            # if it looks like a quota/auth error, mark exhausted for today
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "rate" in msg or "401" in msg or "403" in msg:
                await db.mark_exhausted(row["id"])
            continue

    raise NoUsableKeyError(f"All API keys failed. Last error: {last_error}")


async def _maybe_alert(key_row, bot, owner_id):
    if not bot or not owner_id:
        return
    limit = key_row["daily_limit"] or 1500
    usage = key_row["daily_usage"]
    pct = int((usage / limit) * 100)
    last_alert = key_row["last_alert_pct"]

    for threshold in ALERT_THRESHOLDS:
        if pct >= threshold > last_alert:
            try:
                await bot.send_message(
                    owner_id,
                    f"⚠️ API key #{key_row['id']} ({key_row['provider']}) has reached "
                    f"{pct}% of its daily quota ({usage}/{limit}).",
                )
            except Exception:
                logger.exception("Failed to send key-usage alert to owner")
            await db.set_alert_pct(key_row["id"], threshold)
            if threshold == 100:
                await db.mark_exhausted(key_row["id"])
            break


async def get_quiz_question(topic: str, level: str = "Class 6-10", bot=None, owner_id=None):
    async def factory(provider, key, extra):
        return await providers.generate_quiz_json(provider, key, extra, topic, level)
    return await run_ai_call(factory, bot, owner_id)


async def get_motivation(correct: bool, mention: str, bot=None, owner_id=None):
    async def factory(provider, key, extra):
        return await providers.generate_motivation(provider, key, extra, correct, mention)
    return await run_ai_call(factory, bot, owner_id)


async def get_shayari(correct: bool, mention: str, bot=None, owner_id=None):
    async def factory(provider, key, extra):
        return await providers.generate_shayari(provider, key, extra, correct, mention)
    return await run_ai_call(factory, bot, owner_id)


async def get_reply(user_message: str, bot=None, owner_id=None):
    async def factory(provider, key, extra):
        return await providers.generate_reply(provider, key, extra, user_message)
    return await run_ai_call(factory, bot, owner_id)
