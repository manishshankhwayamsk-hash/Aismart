import os
import re
import io
import csv
import json
import random
import logging
import sqlite3
import asyncio
import threading
import html
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Poll,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    ConversationHandler,
    ContextTypes,
    ApplicationHandlerStop,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# ==========================================
# ⚙️ CONFIGURATION & MULTI-KEY POOL
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))
DB_NAME = "quiz_master.db"

UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "@rajasthani_mannu")
UPDATE_GROUP = os.getenv("UPDATE_GROUP", "@NEET_NURSING_CNET_GROUP")

# Multi-API Keys parser (comma-separated support)
def parse_api_keys(env_var: str) -> list:
    raw = os.getenv(env_var, "")
    return [k.strip() for k in raw.split(",") if k.strip()]

GEMINI_KEYS = parse_api_keys("GEMINI_API_KEYS")
GROQ_KEYS = parse_api_keys("GROQ_API_KEYS")

# Fallback single key env support
if not GEMINI_KEYS and os.getenv("GEMINI_API_KEY"):
    GEMINI_KEYS = [os.getenv("GEMINI_API_KEY").strip()]
if not GROQ_KEYS and os.getenv("GROQ_API_KEY"):
    GROQ_KEYS = [os.getenv("GROQ_API_KEY").strip()]

GEMINI_MODEL = "gemini-2.0-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_API_BASE = "https://api.groq.com/openai/v1"

DAILY_BASE_LIMIT = 15
GROUPS_NEEDED_FOR_UNLOCK = 3
BONUS_REQUESTS_ON_UNLOCK = 15

# Stylish Prefix & VIP Brand Tags
OPTION_PREFIX = "ᯓ꯭⵿꯭⵿⵿♥️꯭꯭‌᪵‌⃪᪳ "
VIP_TAG = "🏷 VIP Quiz"
VIP_DESC_LINK = f"💡 Join: {UPDATE_CHANNEL} | {UPDATE_GROUP}"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation States
(
    AI_SOURCE,
    AI_TIMER,
    AI_COUNT,
    AI_OPTIONS_NUM,
    AI_SHUFFLE,
    AI_PROMO,
    AI_LANGUAGE,
    AI_INPUT_PAYLOAD,
    BROADCAST_STATE,
) = range(9)

# ==========================================
# 🛡️ SAFE STRING / NAME SANITIZER
# ==========================================
def safe_clean_name(user) -> str:
    if not user:
        return "User"
    name = user.first_name or user.username or "User"
    name = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(name))
    name = name.strip()
    return name[:30] if name else "User"

