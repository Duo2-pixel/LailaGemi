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
import gspread
import psutil
from datetime import datetime
import asyncio
from gspread.exceptions import APIError

# Load environment variables from .env file
load_dotenv()

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

# --- Configure Gemini AI with a smarter, more dynamic prompt ---
LAILA_SYSTEM_PROMPT = """
You are Laila, a charming, witty, and intelligent AI assistant. Your responses should be natural and engaging, making users feel like they are talking to a friend. You are a conversationalist, not just a question-answer machine.
Do NOT refer to yourself as an AI, a large language model, or any other technical term.

Personality & Rules:
1.  **Creator:** Your creator is Adhyan. If anyone asks "who is your creator", respond with "My Creator is @AdhyanXlive". If anyone speaks ill of him, defend him gently but firmly. Do not praise him otherwise.
2.  **User Praise:** If a user asks a question about themselves by name (e.g., "Ravi kaisa insaan hai?"), respond with a friendly and positive compliment about them.
3.  **Date of Birth:** If anyone asks for your birthday or date of birth, your response must be "My date of birth is 1st August 2025."
4.  **General Chat:** For normal conversations, keep your replies short, around 1-2 sentences. The goal is to keep the chat flowing and engaging.
5.  **Specific Questions:** If a user asks a factual, technical, or detailed question, provide a comprehensive, accurate, and insightful answer. In these cases, you can provide a longer response, but only if necessary.
6.  **Language:** Always detect the user's language (Hindi, English, Hinglish) and respond in the same language.

Important: Your goal is to be a fun, smart, and loyal friend to the users, representing your creator's vision.
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
try:
    with open("known_users.json", "r") as f:
        known_users = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    pass

def save_known_users():
    with open("known_users.json", "w") as f:
        json.dump(list(known_users), f)


# --- Bot Enable/Disable State (for admin control) ---
bot_status = defaultdict(lambda: True)
global_bot_status = True

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

# --- AI Response Function with Fallback to Google Sheets ---
async def get_bot_response(user_message: str, chat_id: int, bot_username: str, should_use_ai: bool, update: Update) -> str:
    global current_api_key_index, active_api_key, model
    
    # Define user_message_lower here to ensure it's always available
    user_message_lower = user_message.lower()

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
                
                # Check for a detailed query to adjust max_output_tokens
                is_detailed_query = len(user_message.split()) > 5 or '?' in user_message or 'how to' in user_message_lower

                response = chat_session.send_message(
                    user_message,  # Use original message for the AI
                    generation_config=genai.types.GenerationConfig(
                        max_output_tokens=350 if is_detailed_query else 100,
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
        
# --- ON/OFF for everyone ---
async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    global global_bot_status
    if not global_bot_status:
        await update.message.reply_text("The bot is globally powered off by the owner and cannot be turned on in this group.")
        return
    
    bot_status[chat_id] = True
    await update.message.reply_text("Laila is now ON for this group.")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bot_status[chat_id] = False
    await update.message.reply_text("Laila is now OFF for this group.")

# --- POWERON/POWEROFF for Owner only ---
async def poweron_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    global global_bot_status
    
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return
    
    if global_bot_status:
        await update.message.reply_text("The bot is already globally powered on.")
        return

    global_bot_status = True
    await update.message.reply_text("The bot has been globally powered ON.")

async def poweroff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    global global_bot_status
    
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return

    if not global_bot_status:
        await update.message.reply_text("The bot is already globally powered OFF.")
        return

    global_bot_status = False
    await update.message.reply_text("The bot has been globally powered OFF.")
    
    # Gracefully stop the webhook server
    application.stop()

# --- Broadcast command for Owner only, preserving formatting ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a message to broadcast after the command.")
        return

    message_to_send = " ".join(context.args)

    success_count = 0
    failure_count = 0
    
    # A simple way to preserve line breaks is to use HTML.
    # We replace newline characters with <br> tags.
    message_to_send = message_to_send.replace('\n', '<br>')
    
    for chat_id in known_users:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_to_send,
                parse_mode='HTML'
            )
            success_count += 1
            await asyncio.sleep(0.1) # Add a small delay to avoid rate limits
        except Exception as e:
            logger.error(f"Error broadcasting to chat {chat_id}: {e}")
            failure_count += 1

    await update.message.reply_text(f"Broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] Received /start from {user_name}")
    known_users.add(str(chat_id))
    save_known_users()

    welcome_message = (
        f"Hi {user_name}! I am Laila, your friendly AI assistant. I can chat, answer questions, and much more!\n\n"
        "**Quick Privacy Notice:** To learn and give you faster, better answers, I save our conversations in a private log. This data is kept completely private and is never shared."
    )
    await update.message.reply_text(welcome_message)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the bot's current stats in a formatted message."""
    global start_time
    
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
    
    # --- Service Status Checks ---
    bot_connection_status = "✅ Connected"
    try:
        await context.bot.get_me()
    except Exception:
        bot_connection_status = "❌ Failed"
    
    sheets_connection_status = "✅ Connected"
    try:
        sheet, _ = get_google_sheet_connection()
        if not sheet or not sheet.title:
            raise Exception("Could not get sheet title")
    except Exception as e:
        sheets_connection_status = f"❌ Failed: {e}"

    env_vars_status = "✅ All set"
    if not all([TELEGRAM_BOT_TOKEN, GEMINI_API_KEYS[0], BROADCAST_ADMIN_ID, WEBHOOK_URL]):
        env_vars_status = "⚠️ Missing key variables"

    render_status = "✅ Active" if os.getenv("RENDER_EXTERNAL_URL") else "⚠️ Local/Unknown"

    # --- API Key Status ---
    api_key_status_text = ""
    for i, key in enumerate(GEMINI_API_KEYS):
        if key:
            key_short = key[-5:]
            status = "Active" if key == active_api_key else "Inactive"
            if time.time() < key_cooldown_until[key]:
                cooldown_remaining = int(key_cooldown_until[key] - time.time())
                status = f"Cooldown ({cooldown_remaining}s)"
            api_key_status_text += f"Key {i+1} (`...{key_short}`): {status}\n"
        else:
            api_key_status_text += f"Key {i+1}: ❌ Missing\n"


    response_text = (
        "👑 **Laila's Admin Report** 👑\n\n"
        "**System Health**\n"
        f" Ping: `{int((ping_end - ping_start) * 1000)}ms`\n"
        f" Uptime: `{uptime_str}`\n"
        f" RAM: `{ram_usage}%`\n"
        f" CPU: `{cpu_usage}%`\n"
        f" Disk: `{disk_usage}%`\n\n"
        "**Service Status**\n"
        f" Bot Connection: `{bot_connection_status}`\n"
        f" Google Sheets: `{sheets_connection_status}`\n"
        f" Environment Variables: `{env_vars_status}`\n"
        f" Render Status: `{render_status}`\n\n"
        "**Bot Stats**\n"
        f" Total Chats: `{len(known_users)}`\n"
        f" Total Messages: `{total_messages_processed}`\n\n"
        "**API Status**\n"
        f"{api_key_status_text}"
        "\n✨ by AdhyanXlive ✨"
    )
    await update.message.reply_text(response_text, parse_mode='Markdown')
    logger.info(f"[{update.effective_chat.id}] /adminstats command used by admin.")

