import os 
import sqlite3
import logging
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

# Initialize database
DB_FILE = 'bot.db'

# Initialize database tables with schema migration
def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        
        c = conn.cursor()
        
        # Create tables if they don't exist
        c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            anonymous_name TEXT,
            sex TEXT DEFAULT '👤',
            awaiting_name BOOLEAN DEFAULT 0,
            waiting_for_post BOOLEAN DEFAULT 0,
            waiting_for_comment BOOLEAN DEFAULT 0,
            selected_category TEXT,
            comment_post_id INTEGER,
            comment_idx INTEGER,
            reply_idx INTEGER,
            nested_idx INTEGER,
            notifications_enabled BOOLEAN DEFAULT 1,
            privacy_public BOOLEAN DEFAULT 1,
            is_admin BOOLEAN DEFAULT 0,
            waiting_for_private_message BOOLEAN DEFAULT 0,
            private_message_target TEXT
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS followers (
            follower_id TEXT,
            followed_id TEXT,
            PRIMARY KEY (follower_id, followed_id)
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            post_id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            author_id TEXT,
            category TEXT,
            channel_message_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            approved BOOLEAN DEFAULT 0,
            admin_approved_by TEXT,
            media_type TEXT DEFAULT 'text',
            media_id TEXT
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER,
            parent_comment_id INTEGER DEFAULT 0,
            author_id TEXT,
            content TEXT,
            type TEXT DEFAULT 'text',
            file_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES posts (post_id)
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS reactions (
            reaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER,
            user_id TEXT,
            type TEXT,
            FOREIGN KEY (comment_id) REFERENCES comments (comment_id),
            UNIQUE(comment_id, user_id)
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS private_messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT,
            receiver_id TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_read BOOLEAN DEFAULT 0,
            FOREIGN KEY (sender_id) REFERENCES users (user_id),
            FOREIGN KEY (receiver_id) REFERENCES users (user_id)
        )''')
        
        # Check for missing columns and add them
        c.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in c.fetchall()]
        if 'notifications_enabled' not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN notifications_enabled BOOLEAN DEFAULT 1")
        if 'privacy_public' not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN privacy_public BOOLEAN DEFAULT 1")
        if 'is_admin' not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0")
        if 'waiting_for_private_message' not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN waiting_for_private_message BOOLEAN DEFAULT 0")
        if 'private_message_target' not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN private_message_target TEXT")
        
        # Check for media columns in posts
        c.execute("PRAGMA table_info(posts)")
        post_columns = [col[1] for col in c.fetchall()]
        if 'media_type' not in post_columns:
            c.execute("ALTER TABLE posts ADD COLUMN media_type TEXT DEFAULT 'text'")
        if 'media_id' not in post_columns:
            c.execute("ALTER TABLE posts ADD COLUMN media_id TEXT")
            
        # Create admin user if specified
        ADMIN_ID = os.getenv('ADMIN_ID')
        if ADMIN_ID:
            c.execute('''
            INSERT OR IGNORE INTO users (user_id, anonymous_name, is_admin) 
            VALUES (?, ?, 1)
            ''', (ADMIN_ID, "Admin"))
            c.execute('''
            UPDATE users SET is_admin = 1 WHERE user_id = ?
            ''', (ADMIN_ID,))
        
        conn.commit()
    logging.info("Database initialized successfully")

# Initialize database on startup
init_db()

# Database helper functions
def db_execute(query, params=(), fetch=False):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        if fetch:
            return c.fetchall()
        return c.lastrowid if c.lastrowid else True

def db_fetch_one(query, params=()):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchone()

def db_fetch_all(query, params=()):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchall()

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

# Load environment variables
load_dotenv()
TOKEN = os.getenv('TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_ID = os.getenv('ADMIN_ID')

# Initialize Flask app for Render health checks
flask_app = Flask(__name__) 

@flask_app.route('/')
def health_check():
    return jsonify(status="OK", message="Christian Chat Bot is running") 

@flask_app.route('/ping')
def uptimerobot_ping():
    return jsonify(status="OK", message="Pong! Bot is alive") 

# Create main menu keyboard
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🙏 Ask Question")],
        [KeyboardButton("👤 View Profile"), KeyboardButton("🏆 Leaderboard")],
        [KeyboardButton("⚙️ Settings"), KeyboardButton("❓ Help")]
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
    names = ["Anonymous"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def calculate_user_rating(user_id):
    post_count = db_fetch_one(
        "SELECT COUNT(*) FROM posts WHERE author_id = ? AND approved = 1",
        (user_id,)
    )[0] if db_fetch_one(
        "SELECT COUNT(*) FROM posts WHERE author_id = ? AND approved = 1",
        (user_id,)
    ) else 0
    
    comment_count = db_fetch_one(
        "SELECT COUNT(*) FROM comments WHERE author_id = ?",
        (user_id,)
    )[0] if db_fetch_one(
        "SELECT COUNT(*) FROM comments WHERE author_id = ?",
        (user_id,)
    ) else 0
    
    return post_count + comment_count

def format_stars(rating, max_stars=5):
    full = '⭐️' * min(rating, max_stars)
    empty = '☆' * max(0, max_stars - rating)
    return full + empty

def count_all_comments(post_id):
    def count_replies(parent_id=None):
        if parent_id is None:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE post_id = ? AND parent_comment_id = 0",
                (post_id,)
            )
        else:
            comments = db_fetch_all(
                "SELECT comment_id FROM comments WHERE parent_comment_id = ?",
                (parent_id,)
            )
        
        total = len(comments)
        for comment in comments:
            total += count_replies(comment['comment_id'])
        return total
    
    return count_replies()

def get_display_name(user_data):
    return user_data['anonymous_name'] if user_data and user_data['anonymous_name'] else "Anonymous"

def get_display_sex(user_data):
    return user_data['sex'] if user_data and user_data['sex'] else '👤'

def get_user_rank(user_id):
    users = db_fetch_all('''
        SELECT user_id, 
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = 1) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
    ''')
    
    for rank, (uid, _) in enumerate(users, start=1):
        if uid == user_id:
            return rank
    return None

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = 1) + 
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
    if user_rank > 10:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = ?", (user_id,))
        user_contributions = calculate_user_rating(user_id)
        leaderboard_text += (
            f"\n...\n"
            f"{user_rank}. {user_data['anonymous_name']} {user_data['sex']} - {user_contributions} contributions\n"
        )
    
    keyboard = [
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')]
    ]
    
    await update.message.reply_text(
        leaderboard_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    try:
        user = db_fetch_one("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = ?", (user_id,))
        
        if not user:
            await update.message.reply_text("Please use /start first to initialize your profile.")
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
        await update.message.reply_text("❌ Error loading settings. Please try again.")

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None):
    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data='edit_post'),
            InlineKeyboardButton("❌ Cancel", callback_data='cancel_post')
        ],
        [
            InlineKeyboardButton("✅ Submit", callback_data='confirm_post')
        ]
    ]
    
    preview_text = (
        f"📝 *Post Preview* [{category}]\n\n"
        f"{escape_markdown(post_content, version=2)}\n\n"
        f"Please confirm your post:"
    )
    
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
        await update.message.reply_text("❌ Error showing confirmation. Please try again.")

async def notify_user_of_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, comment_id: int, replier_id: str):
    try:
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
        if not comment:
            return
        
        original_author = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (comment['author_id'],))
        if not original_author or not original_author['notifications_enabled']:
            return
        
        replier = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (replier_id,))
        replier_name = get_display_name(replier)
        
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
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
    
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
        return
    
    author = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (post['author_id'],))
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

async def notify_user_of_private_message(context: ContextTypes.DEFAULT_TYPE, sender_id: str, receiver_id: str, message_content: str):
    try:
        receiver = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (receiver_id,))
        if not receiver or not receiver['notifications_enabled']:
            return
        
        sender = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (sender_id,))
        sender_name = get_display_name(sender)
        
        # Truncate long messages for the notification
        preview_content = message_content[:100] + '...' if len(message_content) > 100 else message_content
        
        notification_text = (
            f"📩 You received a private message from {sender_name}:\n\n"
            f"{escape_markdown(preview_content, version=2)}\n\n"
            f"[View messages](https://t.me/{BOT_USERNAME}?start=inbox)"
        )
        
        await context.bot.send_message(
            chat_id=receiver_id,
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Error sending private message notification: {e}")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to access this.")
        return
    
    pending_posts = db_fetch_all("SELECT COUNT(*) FROM posts WHERE approved = 0")[0][0]
    
    keyboard = [
        [InlineKeyboardButton(f"📝 Pending Posts ({pending_posts})", callback_data='admin_pending')],
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
        await update.message.reply_text("❌ Error loading admin panel.")

async def show_pending_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to access this.")
        return
    
    posts = db_fetch_all("""
        SELECT p.post_id, p.content, p.category, u.anonymous_name, p.media_type, p.media_id
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        WHERE p.approved = 0
        ORDER BY p.timestamp
    """)
    
    if not posts:
        await update.message.reply_text("✅ No pending posts!")
        return
    
    for post in posts[:10]:
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
                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            elif post['media_type'] == 'photo':
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=post['media_id'],
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            elif post['media_type'] == 'voice':
                await context.bot.send_voice(
                    chat_id=user_id,
                    voice=post['media_id'],
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as e:
            logger.error(f"Error sending pending post: {e}")

async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to do this.")
        return
    
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
        await update.message.reply_text("❌ Post not found.")
        return
    
    try:
        hashtag = f"#{post['category']}"
        caption_text = (
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/gospelyrics)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
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
        
        db_execute(
            "UPDATE posts SET approved = 1, admin_approved_by = ?, channel_message_id = ? WHERE post_id = ?",
            (user_id, msg.message_id, post_id)
        )
        
        await context.bot.send_message(
            chat_id=post['author_id'],
            text="✅ Your post has been approved and published!"
        )
        
        await update.callback_query.edit_message_text(
            f"✅ Post approved and published!\n\n{post['content'][:100]}...",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        await update.callback_query.edit_message_text("❌ Failed to approve post. Please try again.")

async def reject_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to do this.")
        return
    
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
        await update.message.reply_text("❌ Post not found.")
        return
    
    try:
        await context.bot.send_message(
            chat_id=post['author_id'],
            text="❌ Your post was not approved by the admin."
        )
        
        db_execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        await update.callback_query.edit_message_text("❌ Post rejected and deleted")
        
    except Exception as e:
        logger.error(f"Error rejecting post: {e}")
        await update.callback_query.edit_message_text("❌ Failed to reject post. Please try again.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    # Check if user exists
    user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not user:
        anon = create_anonymous_name(user_id)
        db_execute(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (?, ?, ?, ?)",
            (user_id, anon, '👤', 1 if user_id == ADMIN_ID else 0)
        )
    
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
                    "UPDATE users SET waiting_for_comment = 1, comment_post_id = ? WHERE user_id = ?",
                    (post_id, user_id)
                )
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
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

        elif arg.startswith("profile_"):
            target_name = arg.split("_", 1)[1]
            user_data = db_fetch_one("SELECT * FROM users WHERE anonymous_name = ?", (target_name,))
            if user_data:
                followers = db_fetch_all(
                    "SELECT * FROM followers WHERE followed_id = ?",
                    (user_data['user_id'],)
                )
                rating = calculate_user_rating(user_data['user_id'])
                stars = format_stars(rating)
                current = user_id
                btn = []
                if user_data['user_id'] != current:
                    is_following = db_fetch_one(
                        "SELECT * FROM followers WHERE follower_id = ? AND followed_id = ?",
                        (current, user_data['user_id'])
                    )
                    if is_following:
                        btn.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{user_data["user_id"]}')])
                        # Add message button if following
                        btn.append([InlineKeyboardButton("✉️ Send Message", callback_data=f'message_{user_data["user_id"]}')])
                    else:
                        btn.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{user_data["user_id"]}')])
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
                    parse_mode=ParseMode.MARKDOWN)
                return
                
        elif arg == "inbox":
            await show_inbox(update, context)
            return

    keyboard = [
        [
            InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'),
            InlineKeyboardButton("⚙️ Settings", callback_data='settings')
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data='help'),
            InlineKeyboardButton("ℹ️ About Us", callback_data='about')
        ]
    ]

    await update.message.reply_text(
        "🌟✝️ *እንኳን ወደ Christian vent በሰላም መጡ* ✝️🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "ማንነታችሁ ሳይገለጽ ሃሳባችሁን ማጋራት ትችላላችሁ.\n\n የሚከተሉትን ምረጁ :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN)
    
    await update.message.reply_text(
        "You can use the buttons below to navigate:",
        reply_markup=main_menu
    )

async def show_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Get unread messages count
    unread_count = db_fetch_one(
        "SELECT COUNT(*) FROM private_messages WHERE receiver_id = ? AND is_read = 0",
        (user_id,)
    )[0]
    
    # Get recent messages
    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = ?
        ORDER BY pm.timestamp DESC
        LIMIT 10
    ''', (user_id,))
    
    if not messages:
        await update.message.reply_text(
            "📭 *Your Inbox*\n\nYou don't have any messages yet.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    inbox_text = f"📭 *Your Inbox* ({unread_count} unread)\n\n"
    
    for msg in messages:
        status = "🔵" if not msg['is_read'] else "⚪️"
        timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d')
        preview = msg['content'][:30] + '...' if len(msg['content']) > 30 else msg['content']
        inbox_text += f"{status} *{msg['sender_name']}* {msg['sender_sex']} - {preview} ({timestamp})\n"
    
    keyboard = [
        [InlineKeyboardButton("📝 View Messages", callback_data='view_messages')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    
    await update.message.reply_text(
        inbox_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_id = str(update.effective_user.id)
    
    # Mark messages as read when viewing
    db_execute(
        "UPDATE private_messages SET is_read = 1 WHERE receiver_id = ?",
        (user_id,)
    )
    
    # Get messages with pagination
    per_page = 5
    offset = (page - 1) * per_page
    
    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = ?
        ORDER BY pm.timestamp DESC
        LIMIT ? OFFSET ?
    ''', (user_id, per_page, offset))
    
    total_messages = db_fetch_one(
        "SELECT COUNT(*) FROM private_messages WHERE receiver_id = ?",
        (user_id,)
    )[0]
    total_pages = (total_messages + per_page - 1) // per_page
    
    if not messages:
        await update.message.reply_text(
            "📭 *Your Messages*\n\nYou don't have any messages yet.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    messages_text = f"📭 *Your Messages* (Page {page}/{total_pages})\n\n"
    
    for msg in messages:
        timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d, %H:%M')
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
    
    # Reply buttons for each message
    for msg in messages:
        keyboard_buttons.append([
            InlineKeyboardButton(f"↩️ Reply to {msg['sender_name']}", callback_data=f"reply_msg_{msg['sender_id']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                messages_text,
                reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                messages_text,
                reply_markup=InlineKeyboardMarkup(keyboard_buttons),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        logger.error(f"Error showing messages: {e}")
        await update.message.reply_text("❌ Error loading messages. Please try again.")

async def show_comments_menu(update, context, post_id, page=1):
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
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

    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
        await context.bot.send_message(chat_id, "❌ Post not found.", reply_markup=main_menu)
        return

    per_page = 5
    offset = (page - 1) * per_page

    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = ? AND parent_comment_id = 0 ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (post_id, per_page, offset)
    )

    total_comments = count_all_comments(post_id)
    total_pages = (total_comments + per_page - 1) // per_page

    post_text = post['content']
    header = f"{escape_markdown(post_text, version=2)}\n\n"

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
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (commenter_id,))
        display_sex = get_display_sex(commenter)
        display_name = get_display_name(commenter)
        
        rating = calculate_user_rating(commenter_id)
        stars = format_stars(rating)
        profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{display_name}"

        likes = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
            (comment['comment_id'],)
        )['cnt']
        
        dislikes = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
            (comment['comment_id'],)
        )['cnt']

        user_reaction = db_fetch_one(
            "SELECT type FROM reactions WHERE comment_id = ? AND user_id = ?",
            (comment['comment_id'], user_id)
        )

        like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
        dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

        comment_text = escape_markdown(comment['content'], version=2)
        author_text = f"[{escape_markdown(display_name, version=2)}]({profile_url}) {display_sex} {stars}"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment['comment_id']}"),
                InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment['comment_id']}"),
                InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")
            ]
        ])

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{comment_text}\n\n{author_text}",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=header_message_id
        )

        reply_page = reply_pages.get(comment['comment_id'], 1)
        reply_per_page = 5
        reply_offset = (reply_page - 1) * reply_per_page

        replies = db_fetch_all(
            "SELECT * FROM comments WHERE parent_comment_id = ? ORDER BY timestamp LIMIT ? OFFSET ?",
            (comment['comment_id'], reply_per_page, reply_offset)
        )
        total_replies = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM comments WHERE parent_comment_id = ?",
            (comment['comment_id'],)
        )['cnt']
        total_reply_pages = (total_replies + reply_per_page - 1) // reply_per_page

        for reply in replies:
            reply_user_id = reply['author_id']
            reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (reply_user_id,))
            reply_display_name = get_display_name(reply_user)
            reply_display_sex = get_display_sex(reply_user)
            rating_reply = calculate_user_rating(reply_user_id)
            stars_reply = format_stars(rating_reply)
            profile_url_reply = f"https://t.me/{BOT_USERNAME}?start=profile_{reply_display_name}"
            safe_reply = escape_markdown(reply['content'], version=2)

            reply_likes = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
                (reply['comment_id'],)
            )['cnt']
            
            reply_dislikes = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
                (reply['comment_id'],)
            )['cnt']

            reply_user_reaction = db_fetch_one(
                "SELECT type FROM reactions WHERE comment_id = ? AND user_id = ?",
                (reply['comment_id'], user_id)
            )

            reply_like_emoji = "👍" if reply_user_reaction and reply_user_reaction['type'] == 'like' else "👍"
            reply_dislike_emoji = "👎" if reply_user_reaction and reply_user_reaction['type'] == 'dislike' else "👎"

            reply_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{reply_like_emoji} {reply_likes}", callback_data=f"likereply_{reply['comment_id']}"),
                    InlineKeyboardButton(f"{reply_dislike_emoji} {reply_dislikes}", callback_data=f"dislikereply_{reply['comment_id']}"),
                    InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{comment['comment_id']}_{reply['comment_id']}")
                ]
            ])

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{safe_reply}\n\n[{reply_display_name}]({profile_url_reply}) {reply_display_sex} {stars_reply}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=msg.message_id,
                reply_markup=reply_kb
            )

        if total_reply_pages > 1:
            reply_pagination_buttons = []
            if reply_page > 1:
                reply_pagination_buttons.append(
                    InlineKeyboardButton("⬅️ Prev Replies", callback_data=f"replypage_{post_id}_{comment['comment_id']}_{reply_page-1}_{page}")
                )
            if reply_page < total_reply_pages:
                reply_pagination_buttons.append(
                    InlineKeyboardButton("Next Replies ➡️", callback_data=f"replypage_{post_id}_{comment['comment_id']}_{reply_page+1}_{page}")
                )
            if reply_pagination_buttons:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Replies page {reply_page}/{total_reply_pages}",
                    reply_markup=InlineKeyboardMarkup([reply_pagination_buttons]),
                    reply_to_message_id=msg.message_id
                )

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
            reply_to_message_id=header_message_id
        )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'),
            InlineKeyboardButton("⚙️ Settings", callback_data='settings')
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data='help'),
            InlineKeyboardButton("ℹ️ About Us", callback_data='about')
        ]
    ]
    await update.message.reply_text(
        "📱 *Main Menu*\nChoose an option below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    
    await update.message.reply_text(
        "You can also use these buttons:",
        reply_markup=main_menu
    )

async def send_updated_profile(user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not user:
        return
    
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = calculate_user_rating(user_id)
    stars = format_stars(rating)
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = ?",
        (user_id,)
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
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
                "UPDATE users SET waiting_for_post = 1, selected_category = ? WHERE user_id = ?",
                (category, user_id)
            )

            await query.message.reply_text(
                f"✍️ *Please type your thought for #{category}:*\n\nYou may also send a photo or voice message.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=True))
        
        elif query.data == 'menu':
            keyboard = [
                [
                    InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'),
                    InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')
                ],
                [
                    InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard'),
                    InlineKeyboardButton("⚙️ Settings", callback_data='settings')
                ],
                [
                    InlineKeyboardButton("❓ Help", callback_data='help'),
                    InlineKeyboardButton("ℹ️ About Us", callback_data='about')
                ]
            ]
            await query.message.edit_text(
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
            current = db_fetch_one("SELECT notifications_enabled FROM users WHERE user_id = ?", (user_id,))['notifications_enabled']
            db_execute(
                "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
                (not current, user_id)
            )
            await show_settings(update, context)
        
        elif query.data == 'toggle_privacy':
            current = db_fetch_one("SELECT privacy_public FROM users WHERE user_id = ?", (user_id,))['privacy_public']
            db_execute(
                "UPDATE users SET privacy_public = ? WHERE user_id = ?",
                (not current, user_id)
            )
            await show_settings(update, context)

        elif query.data == 'help':
            help_text = (
                "ℹ️ *የዚህ ቦት አጠቃቀም:*\n"
                "•  menu button በመጠቀም የተለያዩ አማራጮችን ማየት ይችላሉ.\n"
                "• 'Ask Question' የሚለውን በመንካት በፈለጉት ነገር ጥያቄም ሆነ ሃሳብ መጻፍ ይችላሉ.\n"
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
                "UPDATE users SET awaiting_name = 1 WHERE user_id = ?",
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
            sex = '👨' if 'male' in query.data else '👩'
            db_execute(
                "UPDATE users SET sex = ? WHERE user_id = ?",
                (sex, user_id)
            )
            await query.message.reply_text("✅ Sex updated!")
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data.startswith(('follow_', 'unfollow_')):
            target_uid = query.data.split('_', 1)[1]
            if query.data.startswith('follow_'):
                try:
                    db_execute(
                        "INSERT INTO followers (follower_id, followed_id) VALUES (?, ?)",
                        (user_id, target_uid)
                    )
                except sqlite3.IntegrityError:
                    pass
            else:
                db_execute(
                    "DELETE FROM followers WHERE follower_id = ? AND followed_id = ?",
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
                    "UPDATE users SET waiting_for_comment = 1, comment_post_id = ? WHERE user_id = ?",
                    (post_id, user_id)
                )
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
                preview_text = "Original content not found"
                if post:
                    content = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n✍️ Please type your comment:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )

        elif query.data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
            try:
                parts = query.data.split('_')
                comment_id = int(parts[1])
                reaction_type = 'like' if parts[0] in ('likecomment', 'likereply') else 'dislike'

                db_execute(
                    "DELETE FROM reactions WHERE comment_id = ? AND user_id = ?",
                    (comment_id, user_id)
                )

                current_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = ? AND user_id = ?",
                    (comment_id, user_id)
                )
                
                if not current_reaction or current_reaction['type'] != reaction_type:
                    db_execute(
                        "INSERT INTO reactions (comment_id, user_id, type) VALUES (?, ?, ?)",
                        (comment_id, user_id, reaction_type)
                    )

                likes = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
                    (comment_id,)
                )['cnt']
                
                dislikes = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
                    (comment_id,)
                )['cnt']

                comment = db_fetch_one(
                    "SELECT post_id, parent_comment_id FROM comments WHERE comment_id = ?",
                    (comment_id,)
                )
                if not comment:
                    await query.answer("Comment not found", show_alert=True)
                    return

                post_id = comment['post_id']
                parent_comment_id = comment['parent_comment_id']

                user_reaction = db_fetch_one(
                    "SELECT type FROM reactions WHERE comment_id = ? AND user_id = ?",
                    (comment_id, user_id)
                )

                like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
                dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

                if parent_comment_id == 0:
                    new_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment_id}")
                        ]
                    ])
                else:
                    new_kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likereply_{comment_id}"),
                            InlineKeyboardButton(f"{dislike_emoji} {dislikes", callback_data=f"dislikereply_{comment_id}"),
                            InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{parent_comment_id}_{comment_id}")
                        ]
                    ])

                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        reply_markup=new_kb
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        logger.error(f"Error updating reaction buttons: {e}")
                
                if user_reaction and user_reaction['type'] != reaction_type:
                    comment_author = db_fetch_one(
                        "SELECT user_id, notifications_enabled FROM users WHERE user_id = ?",
                        (comment['author_id'],)
                    )
                    if comment_author and comment_author['notifications_enabled'] and comment_author['user_id'] != user_id:
                        reactor_name = get_display_name(
                            db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
                        )
                        post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
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

        elif query.data.startswith("reply_"):
            parts = query.data.split("_")
            if len(parts) == 3:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                db_execute(
                    "UPDATE users SET waiting_for_comment = 1, comment_post_id = ?, comment_idx = ? WHERE user_id = ?",
                    (post_id, comment_id, user_id)
                )
                
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
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
                parent_comment_id = int(parts[2])
                comment_id = int(parts[3])
                db_execute(
                    "UPDATE users SET waiting_for_comment = 1, comment_post_id = ?, comment_idx = ?, reply_idx = ? WHERE user_id = ?",
                    (post_id, parent_comment_id, comment_id, user_id)
                )
                
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
                preview_text = "Original reply not found"
                if comment:
                    content = comment['content'][:100] + '...' if len(comment['content']) > 100 else comment['content']
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n↩️ Please type your *reply*:",
                    reply_markup=ForceReply(selective=True),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        
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
                del context.user_data['pending_post']
                return
            
            elif query.data == 'confirm_post':
                category = pending_post['category']
                post_content = pending_post['content']
                media_type = pending_post.get('media_type', 'text')
                media_id = pending_post.get('media_id')
                del context.user_data['pending_post']
                
                post_id = db_execute(
                    "INSERT INTO posts (content, author_id, category, media_type, media_id) VALUES (?, ?, ?, ?, ?)",
                    (post_content, user_id, category, media_type, media_id)
                )
                
                await notify_admin_of_new_post(context, post_id)
                
                await query.message.edit_text(
                    "✅ Your post has been submitted for admin approval!\n"
                    "You'll be notified when it's approved and published."
                )
                return

        elif query.data == 'admin_panel':
            await admin_panel(update, context)
            
        elif query.data == 'admin_pending':
            await show_pending_posts(update, context)
            
        elif query.data == 'admin_stats':
            await show_admin_stats(update, context)
            
        elif query.data.startswith('approve_post_'):
            post_id = int(query.data.split('_')[-1])
            await approve_post(update, context, post_id)
            
        elif query.data.startswith('reject_post_'):
            post_id = int(query.data.split('_')[-1])
            await reject_post(update, context, post_id)
            
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
                "UPDATE users SET waiting_for_private_message = 1, private_message_target = ? WHERE user_id = ?",
                (target_id, user_id)
            )
            
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = ?", (target_id,))
            target_name = target_user['anonymous_name'] if target_user else "this user"
            
            await query.message.reply_text(
                f"✉️ *Composing message to {target_name}*\n\nPlease type your message:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN
            )
            
        elif query.data.startswith('reply_msg_'):
            target_id = query.data.split('_', 2)[2]
            db_execute(
                "UPDATE users SET waiting_for_private_message = 1, private_message_target = ? WHERE user_id = ?",
                (target_id, user_id)
            )
            
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = ?", (target_id,))
            target_name = target_user['anonymous_name'] if target_user else "this user"
            
            await query.message.reply_text(
                f"↩️ *Replying to {target_name}*\n\nPlease type your message:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        try:
            await query.message.reply_text("❌ An error occurred. Please try again.")
        except:
            pass

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to access this.")
        return
    
    stats = db_fetch_one('''
        SELECT 
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM posts WHERE approved = 1) as approved_posts,
            (SELECT COUNT(*) FROM posts WHERE approved = 0) as pending_posts,
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
        await update.message.reply_text("❌ Error loading statistics.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.message.from_user.id)
    message = update.message
    user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    if user and user['waiting_for_post']:
        category = user['selected_category']
        db_execute(
            "UPDATE users SET waiting_for_post = 0, selected_category = NULL WHERE user_id = ?",
            (user_id,)
        )
        display_name = get_display_name(user)
        
        post_content = ""
        media_type = 'text'
        media_id = None
        
        try:
            if update.message.text:
                post_content = update.message.text
                await send_post_confirmation(update, context, post_content, category)
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

        await send_post_confirmation(update, context, post_content, category, media_type, media_id)
        return

    elif user and user['waiting_for_comment']:
        post_id = user['comment_post_id']
        parent_comment_id = 0
        comment_type = 'text'
        file_id = None
        
        if user['reply_idx'] is not None:
            parent_comment_id = user['reply_idx']
        elif user['comment_idx'] is not None:
            parent_comment_id = user['comment_idx']
        
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
        
        comment_id = db_execute(
            """INSERT INTO comments 
            (post_id, parent_comment_id, author_id, content, type, file_id) 
            VALUES (?, ?, ?, ?, ?, ?)""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id)
        )
        
        total_comments = count_all_comments(post_id)
        try:
            post_data = db_fetch_one("SELECT channel_message_id FROM posts WHERE post_id = ?", (post_id,))
            if post_data and post_data['channel_message_id']:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=post_data['channel_message_id'],
                    reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Failed to update comment count: {e}")
        
        db_execute(
            "UPDATE users SET waiting_for_comment = 0, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL, nested_idx = NULL WHERE user_id = ?",
            (user_id,)
        )
        
        if parent_comment_id != 0:
            await notify_user_of_reply(context, post_id, parent_comment_id, user_id)
        
        await update.message.reply_text("✅ Your comment has been added!", reply_markup=main_menu)
        return

    elif user and user['waiting_for_private_message']:
        target_id = user['private_message_target']
        message_content = text
        
        # Save the private message
        db_execute(
            "INSERT INTO private_messages (sender_id, receiver_id, content) VALUES (?, ?, ?)",
            (user_id, target_id, message_content)
        )
        
        # Reset the user state
        db_execute(
            "UPDATE users SET waiting_for_private_message = 0, private_message_target = NULL WHERE user_id = ?",
            (user_id,)
        )
        
        # Notify the receiver
        await notify_user_of_private_message(context, user_id, target_id, message_content)
        
        await update.message.reply_text(
            "✅ Your message has been sent!",
            reply_markup=main_menu
        )
        return

    if user and user['awaiting_name']:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = ?, awaiting_name = 0 WHERE user_id = ?",
                (new_name, user_id)
            )
            await update.message.reply_text(f"✅ Name updated to *{new_name}*!", parse_mode=ParseMode.MARKDOWN)
            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    if text == "🙏 Ask Question":
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

    elif text == "❓ Help":
        help_text = (
            "ℹ️ *How to Use This Bot:*\n"
            "• Use the menu buttons to navigate.\n"
            "• Tap 'Ask Question' to share your thoughts anonymously.\n"
            "• Choose a category and type or send your message (text, photo, or voice).\n"
            "• After posting, others can comment on your posts.\n"
            "• View your profile, set your name and sex anytime.\n"
            "• Use the comments button on channel posts to join the conversation here.\n"
            "• Follow users to send them private messages."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
        return 

    elif text == "ℹ️ About Us":
        about_text = (
            "👤 Creator: Yididiya Tamiru\n\n"
            "🔗 Telegram: @YIDIDIYATAMIRUU\n"
            "🙏 This bot helps you share your thoughts anonymously with the Christian community."
        )
        await update.message.reply_text(about_text, parse_mode=ParseMode.MARKDOWN)
        return

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

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
    
    if ADMIN_ID:
        commands.append(BotCommand("admin", "Admin panel (admin only)"))
    
    await app.bot.set_my_commands(commands)

def main():
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
    # Start Flask server in a separate thread for Render
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    # Start Telegram bot in main thread
    main()