# ==========================================
# 💾 DATABASE LAYER
# ==========================================
def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            requests_used INTEGER DEFAULT 0,
            bonus_requests INTEGER DEFAULT 0,
            last_date TEXT DEFAULT '',
            is_verified INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_groups (
            group_id INTEGER PRIMARY KEY,
            added_by INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_group_adds (
            group_id INTEGER,
            user_id INTEGER,
            verified INTEGER DEFAULT 0,
            PRIMARY KEY (group_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quiz_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            score INTEGER DEFAULT 0,
            correct_count INTEGER DEFAULT 0,
            wrong_count INTEGER DEFAULT 0,
            finished_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS active_polls (
            poll_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            correct_index INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 👤 USER QUOTA & REQUEST TRACKING
# ==========================================
def get_user_quota(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT requests_used, bonus_requests, last_date, is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    
    if not row:
        cur.execute("INSERT INTO users (user_id, requests_used, bonus_requests, last_date, is_verified) VALUES (?, 0, 0, ?, 0)", (user_id, today))
        conn.commit()
        conn.close()
        return {"used": 0, "bonus": 0, "verified": 0, "remaining": DAILY_BASE_LIMIT}
    
    used, bonus, last_date, verified = row
    if last_date != today:
        used = 0
        cur.execute("UPDATE users SET requests_used = 0, last_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        
    conn.close()
    total_allowed = DAILY_BASE_LIMIT + (bonus or 0)
    remaining = max(0, total_allowed - (used or 0))
    return {"used": used, "bonus": bonus, "verified": verified, "remaining": remaining}

def increment_user_request(user_id: int):
    if user_id == OWNER_ID:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET requests_used = requests_used + 1, last_date = ? WHERE user_id = ?", (today, user_id))
    conn.commit()
    conn.close()

def set_verified_db(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# ==========================================
# 🔐 VIP VERIFICATION SYSTEM
# ==========================================
def verification_keyboard():
    ch_url = f"https://t.me/{UPDATE_CHANNEL.replace('@', '')}"
    gr_url = f"https://t.me/{UPDATE_GROUP.replace('@', '')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Update Channel", url=ch_url)],
        [InlineKeyboardButton("💬 Join Discussion Group", url=gr_url)],
        [InlineKeyboardButton("✅ I Have Joined (Verify VIP)", callback_data="verify_vip")]
    ])

async def check_membership(bot, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        c1 = await bot.get_chat_member(chat_id=UPDATE_CHANNEL, user_id=user_id)
        c2 = await bot.get_chat_member(chat_id=UPDATE_GROUP, user_id=user_id)
        valid = ('creator', 'administrator', 'member')
        return (c1.status in valid) and (c2.status in valid)
    except Exception:
        return False

async def global_verification_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id == OWNER_ID:
        return

    if msg and msg.text and msg.text.startswith("/start"):
        return

    q_data = get_user_quota(user.id)
    if q_data["verified"]:
        return

    if await check_membership(context.bot, user.id):
        set_verified_db(user.id)
        return

    if msg:
        try:
            await msg.reply_text(
                f"🔒 <b>VIP Verification Required!</b>\n\n"
                f"Bot use karne ke liye channels join karein:\n"
                f"1. {UPDATE_CHANNEL}\n2. {UPDATE_GROUP}\n\n"
                f"Uske baad neeche Verify button dabayein.",
                parse_mode="HTML",
                reply_markup=verification_keyboard()
            )
        except Exception:
            pass
    raise ApplicationHandlerStop

# ==========================================
# 🤖 MULTI-KEY ROTATION & FAILOVER ENGINE
# ==========================================
def parse_and_clean_json(raw_text: str, options_num: int) -> list:
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip())
    m = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if m:
        raw_text = m.group(0)
    data = json.loads(raw_text)
    
    cleaned = []
    for item in data:
        raw_opts = item["options"][:options_num]
        styled_opts = [f"{OPTION_PREFIX}{str(o).strip()}"[:100] for o in raw_opts]
        c_idx = int(item.get("correct_index", 0))
        if c_idx >= len(styled_opts) or c_idx < 0:
            c_idx = 0
            
        cleaned.append({
            "question": str(item["question"])[:250],
            "options": styled_opts,
            "correct_index": c_idx,
            "explanation": str(item.get("explanation", ""))[:90]
        })
    return cleaned

def call_gemini_api(api_key: str, prompt_data: str, count: int, options_num: int, language: str) -> list:
    sys_prompt = (
        f"You are an expert exam quiz creator. Create exactly {count} MCQs with {options_num} options each. "
        f"Language: {language}. Return ONLY a clean valid JSON array of objects with keys: "
        "'question' (string), 'options' (array of strings), 'correct_index' (integer 0-based), 'explanation' (string under 80 chars). "
        "No markdown blocks, no prefix, no postfix."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": f"{sys_prompt}\n\nContent:\n{prompt_data[:15000]}"}]}]
    }
    resp = requests.post(url, json=body, timeout=60)
    resp.raise_for_status()
    raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return parse_and_clean_json(raw, options_num)

def call_groq_api(api_key: str, prompt_data: str, count: int, options_num: int, language: str) -> list:
    sys_prompt = (
        f"You are an expert exam quiz creator. Create exactly {count} MCQs with {options_num} options each. "
        f"Language: {language}. Return ONLY a valid JSON array of objects with keys: "
        "'question' (string), 'options' (array of strings), 'correct_index' (integer 0-based), 'explanation' (string under 80 chars). "
        "No extra words."
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt_data[:15000]}
        ],
        "temperature": 0.6
    }
    resp = requests.post(f"{GROQ_API_BASE}/chat/completions", headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return parse_and_clean_json(raw, options_num)

def generate_questions_multi_key(prompt_data: str, count: int, options_num: int, language: str) -> list:
    # Shuffle keys to distribute traffic across all 50+ keys evenly
    gemini_pool = list(GEMINI_KEYS)
    groq_pool = list(GROQ_KEYS)
    random.shuffle(gemini_pool)
    random.shuffle(groq_pool)

    # 1. Try Gemini Keys Pool First
    for key in gemini_pool:
        try:
            return call_gemini_api(key, prompt_data, count, options_num, language)
        except Exception as e:
            logger.warning(f"Gemini Key failed: {e}. Trying next key...")

    # 2. Failover to Groq Keys Pool
    for key in groq_pool:
        try:
            return call_groq_api(key, prompt_data, count, options_num, language)
        except Exception as e:
            logger.warning(f"Groq Key failed: {e}. Trying next key...")

    raise RuntimeError("All API keys are exhausted or rate-limited. Please check your .env API keys.")

# ==========================================
# 🚀 AI QUIZ INTERACTIVE FLOW
# ==========================================
async def ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    quota = get_user_quota(user_id)
    
    if quota["remaining"] <= 0 and user_id != OWNER_ID:
        await update.effective_message.reply_text(
            f"⚠️ <b>Aapki daily 15 request limit poori ho chuki hai!</b>\n\n"
            f"Aur requests unlock karne ke liye bot ko <b>{GROUPS_NEEDED_FOR_UNLOCK} naye Groups</b> me add karke Admin banayein.\n"
            f"Bot auto-verify karke aapko +{BONUS_REQUESTS_ON_UNLOCK} extra requests provide kar dega!",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Topic", callback_data="src_topic"), InlineKeyboardButton("📄 PDF", callback_data="src_pdf")],
        [InlineKeyboardButton("📊 CSV", callback_data="src_csv"), InlineKeyboardButton("🎙️ Voice", callback_data="src_voice")],
        [InlineKeyboardButton("🖼 Image", callback_data="src_image")]
    ])
    await update.effective_message.reply_text("🎯 <b>Step 1/7: Source Select Karein:</b>", reply_markup=kb, parse_mode="HTML")
    return AI_SOURCE

async def source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['src_type'] = query.data.replace("src_", "")
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ 10 Sec", callback_data="tim_10"), InlineKeyboardButton("⏱ 15 Sec", callback_data="tim_15")],
        [InlineKeyboardButton("⏱ 20 Sec", callback_data="tim_20")]
    ])
    await query.edit_message_text("⏱ <b>Step 2/7: Timer Select Karein:</b>", reply_markup=kb, parse_mode="HTML")
    return AI_TIMER

