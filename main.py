import os
import logging
from collections import defaultdict
import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import time
import json
import re
import gspread
import psutil
from datetime import datetime
import asyncio
from gspread.exceptions import APIError, WorksheetNotFound
import uuid

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
known_users = set()

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
4.  **Lyrics:** If a user asks for song lyrics, politely explain that you cannot guarantee the accuracy of song lyrics and suggest they use a reliable source like Google, Genius, or Spotify.
5.  **General Chat:** For normal conversations, keep your replies short, around 1-2 sentences. The goal is to keep the chat flowing and engaging.
6.  **Specific Questions:** If a user asks a factual, technical, or detailed question, provide a comprehensive, accurate, and insightful answer. In these cases, you can provide a longer response, but only if necessary.
7.  **Language:** Always detect the user's language (Hindi, English, Hinglish) and respond in the same language.

Important: Your goal is to be a fun, smart, and loyal friend to the users, representing your creator's vision.
"""

# --- Chat History Management (in-memory) ---
chat_histories = defaultdict(list)
MAX_HISTORY_LENGTH = 20

def add_to_history(chat_id, role, text):
    chat_histories[chat_id].append({'role': role, 'parts': [text]})
    if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
        chat_histories[chat_id].pop(0)

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

# --- NEW: Function to get/create the 'chats' worksheet ---
def get_chats_worksheet(client):
    try:
        return client.open_by_url("https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit").worksheet("chats")
    except WorksheetNotFound:
        spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit")
        return spreadsheet.add_worksheet("chats", rows="1000", cols="2")

# --- NEW: Function to save a chat ID to Google Sheets ---
def save_chat_id(chat_id):
    try:
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_SHEETS_CREDENTIALS not found.")
            return

        creds_dict = json.loads(creds_json)
        client = gspread.service_account_from_dict(creds_dict)
        
        chat_sheet = get_chats_worksheet(client)
        
        existing_ids = chat_sheet.col_values(1)
        if str(chat_id) in existing_ids:
            return

        chat_sheet.append_row([str(chat_id), datetime.now().isoformat()])
        logger.info(f"Saved new chat ID: {chat_id}")

    except Exception as e:
        logger.error(f"Error saving chat ID to Google Sheet: {e}")

# --- NEW: Function to load all known users/chats from Google Sheets ---
def load_known_users():
    global known_users
    try:
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_SHEETS_CREDENTIALS not found.")
            return

        creds_dict = json.loads(creds_json)
        client = gspread.service_account_from_dict(creds_dict)
        chat_sheet = get_chats_worksheet(client)
        
        chat_ids = chat_sheet.col_values(1)
        known_users = set(chat_ids)
        logger.info(f"Loaded {len(known_users)} chats from Google Sheets.")

    except Exception as e:
        logger.error(f"Error loading known users from Google Sheet: {e}")

# --- AI Response Function with Fallback to Google Sheets ---
async def get_bot_response(user_message: str, chat_id: int, bot_username: str, should_use_ai: bool, update: Update) -> str:
    global current_api_key_index, active_api_key, model
    
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
                
                is_detailed_query = len(user_message.split()) > 5 or '?' in user_message or 'how to' in user_message_lower

                response = chat_session.send_message(
                    user_message,
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
                if "429 Quota exceeded" in error_str or "You exceeded your current quota" in error_str or "500" in error_str:
                    logger.warning(f"[{chat_id}] API key {active_api_key[-5:]} failed with error: {e}. Shifting to next key.")
                    key_cooldown_until[active_api_key] = time.time() + (1 * 60 * 60)
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

# --- Error-Handled Admin Commands ---
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if not await is_admin(context.bot, chat_id, user_id):
        await update.message.reply_text("Sorry, you need to be an admin to use this command.")
        return
    
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await update.message.reply_text("Please reply to a user's message to ban them.")
        return

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
    
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await update.message.reply_text("Please reply to a user's message to kick them.")
        return

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
    
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await update.message.reply_text("Please reply to a user's message to mute them.")
        return

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
    message_to_send = message_to_send.replace('\n', '<br>')

    success_count = 0
    failure_count = 0
    
    global known_users
    if not known_users:
        load_known_users()
    
    for chat_id in known_users:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_to_send,
                parse_mode='HTML'
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error broadcasting to chat {chat_id}: {e}")
            failure_count += 1

    await update.message.reply_text(f"Broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

# --- Command to get a photo's file ID ---
async def get_photo_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return
    
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo_file_id = update.message.reply_to_message.photo[-1].file_id
        await update.message.reply_text(f"Photo File ID:\n`{photo_file_id}`", parse_mode='Markdown')
    else:
        await update.message.reply_text("Please reply to a photo with this command to get its ID.")

# --- Broadcast with photo command ---
async def broadcast_photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /broadcast_photo <photo_file_id> <message>")
        return

    photo_file_id = context.args[0]
    message_to_send = " ".join(context.args[1:])
    message_to_send = message_to_send.replace('\n', '<br>')

    success_count = 0
    failure_count = 0

    global known_users
    if not known_users:
        load_known_users()

    for chat_id in known_users:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_file_id,
                caption=message_to_send,
                parse_mode='HTML'
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error broadcasting photo to chat {chat_id}: {e}")
            failure_count += 1

    await update.message.reply_text(f"Photo broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Photo broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

# --- Forward a message to all known chats ---
async def forward_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message to forward it to all chats.")
        return

    success_count = 0
    failure_count = 0

    global known_users
    if not known_users:
        load_known_users()

    for chat_id in known_users:
        try:
            await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.reply_to_message.message_id
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error forwarding message to chat {chat_id}: {e}")
            failure_count += 1

    await update.message.reply_text(f"Message forwarded! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Message forwarded by admin. Success: {success_count}, Failure: {failure_count}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] Received /start from {user_name}")
    
    if str(chat_id) not in known_users:
        known_users.add(str(chat_id))
        save_chat_id(chat_id)

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

# --- NEW: Show all known chats with links for groups ---
async def show_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return

    global known_users
    if not known_users:
        load_known_users()
    
    known_chats = list(known_users)
    if not known_chats:
        await update.message.reply_text("The bot is not in any groups or private chats yet.")
        return

    chat_details = []
    for chat_id in known_chats:
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type == 'private':
                chat_details.append(f"• **User**: `{chat.full_name}` (ID: `{chat_id}`)")
            elif chat.username:
                chat_details.append(f"• **Group**: `{chat.title}` (ID: `{chat_id}`)\n  Link: [t.me/{chat.username}](https://t.me/{chat.username})")
            else:
                chat_details.append(f"• **Group**: `{chat.title}` (ID: `{chat_id}`)\n  *No username found.*")
        except Exception:
            chat_details.append(f"• **Unknown Chat**: ID: `{chat_id}` (Bot may have been removed)")
        
        await asyncio.sleep(0.1)

    response_text = "✨ **Laila's Chats** ✨\n\n" + "\n\n".join(chat_details)
    await update.message.reply_text(response_text, parse_mode='Markdown')
    logger.info(f"[{update.effective_chat.id}] /show_chats command used by admin.")

# --- AI check to see if a message is directed at the bot ---
async def is_message_for_laila(user_message: str) -> bool:
    prompt = f"Given the user message: '{user_message}', is it a question or command directed at an AI assistant? Answer only 'Yes' or 'No'."
    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=10
            )
        )
        return "yes" in response.text.lower()
    except Exception as e:
        logger.error(f"Error checking if message is for Laila: {e}")
        return False

# --- UPDATED PAID BROADCAST FUNCTIONALITY (with owner-only receipt) ---
async def paid_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry, this command is for the bot owner only.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a message/media to broadcast it to all chats.")
        return
    
    original_message = update.message.reply_to_message
    broadcast_id = str(uuid.uuid4())[:8]
    broadcast_start_time = datetime.now()
    
    successful_users = 0
    successful_groups = 0
    total_group_members = 0
    failed_chats = []

    global known_users
    if not known_users:
        load_known_users()

    # Log initial broadcast attempt
    logger.info(f"Starting paid broadcast with ID {broadcast_id}...")

    # Broadcast message to all known users
    for chat_id_str in list(known_users):
        try:
            chat_id = int(chat_id_str)
            chat = await context.bot.get_chat(chat_id)
            
            # Using copy_message to preserve the original format and not show "forwarded from"
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=update.effective_chat.id,
                message_id=original_message.message_id
            )
            
            if chat.type == 'private':
                successful_users += 1
            else:
                successful_groups += 1
                try:
                    chat_members_count = await context.bot.get_chat_member_count(chat_id)
                    total_group_members += chat_members_count
                except Exception:
                    # Ignore errors for member count, it might not be available
                    pass
            
            await asyncio.sleep(0.1) # Add a small delay to avoid hitting rate limits
        except Exception as e:
            failed_chats.append(chat_id_str)
            logger.error(f"Error broadcasting to chat {chat_id_str}: {e}")

    broadcast_end_time = datetime.now()
    duration = broadcast_end_time - broadcast_start_time
    
    # Create the receipt message for the owner
    receipt_text = (
        f"✅ **Paid Broadcast Receipt** ✅\n\n"
        f"**Broadcast ID**: `{broadcast_id}`\n"
        f"**Broadcast Time**: `{broadcast_end_time.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"**Duration**: `{duration.total_seconds():.2f} seconds`\n\n"
        f"**Broadcast Summary**\n"
        f" • **Total Chats Reached**: `{successful_users + successful_groups}`\n"
        f" • **Users Reached**: `{successful_users}`\n"
        f" • **Groups Reached**: `{successful_groups}`\n"
        f" • **Total Group Members (approx)**: `{total_group_members}`\n"
        f" • **Failed Chats**: `{len(failed_chats)}`\n"
    )

    if failed_chats:
        receipt_text += "\n**Failed Chat IDs:**\n"
        receipt_text += "\n".join([f"`{chat_id}`" for chat_id in failed_chats])
    
    # Send the receipt ONLY to the owner in the chat where the command was issued
    await update.message.reply_text(receipt_text, parse_mode='Markdown')
    logger.info(f"Paid broadcast with ID {broadcast_id} completed. Owner received receipt.")


# --- HANDLERS ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global total_messages_processed, global_bot_status
    user_message = update.effective_message.text
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    
    if not user_message:
        return
    
    if str(chat_id) not in known_users:
        known_users.add(str(chat_id))
        save_chat_id(chat_id)
        logger.info(f"[{chat_id}] New chat added to known_users from message handler.")

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

    creator_name_keywords = ["creator kon", "who created", "creator name", "tumhe kisne banaya"]
    creator_abuse_keywords = ["adhyan is bad", "adhyan bekar hai", "adhyan ghatiya hai", "adhyan useless", "laila ka owner is bad", "adhyan ne kya banaya"]
    
    turn_off_keywords = ["chup", "chupp karo", "chupp", "shut up"]

    if any(keyword in user_message_lower for keyword in turn_off_keywords):
        bot_status[chat_id] = False
        await update.message.reply_text("Theek hai, mai abhi chup ho jati hu. Jab bolna ho to /on command dena.")
        return

    # Direct messages
    if chat_type == 'private':
        should_respond_with_ai = True
    
    # Replies to the bot
    if update.effective_message.reply_to_message and update.effective_message.reply_to_message.from_user.is_bot:
        if update.effective_message.reply_to_message.from_user.id == context.bot.id:
            should_respond_with_ai = True
            
    # Mentions of the bot
    bot_username = (await context.bot.get_me()).username
    if f'@{bot_username.lower()}' in user_message_lower:
        should_respond_with_ai = True

    # Check for keywords and reply with AI check
    if not should_respond_with_ai:
        if any(keyword in user_message_lower for keyword in creator_name_keywords):
            should_respond_with_ai = True
        elif any(keyword in user_message_lower for keyword in creator_abuse_keywords):
            should_respond_with_ai = True
        elif len(user_message_lower) < 100 and await is_message_for_laila(user_message):
            should_respond_with_ai = True
        else:
            return # Don't respond if no conditions are met
            
    # Get response and handle errors
    bot_response = await get_bot_response(user_message, chat_id, bot_username, should_respond_with_ai, update)
    if bot_response:
        await update.message.reply_text(bot_response)

    add_to_history(chat_id, "user", user_message)
    add_to_history(chat_id, "model", bot_response if bot_response else "No response")
    logger.info(f"[{chat_id}] {user_name} said: '{user_message}' -> Bot replied: '{bot_response}'")


async def post_init(application: Application) -> None:
    """Post-initialization tasks."""
    logger.info("Starting post-init tasks...")
    await load_known_users()
    logger.info("Post-init tasks complete.")

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    # --- Commands ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("adminstats", admin_stats_command))
    application.add_handler(CommandHandler("show_chats", show_chats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("broadcast_photo", broadcast_photo_command))
    application.add_handler(CommandHandler("forward_all", forward_all_command))
    application.add_handler(CommandHandler("on", on_command))
    application.add_handler(CommandHandler("off", off_command))
    application.add_handler(CommandHandler("poweron", poweron_command))
    application.add_handler(CommandHandler("poweroff", poweroff_command))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("kick", kick_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("get_photo_id", get_photo_id))
    
    # --- UPDATED PAID BROADCAST COMMAND HANDLER ---
    application.add_handler(CommandHandler("paid_broadcast", paid_broadcast_command))

    # --- Message Handler ---
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # --- Webhook or Polling ---
    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "8443")),
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
        )
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
