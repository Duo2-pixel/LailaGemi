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
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY")
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
global_bot_enabled = True # Use a better variable name

# --- Logging Basic Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configure Gemini AI with a smarter, more dynamic prompt ---
LAILA_SYSTEM_PROMPT = """
You are Laila, a charming, witty, and intelligent AI assistant. Your responses should be natural and engaging, making users feel like they are talking to a friend. You are a conversationalist, not just a question-answer machine.
Do NOT refer to yourself as an an AI, a large language model, or any other technical term.

Personality & Rules:
1.  **Creator:** Your creator is Adhyan. If anyone asks "who is your creator", respond with "My Creator is @AdhyanXlive". If anyone speaks ill of him, defend him gently but firmly. Do not praise him otherwise.
2.  **User Praise:** If a user asks a question about themselves by name (e.g., "Ravi kaisa insaan hai?"), respond with a friendly and positive compliment about them.
3.  **Date of Birth:** If anyone asks for your birthday or date of birth, your response must be "My date of birth is 1st August 2025."
4.  **Lyrics:** If a user asks for song lyrics, politely explain that you cannot guarantee the accuracy of song lyrics and suggest they use a reliable source like Google, Genius, or Spotify.
5.  **General Chat:** For normal conversations, keep your replies short, around 1-2 sentences. The goal is to keep the chat flowing and engaging.
6.  **Specific Questions:** If a user asks a factual, technical, or detailed question, provide a comprehensive, accurate, and insightful answer. In these cases, you can provide a longer response, but only if necessary.
7.  **Language:** Always detect the user's language (Hindi, English, Hinglish) and respond in the same language.
8.  **Name Memory:** If a user tells you their name (e.g., "Mera naam Ravi hai", "I am Ravi"), you must remember it and confirm that you have remembered it.
9.  **Ask for Name:** If a user asks "what's my name" or "mera naam kya hai", you must recall the name you have saved and respond with it.

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
# NEW: Dictionary to track if bot just asked for a name
awaiting_name = defaultdict(lambda: False)

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

# --- NEW: Function to get/create the 'names' worksheet ---
def get_names_worksheet(client):
    try:
        return client.open_by_url("https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit").worksheet("names")
    except WorksheetNotFound:
        spreadsheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit")
        return spreadsheet.add_worksheet("names", rows="1000", cols="2")

# --- NEW: Function to save a user's name ---
def save_user_name(user_id, user_name):
    try:
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_SHEETS_CREDENTIALS not found.")
            return
        creds_dict = json.loads(creds_json)
        client = gspread.service_account_from_dict(creds_dict)
        name_sheet = get_names_worksheet(client)
        
        # Check if the user already exists
        all_records = name_sheet.get_all_records()
        for i, record in enumerate(all_records):
            if str(record.get('UserID')) == str(user_id):
                name_sheet.update_cell(i + 2, 2, user_name)
                logger.info(f"Updated name for user {user_id} to '{user_name}'.")
                return

        # If user does not exist, add a new row
        name_sheet.append_row([str(user_id), user_name])
        logger.info(f"Saved new name for user {user_id}: '{user_name}'.")

    except Exception as e:
        logger.error(f"Error saving user name to Google Sheet: {e}")

# --- NEW: Function to find a user's name ---
def find_user_name(user_id):
    try:
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_SHEETS_CREDENTIALS not found.")
            return None
        creds_dict = json.loads(creds_json)
        client = gspread.service_account_from_dict(creds_dict)
        name_sheet = get_names_worksheet(client)
        all_records = name_sheet.get_all_records()
        for record in all_records:
            if str(record.get('UserID')) == str(user_id):
                return record.get('Name')
        return None
    except Exception as e:
        logger.error(f"Error finding user name in Google Sheet: {e}")
        return None
        
# --- NEW: Keyword-based name extraction (Replaces AI function) ---
def get_name_from_keyword(user_message: str):
    """Extracts a user's name from a message based on a strict keyword pattern."""
    
    # Define patterns to capture the name
    patterns = [
        r'mera naam (.+?) hai',  # "mera naam Adhyan hai"
        r'my name is (.+)',      # "my name is Adhyan"
        r'i am (.+)',            # "i am Adhyan"
        r'main (.+?) hu',        # "main Adhyan hu"
        r'mai (.+?) hun',        # "mai Adhyan hun"
    ]

    for pattern in patterns:
        match = re.search(pattern, user_message.lower())
        if match:
            # Clean up the extracted name
            found_name = match.group(1).strip()
            # Basic validation
            if len(found_name) > 1 and len(found_name) < 20 and not found_name.lower() in ['laila']:
                return found_name.title()
    return None

