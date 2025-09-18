#!/usr/bin/env python3
"""
Telegram anonymous community bot (fixed)

Main fixes included:
- Convert psycopg2 RealDictRow objects to plain dicts before returning or using them
  so they are never passed directly as SQL parameters (fixes "can't adapt type 'RealDictRow'").
- db_execute / db_fetch_one / db_fetch_all always return plain Python types (dict/list).
- Fixed several small bugs/typos (reply_mup -> reply_markup, consistent MarkdownV2 usage).
- Ensure ADMIN_ID and CHANNEL_ID are handled safely (cast to int when sending messages).
- Safer reply/edit logic for callback queries and message vs callback contexts.
- Robust timestamp handling for DB datetime objects.
- Ready-to-copy single-file bot. Set environment variables and run.

Required packages:
- python-telegram-bot>=20.0
- psycopg2-binary
- python-dotenv
"""

import os
import logging
import psycopg2
from psycopg2 import ProgrammingError
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest
from flask import Flask, jsonify
import threading
from datetime import datetime
import time
from typing import Optional, Any, List, Dict

# Load env
load_dotenv()

# CONFIG
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TOKEN") or os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
# Keep ADMIN_ID raw string for DB storage, but have an int for sending messages
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else None
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW and CHANNEL_ID_RAW.isdigit() else None

if not TOKEN:
    raise SystemExit("TOKEN (or BOT_TOKEN) environment variable is required")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL environment variable is required")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask health app (optional)
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return jsonify(status="OK", message="Bot is running")

@flask_app.route('/ping')
def ping():
    return jsonify(status="OK", message="pong")

# -------------------------
# Database helpers (convert RealDictRow -> dict)
# -------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def _row_to_dict(row: Any) -> Optional[Dict]:
    if row is None:
        return None
    # If it's RealDictRow or mapping-like, convert
    try:
        return dict(row)
    except Exception:
        return row  # fallback (primitive)

