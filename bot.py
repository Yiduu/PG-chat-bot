import os 
import logging
import psycopg2
from urllib.parse import quote
from psycopg2 import sql, IntegrityError, ProgrammingError
from psycopg2.extras import RealDictCursor
from pathlib import Path
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, 
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest
import threading
from flask import Flask, jsonify 
from contextlib import closing
from datetime import datetime
import random
import time
from typing import Optional

# Load environment variables first
load_dotenv()

# Initialize database connection
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv('TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_ID = os.getenv('ADMIN_ID')

# Initialize database tables with schema migration
def init_db():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as c:
                
                # ---------------- Create Tables ----------------
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
                    admin_approved_by TEXT,
                    thread_from_post_id BIGINT DEFAULT NULL
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

                # ---------------- Create admin user if specified ----------------
                if ADMIN_ID:
                    c.execute('''
                        INSERT INTO users (user_id, anonymous_name, is_admin)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE
                    ''', (ADMIN_ID, "Admin"))

            conn.commit()
        logging.info("PostgreSQL database initialized successfully")
    except Exception as e:
        logging.error(f"Database initialization failed: {e}")

# Database helper functions - FIXED VERSION
# -------------------- PostgreSQL Connection Pool --------------------
from psycopg2 import pool

# Create a global connection pool (reuses DB connections instead of reconnecting every time)
try:
    db_pool = pool.SimpleConnectionPool(
        1, 10,  # min 1, max 10 connections
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    logging.info("✅ Database connection pool created successfully")
except Exception as e:
    logging.error(f"❌ Failed to create database pool: {e}")
    db_pool = None


def db_execute(query, params=(), fetch=False, fetchone=False):
    """Execute a SQL query using the global connection pool."""
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch:
                result = cur.fetchall()
            elif fetchone:
                result = cur.fetchone()
            else:
                result = True
            conn.commit()
            return result
    except Exception as e:
        logging.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            db_pool.putconn(conn)


def db_fetch_one(query, params=()):
    return db_execute(query, params, fetchone=True)

def db_fetch_all(query, params=()):
    return db_execute(query, params, fetch=True)

# Categories
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

# Initialize Flask app for Render health checks
flask_app = Flask(__name__) 

@flask_app.route('/')
def health_check():
    return jsonify(status="OK", message="Christian Chat Bot is running") 

@flask_app.route('/ping')
def uptimerobot_ping():
    return jsonify(status="OK", message="Pong! Bot is alive") 

# Create main menu keyboard with improved buttons
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🌟 Share My Thoughts")],
        [KeyboardButton("👤 View Profile"), KeyboardButton("📚 My Previous Posts")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("❓ Help")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
) 

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__) 

def create_anonymous_name(user_id):
    try:
        uid_int = int(user_id)
    except ValueError:
        uid_int = abs(hash(user_id)) % 10000
    names = ["Anonymous", "Believer", "Christian", "Servant", "Disciple", "ChildOfGod"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def calculate_user_rating(user_id):
    post_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
        (user_id,)
    )
    post_count = post_row['count'] if post_row else 0
    
    comment_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
        (user_id,)
    )
    comment_count = comment_row['count'] if comment_row else 0
    
    return post_count + comment_count

def format_stars(rating, max_stars=5):
    full_stars = min(rating // 5, max_stars)
    empty_stars = max(0, max_stars - full_stars)
    return '⭐️' * full_stars + '☆' * empty_stars

def count_all_comments(post_id):
    def count_replies(parent_id=None):
        if parent_id is None:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE post_id = %s AND parent_comment_id = 0",
                (post_id,)
            )
        else:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE parent_comment_id = %s",
                (parent_id,)
            )
        
        total = len(comments)
        for comment in comments:
            total += count_replies(comment['comment_id'])
        return total
    
    return count_replies()

def get_display_name(user_data):
    if user_data and user_data.get('anonymous_name'):
        return user_data['anonymous_name']
    return "Anonymous"

def get_display_sex(user_data):
    if user_data and user_data.get('sex'):
        return user_data['sex']
    return '👤'

def get_user_rank(user_id):
    users = db_fetch_all('''
        SELECT user_id, 
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
    ''')
    
    for rank, user in enumerate(users, start=1):
        if user['user_id'] == user_id:
            return rank
    return None