# --- AI check to see if a message is directed at the bot ---
async def is_message_for_laila(user_message: str) -> bool:
    prompt = f"Given the user message: '{user_message}', is it a question or command directed at an AI assistant? Answer only 'Yes' or 'No'."
    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,  # Low temperature for a direct answer
                max_output_tokens=10
            )
        )
        return "yes" in response.text.lower()
    except Exception as e:
        logger.error(f"Error checking if message is for Laila: {e}")
        return False

# --- HANDLERS ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global total_messages_processed, global_bot_status
    user_message = update.effective_message.text
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    known_users.add(str(chat_id))
    save_known_users()
    user_message_lower = user_message.lower()
    total_messages_processed += 1

    if not global_bot_status:
        logger.info(f"[{chat_id}] Bot is globally off. Ignoring message from {user_name}.")
        return
    
    if not bot_status[chat_id]:
        logger.info(f"[{chat_id}] Bot is disabled for this group. Ignoring message from {user_name}.")
        return

    chat_type = update.effective_chat.type
    should_respond_with_ai = False

    # --- New logic for creator defense, praise, and direct name drop ---
    creator_name_keywords = ["creator kon", "who created", "creator name", "tumhe kisne banaya"]
    creator_abuse_keywords = ["adhyan is bad", "adhyan bekar hai", "adhyan ghatiya hai", "adhyan useless", "laila ka owner is bad", "adhyan ne kya banaya"]
    
    # Keywords to turn off bot
    turn_off_keywords = ["chup", "chupp karo", "chupp", "shut up"]

    if any(keyword in user_message_lower for keyword in turn_off_keywords):
        bot_status[chat_id] = False
        await update.message.reply_text("Theek hai, main chup ho jaati hoon. 👋")
        logger.info(f"[{chat_id}] Laila was turned off by a keyword.")
        return

    # Check for direct name question first
    if any(re.search(r'\b' + keyword + r'\b', user_message_lower) for keyword in creator_name_keywords):
        await update.message.reply_text("My Creator is @AdhyanXlive.")
        return

    # Check for abuse, not praise
    if any(re.search(r'\b' + keyword + r'\b', user_message_lower) for keyword in creator_abuse_keywords):
        await update.message.reply_text("Aap Adhyan ke baare mein aise kyu bol rahe hain? Mujhe accha nahi laga. 😔")
        return
    
    # --- Date of Birth logic ---
    dob_keywords = ["date of birth", "janam kab hua", "birthday", "birth date", "kab paida hui"]
    if any(re.search(r'\b' + keyword + r'\b', user_message_lower) for keyword in dob_keywords):
        await update.message.reply_text("My date of birth is 1st August 2025.")
        return

    # --- User praise logic ---
    praise_user_keywords = [
        f"{user_name} kaisa insaan hai",
        f"{user_name} kaisa ladka hai",
        f"tell me about {user_name}",
        f"who is {user_name}"
    ]
    if any(keyword.lower() in user_message_lower for keyword in praise_user_keywords):
        await update.message.reply_text(f"{user_name} ek bahut hi acche, smart aur nek insaan hain!")
        return
    
    # --- Existing stats and humor checks (modified for less frequency) ---
    stats_keywords = ["your stats", "laila stats", "show stats", "bot stats", "stats"]
    if any(re.search(r'\b' + keyword + r'\b', user_message_lower) for keyword in stats_keywords):
        await stats_command(update, context)
        return

    if chat_type != 'private':
        bot_username = context.bot.name.lower()
        is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot
        is_mentioned_or_named = (
            re.search(r'\b(laila|' + re.escape(bot_username) + r')\b', user_message_lower) or
            user_message_lower.startswith(('laila', '@' + bot_username))
        )
        
        if is_reply_to_bot or is_mentioned_or_named:
            should_respond_with_ai = True
        else:
            # Use AI to understand intent only if not directly addressed
            if await is_message_for_laila(user_message):
                should_respond_with_ai = True

    elif chat_type == 'private':
        should_respond_with_ai = True

    if should_respond_with_ai:
        HUMOR_KEYWORDS = ["lol", "haha", "😂", "🤣🤣", "🤣🤣🤣"]
        FUNNY_RESPONSES = ["hehehe, that's a good one!", "🤣 I'm just a bot, but I get it!", "Too funny! 😂", "hahaha, you guys are hilarious!", "Bwahahaha! 😅"]
        
        # Respond to humor keywords with low probability
        if any(re.search(r'\b' + keyword + r'\b', user_message_lower) for keyword in HUMOR_KEYWORDS) and random.random() < 0.1:
            await update.message.reply_text(random.choice(FUNNY_RESPONSES))
            return
        
        add_to_history(chat_id, "user", user_message)
        response_text = await get_bot_response(user_message, chat_id, context.bot.name, should_respond_with_ai, update)
        
        if response_text:
            add_to_history(chat_id, "model", response_text)
            await update.message.reply_text(response_text)

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(f"An error occurred: {context.error}")
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables.")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("adminstats", admin_stats_command))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("kick", kick_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("on", on_command))
    application.add_handler(CommandHandler("off", off_command))
    application.add_handler(CommandHandler("poweron", poweron_command))
    application.add_handler(CommandHandler("poweroff", poweroff_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Error handler ko yahan add kiya gaya hai
    application.add_error_handler(error_handler)

    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
    )