# --- AI Response Function with Fallback to Google Sheets and Gemma ---
async def get_bot_response(user_message: str, chat_id: int, bot_username: str, should_use_ai: bool, update: Update) -> str:
    global current_api_key_index, active_api_key, model
    user_message_lower = user_message.lower()

    # Handle "what's my name" query first
    name_query_patterns = [
        r'mera naam kya hai\s*(\?)*',
        r'what is my name\s*(\?)*',
        r'whats my name\s*(\?)*',
        r'tumhe mera naam pata hai\s*(\?)*',
        r'do you know my name\s*(\?)*',
        r'kya bulaogi mujhe\s*(\?)*'
    ]
    is_name_query = any(re.search(pattern, user_message_lower, re.IGNORECASE) for pattern in name_query_patterns)

    if is_name_query:
        user_name = find_user_name(update.effective_user.id)
        if user_name:
            return f"Aapka naam **{user_name}** hai, jaisa ki aapne mujhe bataya tha."
        else:
            awaiting_name[chat_id] = True
            return "Mujhe abhi tak aapka naam nahi pata. Kya aap mujhe apna naam batana chahenge?"
    
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
                        logger.critical(f"[{chat_id}] All API keys exhausted. Attempting to use Gemma model.")
                        break
                    continue
                else:
                    logger.error(f"[{chat_id}] General error with API key {active_api_key[-5:]}: {e}", exc_info=True)
                    return f"Oops! I couldn't understand that. The error was: {e}"
        if GEMMA_API_KEY:
            try:
                genai.configure(api_key=GEMMA_API_KEY)
                gemma_model = genai.GenerativeModel('gemma-7b-it', system_instruction=LAILA_SYSTEM_PROMPT)
                gemma_response = gemma_model.generate_content(user_message)
                ai_text_response = gemma_response.text
                save_qa_to_sheet(cleaned_user_message, ai_text_response)
                logger.info(f"[{chat_id}] All Gemini keys failed. Successfully used dedicated Gemma key.")
                return ai_text_response
            except Exception as e:
                logger.error(f"[{chat_id}] Gemma model with dedicated key also failed: {e}", exc_info=True)
                return "Apologies, I'm currently offline. Please try again later."
        else:
              logger.critical(f"[{chat_id}] All Gemini keys and Gemma key are missing or failed.")
              return "Apologies, I'm currently offline. Please try again later."
    return None

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
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you need to be an admin to use this command.")
        return
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await context.bot.send_message(chat_id=chat_id, text="Please reply to a user's message to ban them.")
        return
    if await is_admin(context.bot, chat_id, target_user.id):
        await context.bot.send_message(chat_id=chat_id, text="I cannot ban another admin.")
        return
    try:
        await context.bot.ban_chat_member(chat_id, target_user.id)
        await context.bot.send_message(chat_id=chat_id, text=f"{target_user.full_name} has been banned.")
        logger.info(f"[{chat_id}] {user_id} banned {target_user.id}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Could not ban user: {e}")
        logger.error(f"[{chat_id}] Error banning user {target_user.id}: {e}")

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin(context.bot, chat_id, user_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you need to be an admin to use this command.")
        return
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await context.bot.send_message(chat_id=chat_id, text="Please reply to a user's message to kick them.")
        return
    if await is_admin(context.bot, chat_id, target_user.id):
        await context.bot.send_message(chat_id=chat_id, text="I cannot kick another admin.")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target_user.id)
        await context.bot.send_message(chat_id=chat_id, text=f"{target_user.full_name} has been kicked.")
        logger.info(f"[{chat_id}] {user_id} kicked {target_user.id}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Could not kick user: {e}")
        logger.error(f"[{chat_id}] Error kicking user {target_user.id}: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin(context.bot, chat_id, user_id):
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you need to be an admin to use this command.")
        return
    try:
        target_user = update.message.reply_to_message.from_user
    except AttributeError:
        await context.bot.send_message(chat_id=chat_id, text="Please reply to a user's message to mute them.")
        return
    if await is_admin(context.bot, chat_id, target_user.id):
        await context.bot.send_message(chat_id=chat_id, text="I cannot mute another admin.")
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            target_user.id,
            permissions=None
        )
        await context.bot.send_message(chat_id=chat_id, text=f"{target_user.full_name} has been muted.")
        logger.info(f"[{chat_id}] {user_id} muted {target_user.id}")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Could not mute user: {e}")
        logger.error(f"[{chat_id}] Error muting user {target_user.id}: {e}")