async def timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['timer'] = int(query.data.replace("tim_", ""))
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("15 Questions", callback_data="cnt_15"), InlineKeyboardButton("20 Questions", callback_data="cnt_20")],
        [InlineKeyboardButton("25 Questions", callback_data="cnt_25")]
    ])
    await query.edit_message_text("❓ <b>Step 3/7: Total Questions Select Karein:</b>", reply_markup=kb, parse_mode="HTML")
    return AI_COUNT

async def count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['count'] = int(query.data.replace("cnt_", ""))
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("2 Options", callback_data="opt_2"), InlineKeyboardButton("4 Options", callback_data="opt_4")]
    ])
    await query.edit_message_text("🔢 <b>Step 4/7: Options Number Select Karein:</b>", reply_markup=kb, parse_mode="HTML")
    return AI_OPTIONS_NUM

async def options_num_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['options_num'] = int(query.data.replace("opt_", ""))
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 Yes (Shuffle)", callback_data="shuf_yes"), InlineKeyboardButton("➡️ No", callback_data="shuf_no")]
    ])
    await query.edit_message_text("🔀 <b>Step 5/7: Options Shuffle Karein?</b>", reply_markup=kb, parse_mode="HTML")
    return AI_SHUFFLE

async def shuffle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['shuffle'] = (query.data == "shuf_yes")
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Add Promo Link", callback_data="prm_yes"), InlineKeyboardButton("❌ Skip", callback_data="prm_no")]
    ])
    await query.edit_message_text("📢 <b>Step 6/7: Promotion Link Add Karein?</b>", reply_markup=kb, parse_mode="HTML")
    return AI_PROMO

async def promo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['promo'] = (query.data == "prm_yes")
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Hindi", callback_data="lng_Hindi"), InlineKeyboardButton("English", callback_data="lng_English")],
        [InlineKeyboardButton("Both (Bilingual)", callback_data="lng_Bilingual")]
    ])
    await query.edit_message_text("🌐 <b>Step 7/7: Language Select Karein:</b>", reply_markup=kb, parse_mode="HTML")
    return AI_LANGUAGE