async def update_channel_post_comment_count(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Update the comment count on the channel post"""
    try:
        # Get the post details
        post = db_fetch_one("SELECT channel_message_id, comment_count FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post['channel_message_id']:
            return
        
        # Count all comments for this post
        total_comments = count_all_comments(post_id)
        
        # Update the database with the new count
        db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (total_comments, post_id))
        
        # Update the channel message button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Try to edit the message in the channel
        await context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=post['channel_message_id'],
            reply_markup=keyboard
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Failed to update comment count in channel: {e}")
    except Exception as e:
        logger.error(f"Error updating channel post comment count: {e}")

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
        stars = format_stars(user['total'] // 5)
        leaderboard_text += (
            f"{idx}. {user['anonymous_name']} {user['sex']} - {user['total']} contributions {stars}\n"
        )
    
    user_id = str(update.effective_user.id)
    user_rank = get_user_rank(user_id)
    
    if user_rank and user_rank > 10:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = calculate_user_rating(user_id)
            leaderboard_text += (
                f"\n...\n"
                f"{user_rank}. {user_data['anonymous_name']} {user_data['sex']} - {user_contributions} contributions\n"
            )
    
    keyboard = [
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')]
    ]
    
    if update.message:
        await update.message.reply_text(
            leaderboard_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                leaderboard_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await update.callback_query.message.reply_text(
                leaderboard_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    try:
        user = db_fetch_one("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = %s", (user_id,))
        
        if not user:
            if update.message:
                await update.message.reply_text("Please use /start first to initialize your profile.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("Please use /start first to initialize your profile.")
            return
        
        notifications_status = "✅ ON" if user['notifications_enabled'] else "❌ OFF"
        privacy_status = "🌍 Public" if user['privacy_public'] else "🔒 Private"
        
        keyboard = [
            [
                InlineKeyboardButton(f"🔔 Notifications: {notifications_status}", 
                                   callback_data='toggle_notifications')
            ],
            [
                InlineKeyboardButton(f"👁‍🗨 Privacy: {privacy_status}", 
                                   callback_data='toggle_privacy')
            ],
            [
                InlineKeyboardButton("📱 Main Menu", callback_data='menu'),
                InlineKeyboardButton("👤 Profile", callback_data='profile')
            ]
        ]
        
        # Add admin panel button if user is admin
        if user['is_admin']:
            keyboard.insert(0, [InlineKeyboardButton("🛠 Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    "⚙️ *Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.callback_query.message.reply_text(
                    "⚙️ *Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text(
                "⚙️ *Settings Menu*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Error in show_settings: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading settings. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error loading settings. Please try again.")

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None, thread_from_post_id: int = None):
    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data='edit_post'),
            InlineKeyboardButton("❌ Cancel", callback_data='cancel_post')
        ],
        [
            InlineKeyboardButton("✅ Submit", callback_data='confirm_post')
        ]
    ]
    
    thread_text = ""
    if thread_from_post_id:
        thread_post = db_fetch_one("SELECT content FROM posts WHERE post_id = %s", (thread_from_post_id,))
        if thread_post:
            thread_preview = thread_post['content'][:100] + '...' if len(thread_post['content']) > 100 else thread_post['content']
            thread_text = f"🔄 *Threading from previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
    
    preview_text = (
        f"{thread_text}📝 *Post Preview* [{category}]\n\n"
        f"{escape_markdown(post_content, version=2)}\n\n"
        f"Please confirm your post:"
    )
    
    context.user_data['pending_post'] = {
        'content': post_content,
        'category': category,
        'media_type': media_type,
        'media_id': media_id,
        'thread_from_post_id': thread_from_post_id,
        'timestamp': time.time()
    }
    
    try:
        if update.callback_query:
            if media_type == 'text':
                await update.callback_query.edit_message_text(
                    preview_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                await update.callback_query.edit_message_caption(
                    caption=preview_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        else:
            if media_type == 'text':
                await update.message.reply_text(
                    preview_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                # For media posts, we need to resend the media with the confirmation
                if media_type == 'photo':
                    await update.message.reply_photo(
                        photo=media_id,
                        caption=preview_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                elif media_type == 'voice':
                    await update.message.reply_voice(
                        voice=media_id,
                        caption=preview_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
    except Exception as e:
        logger.error(f"Error in send_post_confirmation: {e}")
        if update.message:
            await update.message.reply_text("❌ Error showing confirmation. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error showing confirmation. Please try again.")

async def notify_user_of_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, comment_id: int, replier_id: str):
    try:
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        if not comment:
            return
        
        original_author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (comment['author_id'],))
        if not original_author or not original_author['notifications_enabled']:
            return
        
        replier = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (replier_id,))
        replier_name = get_display_name(replier)
        
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
        
        notification_text = (
            f"💬 {replier_name} replied to your comment:\n\n"
            f"🗨 {escape_markdown(comment['content'][:100], version=2)}\n\n"
            f"📝 Post: {escape_markdown(post_preview, version=2)}\n\n"
            f"[View conversation](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
        )
        
        await context.bot.send_message(
            chat_id=original_author['user_id'],
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Error sending reply notification: {e}")

async def notify_admin_of_new_post(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    if not ADMIN_ID:
        return
    
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        return
    
    author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (post['author_id'],))
    author_name = get_display_name(author)
    
    post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post_id}")
        ]
    ])
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🆕 New post awaiting approval from {author_name}:\n\n{post_preview}",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error notifying admin: {e}")

async def notify_user_of_private_message(context: ContextTypes.DEFAULT_TYPE, sender_id: str, receiver_id: str, message_content: str, message_id: int):
    try:
        # Check if receiver has blocked the sender
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (receiver_id, sender_id)
        )
        if is_blocked:
            return  # Don't notify if blocked
        
        receiver = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (receiver_id,))
        if not receiver or not receiver['notifications_enabled']:
            return
        
        sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
        sender_name = get_display_name(sender)
        
        # Truncate long messages for the notification
        preview_content = message_content[:100] + '...' if len(message_content) > 100 else message_content
        
        notification_text = (
            f"📩 *New Private Message*\n\n"
            f"👤 From: {escape_markdown(sender_name, version=2)}\n\n"
            f"💬 {escape_markdown(preview_content, version=2)}\n\n"
            f"💭 _Use /inbox to view all messages_"
        )
        
        # Create inline keyboard with reply and block buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 Reply", callback_data=f"reply_msg_{sender_id}"),
                InlineKeyboardButton("⛔ Block", callback_data=f"block_user_{sender_id}")
            ]
        ])
        
        await context.bot.send_message(
            chat_id=receiver_id,
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error sending private message notification: {e}")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("❌ You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ You don't have permission to access this.")
        return
    
    pending_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = FALSE")
    pending_count = pending_posts['count'] if pending_posts else 0
    
    keyboard = [
        [InlineKeyboardButton(f"📝 Pending Posts ({pending_count})", callback_data='admin_pending')],
        [InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("👥 User Management", callback_data='admin_users')],
        [InlineKeyboardButton("📢 Broadcast", callback_data='admin_broadcast')],
        [InlineKeyboardButton("🔙 Back", callback_data='settings')]
    ]
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "🛠 *Admin Panel*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "🛠 *Admin Panel*",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error in admin_panel: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading admin panel.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error loading admin panel.")

async def show_pending_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("❌ You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ You don't have permission to access this.")
        return
    
    # Get pending posts
    posts = db_fetch_all("""
        SELECT p.post_id, p.content, p.category, u.anonymous_name, p.media_type, p.media_id
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        WHERE p.approved = FALSE
        ORDER BY p.timestamp
    """)
    
    if not posts:
        if update.callback_query:
            await update.callback_query.message.reply_text("✅ No pending posts!")
        else:
            await update.message.reply_text("✅ No pending posts!")
        return
    
    # Send each pending post to admin
    for post in posts[:10]:  # Limit to 10 posts to avoid flooding
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_post_{post['post_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_post_{post['post_id']}")
            ]
        ])
        
        preview = post['content'][:200] + '...' if len(post['content']) > 200 else post['content']
        text = f"📝 *Pending Post* [{post['category']}]\n\n{preview}\n\n👤 {post['anonymous_name']}"
        
        try:
            if post['media_type'] == 'text':
                if update.callback_query:
                    await update.callback_query.message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
            elif post['media_type'] == 'photo':
                if update.callback_query:
                    await update.callback_query.message.reply_photo(
                        photo=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_photo(
                        photo=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
            elif post['media_type'] == 'voice':
                if update.callback_query:
                    await update.callback_query.message.reply_voice(
                        voice=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_voice(
                        voice=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
        except Exception as e:
            logger.error(f"Error sending pending post {post['post_id']}: {e}")
            # Send as text if media fails
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    f"❌ Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(
                    f"❌ Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )

async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        try:
            await query.answer("❌ You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("❌ You don't have permission to do this.")
        return
    
    # Get the post
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await query.answer("❌ Post not found.", show_alert=True)
        except:
            await query.edit_message_text("❌ Post not found.")
        return
    
    try:
        # Format the post content for the channel
        hashtag = f"#{post['category']}"
        caption_text = (
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
        # Create the comments button
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Send post to channel based on media type
        if post['media_type'] == 'text':
            msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
        elif post['media_type'] == 'photo':
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
        elif post['media_type'] == 'voice':
            msg = await context.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
        else:
            await query.answer("❌ Unsupported media type.", show_alert=True)
            return
        
        # Update the post in database
        success = db_execute(
            "UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s WHERE post_id = %s",
            (user_id, msg.message_id, post_id)
        )
        
        if not success:
            await query.answer("❌ Failed to update database.", show_alert=True)
            return
        
        # Notify the author
        try:
            await context.bot.send_message(
                chat_id=post['author_id'],
                text="✅ Your post has been approved and published!"
            )
        except Exception as e:
            logger.error(f"Error notifying author: {e}")
        
        # Update the admin's message
        try:
            await query.edit_message_text(
                f"✅ Post approved and published!\n\n{post['content'][:100]}...",
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            await query.message.reply_text(
                f"✅ Post approved and published!\n\n{post['content'][:100]}...",
                parse_mode=ParseMode.MARKDOWN
            )
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        try:
            await query.answer(f"❌ Failed to approve post: {str(e)}", show_alert=True)
        except:
            await query.edit_message_text("❌ Failed to approve post. Please try again.")

async def reject_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        try:
            await query.answer("❌ You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("❌ You don't have permission to do this.")
        return
    
    # Get the post
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await query.answer("❌ Post not found.", show_alert=True)
        except:
            await query.edit_message_text("❌ Post not found.")
        return
    
    try:
        # Notify the author
        try:
            await context.bot.send_message(
                chat_id=post['author_id'],
                text="❌ Your post was not approved by the admin."
            )
        except Exception as e:
            logger.error(f"Error notifying author: {e}")
        
        # Delete the post from database
        success = db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
        
        if not success:
            await query.answer("❌ Failed to delete post from database.", show_alert=True)
            return
        
        # Update the admin's message
        try:
            await query.edit_message_text("❌ Post rejected and deleted")
        except BadRequest:
            await query.message.reply_text("❌ Post rejected and deleted")
        
    except Exception as e:
        logger.error(f"Error rejecting post: {e}")
        try:
            await query.answer(f"❌ Failed to reject post: {str(e)}", show_alert=True)
        except:
            await query.edit_message_text("❌ Failed to reject post. Please try again.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Check if user exists and create if not - FIXED
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        anon = create_anonymous_name(user_id)
        # FIXED: Properly set is_admin based on ADMIN_ID comparison
        is_admin = str(user_id) == str(ADMIN_ID)
        success = db_execute(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        )
        if not success:
            await update.message.reply_text("❌ Error creating user profile. Please try again.")
            return
    
    args = context.args

    if args:
        arg = args[0]

        if arg.startswith("comments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                await show_comments_menu(update, context, post_id, page=1)
            return

        elif arg.startswith("viewcomments_"):
            parts = arg.split("_")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                post_id = int(parts[1])
                page = int(parts[2])
                await show_comments_page(update, context, post_id, page)
            return

        elif arg.startswith("writecomment_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s",
                    (post_id, user_id)
                )
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                preview_text = "Original content not found"
                if post:
                    content = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                
                await update.message.reply_text(
                    f"{preview_text}\n\n✍️ Please type your comment:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
        
        # FIXED: Use profileid_ with user_id instead of profile_ with name
        elif arg.startswith("profileid_"):
            target_user_id = arg.split("_", 1)[1]
            
            user_data = db_fetch_one(
                "SELECT * FROM users WHERE user_id = %s",
                (target_user_id,)
            )
            
            if user_data:
                followers = db_fetch_all(
                    "SELECT * FROM followers WHERE followed_id = %s",
                    (user_data['user_id'],)
                )
                
                rating = calculate_user_rating(user_data['user_id'])
                stars = format_stars(rating)
                current_user_id = user_id
                btn = []
                
                # Follow / Unfollow buttons
                if user_data['user_id'] != current_user_id:
                    is_following = db_fetch_one(
                        "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
                        (current_user_id, user_data['user_id'])
                    )
                    
                    if is_following:
                        btn.append([
                            InlineKeyboardButton(
                                "🚫 Unfollow",
                                callback_data=f'unfollow_{user_data["user_id"]}'
                            )
                        ])
                        btn.append([
                            InlineKeyboardButton(
                                "✉️ Send Message",
                                callback_data=f'message_{user_data["user_id"]}'
                            )
                        ])
                    else:
                        btn.append([
                            InlineKeyboardButton(
                                "🫂 Follow",
                                callback_data=f'follow_{user_data["user_id"]}'
                            )
                        ])
                
                display_name = get_display_name(user_data)
                display_sex = get_display_sex(user_data)
                
                await update.message.reply_text(
                    f"👤 *{display_name}* 🎖 \n"
                    f"📌 Sex: {display_sex}\n\n"
                    f"👥 Followers: {len(followers)}\n"
                    f"🎖 Batch: User\n"
                    f"⭐️ Contributions: {rating} {stars}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                    f"_Use /menu to return_",
                    reply_markup=InlineKeyboardMarkup(btn) if btn else None,
                    parse_mode=ParseMode.MARKDOWN
                )
                return
        
        elif arg == "inbox":
            await show_inbox(update, context)
            return
    
    # Show main menu with improved buttons
    keyboard = [
        [
            InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile", callback_data='profile')
        ],
        [
            InlineKeyboardButton("📚 My Previous Posts", callback_data='previous_posts'),
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard')
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
            InlineKeyboardButton("❓ Help", callback_data='help')
        ]
    ]
    
    await update.message.reply_text(
        "🌟✝️ *እንኳን ወደ Christian vent በሰላም መጡ* ✝️🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "ማንነታችሁ ሳይገለጽ ሃሳባችሁን ማጋራት ትችላላችሁ.\n\n የሚከተሉትን ምረጁ :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    
    await update.message.reply_text(
        "You can use the buttons below to navigate:",
        reply_markup=main_menu
    )

async def show_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Get unread messages count
    unread_count_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE",
        (user_id,)
    )
    unread_count = unread_count_row['count'] if unread_count_row else 0
    
    # Get recent messages
    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT 10
    ''', (user_id,))
    
    if not messages:
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(
                "📭 *Your Inbox*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(
                "📭 *Your Inbox*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    inbox_text = f"📭 *Your Inbox* ({unread_count} unread)\n\n"
    
    for msg in messages:
        status = "🔵" if not msg['is_read'] else "⚪️"
        # Handle timestamp whether it's string or datetime object
        if isinstance(msg['timestamp'], str):
            timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d')
        else:
            timestamp = msg['timestamp'].strftime('%b %d')
        preview = msg['content'][:30] + '...' if len(msg['content']) > 30 else msg['content']
        inbox_text += f"{status} *{msg['sender_name']}* {msg['sender_sex']} - {preview} ({timestamp})\n"
    
    keyboard = [
        [InlineKeyboardButton("📝 View Messages", callback_data='view_messages')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(
            inbox_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    elif hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.message.reply_text(
            inbox_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_id = str(update.effective_user.id)
    
    # Mark messages as read when viewing
    db_execute(
        "UPDATE private_messages SET is_read = TRUE WHERE receiver_id = %s",
        (user_id,)
    )
    
    # Get messages with pagination
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
    
    total_messages_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s",
        (user_id,)
    )
    total_messages = total_messages_row['count'] if total_messages_row else 0
    total_pages = (total_messages + per_page - 1) // per_page
    
    if not messages:
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(
                "📭 *Your Messages*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(
                "📭 *Your Messages*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    messages_text = f"📭 *Your Messages* (Page {page}/{total_pages})\n\n"
    
    for msg in messages:
        # Handle timestamp whether it's string or datetime object
        if isinstance(msg['timestamp'], str):
            timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d, %H:%M')
        else:
            timestamp = msg['timestamp'].strftime('%b %d, %H:%M')
        messages_text += f"👤 *{msg['sender_name']}* {msg['sender_sex']} ({timestamp}):\n"
        messages_text += f"{escape_markdown(msg['content'], version=2)}\n\n"
        messages_text += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Build keyboard with pagination and reply options
    keyboard_buttons = []
    
    # Pagination buttons
    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"messages_page_{page-1}"))
    if page < total_pages:
        pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"messages_page_{page+1}"))
    if pagination_row:
        keyboard_buttons.append(pagination_row)
    
    # Reply and block buttons for each message
    for msg in messages:
        keyboard_buttons.append([
            InlineKeyboardButton(f"💬 Reply to {msg['sender_name']}", callback_data=f"reply_msg_{msg['sender_id']}"),
            InlineKeyboardButton(f"⛔ Block {msg['sender_name']}", callback_data=f"block_user_{msg['sender_id']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                messages_text,
                reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text(
                    messages_text,
                    reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
    except Exception as e:
        logger.error(f"Error showing messages: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Error loading messages. Please try again.")

async def show_comments_menu(update, context, post_id, page=1):
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Post not found.", reply_markup=main_menu)
        return

    comment_count = count_all_comments(post_id)
    keyboard = [
        [
            InlineKeyboardButton(f"👁 View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_{page}"),
            InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")
        ]
    ]

    post_text = post['content']
    escaped_text = escape_markdown(post_text, version=2)

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(
            f"💬\n{escaped_text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def show_comments_page(update, context, post_id, page=1, reply_pages=None):
    if update.effective_chat is None:
        logger.error("Cannot determine chat from update: %s", update)
        return
    chat_id = update.effective_chat.id

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

    # CHANGED: Only show comments, not post content
    header = "💬 *Comments*\n\n"

    if not comments and page == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text=header + "\\_No comments yet.\\_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu
        )
        return

    header_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=header,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu
    )
    header_message_id = header_msg.message_id

    user_id = str(update.effective_user.id)

    if reply_pages is None:
        reply_pages = {}

    for idx, comment in enumerate(comments):
        commenter_id = comment['author_id']
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (commenter_id,))
        display_sex = get_display_sex(commenter)
        display_name = get_display_name(commenter)
        
        rating = calculate_user_rating(commenter_id)
        stars = format_stars(rating)
        
        # FIXED: Use profileid_ with user_id instead of profile_ with name
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{commenter_id}"

        # FIXED: Proper reaction counting
        likes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'",
            (comment['comment_id'],)
        )
        likes = likes_row['cnt'] if likes_row else 0
        
        dislikes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'",
            (comment['comment_id'],)
        )
        dislikes = dislikes_row['cnt'] if dislikes_row else 0

        user_reaction = db_fetch_one(
            "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
            (comment['comment_id'], user_id)
        )

        like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
        dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

        comment_text = escape_markdown(comment['content'], version=2)
        
        # FIXED: Use proper markdown link with user_id-based profile link
        author_text = (
            f"[{escape_markdown(display_name, version=2)}]({profile_link}) "
            f"{display_sex} {stars}"
        )

        # NEW: Add edit and delete buttons for comment author
        kb_buttons = [
            [
                InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment['comment_id']}"),
                InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment['comment_id']}"),
                InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")
            ]
        ]
        
        # Add edit/delete buttons only for comment author
        if comment['author_id'] == user_id:
            kb_buttons.append([
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment['comment_id']}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment['comment_id']}")
            ])

        kb = InlineKeyboardMarkup(kb_buttons)

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{comment_text}\n\n{author_text}",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=header_message_id,
            disable_web_page_preview=True  # ADDED: Disable link previews
        )

        # Recursive function to display replies under this comment
        MAX_REPLY_DEPTH = 6  # avoid infinite nesting

        async def send_replies_recursive(parent_comment_id, parent_msg_id, depth=1):
            if depth > MAX_REPLY_DEPTH:
                return
            children = db_fetch_all(
                "SELECT * FROM comments WHERE parent_comment_id = %s ORDER BY timestamp",
                (parent_comment_id,)
            )
            for child in children:
                reply_user_id = child['author_id']
                reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (reply_user_id,))
                reply_display_name = get_display_name(reply_user)
                reply_display_sex = get_display_sex(reply_user)
                rating_reply = calculate_user_rating(reply_user_id)
                stars_reply = format_stars(rating_reply)
                
                # FIXED: Use profileid_ with user_id for replies too
                reply_profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply_user_id}"
                safe_reply = escape_markdown(child['content'], version=2)

                # FIXED: Proper reaction counting for replies
                reply_likes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'",
                    (child['comment_id'],)
                )
                reply_likes = reply_likes_row['cnt'] if reply_likes_row else 0
                
                reply_dislikes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'",
                    (child['comment_id'],)
                )
                reply_dislikes = reply_dislikes_row['cnt'] if reply_dislikes_row else 0

                reply_user_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (child['comment_id'], user_id)
                )

                reply_like_emoji = "👍" if reply_user_reaction and reply_user_reaction['type'] == 'like' else "👍"
                reply_dislike_emoji = "👎" if reply_user_reaction and reply_user_reaction['type'] == 'dislike' else "👎"

                # NEW: Add edit and delete buttons for reply author
                reply_kb_buttons = [
                    [
                        InlineKeyboardButton(f"{reply_like_emoji} {reply_likes}", callback_data=f"likereply_{child['comment_id']}"),
                        InlineKeyboardButton(f"{reply_dislike_emoji} {reply_dislikes}", callback_data=f"dislikereply_{child['comment_id']}"),
                        InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{child['comment_id']}")
                    ]
                ]
                
                # Add edit/delete buttons only for reply author
                if child['author_id'] == user_id:
                    reply_kb_buttons.append([
                        InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{child['comment_id']}"),
                        InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{child['comment_id']}")
                    ])

                reply_kb = InlineKeyboardMarkup(reply_kb_buttons)

                # Send this reply under its parent message
                child_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{safe_reply}\n\n[{escape_markdown(reply_display_name, version=2)}]({reply_profile_link}) {reply_display_sex} {stars_reply}",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_to_message_id=parent_msg_id,
                    reply_markup=reply_kb,
                    disable_web_page_preview=True  # ADDED: Disable link previews
                )

                # Recursively show this child's own replies
                await send_replies_recursive(child['comment_id'], child_msg.message_id, depth + 1)

        # Start recursion for this top-level comment
        await send_replies_recursive(comment['comment_id'], msg.message_id, depth=1)

    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"viewcomments_{post_id}_{page-1}"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"viewcomments_{post_id}_{page+1}"))
    if pagination_buttons:
        pagination_markup = InlineKeyboardMarkup([pagination_buttons])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📄 Page {page}/{total_pages}",
            reply_markup=pagination_markup,
            reply_to_message_id=header_message_id,
            disable_web_page_preview=True  # ADDED: Disable link previews
        )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile", callback_data='profile')
        ],
        [
            InlineKeyboardButton("📚 My Previous Posts", callback_data='previous_posts'),
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard')
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
            InlineKeyboardButton("❓ Help", callback_data='help')
        ]
    ]
    
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(
            "📱 *Main Menu*\nChoose an option below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
        await update.message.reply_text(
            "You can also use these buttons:",
            reply_markup=main_menu
        )
    elif hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.message.reply_text(
            "📱 *Main Menu*\nChoose an option below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
        await update.callback_query.message.reply_text(
            "You can also use these buttons:",
            reply_markup=main_menu
        )

async def send_updated_profile(user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        return
    
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = calculate_user_rating(user_id)
    stars = format_stars(rating)
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = %s",
        (user_id,)
    )
    
    # UPDATED: Changed "My Vent" to "My Previous Posts"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📚 My Previous Posts", callback_data='previous_posts')],
        [InlineKeyboardButton("📭 Inbox", callback_data='inbox')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤 *{display_name}* 🎖 \n"
            f"📌 Sex: {display_sex}\n"
            f"⭐️ Rating: {rating} {stars}\n"
            f"🎖 Batch: User\n"
            f"👥 Followers: {len(followers)}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"_Use /menu to return_"
        ),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN)

# UPDATED: Function to show user's previous posts (renamed from show_my_vent)
async def show_previous_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_id = str(update.effective_user.id)
    
    per_page = 5
    offset = (page - 1) * per_page
    
    # Get user's posts with pagination
    posts = db_fetch_all(
        "SELECT * FROM posts WHERE author_id = %s AND approved = TRUE ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    )
    
    total_posts_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
        (user_id,)
    )
    total_posts = total_posts_row['count'] if total_posts_row else 0
    total_pages = (total_posts + per_page - 1) // per_page
    
    if not posts:
        text = "📚 *My Previous Posts*\n\nYou haven't posted anything yet or your posts are pending approval."
        keyboard = [
            [InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
    else:
        # FIXED: Use proper markdown escaping for page numbers
        text = f"📚 *My Previous Posts* \\- Page {page}/{total_pages}\n\n"
        for post in posts:
            post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
            # FIXED: Properly escape category and post content
            escaped_category = escape_markdown(post['category'], version=2)
            escaped_preview = escape_markdown(post_preview, version=2)
            text += f"📄 *{escaped_category}*:\n{escaped_preview}\n"
            text += f"💬 Comments: {count_all_comments(post['post_id'])}\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        keyboard = []
        
        # Pagination buttons
        pagination_row = []
        if page > 1:
            pagination_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"previous_posts_{page-1}"))
        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"previous_posts_{page+1}"))
        if pagination_row:
            keyboard.append(pagination_row)
        
        # Action buttons for each post
        for post in posts:
            keyboard.append([
                InlineKeyboardButton(f"💬 View Comments", callback_data=f"viewcomments_{post['post_id']}_1"),
                InlineKeyboardButton(f"➕ Continue This Post", callback_data=f"continue_post_{post['post_id']}")
            ])
        
        keyboard.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
    except Exception as e:
        logger.error(f"Error showing previous posts: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Error loading your posts. Please try again.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Error answering callback query: {e}")
    
    user_id = str(query.from_user.id)

    try:
        if query.data == 'ask':
            await query.message.reply_text(
                "📚 *Choose a category:*",
                reply_markup=build_category_buttons(),
                parse_mode=ParseMode.MARKDOWN
            )

        elif query.data.startswith('category_'):
            category = query.data.split('_', 1)[1]
            db_execute(
                "UPDATE users SET waiting_for_post = TRUE, selected_category = %s WHERE user_id = %s",
                (category, user_id)
            )

            await query.message.reply_text(
                f"✍️ *Please type your thought for #{category}:*\n\nYou may also send a photo or voice message.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=True))
        
        elif query.data == 'menu':
            keyboard = [
                [
                    InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
                    InlineKeyboardButton("👤 View Profile", callback_data='profile')
                ],
                [
                    InlineKeyboardButton("📚 My Previous Posts", callback_data='previous_posts'),
                    InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard')
                ],
                [
                    InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
                    InlineKeyboardButton("❓ Help", callback_data='help')
                ]
            ]
            try:
                await query.message.edit_text(
                    "📱 *Main Menu*\nChoose an option below:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await query.message.reply_text(
                    "📱 *Main Menu*\nChoose an option below:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )    

        elif query.data == 'profile':
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data == 'leaderboard':
            await show_leaderboard(update, context)

        elif query.data == 'settings':
            await show_settings(update, context)

        elif query.data == 'toggle_notifications':
            current = db_fetch_one("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,))
            if current:
                new_value = not current['notifications_enabled']
                db_execute(
                    "UPDATE users SET notifications_enabled = %s WHERE user_id = %s",
                    (new_value, user_id)
                )
            await show_settings(update, context)
        
        elif query.data == 'toggle_privacy':
            current = db_fetch_one("SELECT privacy_public FROM users WHERE user_id = %s", (user_id,))
            if current:
                new_value = not current['privacy_public']
                db_execute(
                    "UPDATE users SET privacy_public = %s WHERE user_id = %s",
                    (new_value, user_id)
                )
            await show_settings(update, context)

        elif query.data == 'help':
            help_text = (
                "ℹ️ *የዚህ ቦት አጠቃቀም:*\n"
                "•  menu button በመጠቀም የተለያዩ አማራጮችን ማየት ይችላሉ.\n"
                "• 'Share My Thoughts' የሚለውን በመንካት በፈለጉት ነገር ጥያቄም ሆነ ሃሳብ መጻፍ ይችላሉ.\n"
                "•  category ወይም መደብ በመምረጥ በ ጽሁፍ፣ ፎቶ እና ድምጽ ሃሳቦን ማንሳት ይችላሉ.\n"
                "• እርስዎ ባነሱት ሃሳብ ላይ ሌሎች ሰዎች አስተያየት መጻፍ ይችላሉ\n"
                "• View your profile የሚለውን በመንካት ስም፣ ጾታዎን መቀየር እንዲሁም እርስዎን የሚከተሉ ሰዎች ብዛት ማየት ይችላሉ.\n"
                "• በተነሱ ጥያቄዎች ላይ ከቻናሉ comments የሚለድን በመጫን አስተያየትዎን መጻፍ ይችላሉ."
            )
            keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
            await query.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'about':
            about_text = (
                "👤 Creator: Yididiya Tamiru\n\n"
                "🔗 Telegram: @YIDIDIYATAMIRUU\n"
                "🙏 This bot helps you share your thoughts anonymously with the Christian community."
            )
            keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
            await query.message.reply_text(about_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'edit_name':
            db_execute(
                "UPDATE users SET awaiting_name = TRUE WHERE user_id = %s",
                (user_id,)
            )
            await query.message.reply_text("✏️ Please type your new anonymous name:", parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'edit_sex':
            btns = [
                [InlineKeyboardButton("👨 Male", callback_data='sex_male')],
                [InlineKeyboardButton("👩 Female", callback_data='sex_female')]
            ]
            await query.message.reply_text("⚧️ Select your sex:", reply_markup=InlineKeyboardMarkup(btns))

        elif query.data.startswith('sex_'):
            if query.data == 'sex_male':
                sex = '👨'
            elif query.data == 'sex_female':
                sex = '👩'
            else:
                sex = '👤'  # fallback
            
            db_execute(
                "UPDATE users SET sex = %s WHERE user_id = %s",
                (sex, user_id)
            )
            await query.message.reply_text("✅ Sex updated!")
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data.startswith(('follow_', 'unfollow_')):
            target_uid = query.data.split('_', 1)[1]
            if query.data.startswith('follow_'):
                try:
                    db_execute(
                        "INSERT INTO followers (follower_id, followed_id) VALUES (%s, %s)",
                        (user_id, target_uid)
                    )
                except psycopg2.IntegrityError:
                    pass
            else:
                db_execute(
                    "DELETE FROM followers WHERE follower_id = %s AND followed_id = %s",
                    (user_id, target_uid)
                )
            await query.message.reply_text("✅ Successfully updated!")
            await send_updated_profile(target_uid, query.message.chat.id, context)
        
        elif query.data.startswith('viewcomments_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                    post_id = int(parts[1])
                    page = int(parts[2])
                    await show_comments_page(update, context, post_id, page)
            except Exception as e:
                logger.error(f"ViewComments error: {e}")
                await query.answer("❌ Error loading comments")
  
        elif query.data.startswith('writecomment_'):
            post_id_str = query.data.split('_', 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s",
                    (post_id, user_id)
                )
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                preview_text = "Original content not found"
                if post:
                    content = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n✍️ Please type your comment:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )

        # FIXED: Like/Dislike reaction handling
        elif query.data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
            try:
                parts = query.data.split('_')
                comment_id = int(parts[1])
                reaction_type = 'like' if parts[0] in ('likecomment', 'likereply') else 'dislike'

                # Check if user already has a reaction on this comment
                existing_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                )

                if existing_reaction:
                    if existing_reaction['type'] == reaction_type:
                        # User is clicking the same reaction - remove it (toggle off)
                        db_execute(
                            "DELETE FROM reactions WHERE comment_id = %s AND user_id = %s",
                            (comment_id, user_id)
                        )
                    else:
                        # User is changing reaction - update it
                        db_execute(
                            "UPDATE reactions SET type = %s WHERE comment_id = %s AND user_id = %s",
                            (reaction_type, comment_id, user_id)
                        )
                else:
                    # User is adding a new reaction
                    db_execute(
                        "INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)",
                        (comment_id, user_id, reaction_type)
                    )

                # Get updated counts
                likes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'like'",
                    (comment_id,)
                )
                likes = likes_row['cnt'] if likes_row else 0
                
                dislikes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type = 'dislike'",
                    (comment_id,)
                )
                dislikes = dislikes_row['cnt'] if dislikes_row else 0

                comment = db_fetch_one(
                    "SELECT post_id, parent_comment_id, author_id FROM comments WHERE comment_id = %s",
                    (comment_id,)
                )
                if not comment:
                    await query.answer("Comment not found", show_alert=True)
                    return

                post_id = comment['post_id']
                parent_comment_id = comment['parent_comment_id']

                # Get user's current reaction after update
                user_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                )

                like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
                dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

                if parent_comment_id == 0:
                    # Build keyboard with edit/delete buttons for author
                    kb_buttons = [
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment_id}")
                        ]
                    ]
                    
                    # Add edit/delete buttons only for comment author
                    if comment['author_id'] == user_id:
                        kb_buttons.append([
                            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                        ])
                    
                    new_kb = InlineKeyboardMarkup(kb_buttons)
                else:
                    # Build keyboard for replies with edit/delete buttons for author
                    kb_buttons = [
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likereply_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikereply_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{comment_id}")
                        ]
                    ]
                    
                    # Add edit/delete buttons only for reply author
                    if comment['author_id'] == user_id:
                        kb_buttons.append([
                            InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                        ])
                    
                    new_kb = InlineKeyboardMarkup(kb_buttons)

                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        reply_markup=new_kb
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        logger.error(f"Error updating reaction buttons: {e}")
                
                # Send notification only if reaction was added (not removed)
                if not existing_reaction or existing_reaction['type'] != reaction_type:
                    comment_author = db_fetch_one(
                        "SELECT user_id, notifications_enabled FROM users WHERE user_id = %s",
                        (comment['author_id'],)
                    )
                    if comment_author and comment_author['notifications_enabled'] and comment_author['user_id'] != user_id:
                        reactor_name = get_display_name(
                            db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                        )
                        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
                        
                        notification_text = (
                            f"❤️ {reactor_name} reacted to your comment:\n\n"
                            f"🗨 {escape_markdown(comment['content'][:100], version=2)}\n\n"
                            f"📝 Post: {escape_markdown(post_preview, version=2)}\n\n"
                            f"[View conversation](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
                        )
                        
                        await context.bot.send_message(
                            chat_id=comment_author['user_id'],
                            text=notification_text,
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
            except Exception as e:
                logger.error(f"Error processing reaction: {e}")
                await query.answer("❌ Error updating reaction", show_alert=True)

        # NEW: Handle edit comment
        elif query.data.startswith("edit_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
            
            if comment and comment['author_id'] == user_id:
                context.user_data['editing_comment'] = comment_id
                await query.message.reply_text(
                    f"✏️ *Editing your comment:*\n\n{escape_markdown(comment['content'], version=2)}\n\nPlease type your new comment:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                await query.answer("❌ You can only edit your own comments", show_alert=True)

        # NEW: Handle delete comment
        elif query.data.startswith("delete_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
            
            if comment and comment['author_id'] == user_id:
                # Get post_id before deleting for updating comment count
                post_id = comment['post_id']
                
                # Delete the comment and its reactions
                db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment_id,))
                db_execute("DELETE FROM comments WHERE comment_id = %s", (comment_id,))
                
                await query.answer("✅ Comment deleted")
                await query.message.delete()
                
                # Update comment count
                await update_channel_post_comment_count(context, post_id)
            else:
                await query.answer("❌ You can only delete your own comments", show_alert=True)
                
        elif query.data.startswith("reply_"):
            parts = query.data.split("_")
            if len(parts) == 3:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s",
                    (post_id, comment_id, user_id)
                )
                
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
                preview_text = "Original comment not found"
                if comment:
                    content = comment['content'][:100] + '...' if len(comment['content']) > 100 else comment['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n↩️ Please type your *reply*:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                
        elif query.data.startswith("replytoreply_"):
            parts = query.data.split("_")
            if len(parts) == 4:
                post_id = int(parts[1])
                # parts[2] is the immediate parent id (not needed for storage)
                comment_id = int(parts[3])   # this is the comment/reply the user is replying TO
                # Store the exact comment id being replied to in comment_idx
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s",
                    (post_id, comment_id, user_id)
                )
        
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
                preview_text = "Original reply not found"
                if comment:
                    content = comment['content'][:100] + '...' if len(comment['content']) > 100 else comment['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
        
                await query.message.reply_text(
                    f"{preview_text}\n\n↩️ Please type your *reply*:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )

        # UPDATED: Handle Previous Posts pagination
        elif query.data.startswith("previous_posts_"):
            try:
                page = int(query.data.split('_')[2])
                await show_previous_posts(update, context, page)
            except (IndexError, ValueError):
                await show_previous_posts(update, context, 1)

        # UPDATED: Handle Previous Posts button
        elif query.data == 'previous_posts':
            await show_previous_posts(update, context, 1)

        # UPDATED: Handle continue post (threading) - renamed from elaborate
        elif query.data.startswith("continue_post_"):
            post_id = int(query.data.split('_')[2])
            post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
            
            if post and post['author_id'] == user_id:
                context.user_data['thread_from_post_id'] = post_id
                await query.message.reply_text(
                    "📚 *Choose a category for your continuation:*",
                    reply_markup=build_category_buttons(),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.answer("❌ You can only continue your own posts", show_alert=True)
        
        elif query.data.startswith("replypage_"):
            parts = query.data.split("_")
            if len(parts) == 5:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                reply_page = int(parts[3])
                comment_page = int(parts[4])
                await show_comments_page(update, context, post_id, comment_page, reply_pages={comment_id: reply_page})
            return

        elif query.data in ('edit_post', 'cancel_post', 'confirm_post'):
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.message.edit_text("❌ Post data not found. Please start over.")
                return
            
            if query.data == 'edit_post':
                if time.time() - pending_post.get('timestamp', 0) > 300:
                    await query.message.edit_text("❌ Edit time expired. Please start a new post.")
                    del context.user_data['pending_post']
                    return
                    
                await query.message.edit_text(
                    "✏️ Please edit your post:",
                    reply_markup=ForceReply(selective=True)
                )
                return
            
            elif query.data == 'cancel_post':
                await query.message.edit_text("❌ Post cancelled.")
                if 'pending_post' in context.user_data:
                    del context.user_data['pending_post']
                if 'thread_from_post_id' in context.user_data:
                    del context.user_data['thread_from_post_id']
                return
            
            elif query.data == 'confirm_post':
                category = pending_post['category']
                post_content = pending_post['content']
                media_type = pending_post.get('media_type', 'text')
                media_id = pending_post.get('media_id')
                thread_from_post_id = pending_post.get('thread_from_post_id')
                
                # Insert post with thread reference if available
                if thread_from_post_id:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, category, media_type, media_id, thread_from_post_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, category, media_type, media_id, thread_from_post_id),
                        fetchone=True
                    )
                else:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, category, media_type, media_id) VALUES (%s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, category, media_type, media_id),
                        fetchone=True
                    )
                
                # Clean up user data
                if 'pending_post' in context.user_data:
                    del context.user_data['pending_post']
                if 'thread_from_post_id' in context.user_data:
                    del context.user_data['thread_from_post_id']
                
                if post_row:
                    post_id = post_row['post_id']
                    await notify_admin_of_new_post(context, post_id)
                    
                    await query.message.edit_text(
                        "✅ Your post has been submitted for admin approval!\n"
                        "You'll be notified when it's approved and published."
                    )
                    await query.message.reply_text(
                        "What would you like to do next?",
                        reply_markup=main_menu
                    )
                else:
                    await query.message.edit_text("❌ Failed to submit post. Please try again.")
                return

        elif query.data == 'admin_panel':
            await admin_panel(update, context)
            
        elif query.data == 'admin_pending':
            await show_pending_posts(update, context)
            
        elif query.data == 'admin_stats':
            await show_admin_stats(update, context)
            
        elif query.data.startswith('approve_post_'):
            try:
                post_id = int(query.data.split('_')[-1])
                logger.info(f"Admin {user_id} approving post {post_id}")
                await approve_post(update, context, post_id)
            except ValueError:
                await query.answer("❌ Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in approve_post handler: {e}")
                await query.answer("❌ Error approving post", show_alert=True)
            
        elif query.data.startswith('reject_post_'):
            try:
                post_id = int(query.data.split('_')[-1])
                logger.info(f"Admin {user_id} rejecting post {post_id}")
                await reject_post(update, context, post_id)
            except ValueError:
                await query.answer("❌ Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in reject_post handler: {e}")
                await query.answer("❌ Error rejecting post", show_alert=True)
            
        # Private messaging functionality
        elif query.data == 'inbox':
            await show_inbox(update, context)
            
        elif query.data == 'view_messages':
            await show_messages(update, context)
            
        elif query.data.startswith('messages_page_'):
            page = int(query.data.split('_')[-1])
            await show_messages(update, context, page)
            
        elif query.data.startswith('message_'):
            target_id = query.data.split('_', 1)[1]
            db_execute(
                "UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s",
                (target_id, user_id)
            )
            
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            target_name = target_user['anonymous_name'] if target_user else "this user"
            
            await query.message.reply_text(
                f"✉️ *Composing message to {target_name}*\n\nPlease type your message:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN
            )
            
        elif query.data.startswith('reply_msg_'):
            # Fixed: Properly extract target_id from reply_msg_{target_id}
            target_id = query.data.split('_')[2] if len(query.data.split('_')) > 2 else query.data.split('_')[1]
            db_execute(
                "UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s",
                (target_id, user_id)
            )
            
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            target_name = target_user['anonymous_name'] if target_user else "this user"
            
            await query.message.reply_text(
                f"↩️ *Replying to {target_name}*\n\nPlease type your message:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN
            )
            
        elif query.data.startswith('block_user_'):
            target_id = query.data.split('_', 2)[2]
            
            # Add to blocks table
            try:
                db_execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_id, target_id)
                )
                await query.message.reply_text("✅ User has been blocked. They can no longer send you messages.")
            except psycopg2.IntegrityError:
                await query.message.reply_text("❌ User is already blocked.")
            
    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        try:
            await query.message.reply_text("❌ An error occurred. Please try again.")
        except:
            pass

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("❌ You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ You don't have permission to access this.")
        return
    
    stats = db_fetch_one('''
        SELECT 
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM posts WHERE approved = TRUE) as approved_posts,
            (SELECT COUNT(*) FROM posts WHERE approved = FALSE) as pending_posts,
            (SELECT COUNT(*) FROM comments) as total_comments,
            (SELECT COUNT(*) FROM private_messages) as total_messages
    ''')
    
    text = (
        "📊 *Bot Statistics*\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"📝 Approved Posts: {stats['approved_posts']}\n"
        f"🕒 Pending Posts: {stats['pending_posts']}\n"
        f"💬 Total Comments: {stats['total_comments']}\n"
        f"📩 Private Messages: {stats['total_messages']}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]
    ])
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error showing admin stats: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading statistics.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error loading statistics.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # NEW: Handle comment editing
    if 'editing_comment' in context.user_data:
        comment_id = context.user_data['editing_comment']
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        
        if comment and comment['author_id'] == user_id:
            # Update the comment
            db_execute(
                "UPDATE comments SET content = %s WHERE comment_id = %s",
                (text, comment_id)
            )
            
            # Clean up
            del context.user_data['editing_comment']
            
            await update.message.reply_text(
                "✅ Comment updated successfully!",
                reply_markup=main_menu
            )
            return
        else:
            del context.user_data['editing_comment']
            await update.message.reply_text(
                "❌ Error updating comment. Please try again.",
                reply_markup=main_menu
            )
            return

    # If user doesn't exist, create them
    if not user:
        anon = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        db_execute(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        )
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    # NEW: Check if we have a thread_from_post_id for continuation
    thread_from_post_id = context.user_data.get('thread_from_post_id')
    
    if user and user['waiting_for_post']:
        category = user['selected_category']
        db_execute(
            "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
            (user_id,)
        )
        
        post_content = ""
        media_type = 'text'
        media_id = None
        
        try:
            if update.message.text:
                post_content = update.message.text
                await send_post_confirmation(update, context, post_content, category, thread_from_post_id=thread_from_post_id)
                return
            elif update.message.photo:
                photo = update.message.photo[-1]
                media_id = photo.file_id
                media_type = 'photo'
                post_content = update.message.caption or ""
            elif update.message.voice:
                voice = update.message.voice
                media_id = voice.file_id
                media_type = 'voice'
                post_content = update.message.caption or ""
            else:
                post_content = "(Unsupported content type)"
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            post_content = "(Unsupported content type)" 

        await send_post_confirmation(update, context, post_content, category, media_type, media_id, thread_from_post_id=thread_from_post_id)
        return

    elif user and user['waiting_for_comment']:
        post_id = user['comment_post_id']
    
        parent_comment_id = 0
        if user['comment_idx']:
            try:
                parent_comment_id = int(user['comment_idx'])
            except Exception:
                parent_comment_id = 0
    
        comment_type = 'text'
        file_id = None
    
        if update.message.text:
            content = update.message.text
        elif update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            comment_type = 'photo'
            content = update.message.caption or ""
        elif update.message.voice:
            voice = update.message.voice
            file_id = voice.file_id
            comment_type = 'voice'
            content = update.message.caption or ""
        else:
            await update.message.reply_text("❌ Unsupported comment type. Please send text, photo, or voice message.")
            return
    
        # Insert new comment
        comment_row = db_execute(
            """INSERT INTO comments 
            (post_id, parent_comment_id, author_id, content, type, file_id) 
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING comment_id""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id),
            fetchone=True
        )
    
        # Reset state
        db_execute(
            "UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL WHERE user_id = %s",
            (user_id,)
        )
    
        await update.message.reply_text("✅ Your comment has been posted!", reply_markup=main_menu)
        
        # Update comment count
        await update_channel_post_comment_count(context, post_id)
        
        # Notify parent comment author if this is a reply
        if parent_comment_id != 0:
            await notify_user_of_reply(context, post_id, parent_comment_id, user_id)
        return

    elif user and user['waiting_for_private_message']:
        target_id = user['private_message_target']
        message_content = text
        
        # Check if blocked
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (target_id, user_id)
        )
        
        if is_blocked:
            await update.message.reply_text(
                "❌ You cannot send messages to this user. They have blocked you.",
                reply_markup=main_menu
            )
            db_execute(
                "UPDATE users SET waiting_for_private_message = FALSE, private_message_target = NULL WHERE user_id = %s",
                (user_id,)
            )
            return
        
        # Save message
        message_row = db_execute(
            "INSERT INTO private_messages (sender_id, receiver_id, content) VALUES (%s, %s, %s) RETURNING message_id",
            (user_id, target_id, message_content),
            fetchone=True
        )
        
        # Reset state
        db_execute(
            "UPDATE users SET waiting_for_private_message = FALSE, private_message_target = NULL WHERE user_id = %s",
            (user_id,)
        )
        
        # Notify receiver
        await notify_user_of_private_message(context, user_id, target_id, message_content, message_row['message_id'] if message_row else None)
        
        await update.message.reply_text(
            "✅ Your message has been sent!",
            reply_markup=main_menu
        )
        return

    if user and user['awaiting_name']:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s",
                (new_name, user_id)
            )
            await update.message.reply_text(f"✅ Name updated to *{new_name}*!", parse_mode=ParseMode.MARKDOWN)
            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle main menu buttons
    if text == "🌟 Share My Thoughts":
        await update.message.reply_text(
            "📚 *Choose a category:*",
            reply_markup=build_category_buttons(),
            parse_mode=ParseMode.MARKDOWN
        )
        return 

    elif text == "👤 View Profile":
        await send_updated_profile(user_id, update.message.chat.id, context)
        return 

    elif text == "🏆 Leaderboard":
        await show_leaderboard(update, context)
        return

    elif text == "⚙️ Settings":
        await show_settings(update, context)
        return

    elif text == "📚 My Previous Posts":
        await show_previous_posts(update, context, 1)
        return

    elif text == "❓ Help":
        help_text = (
            "ℹ️ *How to Use This Bot:*\n"
            "• Use the menu buttons to navigate.\n"
            "• Tap 'Share My Thoughts' to share your thoughts anonymously.\n"
            "• Choose a category and type or send your message (text, photo, or voice).\n"
            "• After posting, others can comment on your posts.\n"
            "• View your profile, set your name and sex anytime.\n"
            "• Use 'My Previous Posts' to view and continue your past posts.\n"
            "• Use the comments button on channel posts to join the conversation here.\n"
            "• Follow users to send them private messages."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
        return 

    # If none of the above, show main menu
    await update.message.reply_text(
        "How can I help you?",
        reply_markup=main_menu
    )

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("menu", "📱 Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Share your thoughts"),
        BotCommand("leaderboard", "View top contributors"),
        BotCommand("settings", "Configure your preferences"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
        BotCommand("inbox", "View your private messages"),
    ]
    
    if ADMIN_ID:
        commands.append(BotCommand("admin", "Admin panel (admin only)"))
    
    await app.bot.set_my_commands(commands)

def main():
    # Initialize database before starting the bot
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("inbox", show_inbox))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    # Start polling
    app.run_polling() 

if __name__ == "__main__": 
    # Initialize database first
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        exit(1)
    
    # Start Flask server in a separate thread for Render
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    # Start Telegram bot in main thread
    main()