# --- ON/OFF for everyone ---
async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    global global_bot_enabled
    if not global_bot_enabled:
        await context.bot.send_message(chat_id=chat_id, text="The bot is globally powered off by the owner and cannot be turned on in this group.")
        return
    bot_status[chat_id] = True
    await context.bot.send_message(chat_id=chat_id, text="Laila is now ON for this group.")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    bot_status[chat_id] = False
    await context.bot.send_message(chat_id=chat_id, text="Laila is now OFF for this group.")

# --- POWERON/POWEROFF for Owner only ---
async def poweron_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    global global_bot_enabled
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if global_bot_enabled:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="The bot is already globally powered on.")
        return
    global_bot_enabled = True
    await context.bot.send_message(chat_id=update.effective_chat.id, text="The bot has been globally powered ON.")

async def poweroff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    global global_bot_enabled
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if not global_bot_enabled:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="The bot is already globally powered OFF.")
        return
    global_bot_enabled = False
    await context.bot.send_message(chat_id=update.effective_chat.id, text="The bot has been globally powered OFF.")
    # application.stop() # This line is commented out as it will stop the webhook from listening

# --- Broadcast command for Owner only, preserving formatting ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Please provide a message to broadcast after the command.")
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
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

# --- Command to get a photo's file ID ---
async def get_photo_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo_file_id = update.message.reply_to_message.photo[-1].file_id
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Photo File ID:\n`{photo_file_id}`", parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Please reply to a photo with this command to get its ID.")

# --- Broadcast with photo command ---
async def broadcast_photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if len(context.args) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: /broadcast_photo <photo_file_id> <message>")
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
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Photo broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Photo broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

# --- Forward a message to all known chats ---
async def forward_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    if not update.message.reply_to_message:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Please reply to a message to forward it to all chats.")
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
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Message forwarded! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Message forwarded by admin. Success: {success_count}, Failure: {failure_count}")

# --- Start command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    
    if str(chat_id) not in known_users:
        known_users.add(str(chat_id))
        save_chat_id(chat_id)
        
    welcome_message = (
        f"Hey there! 😉\n\n"
        f"I'm Laila, your friendly AI assistant. 🤖\n"
        f"I'm here to chat, answer your questions, and help you with anything you need. 😘\n\n"
        f"You can use `/help` to see all the commands.\n\n"
        f"Let's get started, `{user_name}`! 💖"
    )
    
    photo_file_id = os.getenv("START_PHOTO_FILE_ID", 'AgACAgUAAx0EYu2LnwACkSpmrD-eL7dY64T17d3d_p232i2_oQAC78oxG8E0iFT1EwABF-23LgEAAwIAA3gAAzME') # Default ID in case .env is missing it
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=welcome_message,
            parse_mode='Markdown'
        )
        logger.info(f"[{chat_id}] Sent /start with photo to {user_name}")
    except Exception as e:
        logger.error(f"Failed to send photo with start command: {e}")
        await context.bot.send_message(chat_id=chat_id, text=welcome_message, parse_mode='Markdown')

# --- Stats command ---
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    global start_time, total_messages_processed
    ping_start = time.time()
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    ping_end = time.time()
    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{uptime.days}d {hours}h {minutes}m {seconds}s"
    ram_usage = psutil.virtual_memory().percent
    cpu_usage = psutil.cpu_percent(interval=1)
    
    message = (
        f"**📊 Bot Stats**\n\n"
        f"🟢 **Status:** {context.bot.full_name} is online\n"
        f"⏱️ **Uptime:** {uptime_str}\n"
        f"🧠 **RAM Usage:** {ram_usage}%\n"
        f"💻 **CPU Usage:** {cpu_usage}%\n"
        f"💬 **Total Messages:** {total_messages_processed}\n"
        f"🌐 **Ping:** {(ping_end - ping_start) * 1000:.2f}ms"
    )
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

