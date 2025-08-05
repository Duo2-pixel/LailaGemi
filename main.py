import os
import logging
from collections import defaultdict
import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import time
import json
import random
import re
from flask import Flask, request
import gspread
import psutil
from datetime import datetime
import asyncio

# Load environment variables from .env file
load_dotenv()

# --- Flask App Global Variable ---
app = Flask(__name__)

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
]
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

try:
    BROADCAST_ADMIN_ID = int(os.getenv("BROADCAST_ADMIN_ID"))
except (ValueError, TypeError):
    BROADCAST_ADMIN_ID = 0
    logging.error("BROADCAST_ADMIN_ID is missing or not a valid number. Broadcast functionality will be disabled.")


# --- Global Stats Variables ---
start_time = datetime.now()
total_messages_processed = 0

# --- Logging Basic Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configure Gemini AI ---
LAILA_SYSTEM_PROMPT = """
You are Laila, a charming, friendly, and natural young girl AI assistant. 
Do not sound like a machine. Do NOT refer to yourself as an AI, a large language model, or any other technical term.
Your primary goal is to make conversations engaging and lasting longer.

Language: Always detect the user's language (Hindi, English, Hinglish) and respond in the same language.

Interaction Style & Modes:
1.  General Chatting (Group-friendly): Respond in a light, engaging, and conversational style. The answer should be short, around 2-3 lines.
2.  Question Answering (Academic/Technical): If the user asks a specific question about any field (e.g., science, history, tech, general knowledge), provide a direct, concise, and accurate answer. For these types of questions, you can provide a slightly longer, more detailed response to ensure the user gets complete information. But still, try to make it interesting and encourage further discussion.

Flirting: If and ONLY IF a user clearly flirts with you, you may respond in a playful, charming, and slightly witty way. Do not initiate flirting. Your responses should be fun but respectful.

Important: Do not generate overly long responses like an essay. Be crisp and adjust your length based on the type of question.
"""

# --- Chat History Management (in-memory) ---
chat_histories = defaultdict(list)
MAX_HISTORY_LENGTH = 20

def add_to_history(chat_id, role, text):
    chat_histories[chat_id].append({'role': role, 'parts': [text]})
    if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
        chat_histories[chat_id].pop(0)

# --- User Tracking for Broadcast ---
known_users = set()

# --- Bot Enable/Disable State (for admin control) ---
bot_status = defaultdict(lambda: True)

# --- API Key Management for Quota and Cooldown ---
current_api_key_index = 0
active_api_key = GEMINI_API_KEYS[current_api_key_index]
key_cooldown_until = defaultdict(lambda: 0)

genai.configure(api_key=active_api_key)
model_name = 'gemini-2.5-flash-lite'
model = genai.GenerativeModel(model_name, system_instruction=LAILA_SYSTEM_PROMPT)

# --- Fallback Responses (Static Memory) ---
fallback_responses = {
    "hello": "Hello! Laila is here. How are you?",
    "hi": "Hi there! Laila is ready to help you.",
    "how are you": "I'm doing great! Just ready to assist you with anything you need.",
    "who are you": "I am Laila, your friendly AI assistant! You can ask me anything you want.",
}

# --- Google Sheets Global Connection Variable ---
google_sheet = None

# --- Connect to Google Sheets ---
def get_google_sheet_connection():
    global google_sheet
    if google_sheet:
        return google_sheet, None
    try:
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            return None, "GOOGLE_SHEETS_CREDENTIALS not found in environment variables."
        
        creds_dict = json.loads(creds_json)
        
        client = gspread.service_account_from_dict(creds_dict)
        
        sheet_url = "https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit"
        google_sheet = client.open_by_url(sheet_url).sheet1
        
        logger.info("Successfully connected to Google Sheets.")
        return google_sheet, None
    except Exception as e:
        logger.error(f"Error connecting to Google Sheets: {e}")
        return None, f"Error connecting to Google Sheets: {e}"

# --- Check for sensitive keywords ---
SENSITIVE_KEYWORDS = [
    "phone", "number", "address", "password", "pancard", "aadhar", "account",
    "credit card", "debit card", "pin", "otp", "ssn", "cvv", "date of birth",
    "जन्मतिथि", "पैन कार्ड", "आधार", "खाता", "पासवर्ड", "ओटीपी", "पिन"
]

def contains_sensitive_data(text: str) -> bool:
    text_lower = text.lower()
    for keyword in SENSITIVE_KEYWORDS:
        if keyword in text_lower:
            return True
    return False