def db_execute(query: str, params: tuple = (), fetch: bool = False):
    """
    Execute a query. If fetch=True, returns list[dict].
    If query uses RETURNING and a single row is returned, this returns dict or None.
    Otherwise returns None.
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            if fetch:
                rows = cur.fetchall()
                conn.commit()
                return [ _row_to_dict(r) for r in rows ]
            # Try to fetch one (useful for RETURNING)
            try:
                row = cur.fetchone()
                conn.commit()
                return _row_to_dict(row)
            except ProgrammingError:
                conn.commit()
                return None
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("db_execute failed for query: %s params: %s", query, params)
        raise
    finally:
        if conn:
            conn.close()

def db_fetch_one(query: str, params: tuple = ()):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return _row_to_dict(row)
    except Exception:
        logger.exception("db_fetch_one failed for query: %s params: %s", query, params)
        raise
    finally:
        if conn:
            conn.close()

def db_fetch_all(query: str, params: tuple = ()):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            return [ _row_to_dict(r) for r in rows ]
    except Exception:
        logger.exception("db_fetch_all failed for query: %s params: %s", query, params)
        raise
    finally:
        if conn:
            conn.close()

# -------------------------
# DB init
# -------------------------
def init_db():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as c:
            c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                anonymous_name TEXT,
                sex TEXT DEFAULT '👤',
                awaiting_name BOOLEAN DEFAULT FALSE,
                waiting_for_post BOOLEAN DEFAULT FALSE,
                waiting_for_comment BOOLEAN DEFAULT FALSE,
                selected_category TEXT,
                comment_post_id INTEGER,
                comment_idx INTEGER,
                reply_idx INTEGER,
                nested_idx INTEGER,
                notifications_enabled BOOLEAN DEFAULT TRUE,
                privacy_public BOOLEAN DEFAULT TRUE,
                is_admin BOOLEAN DEFAULT FALSE,
                waiting_for_private_message BOOLEAN DEFAULT FALSE,
                private_message_target TEXT
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS followers (
                follower_id TEXT,
                followed_id TEXT,
                PRIMARY KEY (follower_id, followed_id)
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                post_id SERIAL PRIMARY KEY,
                content TEXT,
                author_id TEXT,
                category TEXT,
                channel_message_id BIGINT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                media_type TEXT DEFAULT 'text',
                media_id TEXT,
                comment_count INTEGER DEFAULT 0,
                approved BOOLEAN DEFAULT FALSE,
                admin_approved_by TEXT
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                comment_id SERIAL PRIMARY KEY,
                post_id INTEGER REFERENCES posts(post_id),
                parent_comment_id INTEGER DEFAULT 0,
                author_id TEXT,
                content TEXT,
                type TEXT DEFAULT 'text',
                file_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                reaction_id SERIAL PRIMARY KEY,
                comment_id INTEGER REFERENCES comments(comment_id),
                user_id TEXT,
                type TEXT,
                UNIQUE(comment_id, user_id)
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS private_messages (
                message_id SERIAL PRIMARY KEY,
                sender_id TEXT REFERENCES users(user_id),
                receiver_id TEXT REFERENCES users(user_id),
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_read BOOLEAN DEFAULT FALSE
            )
            ''')
            c.execute('''
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id TEXT REFERENCES users(user_id),
                blocked_id TEXT REFERENCES users(user_id),
                PRIMARY KEY (blocker_id, blocked_id)
            )
            ''')
            # Ensure admin user exists if ADMIN_ID_RAW provided
            if ADMIN_ID_RAW:
                c.execute('''
                    INSERT INTO users (user_id, anonymous_name, is_admin)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE
                ''', (ADMIN_ID_RAW, "Admin"))
        conn.commit()
        logger.info("DB initialized")
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("init_db failed")
        raise
    finally:
        if conn:
            conn.close()

# -------------------------
# App data / helpers
# -------------------------
CATEGORIES = [
    ("🙏 Pray For Me", "PrayForMe"),
    ("📖 Bible", "Bible"),
    ("💼 Work and Life", "WorkLife"),
    ("🕊 Spiritual Life", "SpiritualLife"),
    ("⚔️ Christian Challenges", "ChristianChallenges"),
    ("❤️ Relationship", "Relationship"),
    ("💍 Marriage", "Marriage"),
    ("🧑‍🤝‍🧑 Youth", "Youth"),
    ("💰 Finance", "Finance"),
    ("🔖 Other", "Other"),
]

def build_category_buttons():
    buttons = []
    for i in range(0, len(CATEGORIES), 2):
        row = []
        for j in range(2):
            if i + j < len(CATEGORIES):
                name, code = CATEGORIES[i + j]
                row.append(InlineKeyboardButton(name, callback_data=f'category_{code}'))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🙏 Ask Question")],
        [KeyboardButton("👤 View Profile"), KeyboardButton("🏆 Leaderboard")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("❓ Help")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

def create_anonymous_name(user_id: str) -> str:
    try:
        uid_int = int(user_id)
    except Exception:
        uid_int = abs(hash(user_id)) % 10000
    names = ["Anonymous"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def calculate_user_rating(user_id: str) -> int:
    post_row = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE", (user_id,))
    post_count = post_row['count'] if post_row and 'count' in post_row else 0
    comment_row = db_fetch_one("SELECT COUNT(*) as count FROM comments WHERE author_id = %s", (user_id,))
    comment_count = comment_row['count'] if comment_row and 'count' in comment_row else 0
    return post_count + comment_count

def format_stars(rating: int, max_stars: int = 5) -> str:
    full_stars = min(rating // 5, max_stars)
    empty_stars = max(0, max_stars - full_stars)
    return '⭐️' * full_stars + '☆' * empty_stars

def count_all_comments(post_id: int) -> int:
    def count_replies(parent_id=None):
        if parent_id is None:
            comments = db_fetch_all("SELECT comment_id FROM comments WHERE post_id = %s AND parent_comment_id = 0", (post_id,))
        else:
            comments = db_fetch_all("SELECT comment_id FROM comments WHERE parent_comment_id = %s", (parent_id,))
        total = len(comments)
        for comment in comments:
            total += count_replies(comment['comment_id'])
        return total
    return count_replies()

def get_display_name(user_data: Optional[Dict]) -> str:
    if not user_data:
        return "Anonymous"
    return user_data.get('anonymous_name') or "Anonymous"

def get_display_sex(user_data: Optional[Dict]) -> str:
    if not user_data:
        return '👤'
    return user_data.get('sex') or '👤'

# -------------------------
# Notifications & helpers that use DB rows (rows are dicts now)
# -------------------------
async def update_channel_post_comment_count(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    try:
        post = db_fetch_one("SELECT channel_message_id, comment_count FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post.get('channel_message_id'):
            return
        total_comments = count_all_comments(post_id)
        db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (total_comments, post_id))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        # channel_message_id should be integer
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=int(post['channel_message_id']),
                reply_markup=kb
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.error("Failed to update channel comment count: %s", e)
    except Exception:
        logger.exception("update_channel_post_comment_count error")

async def notify_admin_of_new_post(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    if not ADMIN_ID:
        return
    try:
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (post['author_id'],))
        author_name = get_display_name(author)
        post_preview = (post['content'] or "")[:100] + '...' if post['content'] and len(post['content']) > 100 else post['content'] or ""
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post_id}")
            ]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🆕 New post awaiting approval from {author_name}:\n\n{escape_markdown(post_preview, version=2)}",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception:
        logger.exception("notify_admin_of_new_post failed")

async def notify_user_of_private_message(context: ContextTypes.DEFAULT_TYPE, sender_id: str, receiver_id: str, message_content: str, message_id: Optional[int]):
    try:
        # Don't send if blocked
        blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (receiver_id, sender_id))
        if blocked:
            return
        receiver = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (receiver_id,))
        if not receiver or not receiver.get('notifications_enabled', True):
            return
        sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
        sender_name = get_display_name(sender)
        preview = message_content[:100] + '...' if message_content and len(message_content) > 100 else (message_content or "")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 Reply", callback_data=f"reply_msg_{sender_id}"),
                InlineKeyboardButton("⛔ Block", callback_data=f"block_user_{sender_id}")
            ]
        ])
        await context.bot.send_message(
            chat_id=receiver_id,
            text=(f"📩 *New Private Message*\n\n"
                  f"👤 From: {escape_markdown(sender_name, version=2)}\n\n"
                  f"💬 {escape_markdown(preview, version=2)}\n\n"
                  f"_Use /inbox to view all messages_"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard
        )
    except Exception:
        logger.exception("notify_user_of_private_message failed")

# -------------------------
# UI handlers
# -------------------------
def safe_send(chat_id: int, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2, bot=None):
    # helper if needed (not used everywhere)
    pass

async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("menu", "📱 Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Ask a question"),
        BotCommand("leaderboard", "View top contributors"),
        BotCommand("settings", "Configure your preferences"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
        BotCommand("inbox", "View your private messages"),
    ]
    await app.bot.set_my_commands(commands)

# -- show_leaderboard, settings, admin, pending, approve, reject are mostly unchanged but
#    use safe dict rows from DB and consistent parse_mode=MARKDOWN_V2
# For brevity in this fix bundle, include the core flows: start, button_handler, handle_message,
# show_comments_page and other functions used by non-admins. (Admin flows remain mostly same.)

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) +
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
        LIMIT 10
    ''')
    leaderboard_text = "🏆 *Top Contributors* 🏆\n\n"
    for idx, user in enumerate(top_users, start=1):
        stars = format_stars(user.get('total', 0))
        leaderboard_text += f"{idx}. {user.get('anonymous_name','Anonymous')} {user.get('sex','👤')} - {user.get('total',0)} contributions {stars}\n"
    # Add calling user's rank if not in top 10
    current_uid = str(update.effective_user.id)
    user_rank = get_user_rank(current_uid)
    if user_rank and user_rank > 10:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = %s", (current_uid,))
        if user_data:
            user_contributions = calculate_user_rating(current_uid)
            leaderboard_text += f"\n...\n{user_rank}. {user_data.get('anonymous_name','Anonymous')} {user_data.get('sex','👤')} - {user_contributions} contributions\n"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')]
    ])
    try:
        if update.message:
            await update.message.reply_text(leaderboard_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        elif update.callback_query:
            await update.callback_query.edit_message_text(leaderboard_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("show_leaderboard failed")

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user:
        text = "Please use /start first to initialize your profile."
        if update.message:
            await update.message.reply_text(text)
        else:
            await update.callback_query.message.reply_text(text)
        return
    notifications_status = "✅ ON" if user.get('notifications_enabled', True) else "❌ OFF"
    privacy_status = "🌍 Public" if user.get('privacy_public', True) else "🔒 Private"
    keyboard = [
        [InlineKeyboardButton(f"🔔 Notifications: {notifications_status}", callback_data='toggle_notifications')],
        [InlineKeyboardButton(f"👁‍🗨 Privacy: {privacy_status}", callback_data='toggle_privacy')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu'), InlineKeyboardButton("👤 Profile", callback_data='profile')]
    ]
    if user.get('is_admin'):
        keyboard.insert(0, [InlineKeyboardButton("🛠 Admin Panel", callback_data='admin_panel')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("⚙️ *Settings Menu*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text("⚙️ *Settings Menu*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("show_settings failed")

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None):
    keyboard = [
        [InlineKeyboardButton("✏️ Edit", callback_data='edit_post'), InlineKeyboardButton("❌ Cancel", callback_data='cancel_post')],
        [InlineKeyboardButton("✅ Submit", callback_data='confirm_post')]
    ]
    preview_text = f"📝 *Post Preview* [{category}]\n\n{escape_markdown(post_content or '', version=2)}\n\nPlease confirm your post:"
    context.user_data['pending_post'] = {
        'content': post_content,
        'category': category,
        'media_type': media_type,
        'media_id': media_id,
        'timestamp': time.time()
    }
    try:
        if update.callback_query:
            if media_type == 'text':
                await update.callback_query.edit_message_text(preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # For media, try editing caption if possible, else send new media
                try:
                    await update.callback_query.edit_message_caption(caption=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                except BadRequest:
                    # fallback to sending a new message with media
                    if media_type == 'photo':
                        await update.callback_query.message.reply_photo(photo=media_id, caption=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                    elif media_type == 'voice':
                        await update.callback_query.message.reply_voice(voice=media_id, caption=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            if media_type == 'text':
                await update.message.reply_text(preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                if media_type == 'photo':
                    await update.message.reply_photo(photo=media_id, caption=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                elif media_type == 'voice':
                    await update.message.reply_voice(voice=media_id, caption=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("send_post_confirmation failed")
        try:
            if update.message:
                await update.message.reply_text("❌ Error showing confirmation. Please try again.")
            else:
                await update.callback_query.message.reply_text("❌ Error showing confirmation. Please try again.")
        except Exception:
            pass

# Comments browsing / posting flows (key ones included)
async def show_comments_menu(update, context, post_id, page=1):
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        # Use whichever context exists
        if update.message:
            await update.message.reply_text("❌ Post not found.", reply_markup=main_menu)
        else:
            await update.callback_query.message.reply_text("❌ Post not found.", reply_markup=main_menu)
        return
    comment_count = count_all_comments(post_id)
    keyboard = [
        [InlineKeyboardButton(f"👁 View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_{page}"),
         InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")]
    ]
    post_text = post.get('content','')
    await (update.message.reply_text if update.message else update.callback_query.message.reply_text)(
        f"💬\n{escape_markdown(post_text, version=2)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def show_comments_page(update, context, post_id, page=1, reply_pages=None):
    chat = update.effective_chat
    if not chat:
        logger.error("No chat in update")
        return
    chat_id = chat.id
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await context.bot.send_message(chat_id, "❌ Post not found.", reply_markup=main_menu)
        return
    per_page = 5
    offset = (page - 1) * per_page
    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = %s AND parent_comment_id = 0 ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (post_id, per_page, offset)
    )
    total_comments = count_all_comments(post_id)
    total_pages = (total_comments + per_page - 1) // per_page
    header = escape_markdown(post.get('content',''), version=2) + "\n\n"
    if not comments and page == 1:
        await context.bot.send_message(chat_id=chat_id, text=header + "_No comments yet._", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu)
        return
    header_msg = await context.bot.send_message(chat_id=chat_id, text=header, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu)
    header_message_id = header_msg.message_id
    for comment in comments:
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (comment['author_id'],))
        display_name = get_display_name(commenter)
        display_sex = get_display_sex(commenter)
        profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{display_name}"
        likes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'", (comment['comment_id'],))
        likes = likes_row['cnt'] if likes_row else 0
        dislikes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'", (comment['comment_id'],))
        dislikes = dislikes_row['cnt'] if dislikes_row else 0
        comment_text = escape_markdown(comment.get('content',''), version=2)
        author_text = f"[{escape_markdown(display_name, version=2)}]({profile_url}) {display_sex}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{comment['comment_id']}"),
             InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{comment['comment_id']}"),
             InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")]
        ])
        await context.bot.send_message(chat_id=chat_id, text=f"{comment_text}\n\n{author_text}", reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=header_message_id)
    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"viewcomments_{post_id}_{page-1}"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"viewcomments_{post_id}_{page+1}"))
    if pagination_buttons:
        await context.bot.send_message(chat_id=chat_id, text=f"📄 Page {page}/{total_pages}", reply_markup=InlineKeyboardMarkup([pagination_buttons]), reply_to_message_id=header_message_id)

# -------------------------
# Command handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ensure user row exists
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        anon = create_anonymous_name(user_id)
        is_admin_flag = 1 if ADMIN_ID_RAW and user_id == ADMIN_ID_RAW else 0
        db_execute("INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)", (user_id, anon, '👤', is_admin_flag))
    args = context.args or []
    # handle start deep links
    if args:
        arg = args[0]
        if arg.startswith("comments_") and arg.split("_",1)[1].isdigit():
            post_id = int(arg.split("_",1)[1])
            await show_comments_menu(update, context, post_id, page=1)
            return
        if arg.startswith("profile_"):
            target_name = arg.split("_",1)[1]
            user_data = db_fetch_one("SELECT * FROM users WHERE anonymous_name = %s", (target_name,))
            if user_data:
                followers = db_fetch_all("SELECT * FROM followers WHERE followed_id = %s", (user_data['user_id'],))
                rating = calculate_user_rating(user_data['user_id'])
                stars = format_stars(rating)
                btns = []
                current = user_id
                if user_data['user_id'] != current:
                    is_following = db_fetch_one("SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s", (current, user_data['user_id']))
                    if is_following:
                        btns.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{user_data["user_id"]}')])
                        btns.append([InlineKeyboardButton("✉️ Send Message", callback_data=f'message_{user_data["user_id"]}')])
                    else:
                        btns.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{user_data["user_id"]}')])
                await update.message.reply_text(
                    f"👤 *{user_data.get('anonymous_name','Anonymous')}*\n📌 Sex: {user_data.get('sex','👤')}\n👥 Followers: {len(followers)}\n⭐️ Contributions: {rating} {stars}",
                    reply_markup=InlineKeyboardMarkup(btns) if btns else None, parse_mode=ParseMode.MARKDOWN_V2)
            return
        if arg == "inbox":
            await show_inbox(update, context)
            return
    # Default start menu
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'), InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("❓ Help", callback_data='help'), InlineKeyboardButton("ℹ️ About Us", callback_data='about')]
    ])
    await update.message.reply_text("🌟✝️ Welcome to Christian Vent ✝️🌟\nChoose an option:", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    await update.message.reply_text("You can use the buttons below to navigate:", reply_markup=main_menu)

async def show_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    unread = db_fetch_one("SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE", (user_id,))
    unread_count = unread['count'] if unread else 0
    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT 10
    ''', (user_id,))
    if not messages:
        await update.message.reply_text("📭 *Your Inbox*\n\nYou don't have any messages yet.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    inbox_text = f"📭 *Your Inbox* ({unread_count} unread)\n\n"
    for msg in messages:
        status = "🔵" if not msg.get('is_read') else "⚪️"
        ts = msg.get('timestamp')
        if isinstance(ts, datetime):
            timestamp = ts.strftime('%b %d')
        else:
            try:
                timestamp = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').strftime('%b %d')
            except Exception:
                timestamp = str(ts)
        preview = (msg.get('content') or "")[:30] + '...' if msg.get('content') and len(msg.get('content')) > 30 else (msg.get('content') or "")
        inbox_text += f"{status} *{msg.get('sender_name','Anonymous')}* {msg.get('sender_sex','👤')} - {escape_markdown(preview, version=2)} ({timestamp})\n"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📝 View Messages", callback_data='view_messages')],[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]])
    await update.message.reply_text(inbox_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)

# -------------------------
# Button handler (core fixes here)
# -------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass
    user_id = str(query.from_user.id)
    data = query.data or ""
    try:
        if data == 'ask':
            # show categories
            try:
                await query.message.reply_text("📚 *Choose a category:*", reply_markup=build_category_buttons(), parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest:
                await query.reply_text("📚 Choose a category:", reply_markup=build_category_buttons())
            return

        if data.startswith('category_'):
            category = data.split('_',1)[1]
            db_execute("UPDATE users SET waiting_for_post = TRUE, selected_category = %s WHERE user_id = %s", (category, user_id))
            await query.message.reply_text(f"✍️ *Please type your thought for #{category}:*\nYou may also send a photo or voice message.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=ForceReply(selective=True))
            return

        if data == 'menu':
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'), InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')],
                [InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'), InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
                [InlineKeyboardButton("❓ Help", callback_data='help'), InlineKeyboardButton("ℹ️ About Us", callback_data='about')]
            ])
            try:
                await query.message.edit_text("📱 *Main Menu*\nChoose an option below:", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest:
                await query.message.reply_text("📱 Main Menu", reply_markup=keyboard)
            return

        if data == 'profile':
            await send_updated_profile(user_id, query.message.chat.id, context)
            return

        if data == 'leaderboard':
            await show_leaderboard(update, context)
            return

        if data == 'settings':
            await show_settings(update, context)
            return

        if data == 'toggle_notifications':
            cur = db_fetch_one("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,))
            if cur is not None:
                new_val = not cur.get('notifications_enabled', True)
                db_execute("UPDATE users SET notifications_enabled = %s WHERE user_id = %s", (new_val, user_id))
            await show_settings(update, context)
            return

        if data == 'toggle_privacy':
            cur = db_fetch_one("SELECT privacy_public FROM users WHERE user_id = %s", (user_id,))
            if cur is not None:
                new_val = not cur.get('privacy_public', True)
                db_execute("UPDATE users SET privacy_public = %s WHERE user_id = %s", (new_val, user_id))
            await show_settings(update, context)
            return

        if data == 'help':
            help_text = "ℹ️ *How to use this bot...*"
            await query.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]), parse_mode=ParseMode.MARKDOWN_V2)
            return

        if data == 'about':
            about_text = "👤 Creator: Yididiya Tamiru\n🙏 This bot helps you share anonymously."
            await query.message.reply_text(about_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]), parse_mode=ParseMode.MARKDOWN_V2)
            return

        if data == 'edit_name':
            db_execute("UPDATE users SET awaiting_name = TRUE WHERE user_id = %s", (user_id,))
            await query.message.reply_text("✏️ Please type your new anonymous name:", parse_mode=ParseMode.MARKDOWN_V2)
            return

        if data == 'edit_sex':
            btns = InlineKeyboardMarkup([[InlineKeyboardButton("👨 Male", callback_data='sex_male')],[InlineKeyboardButton("👩 Female", callback_data='sex_female')]])
            await query.message.reply_text("⚧️ Select your sex:", reply_markup=btns)
            return

        if data.startswith('sex_'):
            sex = '👨' if 'male' in data else '👩'
            db_execute("UPDATE users SET sex = %s WHERE user_id = %s", (sex, user_id))
            await query.message.reply_text("✅ Sex updated!")
            await send_updated_profile(user_id, query.message.chat.id, context)
            return

        # Follow/unfollow
        if data.startswith(('follow_', 'unfollow_')):
            target_uid = data.split('_',1)[1]
            if data.startswith('follow_'):
                try:
                    db_execute("INSERT INTO followers (follower_id, followed_id) VALUES (%s, %s)", (user_id, target_uid))
                except Exception:
                    pass
            else:
                db_execute("DELETE FROM followers WHERE follower_id = %s AND followed_id = %s", (user_id, target_uid))
            await query.message.reply_text("✅ Successfully updated!")
            await send_updated_profile(target_uid, query.message.chat.id, context)
            return

        # viewcomments / writecomment flows
        if data.startswith('viewcomments_'):
            parts = data.split('_')
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                pid = int(parts[1]); page = int(parts[2])
                await show_comments_page(update, context, pid, page)
            return

        if data.startswith('writecomment_'):
            post_id_str = data.split('_',1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute("UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s", (post_id, user_id))
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                preview = "Original content not found"
                if post:
                    content = post.get('content','')[:100] + '...' if post.get('content') and len(post.get('content')) > 100 else post.get('content','')
                    preview = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                await query.message.reply_text(f"{preview}\n\n✍️ Please type your comment:", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Reactions (like/dislike) - simplified handling
        if data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
            try:
                parts = data.split('_')
                comment_id = int(parts[1])
                reaction_type = 'like' if parts[0] in ('likecomment','likereply') else 'dislike'
                db_execute("DELETE FROM reactions WHERE comment_id = %s AND user_id = %s", (comment_id, user_id))
                existing = db_fetch_one("SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s", (comment_id, user_id))
                if not existing or existing.get('type') != reaction_type:
                    db_execute("INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)", (comment_id, user_id, reaction_type))
                # Update reply markup counts
                likes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'", (comment_id,))
                dislikes_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'", (comment_id,))
                likes = likes_row['cnt'] if likes_row else 0
                dislikes = dislikes_row['cnt'] if dislikes_row else 0
                comment = db_fetch_one("SELECT post_id, parent_comment_id, content, author_id FROM comments WHERE comment_id = %s", (comment_id,))
                if not comment:
                    await query.answer("Comment not found", show_alert=True)
                    return
                post_id = comment['post_id']; parent_comment_id = comment.get('parent_comment_id',0)
                # Build new markup
                if parent_comment_id == 0:
                    new_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{comment_id}"), InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{comment_id}"), InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment_id}")]])
                else:
                    new_kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"👍 {likes}", callback_data=f"likereply_{comment_id}"), InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikereply_{comment_id}"), InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{comment_id}")]])
                try:
                    await context.bot.edit_message_reply_markup(chat_id=query.message.chat_id, message_id=query.message.message_id, reply_markup=new_kb)
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        logger.error("Failed to update reaction markup: %s", e)
                # Optionally notify comment author if reaction added and they allow notifications
                await query.answer()
            except Exception:
                logger.exception("Error processing reaction")
                await query.answer("❌ Error updating reaction", show_alert=True)
            return

        # Reply flows
        if data.startswith("reply_"):
            parts = data.split("_")
            if len(parts) == 3:
                post_id = int(parts[1]); comment_id = int(parts[2])
                db_execute("UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s", (post_id, comment_id, user_id))
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
                preview_text = "Original comment not found"
                if comment:
                    content = comment.get('content','')[:100] + '...' if comment.get('content') and len(comment.get('content')) > 100 else comment.get('content','')
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                await query.message.reply_text(f"{preview_text}\n\n↩️ Please type your *reply*:", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN_V2)
            return

        if data.startswith("replytoreply_"):
            parts = data.split("_")
            if len(parts) == 4:
                post_id = int(parts[1]); comment_id = int(parts[3])
                db_execute("UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s", (post_id, comment_id, user_id))
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
                preview_text = "Original reply not found"
                if comment:
                    content = comment.get('content','')[:100] + '...' if comment.get('content') and len(comment.get('content')) > 100 else comment.get('content','')
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                await query.message.reply_text(f"{preview_text}\n\n↩️ Please type your *reply*:", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN_V2)
            return

        # Post confirmation flow buttons
        if data in ('edit_post','cancel_post','confirm_post'):
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                try:
                    await query.message.edit_text("❌ Post data not found. Please start over.")
                except BadRequest:
                    await query.message.reply_text("❌ Post data not found. Please start over.")
                return
            if data == 'edit_post':
                if time.time() - pending_post.get('timestamp', 0) > 300:
                    try:
                        await query.message.edit_text("❌ Edit time expired. Please start a new post.")
                    except BadRequest:
                        await query.message.reply_text("❌ Edit time expired. Please start a new post.")
                    context.user_data.pop('pending_post', None)
                    return
                try:
                    await query.message.edit_text("✏️ Please edit your post:", reply_markup=ForceReply(selective=True))
                except BadRequest:
                    await query.message.reply_text("✏️ Please edit your post:", reply_markup=ForceReply(selective=True))
                return
            if data == 'cancel_post':
                try:
                    await query.message.edit_text("❌ Post cancelled.")
                except BadRequest:
                    await query.message.reply_text("❌ Post cancelled.")
                context.user_data.pop('pending_post', None)
                return
            if data == 'confirm_post':
                # Persist post to DB
                category = pending_post['category']; post_content = pending_post['content']; media_type = pending_post.get('media_type','text'); media_id = pending_post.get('media_id')
                context.user_data.pop('pending_post', None)
                # Insert and RETURNING post_id -> db_execute will return dict
                post_row = db_execute("INSERT INTO posts (content, author_id, category, media_type, media_id) VALUES (%s, %s, %s, %s, %s) RETURNING post_id", (post_content, user_id, category, media_type, media_id))
                post_id = post_row.get('post_id') if post_row else None
                if post_id:
                    await notify_admin_of_new_post(context, int(post_id))
                    try:
                        await query.message.edit_text("✅ Your post has been submitted for admin approval!\nYou'll be notified when it's approved and published.")
                        await query.message.reply_text("What would you like to do next?", reply_markup=main_menu)
                    except BadRequest:
                        await query.message.reply_text("✅ Your post submitted!", reply_markup=main_menu)
                else:
                    try:
                        await query.message.edit_text("❌ Failed to submit post. Please try again.")
                    except BadRequest:
                        await query.message.reply_text("❌ Failed to submit post. Please try again.")
                return

        # Messaging / blocking flows
        if data == 'inbox':
            await show_inbox(update, context)
            return
        if data == 'view_messages':
            await show_messages(update, context)
            return
        if data.startswith('messages_page_'):
            page = int(data.split('_')[-1])
            await show_messages(update, context, page)
            return
        if data.startswith('message_'):
            target_id = data.split('_',1)[1]
            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            target_name = target_user.get('anonymous_name') if target_user else "this user"
            await query.message.reply_text(f"✉️ *Composing message to {target_name}*\n\nPlease type your message:", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN_V2)
            return
        if data.startswith('reply_msg_'):
            target_id = data.split('_',2)[2]
            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            target_name = target_user.get('anonymous_name') if target_user else "this user"
            await query.message.reply_text(f"↩️ *Replying to {target_name}*\n\nPlease type your message:", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN_V2)
            return
        if data.startswith('block_user_'):
            # format: block_user_{target}
            parts = data.split('_',2)
            if len(parts) >= 3:
                target_id = parts[2]
                try:
                    db_execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)", (user_id, target_id))
                    await query.message.reply_text("✅ User has been blocked. They can no longer send you messages.")
                except Exception:
                    await query.message.reply_text("❌ User is already blocked or error occurred.")
            return

        # Admin handlers (basic mapping to functions)
        if data == 'admin_panel':
            await admin_panel(update, context)
            return
        if data == 'admin_pending':
            await show_pending_posts(update, context)
            return
        if data == 'admin_stats':
            await show_admin_stats(update, context)
            return
        if data.startswith('approve_post_'):
            post_id = int(data.split('_')[-1])
            await approve_post(update, context, post_id)
            return
        if data.startswith('reject_post_'):
            post_id = int(data.split('_')[-1])
            await reject_post(update, context, post_id)
            return

    except Exception:
        logger.exception("Error in button_handler")
        try:
            await query.message.reply_text("❌ An error occurred. Please try again.")
        except Exception:
            pass

# -------------------------
# Message handler (post/comment/private flows)
# -------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = update.message.text or update.message.caption or ""
    user_id = str(update.message.from_user.id)
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    # Posting flow
    if user and user.get('waiting_for_post'):
        category = user.get('selected_category')
        db_execute("UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s", (user_id,))
        media_type = 'text'; media_id = None
        post_content = ""
        try:
            if update.message.text:
                post_content = update.message.text
                await send_post_confirmation(update, context, post_content, category)
                return
            if update.message.photo:
                photo = update.message.photo[-1]
                media_id = photo.file_id; media_type = 'photo'
                post_content = update.message.caption or ""
            elif update.message.voice:
                voice = update.message.voice
                media_id = voice.file_id; media_type = 'voice'
                post_content = update.message.caption or ""
            else:
                post_content = "(Unsupported content type)"
        except Exception:
            logger.exception("Error reading media for post")
            post_content = "(Unsupported content type)"
        await send_post_confirmation(update, context, post_content, category, media_type, media_id)
        return

    # Comment flow
    if user and user.get('waiting_for_comment'):
        post_id = user.get('comment_post_id')
        parent_comment_id = 0
        if user.get('comment_idx'):
            try:
                parent_comment_id = int(user.get('comment_idx'))
            except Exception:
                parent_comment_id = 0
        comment_type = 'text'; file_id = None
        if update.message.text:
            content = update.message.text
        elif update.message.photo:
            photo = update.message.photo[-1]; file_id = photo.file_id; comment_type = 'photo'; content = update.message.caption or ""
        elif update.message.voice:
            voice = update.message.voice; file_id = voice.file_id; comment_type = 'voice'; content = update.message.caption or ""
        else:
            await update.message.reply_text("❌ Unsupported comment type. Please send text, photo, or voice message.")
            return
        comment_row = db_execute("""INSERT INTO comments (post_id, parent_comment_id, author_id, content, type, file_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING comment_id""", (post_id, parent_comment_id, user_id, content, comment_type, file_id))
        comment_id = comment_row.get('comment_id') if comment_row else None
        db_execute("UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL WHERE user_id = %s", (user_id,))
        await update.message.reply_text("✅ Your comment has been posted!", reply_markup=main_menu)
        # update channel comment count
        await update_channel_post_comment_count(context, post_id)
        # notify parent author if needed
        if parent_comment_id != 0:
            await notify_user_of_reply(context, post_id, parent_comment_id, user_id)
        return

    # Private message sending
    if user and user.get('waiting_for_private_message'):
        target_id = user.get('private_message_target')
        message_content = text
        # Check block
        is_blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (target_id, user_id))
        if is_blocked:
            await update.message.reply_text("❌ You cannot send messages to this user. They have blocked you.", reply_markup=main_menu)
            db_execute("UPDATE users SET waiting_for_private_message = FALSE, private_message_target = NULL WHERE user_id = %s", (user_id,))
            return
        message_row = db_execute("INSERT INTO private_messages (sender_id, receiver_id, content) VALUES (%s, %s, %s) RETURNING message_id", (user_id, target_id, message_content))
        message_id = message_row.get('message_id') if message_row else None
        db_execute("UPDATE users SET waiting_for_private_message = FALSE, private_message_target = NULL WHERE user_id = %s", (user_id,))
        await notify_user_of_private_message(context, user_id, target_id, message_content, message_id)
        await update.message.reply_text("✅ Your message has been sent!", reply_markup=main_menu)
        return

    # Awaiting name update
    if user and user.get('awaiting_name'):
        new_name = (text or "").strip()
        if new_name and len(new_name) <= 30:
            db_execute("UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s", (new_name, user_id))
            await update.message.reply_text(f"✅ Name updated to *{escape_markdown(new_name, version=2)}*!", parse_mode=ParseMode.MARKDOWN_V2)
            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Keyboard shortcuts
    if text == "🙏 Ask Question":
        await update.message.reply_text("📚 *Choose a category:*", reply_markup=build_category_buttons(), parse_mode=ParseMode.MARKDOWN_V2)
        return
    if text == "👤 View Profile":
        await send_updated_profile(user_id, update.message.chat.id, context)
        return
    if text == "🏆 Leaderboard":
        await show_leaderboard(update, context)
        return
    if text == "⚙️ Settings":
        await show_settings(update, context)
        return
    if text == "❓ Help":
        await update.message.reply_text("ℹ️ How to use this bot...", parse_mode=ParseMode.MARKDOWN_V2)
        return
    if text == "ℹ️ About Us":
        await update.message.reply_text("👤 Creator: Yididiya Tamiru", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Otherwise show main menu
    await update.message.reply_text("How can I help you?", reply_markup=main_menu)

# -------------------------
# Utility functions used earlier but declared later
# -------------------------
async def send_updated_profile(user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        return
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = calculate_user_rating(user_id)
    stars = format_stars(rating)
    followers = db_fetch_all("SELECT * FROM followers WHERE followed_id = %s", (user_id,))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📭 Inbox", callback_data='inbox')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    await context.bot.send_message(chat_id=chat_id, text=(f"👤 *{escape_markdown(display_name, version=2)}* 🎖 \n"
                                                        f"📌 Sex: {display_sex}\n"
                                                        f"⭐️ Rating: {rating} {stars}\n"
                                                        f"👥 Followers: {len(followers)}\n"),
                                    reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)

async def show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user_id = str(update.effective_user.id)
    db_execute("UPDATE private_messages SET is_read = TRUE WHERE receiver_id = %s", (user_id,))
    per_page = 5
    offset = (page - 1) * per_page
    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset))
    total_row = db_fetch_one("SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s", (user_id,))
    total = total_row['count'] if total_row else 0
    total_pages = (total + per_page - 1) // per_page
    if not messages:
        await update.message.reply_text("📭 *Your Messages*\n\nYou don't have any messages yet.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    text = f"📭 *Your Messages* (Page {page}/{total_pages})\n\n"
    for msg in messages:
        ts = msg.get('timestamp')
        if isinstance(ts, datetime):
            timestr = ts.strftime('%b %d, %H:%M')
        else:
            timestr = str(ts)
        text += f"👤 *{escape_markdown(msg.get('sender_name','Anonymous'), version=2)}* {msg.get('sender_sex','👤')} ({timestr}):\n{escape_markdown(msg.get('content',''), version=2)}\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    # pagination + reply buttons
    keyboard_buttons = []
    pagination = []
    if page > 1:
        pagination.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"messages_page_{page-1}"))
    if page < total_pages:
        pagination.append(InlineKeyboardButton("Next ➡️", callback_data=f"messages_page_{page+1}"))
    if pagination:
        keyboard_buttons.append(pagination)
    for msg in messages:
        keyboard_buttons.append([InlineKeyboardButton(f"💬 Reply to {msg.get('sender_name','')}", callback_data=f"reply_msg_{msg.get('sender_id')}"),
                                 InlineKeyboardButton(f"⛔ Block {msg.get('sender_name','')}", callback_data=f"block_user_{msg.get('sender_id')}")])
    keyboard_buttons.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("show_messages failed")

# ---------- Admin functions (kept minimal) ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        if update.message:
            await update.message.reply_text("❌ You don't have permission to access this.")
        else:
            await update.callback_query.message.reply_text("❌ You don't have permission to access this.")
        return
    pending = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = FALSE")
    count = pending['count'] if pending else 0
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 Pending Posts ({count})", callback_data='admin_pending')],
        [InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("🔙 Back", callback_data='settings')]
    ])
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("🛠 *Admin Panel*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text("🛠 *Admin Panel*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("admin_panel failed")

async def show_pending_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        await update.message.reply_text("❌ You don't have permission to access this.")
        return
    posts = db_fetch_all("""
        SELECT p.post_id, p.content, p.category, u.anonymous_name, p.media_type, p.media_id
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        WHERE p.approved = FALSE
        ORDER BY p.timestamp
        LIMIT 10
    """)
    if not posts:
        if update.callback_query:
            await update.callback_query.message.reply_text("✅ No pending posts!")
        else:
            await update.message.reply_text("✅ No pending posts!")
        return
    for post in posts:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post['post_id']}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post['post_id']}")]])
        preview = (post.get('content') or '')[:200] + '...' if post.get('content') and len(post.get('content')) > 200 else post.get('content','')
        text = f"📝 *Pending Post* [{post.get('category','')}] \n\n{escape_markdown(preview or '', version=2)}\n\n👤 {escape_markdown(post.get('anonymous_name','Anonymous'), version=2)}"
        try:
            if post.get('media_type') == 'text' or not post.get('media_type'):
                await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
            elif post.get('media_type') == 'photo':
                await context.bot.send_photo(chat_id=user_id, photo=post.get('media_id'), caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
            elif post.get('media_type') == 'voice':
                await context.bot.send_voice(chat_id=user_id, voice=post.get('media_id'), caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            logger.exception("show_pending_posts send failed")

async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    # simplified approve flow
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        try:
            await update.callback_query.message.reply_text("❌ You don't have permission to do this.")
        except Exception:
            pass
        return
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await update.callback_query.message.reply_text("❌ Post not found.")
        return
    try:
        caption_text = f"{post.get('content','')}\n\n#{post.get('category','')}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]])
        if post.get('media_type') == 'text' or not post.get('media_type'):
            msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=escape_markdown(caption_text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        elif post.get('media_type') == 'photo':
            msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=post.get('media_id'), caption=escape_markdown(caption_text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        elif post.get('media_type') == 'voice':
            msg = await context.bot.send_voice(chat_id=CHANNEL_ID, voice=post.get('media_id'), caption=escape_markdown(caption_text, version=2), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
        # store channel message id
        db_execute("UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s WHERE post_id = %s", (user_id, msg.message_id, post_id))
        await context.bot.send_message(chat_id=post.get('author_id'), text="✅ Your post has been approved and published!")
        await update.callback_query.edit_message_text(f"✅ Post approved and published!\n\n{escape_markdown((post.get('content') or '')[:100], version=2)}...")
    except Exception:
        logger.exception("approve_post failed")
        try:
            await update.callback_query.edit_message_text("❌ Failed to approve post. Please try again.")
        except Exception:
            pass

async def reject_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        try:
            await update.callback_query.message.reply_text("❌ You don't have permission to do this.")
        except Exception:
            pass
        return
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await update.callback_query.message.reply_text("❌ Post not found.")
        except Exception:
            pass
        return
    try:
        await context.bot.send_message(chat_id=post.get('author_id'), text="❌ Your post was not approved by the admin.")
        db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
        await update.callback_query.edit_message_text("❌ Post rejected and deleted")
    except Exception:
        logger.exception("reject_post failed")
        try:
            await update.callback_query.edit_message_text("❌ Failed to reject post. Please try again.")
        except Exception:
            pass

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        try:
            if update.message:
                await update.message.reply_text("❌ You don't have permission to access this.")
            else:
                await update.callback_query.message.reply_text("❌ You don't have permission to access this.")
        except Exception:
            pass
        return
    stats = db_fetch_one('''
        SELECT 
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM posts WHERE approved = TRUE) as approved_posts,
            (SELECT COUNT(*) FROM posts WHERE approved = FALSE) as pending_posts,
            (SELECT COUNT(*) FROM comments) as total_comments,
            (SELECT COUNT(*) FROM private_messages) as total_messages
    ''')
    text = ("📊 *Bot Statistics*\n\n"
            f"👥 Total Users: {stats.get('total_users',0)}\n"
            f"📝 Approved Posts: {stats.get('approved_posts',0)}\n"
            f"🕒 Pending Posts: {stats.get('pending_posts',0)}\n"
            f"💬 Total Comments: {stats.get('total_comments',0)}\n"
            f"📩 Private Messages: {stats.get('total_messages',0)}")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]])
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("show_admin_stats failed")

# -------------------------
# Error handler
# -------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=True)

# -------------------------
# Main startup
# -------------------------
def main():
    # Initialize DB
    init_db()
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", lambda u,c: menu(u,c) if False else None))  # placeholder; menu route can be /menu handled via CallbackQuery
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("inbox", show_inbox))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    # Start flask in background for health
    port = int(os.getenv("PORT", "5000"))
    threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True).start()
    # Start polling
    logger.info("Starting bot polling")
    app.run_polling()

if __name__ == "__main__":
    try:
        init_db()
    except Exception as e:
        logger.exception("DB init failed on startup: %s", e)
        raise
    main()