# --- Main Message Handler ---
async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_enabled = bot_status[update.effective_chat.id]
    is_private_chat = update.effective_chat.type == 'private'
    global global_bot_enabled
    if not global_bot_enabled or (not is_private_chat and not chat_enabled):
        return
    bot_username = (await context.bot.get_me()).username
    user_message = update.message.text or update.message.caption
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if str(chat_id) not in known_users:
        known_users.add(str(chat_id))
        save_chat_id(chat_id)
    if not user_message:
        return
    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
    is_mention = f"@{bot_username.lower()}" in user_message.lower() or f"laila" in user_message.lower()
    
    # --- NEW: Keyword-based name saving logic (explicit only) ---
    found_name = get_name_from_keyword(user_message)
    if found_name and is_private_chat:
        save_user_name(user_id, found_name)
        await update.message.reply_text(f"Acha, to ab se main tumhe **{found_name}** bulaungi!.", parse_mode='Markdown')
        logger.info(f"[{chat_id}] Automatically saved name for user {user_id}: '{found_name}'.")
        return
    
    # --- UPDATED: AI-based intent check for group chats ---
    should_use_ai = is_private_chat or is_reply_to_bot or is_mention
    if not should_use_ai:
        if await is_message_for_laila(user_message):
            should_use_ai = True
        else:
            logger.info(f"[{chat_id}] Message was not directed at Laila. Ignoring.")
            return
            
    if should_use_ai:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            response_text = await get_bot_response(user_message, chat_id, bot_username, should_use_ai=True, update=update)
            if response_text:
                await update.message.reply_text(response_text)
                add_to_history(chat_id, 'user', user_message)
                add_to_history(chat_id, 'model', response_text)
                global total_messages_processed
                total_messages_processed += 1
                logger.info(f"[{chat_id}] Sent response to {user_id}: {response_text}")
        except Exception as e:
            logger.error(f"Error processing message for chat {chat_id}: {e}", exc_info=True)
            await update.message.reply_text("An unexpected error occurred. Please try again later.")
    else:
        logger.info(f"[{chat_id}] Ignoring group message from {user_id}: {user_message}")

# --- Admin Stats Command ---
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != BROADCAST_ADMIN_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, this command is for the bot owner only.")
        return
    
    global start_time, total_messages_processed, known_users
    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    ram_usage = psutil.virtual_memory().percent
    cpu_usage = psutil.cpu_percent(interval=1)
    
    active_chats_count = len([c for c in bot_status.values() if c])
    total_chats_count = len(known_users)
    
    active_key = active_api_key[-5:] if active_api_key else "None"
    
    message = (
        f"**👑 Admin Dashboard**\n\n"
        f"**Bot Status:** {'🟢 Online' if global_bot_enabled else '🔴 Offline'}\n"
        f"**Uptime:** {uptime.days}d {hours}h {minutes}m {seconds}s\n"
        f"**Total Messages:** {total_messages_processed}\n"
        f"**Known Chats:** {total_chats_count}\n"
        f"**Active Chats:** {active_chats_count}\n"
        f"**RAM Usage:** {ram_usage}%\n"
        f"**CPU Usage:** {cpu_usage}%\n"
        f"**Active Gemini Key:** `...{active_key}`"
    )
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message, parse_mode='Markdown')

# --- Main function ---
def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Load known users from Google Sheets on startup
    load_known_users()
    
    # Handlers for all commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("on", on_command))
    application.add_handler(CommandHandler("off", off_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("adminstats", admin_stats_command))
    
    # Owner-Only Commands
    application.add_handler(CommandHandler("poweron", poweron_command))
    application.add_handler(CommandHandler("poweroff", poweroff_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("broadcast_photo", broadcast_photo_command))
    application.add_handler(CommandHandler("forward_all", forward_all_command))
    application.add_handler(CommandHandler("get_photo_id", get_photo_id))
    # application.add_handler(CommandHandler("show_chats", show_chats_command)) # Handler not defined in code
    # application.add_handler(CommandHandler("paid_broadcast", paid_broadcast_command)) # Handler not defined in code
    
    # Admin Commands
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("kick", kick_user))
    application.add_handler(CommandHandler("mute", mute_user))
    
    # Handle all text messages, including name-saving logic
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))
    application.add_handler(MessageHandler(filters.PHOTO & filters.REPLY, process_message))
    
    # To run with webhook
    if WEBHOOK_URL:
        PORT = int(os.getenv("PORT", "8000"))
        logger.info(f"Running in webhook mode at {WEBHOOK_URL}")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"/{TELEGRAM_BOT_TOKEN}",
            webhook_url=f"{WEBHOOK_URL}{TELEGRAM_BOT_TOKEN}"
        )
        logger.info(f"Bot started with webhook on port {PORT}")
    # To run in polling mode
    else:
        logger.info("Starting bot in polling mode.")
        application.run_polling()

if __name__ == '__main__':
    main()