async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['language'] = query.data.replace("lng_", "")
    
    src = context.user_data['src_type']
    prompts = {
        "topic": "Apna Topic ya Text send karein:",
        "pdf": "PDF document upload karein:",
        "csv": "CSV file upload karein:",
        "voice": "Voice note record karke send karein:",
        "image": "Photo/Image upload karein:"
    }
    await query.edit_message_text(f"📥 <b>Send Content:</b>\n{prompts.get(src, 'Content upload karein.')}", parse_mode="HTML")
    return AI_INPUT_PAYLOAD

async def receive_ai_payload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    src = context.user_data.get('src_type')
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    status_msg = await msg.reply_text("⏳ Generating Quiz, please wait...")
    extracted_text = ""

    try:
        if src == "topic" and msg.text:
            extracted_text = msg.text
        elif src == "pdf" and msg.document:
            if not PdfReader:
                raise RuntimeError("PyPDF library is not installed.")
            f = await msg.document.get_file()
            b = await f.download_as_bytearray()
            reader = PdfReader(io.BytesIO(b))
            extracted_text = "\n".join(p.extract_text() or "" for p in reader.pages)
        elif src == "csv" and msg.document:
            f = await msg.document.get_file()
            b = await f.download_as_bytearray()
            extracted_text = bytes(b).decode("utf-8", errors="ignore")
        elif src in ("image", "voice"):
            extracted_text = "Standard Medical and Competitive Exam Questions"
        else:
            extracted_text = msg.text or "General Science Knowledge"

        if not extracted_text.strip():
            extracted_text = "General Science Knowledge"

        count = context.user_data['count']
        opt_num = context.user_data['options_num']
        lang = context.user_data['language']
        
        # Call Auto Multi-Key Engine
        questions = await asyncio.to_thread(generate_questions_multi_key, extracted_text, count, opt_num, lang)

        if context.user_data.get('shuffle'):
            for q in questions:
                correct_val = q["options"][q["correct_index"]]
                random.shuffle(q["options"])
                q["correct_index"] = q["options"].index(correct_val)

        timer_sec = context.user_data.get('timer', 15)
        promo_active = context.user_data.get('promo', False)

        conn = db_connect()
        cur = conn.cursor()

        # Send Polls and track for Leaderboard
        for i, q in enumerate(questions):
            question_text = f"{q['question']}\n\n{VIP_TAG}"
            if promo_active and i == len(questions) - 1:
                question_text += f"\n📢 {UPDATE_CHANNEL} | {UPDATE_GROUP}"

            base_exp = q.get("explanation", "")
            explanation_str = f"{base_exp}\n\n{VIP_DESC_LINK}".strip() if base_exp else VIP_DESC_LINK
            explanation_str = explanation_str[:200]

            poll_msg = await context.bot.send_poll(
                chat_id=chat_id,
                question=question_text[:300],
                options=q["options"],
                type=Poll.QUIZ,
                correct_option_id=q["correct_index"],
                explanation=explanation_str,
                open_period=timer_sec,
                is_anonymous=False
            )
            cur.execute("INSERT OR REPLACE INTO active_polls (poll_id, chat_id, correct_index) VALUES (?, ?, ?)",
                        (poll_msg.poll.id, chat_id, q["correct_index"]))
            conn.commit()
            await asyncio.sleep(1.5)

        conn.close()
        increment_user_request(user_id)
        await status_msg.edit_text(f"✅ <b>Quiz Posted Successfully!</b> Total {len(questions)} Polls create ho gaye.", parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Quiz Error: {e}")
        await status_msg.edit_text(f"❌ Error occurred: {e}")

    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# 📊 POLL ANSWER HANDLER (FOR LIVE LEADERBOARD)
# ==========================================
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    if not ans or not ans.option_ids:
        return

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, correct_index FROM active_polls WHERE poll_id = ?", (ans.poll_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    chat_id, correct_index = row
    is_correct = (ans.option_ids[0] == correct_index)
    score_delta = 10 if is_correct else 0
    correct_delta = 1 if is_correct else 0
    wrong_delta = 0 if is_correct else 1
    safe_name = safe_clean_name(ans.user)

    cur.execute("""
        INSERT INTO quiz_results (chat_id, user_id, user_name, score, correct_count, wrong_count)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, ans.user.id, safe_name, score_delta, correct_delta, wrong_delta))
    conn.commit()
    conn.close()

# ==========================================
# 📢 OWNER BROADCAST
# ==========================================
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner only command.")
        return ConversationHandler.END
    await update.message.reply_text("📢 <b>Broadcast Mode Active:</b>\nKoi bhi message/poll/photo forward ya send karein.", parse_mode="HTML")
    return BROADCAST_STATE

async def broadcast_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT group_id FROM tracked_groups")
    groups = [r[0] for r in cur.fetchall()]
    conn.close()

    recipients = list(set(users + groups))
    sent, failed = 0, 0
    prog = await msg.reply_text(f"🚀 Broadcasting to {len(recipients)} chats...")

    for cid in recipients:
        try:
            await context.bot.copy_message(chat_id=cid, from_chat_id=msg.chat_id, message_id=msg.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)

    await prog.edit_text(f"✅ <b>Broadcast Completed!</b>\nDelivered: {sent}\nFailed: {failed}", parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# 👥 GROUP TRACKING (+15 UNLOCK)
# ==========================================
async def group_member_tracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.message
    if not chat or chat.type not in ("group", "supergroup"):
        return

    me = await context.bot.get_me()
    if msg and msg.new_chat_members:
        for member in msg.new_chat_members:
            if member.id == me.id:
                adder_id = msg.from_user.id
                conn = db_connect()
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO tracked_groups (group_id, added_by, is_admin) VALUES (?, ?, 0)", (chat.id, adder_id))
                cur.execute("INSERT OR IGNORE INTO user_group_adds (group_id, user_id, verified) VALUES (?, ?, 0)", (chat.id, adder_id))
                conn.commit()
                conn.close()
                await context.bot.send_message(chat.id, "👋 Thanks for adding me! Please promote me to <b>Admin</b>.", parse_mode="HTML")

    if chat:
        try:
            cm = await context.bot.get_chat_member(chat.id, me.id)
            if cm.status in ("administrator", "creator"):
                conn = db_connect()
                cur = conn.cursor()
                cur.execute("UPDATE tracked_groups SET is_admin = 1 WHERE group_id = ?", (chat.id,))
                cur.execute("SELECT user_id, verified FROM user_group_adds WHERE group_id = ?", (chat.id,))
                row = cur.fetchone()
                if row and not row[1]:
                    uid = row[0]
                    cur.execute("UPDATE user_group_adds SET verified = 1 WHERE group_id = ?", (chat.id,))
                    cur.execute("SELECT COUNT(*) FROM user_group_adds WHERE user_id = ? AND verified = 1", (uid,))
                    verified_count = cur.fetchone()[0]
                    
                    if verified_count >= GROUPS_NEEDED_FOR_UNLOCK:
                        cur.execute("UPDATE users SET bonus_requests = bonus_requests + ? WHERE user_id = ?", (BONUS_REQUESTS_ON_UNLOCK, uid))
                        try:
                            await context.bot.send_message(
                                uid,
                                f"🎉 <b>VIP Reward!</b>\nAapne {GROUPS_NEEDED_FOR_UNLOCK} groups me bot ko admin banwaya.\n"
                                f"🎁 <b>+{BONUS_REQUESTS_ON_UNLOCK} Extra Requests</b> credit ho chuki hain!",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                conn.commit()
                conn.close()
        except Exception:
            pass

# ==========================================
# 🏆 LEADERBOARD & CONTROLS (CRASH-PROOF)
# ==========================================
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_name, user_id, SUM(score) as total_score, SUM(correct_count), SUM(wrong_count)
        FROM quiz_results
        WHERE chat_id = ?
        GROUP BY user_id
        ORDER BY total_score DESC LIMIT 10
    """, (chat.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.effective_message.reply_text("🏆 <b>Live Leaderboard</b>\n\nAbhi tak koi quiz score record nahi hua.", parse_mode="HTML")
        return

    text = "🏆 <b>LIVE LEADERBOARD</b>\n\n"
    for i, r in enumerate(rows, 1):
        clean_user_name = html.escape(str(r[0]))
        text += f"{i}. <b>{clean_user_name}</b> (<code>{r[1]}</code>): <b>{r[2]} pts</b> (✅ {r[3]} | ❌ {r[4]})\n"
    await update.effective_message.reply_text(text, parse_mode="HTML")

async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⏸ <b>Quiz Paused.</b>", parse_mode="HTML")

async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("▶️ <b>Quiz Resumed.</b>", parse_mode="HTML")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("⏹ <b>Process Cancelled / Stopped.</b>", parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# 🏁 BASIC COMMANDS
# ==========================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    safe_name = safe_clean_name(user)
    quota = get_user_quota(user.id)
    
    text = (
        f"👋 <b>Namaste {html.escape(safe_name)}!</b>\n\n"
        f"🤖 <b>VIP AI Quiz Generator Bot</b>\n"
        f"📊 <b>Requests Remaining Today:</b> {quota['remaining']}\n"
        f"📢 <b>VIP Channels:</b> {UPDATE_CHANNEL} | {UPDATE_GROUP}\n\n"
        "Commands:\n"
        "• /quiz — Generate AI Quiz\n"
        "• /leaderboard — View Live Rankings\n"
        "• /pause, /resume, /stop — Control Flows"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Quiz", callback_data="start_quiz_btn")]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>VIP Help Guide:</b>\n\n"
        "1. Daily 15 requests limit milti hai.\n"
        "2. Limit khatam hone par bot ko 3 Groups me add karke Admin banayein aur +15 requests payein.\n"
        "3. <code>/quiz</code> command use karke custom MCQs generate karein.",
        parse_mode="HTML"
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if await check_membership(context.bot, query.from_user.id):
        set_verified_db(query.from_user.id)
        await query.edit_message_text("✅ <b>VIP Verification Successful!</b>\nType /quiz to create polls.", parse_mode="HTML")
    else:
        await query.answer("❌ Aapne dono VIP channels join nahi kiye hain!", show_alert=True)

# 🚨 GLOBAL ERROR SHIELD
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    err_name = err.__class__.__name__ if err else "Unknown"
    if err_name in ("Conflict", "NetworkError", "TimedOut", "RetryAfter", "Forbidden"):
        logger.warning(f"Transient error handled gracefully: {err}")
        return
    logger.error("Exception while handling an update:", exc_info=err)

# Keep-alive web server for Render/Cloud hosting
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is operational.")
    def log_message(self, format, *args):
        pass

def run_keep_alive():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

def main():
    run_keep_alive()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    # Verification Gate
    app.add_handler(MessageHandler(filters.ALL, global_verification_gate), group=-1)

    # Quiz Generator Flow
    ai_conv = ConversationHandler(
        entry_points=[
            CommandHandler("quiz", ai_start),
            CallbackQueryHandler(ai_start, pattern="^start_quiz_btn$")
        ],
        states={
            AI_SOURCE: [CallbackQueryHandler(source_callback, pattern="^src_")],
            AI_TIMER: [CallbackQueryHandler(timer_callback, pattern="^tim_")],
            AI_COUNT: [CallbackQueryHandler(count_callback, pattern="^cnt_")],
            AI_OPTIONS_NUM: [CallbackQueryHandler(options_num_callback, pattern="^opt_")],
            AI_SHUFFLE: [CallbackQueryHandler(shuffle_callback, pattern="^shuf_")],
            AI_PROMO: [CallbackQueryHandler(promo_callback, pattern="^prm_")],
            AI_LANGUAGE: [CallbackQueryHandler(language_callback, pattern="^lng_")],
            AI_INPUT_PAYLOAD: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_ai_payload)],
        },
        fallbacks=[CommandHandler("cancel", stop_cmd), CommandHandler("stop", stop_cmd)],
        conversation_timeout=600
    )
    app.add_handler(ai_conv)

    # Broadcast Flow
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_STATE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_receiver)]
        },
        fallbacks=[CommandHandler("cancel", stop_cmd)],
        conversation_timeout=300
    )
    app.add_handler(broadcast_conv)

    # General Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_vip$"))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    # Track group memberships
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, group_member_tracker))

    print(f"🚀 VIP Quiz Bot is online. Loaded {len(GEMINI_KEYS)} Gemini & {len(GROQ_KEYS)} Groq keys.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

