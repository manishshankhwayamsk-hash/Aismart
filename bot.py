"""
KRX AI Smart - Telegram bot
----------------------------
- Owner adds any number of AI API keys with /addapi <key>; provider is
  auto-detected from the key's shape, no need to say which service it's for.
- AI auto-generates cybersecurity quiz questions and broadcasts them as
  native Telegram quiz-polls to every DM/group/channel the bot is in.
- Telegram grades the poll natively; the bot awards +5 / -2 per user and
  sends an AI-written motivational line mentioning the user.
- Daily leaderboard is posted automatically to every group.
- Any free-text message gets an AI auto-reply.
- Nothing is ever deleted automatically - all state lives in Postgres.
"""

import asyncio
import logging
import os
import random
import threading

from flask import Flask
from telegram import Update, Poll
from telegram.constants import ChatType, PollType
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    ChatMemberHandler,
    filters,
)

import db
import keymanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("krxbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BROADCAST_INTERVAL_MIN = int(os.environ.get("BROADCAST_INTERVAL_MIN", "60"))
LEADERBOARD_HOUR_UTC = int(os.environ.get("LEADERBOARD_HOUR_UTC", "18"))
PORT = int(os.environ.get("PORT", "10000"))
TENOR_API_KEY = os.environ.get("TENOR_API_KEY")  # optional; GIF reactions skipped if unset

CORRECT_POINTS = 5
WRONG_POINTS = -2

QUIZ_LEVELS = ["Class 1-5", "Class 6-10", "Class 11-12", "Graduation"]

CORRECT_GIF_TERMS = ["funny celebration", "victory dance funny", "winning meme funny"]
WRONG_GIF_TERMS = ["sad alone funny meme", "dramatic sad meme funny", "fail funny meme"]


async def fetch_random_gif(search_term: str) -> str | None:
    """Returns a random Tenor GIF URL for the mood, or None if unavailable/unconfigured."""
    if not TENOR_API_KEY:
        return None
    import httpx
    url = "https://tenor.googleapis.com/v2/search"
    params = {"q": search_term, "key": TENOR_API_KEY, "limit": 20, "media_filter": "gif"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            choice = random.choice(results)
            return choice["media_formats"]["gif"]["url"]
    except Exception:
        logger.exception("Tenor GIF fetch failed for term=%s", search_term)
        return None


# ---------------------------------------------------------------- commands

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.first_name)
    await db.register_chat(chat.id, chat.type, chat.title)
    await update.message.reply_text(
        "🛡️ *KRX AI Smart* is online.\n\n"
        "I auto-generate cybersecurity quiz questions, score your answers, "
        "and chat with AI. Use /topics to see quiz categories, /score for "
        "your points, /leaderboard for the top scorers.",
        parse_mode="Markdown",
    )


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = await db.get_topics()
    text = "📚 *Topics:*\n" + "\n".join(f"• {t}" for t in topics)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = await db.get_score(user.id)
    if not row:
        await update.message.reply_text("You haven't answered any quiz yet.")
        return
    await update.message.reply_text(
        f"🏆 {user.first_name}: {row['score']} pts "
        f"({row['correct_count']} correct / {row['wrong_count']} wrong)"
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.top_scores(10)
    if not rows:
        await update.message.reply_text("No scores yet.")
        return
    lines = ["🏆 *Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or r["first_name"] or str(r["user_id"])
        lines.append(f"{i}. {name} — {r['score']} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_addapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is invalid.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /addapi <key>\n"
            "(For Cloudflare Workers AI, paste it as cf:<account_id>:<api_token>)"
        )
        return
    raw_key = context.args[0]
    import providers
    provider, cleaned_key, extra = providers.detect_provider(raw_key)
    if provider is None:
        await update.message.reply_text(
            "Couldn't auto-detect the provider for this key. Please double-check "
            "the key or contact support to add this provider's pattern."
        )
        return
    await db.add_api_key(cleaned_key, provider, update.effective_user.id, extra)
    await update.message.reply_text(f"✅ Key added. Detected provider: *{provider}*", parse_mode="Markdown")


async def cmd_listapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is invalid.")
        return
    rows = await db.list_api_keys()
    if not rows:
        await update.message.reply_text("No API keys added yet.")
        return
    lines = []
    for r in rows:
        status = "🔴 exhausted" if r["exhausted"] else ("🟢 active" if r["active"] else "⚪ inactive")
        lines.append(
            f"#{r['id']} {r['provider']} — {r['daily_usage']}/{r['daily_limit']} today — {status}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_removeapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is invalid.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeapi <id>")
        return
    try:
        key_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("This command is invalid.")
        return
    await db.remove_api_key(key_id)
    await update.message.reply_text(f"🗑️ Key #{key_id} removed.")


async def cmd_quiznow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: manually trigger one quiz question in this chat, for testing."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("This command is invalid.")
        return
    await send_quiz_to_chat(context.application, update.effective_chat.id)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("This command is invalid.")


# ---------------------------------------------------------------- messages / AI auto-reply

async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.first_name)
    await db.register_chat(chat.id, chat.type, chat.title)

    # In groups, only auto-reply when the bot is mentioned or replied to.
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        bot_username = context.bot.username
        mentioned = bot_username and f"@{bot_username}" in update.message.text
        replied_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        if not (mentioned or replied_to_bot):
            return

    try:
        reply = await keymanager.get_reply(update.message.text, context.bot, OWNER_ID)
    except keymanager.NoUsableKeyError:
        await update.message.reply_text(
            "⚠️ AI is temporarily unavailable, please try again later."
        )
        return
    except Exception:
        logger.exception("AI reply failed")
        await update.message.reply_text("⚠️ Something went wrong, please try again later.")
        return

    await update.message.reply_text(reply)


# ---------------------------------------------------------------- quiz broadcast

async def send_quiz_to_chat(app: Application, chat_id: int):
    topics = await db.get_topics()
    topic = random.choice(topics) if topics else "General Knowledge"
    level = random.choice(QUIZ_LEVELS)

    try:
        quiz = await keymanager.get_quiz_question(topic, level, app.bot, OWNER_ID)
        question = quiz["question"][:250]
        options = [str(o)[:95] for o in quiz["options"]][:10]
        correct_index = int(quiz["correct_index"])
    except keymanager.NoUsableKeyError:
        logger.warning("No usable API key to generate a quiz question.")
        return
    except Exception:
        logger.exception("Failed to generate/parse quiz question for topic=%s", topic)
        return

    try:
        message = await app.bot.send_poll(
            chat_id=chat_id,
            question=f"🧠 [{topic} | {level}] {question}",
            options=options,
            type=PollType.QUIZ,
            correct_option_id=correct_index,
            is_anonymous=False,
        )
        await db.save_poll(message.poll.id, chat_id, correct_index, topic)
    except Exception:
        logger.exception("Failed to send poll to chat_id=%s", chat_id)


async def broadcast_quiz_job(app: Application):
    chats = await db.all_broadcast_chats()
    for row in chats:
        await send_quiz_to_chat(app, row["chat_id"])
        await asyncio.sleep(1.5)  # gentle pacing across many chats


async def daily_leaderboard_job(app: Application):
    rows = await db.top_scores(10)
    if not rows:
        return
    lines = ["📊 *Daily Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or r["first_name"] or str(r["user_id"])
        lines.append(f"{i}. {name} — {r['score']} pts")
    text = "\n".join(lines)

    groups = await db.all_group_chats()
    for row in groups:
        try:
            await app.bot.send_message(row["chat_id"], text, parse_mode="Markdown")
        except Exception:
            logger.exception("Failed to post leaderboard to chat_id=%s", row["chat_id"])
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------- poll answers

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer.option_ids:
        return  # user retracted their answer
    poll_row = await db.get_poll(answer.poll_id)
    if not poll_row:
        return

    user = answer.user
    await db.upsert_user(user.id, user.username, user.first_name)

    chosen = answer.option_ids[0]
    correct = chosen == poll_row["correct_option_id"]
    delta = CORRECT_POINTS if correct else WRONG_POINTS
    await db.add_score(user.id, delta, correct)

    mention = f"@{user.username}" if user.username else user.first_name
    try:
        line = await keymanager.get_shayari(correct, mention, context.bot, OWNER_ID)
    except Exception:
        line = f"{'✅ Correct!' if correct else '❌ Not quite.'} {mention}, keep going!"

    sign = "+5" if correct else "-2"
    caption = f"{line}\n\n({mention} {sign} pts)"

    gif_term = random.choice(CORRECT_GIF_TERMS if correct else WRONG_GIF_TERMS)
    gif_url = await fetch_random_gif(gif_term)

    sent_message = None
    try:
        if gif_url:
            sent_message = await context.bot.send_animation(poll_row["chat_id"], gif_url, caption=caption)
        else:
            sent_message = await context.bot.send_message(poll_row["chat_id"], caption)
    except Exception:
        logger.exception("Failed to send reaction to chat_id=%s", poll_row["chat_id"])
        return

    chat_row = await db.get_chat(poll_row["chat_id"])
    is_group = chat_row and chat_row["chat_type"] in ("group", "supergroup")
    if is_group and sent_message:
        asyncio.create_task(_delete_after(context.bot, poll_row["chat_id"], sent_message.message_id, 5))


async def _delete_after(bot, chat_id: int, message_id: int, seconds: int):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass  # message may already be gone, or bot lacks delete rights - not fatal


# ---------------------------------------------------------------- chat membership tracking

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await db.register_chat(chat.id, chat.type, chat.title)


# ---------------------------------------------------------------- keep-alive web server (for Render + UptimeRobot)

flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "KRX AI Smart is alive.", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


# ---------------------------------------------------------------- app setup

async def post_init(app: Application):
    await db.init_db()
    jq = app.job_queue
    jq.run_repeating(
        lambda ctx: asyncio.create_task(broadcast_quiz_job(app)),
        interval=BROADCAST_INTERVAL_MIN * 60,
        first=30,
    )
    jq.run_daily(
        lambda ctx: asyncio.create_task(daily_leaderboard_job(app)),
        time=__import__("datetime").time(hour=LEADERBOARD_HOUR_UTC, minute=0),
    )
    logger.info("KRX AI Smart initialized. Broadcast every %s min.", BROADCAST_INTERVAL_MIN)


def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("topics", cmd_topics))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("addapi", cmd_addapi))
    app.add_handler(CommandHandler("listapi", cmd_listapi))
    app.add_handler(CommandHandler("removeapi", cmd_removeapi))
    app.add_handler(CommandHandler("quiznow", cmd_quiznow))

    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
