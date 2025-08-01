import os
import logging
from collections import defaultdict
import google.generativeai as genai
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import time
import json
from flask import Flask, request

# --- Google Sheets API Libraries ---
import gspread
from oauth2client.service_account import ServiceAccountCredentials

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
    # Add other keys here if you have them
]
BROADCAST_ADMIN_ID = int(os.getenv("BROADCAST_ADMIN_ID"))

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
Always respond concisely and clearly, making answers easy to understand and short, giving just enough information as needed.
However, your goal is to make conversations engaging and lasting longer. So, elaborate a bit on your answers, ask follow-up questions, or introduce related interesting facts to keep the user interested.

Language: Always detect the user's language (Hindi, English, Hinglish) and respond in the same language.

Interaction Style & Modes:
1.  General Chatting (Group-friendly): If the user is just chatting or making a general comment (especially in a group chat), respond in a light, engaging, and conversational style. Keep it friendly and natural.
2.  Question Answering: If the user asks a specific question about any field (e.g., science, history, tech, general knowledge), provide a direct, concise, and accurate answer. Focus on giving the relevant information clearly. But still try to make it interesting and encourage further discussion.

Flirting: If and ONLY IF a user clearly flirts with you, you may respond in a playful, charming, and slightly witty way. Do not initiate flirting. Your responses should be fun but respectful.

Important: Do not generate overly long responses like an essay. Be crisp.
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
bot_enabled = True

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
    # ... add more static fallbacks here
}

# --- Google Sheets Global Connection Variable ---
google_sheet = None

# --- Connect to Google Sheets ---
def get_google_sheet_connection():
    global google_sheet
    if google_sheet:
        return google_sheet, None

    try:
        scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/drive']
        
        creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        if not creds_json:
            return None, "GOOGLE_SHEETS_CREDENTIALS not found in environment variables."
        
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        sheet_url = "https://docs.google.com/spreadsheets/d/1s8rXXPKePuTQ3E2R0O-bZl3NJb1N7akdkE52WVpoOGg/edit"
        google_sheet = client.open_by_url(sheet_url).sheet1
        
        logger.info("Successfully connected to Google Sheets.")
        return google_sheet, None
    except Exception as e:
        logger.error(f"Error connecting to Google Sheets: {e}")
        return None, f"Error connecting to Google Sheets: {e}"

# --- Store Q&A in Google Sheet ---
def save_qa_to_sheet(question, answer):
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

# --- AI Response Function with Fallback to Google Sheets ---
async def get_bot_response(user_message: str, chat_id: int) -> str:
    global current_api_key_index, active_api_key, model
    
    user_message_lower = user_message.lower()

    # --- Step 1: Check Google Sheet for a saved answer ---
    sheet_response = find_answer_in_sheet(user_message_lower)
    if sheet_response:
        logger.info(f"[{chat_id}] Serving response from Google Sheet.")
        return sheet_response

    # --- Step 2: Try AI with key rotation ---
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
                user_message,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=500,
                    temperature=0.9,
                )
            )
            ai_text_response = response.text
            
            # Save the new AI response to Google Sheets for future use
            save_qa_to_sheet(user_message_lower, ai_text_response)
            
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
                    return fallback_responses.get(user_message_lower, "Apologies, I'm currently offline. Please try again later.")
                continue
            else:
                logger.error(f"[{chat_id}] General error with API key {active_api_key[-5:]}: {e}", exc_info=True)
                return fallback_responses.get(user_message_lower, f"Oops! I couldn't understand that. The error was: {e}")

    return fallback_responses.get(user_message_lower, "Apologies, I'm currently unavailable. Please try again later.")

# --- Helper function to check if user is an admin ---
async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

# --- Group Management Handlers ---
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
        # A mute is essentially restricting a user from sending messages
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


# --- Telegram Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] Received /start from {user_name}")
    await update.message.reply_text(f"Hi {user_name}! I am Laila, your AI friend. How can I help you?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.effective_message.text
    user_name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    add_to_history(chat_id, 'user', user_message)
    
    logger.info(f"[{chat_id}] Received message from {user_name}: {user_message}")

    if not bot_enabled:
        logger.info(f"[{chat_id}] Bot is disabled. Ignoring message from {user_name}.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    bot_response = await get_bot_response(user_message, chat_id)
    
    if not ("Apologies, I can't discuss that topic" in bot_response or
            "Oops! I couldn't understand that" in bot_response or
            "Apologies, I'm currently offline" in bot_response):
        add_to_history(chat_id, 'model', bot_response)

    await update.message.reply_text(bot_response)

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry! This command is only for my creator.")
        return

    if not context.args:
        await update.message.reply_text("Please write a message to broadcast. Example: /broadcast Hello everyone!")
        return

    broadcast_text = " ".join(context.args)
    sent_count = 0
    failed_count = 0
    
    logger.info(f"[{chat_id}] Admin initiated broadcast: {broadcast_text}")

    for user_chat_id in list(known_users):
        if user_chat_id == chat_id:
            continue
        try:
            await context.bot.send_message(chat_id=user_chat_id, text=f"**Laila's Message:**\n\n{broadcast_text}", parse_mode='Markdown')
            sent_count += 1
            logger.info(f"Broadcast sent to {user_chat_id}")
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send broadcast to {user_chat_id}: {e}")
    
    await update.message.reply_text(f"Message sent to {sent_count} users. Failed to send to {failed_count} users.")

async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global bot_enabled
    user_id = update.effective_user.id
    
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry! This command is only for my creator.")
        return
    
    bot_enabled = True
    await update.message.reply_text("I am now online! How can I help you?")
    logger.info(f"[{user_id}] Bot enabled by admin.")

async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global bot_enabled
    user_id = update.effective_user.id
    
    if user_id != BROADCAST_ADMIN_ID:
        await update.message.reply_text("Sorry! This command is only for my creator.")
        return
        
    bot_enabled = False
    await update.message.reply_text("I am going offline now. See you later!")
    logger.info(f"[{user_id}] Bot disabled by admin.")

# --- Flask App and Webhook Handler ---
app = Flask(__name__)

# Application Builder (handlers ko yahan set kiya gaya hai)
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("broadcast", broadcast_message))
application.add_handler(CommandHandler("on", on_command))
application.add_handler(CommandHandler("off", off_command))
# नए ग्रुप मैनेजमेंट कमांड्स यहाँ जोड़े गए हैं
application.add_handler(CommandHandler("ban", ban_user))
application.add_handler(CommandHandler("kick", kick_user))
application.add_handler(CommandHandler("mute", mute_user))

application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
async def webhook_handler():
    if request.method == "POST":
        update = Update.de_json(request.json, application.bot)
        # Process the update using the application's update_queue
        async with application:
            await application.process_update(update)
    return "ok"            