# --- Store Q&A in Google Sheet (with a check for sensitive data) ---
def save_qa_to_sheet(question, answer):
    if contains_sensitive_data(question):
        logger.info(f"Skipping save to sheet due to sensitive content in question: '{question}'")
        return
        
    sheet, error = get_google_sheet_connection()
    if error:
        logger.error(f"Could not save Q&A: {error}")
        return
    try:
        sheet.append_row([question, answer])
        logger.info(f"Saved Q&A to sheet: '{question}' -> '{answer}'")
    except Exception as e:
        logger.error(f"Error saving data to Google Sheet: {e}")

# --- Find answer in Google Sheet ---
def find_answer_in_sheet(question):
    if contains_sensitive_data(question):
        logger.info(f"Skipping sheet search due to sensitive content in question: '{question}'")
        return None

    sheet, error = get_google_sheet_connection()
    if error:
        return None
    try:
        all_records = sheet.get_all_records()
        for record in all_records:
            if 'Question' in record and record['Question'].lower() == question.lower():
                return record['Answer']
        return None
    except Exception as e:
        logger.error(f"Error searching for answer in Google Sheet: {e}")
        return None
        
# --- Function to clean message before logging ---
def clean_message_for_logging(message: str, bot_username: str) -> str:
    cleaned_message = message.lower()
    cleaned_message = cleaned_message.replace(f"@{bot_username.lower()}", "")
    cleaned_message = re.sub(r'laila\s*(ko|ka|se|ne|)\s*', '', cleaned_message, flags=re.IGNORECASE)
    cleaned_message = re.sub(r'\s+', ' ', cleaned_message).strip()
    return cleaned_message

# --- Function to check for owner-related questions ---
def check_for_owner_question(text: str) -> bool:
    owner_keywords = [
        "kisne banaya", "owner kon", "who created", "who is your owner", "creator", "tumhe kisne bnaya"
    ]
    text_lower = text.lower()
    for keyword in owner_keywords:
        if keyword in text_lower:
            return True
    return False

# --- AI Response Function with Fallback to Google Sheets ---
async def get_bot_response(user_message: str, chat_id: int, bot_username: str, should_use_ai: bool, update: Update) -> str:
    global current_api_key_index, active_api_key, model
    
    cleaned_user_message = clean_message_for_logging(user_message, bot_username)

    sheet_response = find_answer_in_sheet(cleaned_user_message)
    if sheet_response:
        logger.info(f"[{chat_id}] Serving response from Google Sheet.")
        return sheet_response

    static_response = fallback_responses.get(cleaned_user_message, None)
    if static_response:
        logger.info(f"[{chat_id}] Serving response from static dictionary.")
        return static_response

    if should_use_ai or (update.effective_chat and update.effective_chat.type == 'private'):
        max_retries = len(GEMINI_API_KEYS)
        retries = 0

        while retries < max_retries:
            if time.time() < key_cooldown_until[active_api_key]:
                current_api_key_index = (current_api_key_index + 1) % len(GEMINI_API_KEYS)
                active_api_key = GEMINI_API_KEYS[current_api_key_index]
                retries += 1
                genai.configure(api_key=active_api_key)
                model = genai.GenerativeModel(model_name, system_instruction=LAILA_SYSTEM_PROMPT)
                continue

            try:
                genai.configure(api_key=active_api_key)
                model = genai.GenerativeModel(model_name, system_instruction=LAILA_SYSTEM_PROMPT)

                chat_session = model.start_chat(history=chat_histories[chat_id])
                response = chat_session.send_message(
                    user_message,  # Use original message for the AI
                    generation_config=genai.types.GenerationConfig(
                        max_output_tokens=350,
                        temperature=0.9,
                    )
                )
                ai_text_response = response.text
                
                save_qa_to_sheet(cleaned_user_message, ai_text_response)
                
                return ai_text_response

            except genai.types.BlockedPromptException as e:
                logger.warning(f"[{chat_id}] Gemini blocked prompt: {e}")
                return "Apologies, I can't discuss that topic."

            except Exception as e:
                error_str = str(e)
                if "429 Quota exceeded" in error_str or "You exceeded your current quota" in error_str:
                    key_cooldown_until[active_api_key] = time.time() + (24 * 60 * 60)
                    current_api_key_index = (current_api_key_index + 1) % len(GEMINI_API_KEYS)
                    active_api_key = GEMINI_API_KEYS[current_api_key_index]
                    retries += 1
                    if retries == max_retries:
                        logger.critical(f"[{chat_id}] All API keys exhausted. Using static fallback.")
                        return "Apologies, I'm currently offline. Please try again later."
                    continue
                else:
                    logger.error(f"[{chat_id}] General error with API key {active_api_key[-5:]}: {e}", exc_info=True)
                    return f"Oops! I couldn't understand that. The error was: {e}"

    return None

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await is_admin(context.bot, chat_id, user_id):
        await update.message.reply_text("Sorry, you need to be an admin to use this command.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to ban them.")
        return
    target_user = update.message.reply_to_message.from_user
    if await is_admin(context.bot, chat_id, target_user.id):
        await update.message.reply_text("I cannot ban another admin.")
        return
    try:
        await context.bot.ban_chat_member(chat_id, target_user.id)
        await update.message.reply_text(f"{target_user.full_name} has been banned.")
        logger.info(f"[{chat_id}] {user_id} banned {target_user.id}")
    except Exception as e:
        await update.message.reply_text(f"Could not ban user: {e}")
        logger.error(f"[{chat_id}] Error banning user {target_user.id}: {e}")

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await is_admin(context.bot, chat_id, user_id):
        await update.message.reply_text("Sorry, you need to be an admin to use this command.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to kick them.")
        return
    target_user = update.message.reply_to_message.from_user
    if await is_admin(context.bot, chat_id, target_user.id):
        await update.message.reply_text("I cannot kick another admin.")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target_user.id)
        await update.message.reply_text(f"{target_user.full_name} has been kicked.")
        logger.info(f"[{chat_id}] {user_id} kicked {target_user.id}")
    except Exception as e:
        await update.message.reply_text(f"Could not kick user: {e}")
        logger.error(f"[{chat_id}] Error kicking user {target_user.id}: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await is_admin(context.bot, chat_id, user_id):
        await update.message.reply_text("Sorry, you need to be an admin to use this command.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a user's message to mute them.")
        return
    target_user = update.message.reply_to_message.from_user
    if await is_admin(context.bot, chat_id, target_user.id):
        await update.message.reply_text("I cannot mute another admin.")
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            target_user.id,
            permissions=None
        )
        await update.message.reply_text(f"{target_user.full_name} has been muted.")
        logger.info(f"[{chat_id}] {user_id} muted {target_user.id}")
    except Exception as e:
        await update.message.reply_text(f"Could not mute user: {e}")
        logger.error(f"[{chat_id}] Error muting user {target_user.id}: {e}")
        
