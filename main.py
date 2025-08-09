import os
import logging
from collections import defaultdict
import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import AudioPiped
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
import yt_dlp
import functools

# --- Environment Variables ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))  # You must get this from my.telegram.org
API_HASH = os.getenv("API_HASH")   # You must get this from my.telegram.org

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

# --- Pyrogram and Py-Tgcalls Clients ---
app = Client(
    "LailaVCBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN
)
py_tgcalls = PyTgCalls(app)

# --- Configure Gemini AI ---
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
async def get_bot_response(user_message: str, chat_id: int, bot_username: str, should_use_ai: bool, update: Message) -> str:
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

    if should_use_ai or (update.chat and update.chat.type == 'private'):
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

async def is_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

# --- Admin Functions (Adapted for Pyrogram) ---
@app.on_message(filters.command("ban"))
async def ban_user_pyrogram(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not await is_admin(client, chat_id, user_id):
        await message.reply("Sorry, you need to be an admin to use this command.")
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to ban them.")
        return

    target_user = message.reply_to_message.from_user
    if await is_admin(client, chat_id, target_user.id):
        await message.reply("I cannot ban another admin.")
        return
    
    try:
        await client.ban_chat_member(chat_id, target_user.id)
        await message.reply(f"{target_user.mention} has been banned.")
        logger.info(f"[{chat_id}] {user_id} banned {target_user.id}")
    except Exception as e:
        await message.reply(f"Could not ban user: {e}")
        logger.error(f"[{chat_id}] Error banning user {target_user.id}: {e}")

# --- Other admin commands like kick, mute etc. need to be adapted similarly. ---
# ... (Example for kick and mute below) ...
@app.on_message(filters.command("kick"))
async def kick_user_pyrogram(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not await is_admin(client, chat_id, user_id):
        await message.reply("Sorry, you need to be an admin to use this command.")
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to kick them.")
        return

    target_user = message.reply_to_message.from_user
    if await is_admin(client, chat_id, target_user.id):
        await message.reply("I cannot kick another admin.")
        return
    
    try:
        await client.kick_chat_member(chat_id, target_user.id)
        await message.reply(f"{target_user.mention} has been kicked.")
        logger.info(f"[{chat_id}] {user_id} kicked {target_user.id}")
    except Exception as e:
        await message.reply(f"Could not kick user: {e}")
        logger.error(f"[{chat_id}] Error kicking user {target_user.id}: {e}")

@app.on_message(filters.command("mute"))
async def mute_user_pyrogram(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if not await is_admin(client, chat_id, user_id):
        await message.reply("Sorry, you need to be an admin to use this command.")
        return
    
    if not message.reply_to_message:
        await message.reply("Please reply to a user's message to mute them.")
        return

    target_user = message.reply_to_message.from_user
    if await is_admin(client, chat_id, target_user.id):
        await message.reply("I cannot mute another admin.")
        return
    
    try:
        await client.restrict_chat_member(
            chat_id,
            target_user.id,
            permissions=None
        )
        await message.reply(f"{target_user.mention} has been muted.")
        logger.info(f"[{chat_id}] {user_id} muted {target_user.id}")
    except Exception as e:
        await message.reply(f"Could not mute user: {e}")
        logger.error(f"[{chat_id}] Error muting user {target_user.id}: {e}")


# --- ON/OFF and Power Commands ---
@app.on_message(filters.command("on"))
async def on_command_pyrogram(client: Client, message: Message):
    chat_id = message.chat.id
    global global_bot_status
    if not global_bot_status:
        await message.reply("The bot is globally powered off by the owner and cannot be turned on in this group.")
        return
    
    bot_status[chat_id] = True
    await message.reply("Laila is now ON for this group.")

@app.on_message(filters.command("off"))
async def off_command_pyrogram(client: Client, message: Message):
    chat_id = message.chat.id
    bot_status[chat_id] = False
    await message.reply("Laila is now OFF for this group.")

# --- Broadcast Commands (Admin only) ---
@app.on_message(filters.command("broadcast") & filters.user(BROADCAST_ADMIN_ID))
async def broadcast_command_pyrogram(client: Client, message: Message):
    if not message.command or len(message.command) < 2:
        await message.reply("Please provide a message to broadcast after the command.")
        return
    
    message_to_send = message.text.split(None, 1)[1]
    success_count = 0
    failure_count = 0
    
    message_to_send = message_to_send.replace('\n', '<br>')
    
    for chat_id in known_users:
        try:
            await client.send_message(
                chat_id=int(chat_id),
                text=message_to_send,
                parse_mode='HTML'
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error broadcasting to chat {chat_id}: {e}")
            failure_count += 1
            
    await message.reply(f"Broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")

# --- Photo Broadcast Commands (Admin only) ---
@app.on_message(filters.command("broadcast_photo") & filters.user(BROADCAST_ADMIN_ID))
async def broadcast_photo_command_pyrogram(client: Client, message: Message):
    if not message.command or len(message.command) < 2:
        await message.reply("Usage: /broadcast_photo <photo_file_id> <message>")
        return
    
    args = message.text.split(None, 2)
    photo_file_id = args[1]
    message_to_send = args[2] if len(args) > 2 else ""
    message_to_send = message_to_send.replace('\n', '<br>')
    
    success_count = 0
    failure_count = 0
    
    for chat_id in known_users:
        try:
            await client.send_photo(
                chat_id=int(chat_id),
                photo=photo_file_id,
                caption=message_to_send,
                parse_mode='HTML'
            )
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error broadcasting photo to chat {chat_id}: {e}")
            failure_count += 1
            
    await message.reply(f"Photo broadcast complete! Sent to {success_count} chats. Failed for {failure_count} chats.")
    logger.info(f"Photo broadcast sent by admin. Success: {success_count}, Failure: {failure_count}")
    
@app.on_message(filters.command("getfileid") & filters.user(BROADCAST_ADMIN_ID) & filters.reply)
async def get_photo_id_pyrogram(client: Client, message: Message):
    if message.reply_to_message and message.reply_to_message.photo:
        photo_file_id = message.reply_to_message.photo.file_id
        await message.reply(f"Photo File ID:\n`{photo_file_id}`", parse_mode='Markdown')
    else:
        await message.reply("Please reply to a photo with this command to get its ID.")

# --- Stats Command ---
@app.on_message(filters.command("stats"))
async def stats_command_pyrogram(client: Client, message: Message):
    global start_time
    
    ping_start = time.time()
    await client.send_chat_action(chat_id=message.chat.id, action="typing")
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
    await message.reply(response_text, parse_mode='Markdown')
    logger.info(f"[{message.chat.id}] /stats command used. Uptime: {uptime_str}")
    
# --- VC Functions ---
@app.on_message(filters.command("play") | filters.regex(r"^(play|baja do)\s+(.+)", re.IGNORECASE))
async def play_song_command(client: Client, message: Message):
    if not message.chat.type in ["supergroup", "group"]:
        return await message.reply("This command only works in groups.")
    
    user_message = message.text
    song_query = ""
    
    if message.command and len(message.command) > 1:
        song_query = " ".join(message.command[1:])
    else:
        match = re.search(r'play|baja do)\s+(.+)', user_message, re.IGNORECASE)
        if match:
            song_query = match.group(2).strip()
    
    if not song_query:
        return await message.reply("Please provide a song name or YouTube link to play.")

    try:
        await message.reply(f"Searching for **{song_query}** and trying to play it in VC...")
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': 'downloads/%(title)s.%(ext)s',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(song_query, download=False)
            if 'entries' in info:
                video = info['entries'][0]
            else:
                video = info
            
            song_url = video['url']
            song_title = video.get('title', 'Unknown Title')
            
            # This part needs to be improved for direct streaming without downloading.
            # For now, it's a simple placeholder to show the flow.
            # You would need to download the file and then pass the file path.
            # A temporary file download is needed here.
            
            await py_tgcalls.join_group_call(
                message.chat.id,
                AudioPiped(song_url)
            )
            await message.reply(f"🎶 Playing: **{song_title}**")
            
    except Exception as e:
        logger.error(f"Error playing song: {e}", exc_info=True)
        await message.reply("Sorry, I could not play that song.")

@app.on_message(filters.command("stop") | filters.command("end"))
async def stop_song_command(client: Client, message: Message):
    if not py_tgcalls.is_call_active(message.chat.id):
        return await message.reply("No song is currently playing in the VC.")
    
    await py_tgcalls.leave_group_call(message.chat.id)
    await message.reply("VC ended. See you next time!")

# --- Main Message Handler (with AI and other logic) ---
@app.on_message(filters.text)
async def handle_message_pyrogram(client: Client, message: Message):
    global total_messages_processed, global_bot_status
    user_message = message.text
    user_name = message.from_user.first_name
    chat_id = message.chat.id
    
    if str(chat_id) not in known_users:
        known_users.add(str(chat_id))
        save_known_users()
        logger.info(f"[{chat_id}] New chat added to known_users.")

    total_messages_processed += 1

    if not global_bot_status:
        logger.info(f"[{chat_id}] Bot is globally off. Ignoring message from {user_name}.")
        return
    
    if not bot_status[chat_id]:
        logger.info(f"[{chat_id}] Bot is disabled for this group. Ignoring message from {user_name}.")
        return
    
    # Handle Laila's custom logic here (Creator, Praise, etc.)
    # ... (Your existing logic) ...
    
    # AI logic
    # ... (Your existing AI response logic) ...

    # This part needs to be re-written to fit the new Pyrogram framework
    # The `handle_message` function is the place to put all your AI logic
    # I have put the handle_message_pyrogram function above with the appropriate decorators
    # and VC functions are separated with their own decorators.

# --- Main function to run the bot ---
async def main():
    await app.start()
    await py_tgcalls.start()
    print("Laila VC bot is running!")
    await idle()
    await py_tgcalls.stop()
    await app.stop()
    print("Laila VC bot stopped.")

if __name__ == "__main__":
    asyncio.run(main())