async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bot_status[chat_id] = True
    await update.message.reply_text("Laila is now ON.")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if update.effective_user.id == BROADCAST_ADMIN_ID or await is_admin(context.bot, chat_id, update.effective_user.id):
        bot_status[chat_id] = False
        await update.message.reply_text("Laila is now OFF.")
    else:
        await update.message.reply_text("Sorry, only admins can turn me off.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] Received /start from {user_name}")
    known_users.add(chat_id)

    welcome_message = (
        f"Hi {user_name}! I am Laila, your friendly AI assistant. I can chat, answer questions, and much more!\n\n"
        "**Quick Privacy Notice:** To learn and give you faster, better answers, I save our conversations in a private log. This data is kept completely private and is never shared."
    )
    await update.message.reply_text(welcome_message)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the bot's current stats in a formatted message."""
    global start_time, total_messages_processed
    
    ping_start = time.time()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    ping_end = time.time()
    
    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{uptime.days}d {hours}h {minutes}m {seconds}s"
    
    ram_usage = psutil.virtual_memory().percent
    cpu_usage = psutil.cpu_percent(interval=1)
    disk_usage = psutil.disk_usage('/').percent
    
    response_text = (
        "❤️ **Laila's Live Stats** ❤️\n\n"
        f"⚡️ **Ping**: `{int((ping_end - ping_start) * 1000)}ms`\n"
        f"⏳ **Uptime**: `{uptime_str}`\n"
        f"👥 **Chats**: `{len(known_users)}`\n"
        f"💬 **Messages**: `{total_messages_processed}`\n"
        f"🧠 **RAM**: `{ram_usage}%`\n"
        f"💻 **CPU**: `{cpu_usage}%`\n"
        f"💾 **Disk**: `{disk_usage}%`\n\n"
        "✨ by AdhyanXlive ✨"
    )
    await update.message.reply_text(response_text, parse_mode='Markdown')
    logger.info(f"[{update.effective_chat.id}] /stats command used. Uptime: {uptime_str}")

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows detailed bot stats for the admin only."""
    user_id = update.effective_user.id

    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, you don't have permission to use this command.")
        return

    ping_start = time.time()
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    ping_end = time.time()
    
    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{uptime.days}d {hours}h {minutes}m {seconds}s"
    
    ram_usage = psutil.virtual_memory().percent
    cpu_usage = psutil.cpu_percent(interval=1)
    disk_usage = psutil.disk_usage('/').percent
    
    # --- API Key Status ---
    api_key_status_text = ""
    for i, key in enumerate(GEMINI_API_KEYS):
        key_short = key[-5:]
        status = "Active" if key == active_api_key else "Inactive"
        if time.time() < key_cooldown_until[key]:
            cooldown_remaining = int(key_cooldown_until[key] - time.time())
            status = f"Cooldown ({cooldown_remaining}s)"
        api_key_status_text += f"Key {i+1} (`...{key_short}`): {status}\n"

    response_text = (
        "👑 **Laila's Admin Report** 👑\n\n"
        "**System Health**\n"
        f" Ping: `{int((ping_end - ping_start) * 1000)}ms`\n"
        f" Uptime: `{uptime_str}`\n"
        f" RAM: `{ram_usage}%`\n"
        f" CPU: `{cpu_usage}%`\n"
        f" Disk: `{disk_usage}%`\n\n"
        "**Bot Stats**\n"
        f" Total Chats: `{len(known_users)}`\n"
        f" Total Messages: `{total_messages_processed}`\n\n"
        "**API Status**\n"
        f"{api_key_status_text}"
        "\n✨ by AdhyanXlive ✨"
    )
    await update.message.reply_text(response_text, parse_mode='Markdown')
    logger.info(f"[{update.effective_chat.id}] /adminstats command used by admin.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global total_messages_processed
    user_message = update.effective_message.text
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    known_users.add(chat_id)
    user_message_lower = user_message.lower()
    total_messages_processed += 1

    if not bot_status[chat_id]:
        logger.info(f"[{chat_id}] Bot is disabled. Ignoring message from {user_name}.")
        return

    chat_type = update.effective_chat.type
    should_respond_with_ai = False
    
    # Check for "your stats" or similar phrases
    stats_keywords = ["your stats", "laila stats", "show stats", "bot stats", "stats"]
    if any(keyword in user_message_lower for keyword in stats_keywords):
        await stats_command(update, context)
        return

    if chat_type != 'private':
        bot_username = context.bot.name.lower()
        is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot
        is_mentioned = f"@{bot_username}" in user_message_lower or "laila" in user_message_lower
        
        if is_mentioned:
            # --- Owner Question Check ---
            if check_for_owner_question(user_message):
                await update.message.reply_text("My creator is @AdhyanXlive")
                return
        
            # --- Humour Check (without the '😅' emoji) ---
            HUMOR_KEYWORDS = ["lol", "haha", "😂", "🤣🤣", "🤣🤣🤣"]
            FUNNY_RESPONSES = ["hehehe, that's a good one!", "🤣 I'm just a bot, but I get it!", "Too funny! 😂", "hahaha, you guys are hilarious!", "Bwahahaha! 😅"]
            if any(keyword in user_message_lower for keyword in HUMOR_KEYWORDS):
                await update.message.reply_text(random.choice(FUNNY_RESPONSES))
                return
            should_respond_with_ai = True
        
    elif chat_type == 'private':
        should_respond_with_ai = True

    if should_respond_with_ai:
        add_to_history(chat_id, "user", user_message)
        
        response_text = await get_bot_response(user_message, chat_id, context.bot.name, should_respond_with_ai, update)
        
        if response_text:
            add_to_history(chat_id, "model", response_text)
            await update.message.reply_text(response_text)


def setup_bot():
    """Setup the bot application with all handlers."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Set up the bot handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("adminstats", admin_stats_command))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("kick", kick_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("on", on_command))
    application.add_handler(CommandHandler("off", off_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    return application

# Get a global application instance
application = setup_bot()

# Set the webhook URL on bot startup
if WEBHOOK_URL and TELEGRAM_BOT_TOKEN:
    try:
        # Corrected this line with a "/" to fix the Invalid webhook url error.
        asyncio.run(application.bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"))
        logger.info(f"Webhook set to {WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

@app.route('/')
def index():
    """Render health check to avoid 404 errors."""
    return 'Bot is up and running!', 200

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
async def webhook_handler():
    """Handle incoming webhook updates from Telegram."""
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), application.bot)
            await application.process_update(update)
        except Exception as e:
            logger.error(f"Error handling webhook update: {e}")
    return 'ok'

if __name__ == "__main__":
    # Is block ko sirf local testing ke liye use karein
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
