import jwt
import requests
import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest
import threading
from flask import Flask, jsonify, request, redirect, send_from_directory
from datetime import datetime, timedelta, timezone, time
import time
import asyncio
from functools import lru_cache
import html

# FIX: moved logger setup to top
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables first
load_dotenv()

# Initialize database connection
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv('TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_ID = os.getenv('ADMIN_ID')
# Add color variables near the top of bot.py (after loading env)
PRIMARY_COLOR = os.getenv('PRIMARY_COLOR')
SECONDARY_COLOR = os.getenv('SECONDARY_COLOR')
CARD_BG_COLOR = os.getenv('CARD_BG_COLOR')
BORDER_COLOR = os.getenv('BORDER_COLOR')
TEXT_COLOR = os.getenv('TEXT_COLOR')
def hex_to_rgb(hex_color):
    """Convert #RRGGBB to "R, G, B" string for CSS rgba() usage."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 6:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return f"{r}, {g}, {b}"
    return "191, 151, 11"  # fallback to default gold

PRIMARY_RGB = hex_to_rgb(PRIMARY_COLOR)

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
                    private_message_target TEXT,
                    bio TEXT DEFAULT 'No bio set.',
                    awaiting_bio BOOLEAN DEFAULT FALSE
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
                CREATE TABLE IF NOT EXISTS chat_requests (
                    id SERIAL PRIMARY KEY,
                    sender_id TEXT,
                    receiver_id TEXT,
                    status TEXT DEFAULT 'pending',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(sender_id, receiver_id)
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

                c.execute('''
                CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                    broadcast_id SERIAL PRIMARY KEY,
                    scheduled_by TEXT,
                    content TEXT,
                    media_type TEXT,
                    media_id TEXT,
                    scheduled_time TIMESTAMP,
                    status TEXT DEFAULT 'scheduled',
                    target_group TEXT DEFAULT 'all',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''')

                c.execute('''
                CREATE TABLE IF NOT EXISTS post_views (
                    user_id TEXT REFERENCES users(user_id),
                    post_id INTEGER REFERENCES posts(post_id),
                    last_viewed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, post_id)
                )
                ''')
                # ---------------- Database Schema Migration (Postgres Robust) ----------------
                
                # Check for 'bio' column in users
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='bio'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: bio to users table")
                    c.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT 'No bio set.'")

                # Check for 'awaiting_bio' column in users
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='awaiting_bio'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: awaiting_bio to users table")
                    c.execute("ALTER TABLE users ADD COLUMN awaiting_bio BOOLEAN DEFAULT FALSE")

                # Check for 'avatar_emoji' column in users
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='avatar_emoji'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: avatar_emoji to users table")
                    c.execute("ALTER TABLE users ADD COLUMN avatar_emoji VARCHAR(10) DEFAULT NULL")


                # ---------------- Database Schema Migration ----------------
                # Check if thread_from_post_id column exists, if not add it
                c.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='posts' AND column_name='thread_from_post_id'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: thread_from_post_id to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN thread_from_post_id BIGINT DEFAULT NULL")

                # Check if vent_number column exists, if not add it
                c.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='posts' AND column_name='vent_number'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: vent_number to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN vent_number INTEGER DEFAULT NULL")
                
                # Check for 'rejection_reason' column in posts
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='posts' AND column_name='rejection_reason'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: rejection_reason to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN rejection_reason TEXT DEFAULT NULL")

                # Check for 'search_vector' column in posts
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='posts' AND column_name='search_vector'
                """)
                if not c.fetchone():
                    logger.info("Adding search_vector to posts table")
                    try:
                        c.execute("""
                            ALTER TABLE posts ADD COLUMN search_vector tsvector
                            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
                        """)
                        c.execute("CREATE INDEX idx_posts_search ON posts USING GIN(search_vector)")
                    except Exception as e:
                        logger.error(f"Failed to add search_vector (maybe not Postgres?): {e}")
                
                # ---------------- Database Multi-Category Migration ----------------
                # 1. Add selected_categories to users table
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='selected_categories'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: selected_categories to users table")
                    c.execute("ALTER TABLE users ADD COLUMN selected_categories TEXT DEFAULT NULL")

                # 2. Check if posts still has 'category' column
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='posts' AND column_name='category'
                """)
                has_category_column = c.fetchone()

                if has_category_column:
                    # Create junction table
                    c.execute('''
                        CREATE TABLE IF NOT EXISTS post_categories (
                            post_id INTEGER REFERENCES posts(post_id) ON DELETE CASCADE,
                            category_code TEXT,
                            PRIMARY KEY (post_id, category_code)
                        )
                    ''')
                    # FIX: added category migration
                    c.execute("""
                        INSERT INTO post_categories (post_id, category_code)
                        SELECT post_id, category FROM posts 
                        WHERE category IS NOT NULL
                        ON CONFLICT DO NOTHING
                    """)
                    # Then drop the category column
                    c.execute("ALTER TABLE posts DROP COLUMN category")
                    logger.info("Migrated posts to multi-category (post_categories table)")

                # ---------------- Weekly Contributor History Migration ----------------
                c.execute("""
                    CREATE TABLE IF NOT EXISTS weekly_rankings (
                        id SERIAL PRIMARY KEY,
                        user_id TEXT REFERENCES users(user_id),
                        week_start DATE NOT NULL,
                        rank INTEGER NOT NULL,
                        points_earned INTEGER,
                        badge_emoji TEXT,
                        UNIQUE(user_id, week_start)
                    )
                """)

                # ---------------- Reports Table ----------------
                c.execute('''
                    CREATE TABLE IF NOT EXISTS reports (
                        report_id SERIAL PRIMARY KEY,
                        reporter_id TEXT REFERENCES users(user_id),
                        target_type TEXT NOT NULL,
                        target_id INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        reviewed_by TEXT,
                        reviewed_at TIMESTAMP,
                        action_taken TEXT
                    )
                ''')

                # ---------------- warning_count column migration ----------------
                c.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='users' AND column_name='warning_count'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: warning_count to users table")
                    c.execute("ALTER TABLE users ADD COLUMN warning_count INTEGER DEFAULT 0")

                # FIX: Added telegram_message_id to comments for cross-page threading
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='comments' AND column_name='telegram_message_id'
                """)
                if not c.fetchone():
                    logger.info("Adding telegram_message_id column to comments table")
                    c.execute("ALTER TABLE comments ADD COLUMN telegram_message_id BIGINT DEFAULT NULL")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_telegram_message_id ON comments(telegram_message_id)")

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
# ==================== LOADING ANIMATIONS ====================
def assign_vent_numbers_to_existing_posts():
    """Assign vent numbers to existing approved posts"""
    try:
        # Get all approved posts without vent numbers
        posts = db_fetch_all(
            "SELECT post_id FROM posts WHERE approved = TRUE AND vent_number IS NULL ORDER BY timestamp ASC"
        )
        
        if not posts:
            return
        
        # Get current max vent number
        max_vent = db_fetch_one("SELECT MAX(vent_number) as max_num FROM posts WHERE approved = TRUE")
        next_vent_number = (max_vent['max_num'] or 0) + 1
        
        # Assign numbers sequentially
        for post in posts:
            db_execute(
                "UPDATE posts SET vent_number = %s WHERE post_id = %s",
                (next_vent_number, post['post_id'])
            )
            
            # Try to update the channel post if it exists
            post_data = db_fetch_one(
                "SELECT content, category, channel_message_id FROM posts WHERE post_id = %s",
                (post['post_id'],)
            )
            
            if post_data and post_data['channel_message_id']:
                logger.info(f"Post {post['post_id']} should be updated to Vent - {next_vent_number:03d}")
            
            next_vent_number += 1
        
        logger.info(f"Assigned vent numbers to {len(posts)} existing posts")
        
    except Exception as e:
        logger.error(f"Error assigning vent numbers: {e}")

async def fix_vent_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix vent numbers"""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    
    await update.message.reply_text("🔄 Reassigning vent numbers to all approved posts...")
    
    try:
        # Reset all vent numbers first
        db_execute("UPDATE posts SET vent_number = NULL WHERE approved = TRUE")
        
        # Get all approved posts in chronological order
        posts = db_fetch_all(
            "SELECT post_id FROM posts WHERE approved = TRUE ORDER BY timestamp ASC"
        )
        
        count = 0
        for idx, post in enumerate(posts, start=1):
            db_execute(
                "UPDATE posts SET vent_number = %s WHERE post_id = %s",
                (idx, post['post_id'])
            )
            count += 1
        
        await update.message.reply_text(f"✅ Successfully assigned vent numbers to {count} posts.")
        
    except Exception as e:
        logger.error(f"Error in fix_vent_numbers: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def reset_weekly_badges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger weekly badge awarding."""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    
    if not user or not user['is_admin']:
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    
    await update.message.reply_text("🔄 Recalculating weekly contributors and announcing...")
    await award_weekly_badges(context)
    await update.message.reply_text("✅ Weekly contributors have been announced.")
def is_media_message(message):
    """Check if a message contains media"""
    return (message.photo or message.voice or message.video or 
            message.document or message.audio or message.sticker or 
            message.animation)
async def show_loading(update_or_message, loading_text="⏳ Processing...", edit_message=True):
    """Show a loading animation"""
    try:
        if hasattr(update_or_message, 'callback_query') and update_or_message.callback_query:
            # For callback queries
            loading_msg = await update_or_message.callback_query.message.edit_text(loading_text)
            return loading_msg
        elif hasattr(update_or_message, 'edit_text'):
            # For messages that can be edited
            if edit_message:
                loading_msg = await update_or_message.edit_text(loading_text)
                return loading_msg
        elif hasattr(update_or_message, 'reply_text'):
            # For new messages
            loading_msg = await update_or_message.reply_text(loading_text)
            return loading_msg
        elif hasattr(update_or_message, 'message'):
            # For update objects with message
            loading_msg = await update_or_message.message.reply_text(loading_text)
            return loading_msg
    except Exception as e:
        logger.error(f"Error showing loading: {e}")
        return None

async def typing_animation(context, chat_id, duration=1):
    """Show typing indicator"""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(duration)
    except:
        pass

async def animated_loading(loading_msg, text="Processing", steps=3):
    """Show animated loading dots"""
    try:
        for i in range(steps):
            dots = "." * (i + 1)
            await loading_msg.edit_text(f"{text}{dots}")
            await asyncio.sleep(0.3)
    except:
        pass

async def replace_with_success(loading_msg, success_text):
    """Replace loading message with success message"""
    try:
        success_msg = await loading_msg.edit_text(f"✅ {success_text}")
        await asyncio.sleep(1)
        return success_msg
    except:
        return loading_msg

async def replace_with_error(loading_msg, error_text):
    """Replace loading message with error message"""
    try:
        await loading_msg.edit_text(f"❌ {error_text}")
        await asyncio.sleep(2)
        return loading_msg
    except:
        return loading_msg
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
async def reset_user_waiting_states(user_id: str, chat_id: int = None, context: ContextTypes.DEFAULT_TYPE = None):
    """Reset all waiting states for a user and optionally restore main menu"""
    # Reset database states
    db_execute('''
        UPDATE users 
        SET waiting_for_post = FALSE, 
            waiting_for_comment = FALSE, 
            awaiting_name = FALSE,
            waiting_for_private_message = FALSE,
            awaiting_bio = FALSE,
            selected_category = NULL,
            selected_categories = NULL,
            comment_post_id = NULL,
            comment_idx = NULL,
            private_message_target = NULL
        WHERE user_id = %s
    ''', (user_id,))
    
    # Reset context flags
    if context:
        context_keys = ['editing_comment', 'editing_post', 'thread_from_post_id', 
                       'pending_post', 'broadcasting', 'broadcast_step', 'broadcast_type',
                       'rejecting_post', 'awaiting_rejection_reason', 'reporting']
        for key in context_keys:
            if key in context.user_data:
                del context.user_data[key]

    
    # If chat_id and context are provided, restore main menu
    if chat_id and context:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="What would you like to do next?",
                reply_markup=get_main_menu(user_id)
            )

        except Exception as e:
            logger.error(f"Error restoring main menu: {e}")

def fix_orphaned_comments_for_post(post_id: int):
    """Scan and fix orphaned replies for a specific post"""
    try:
        # Find comments for this post where parent doesn't exist
        # parent_comment_id != 0 AND parent_comment_id NOT IN (SELECT comment_id FROM comments)
        orphans = db_fetch_all("""
            SELECT comment_id, parent_comment_id 
            FROM comments 
            WHERE post_id = %s 
            AND parent_comment_id != 0 
            AND parent_comment_id NOT IN (SELECT comment_id FROM comments)
        """, (post_id,))
        
        if not orphans:
            return 0
            
        count = 0
        for orphan in orphans:
            db_execute(
                "UPDATE comments SET parent_comment_id = 0 WHERE comment_id = %s",
                (orphan['comment_id'],)
            )
            logger.info(f"Adopted comment {orphan['comment_id']} to top-level because parent {orphan['parent_comment_id']} was missing for post {post_id}")
            count += 1
            
        return count
    except Exception as e:
        logger.error(f"Error fixing orphans for post {post_id}: {e}")
        return 0

async def adopt_orphaned_replies(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Helper to fix orphans and update channel count"""
    fixed_count = fix_orphaned_comments_for_post(post_id)
    
    # Recalculate total count
    new_count = count_all_comments(post_id)
    
    # Update DB column
    db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (new_count, post_id))
    
    # Update channel button
    await update_channel_post_comment_count(context, post_id)
    
    return fixed_count

async def recount_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix orphans and update comment counts for all posts"""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("❌ You don't have permission to use this command.")
        return
        
    status_msg = await update.message.reply_text("🔄 Scanning all posts and fixing comment counts...")
    
    try:
        # Get all approved posts
        posts = db_fetch_all("SELECT post_id FROM posts WHERE approved = TRUE")
        
        posts_scanned = len(posts)
        posts_fixed = 0
        orphans_adopted = 0
        
        for post in posts:
            post_id = post['post_id']
            
            # Adopt orphans for this post
            fixed = fix_orphaned_comments_for_post(post_id)
            if fixed > 0:
                orphans_adopted += fixed
                
            # Recalculate count
            actual_count = count_all_comments(post_id)
            
            # Get current DB count
            db_post = db_fetch_one("SELECT comment_count FROM posts WHERE post_id = %s", (post_id,))
            current_db_count = db_post['comment_count'] if db_post else 0
            
            if actual_count != current_db_count or fixed > 0:
                # Update DB
                db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (actual_count, post_id))
                posts_fixed += 1
                
                # Update channel button if possible
                try:
                    await update_channel_post_comment_count(context, post_id)
                except Exception as e:
                    logger.error(f"Failed to update channel button for post {post_id}: {e}")
                    
        report = (
            f"✅ *Comment Recount Complete*\n\n"
            f"• 📁 Posts Scanned: {posts_scanned}\n"
            f"• 🛠 Posts Updated: {posts_fixed}\n"
            f"• 🐣 Orphans Adopted: {orphans_adopted}"
        )
        await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in recount_comments: {e}")
        await status_msg.edit_text(f"❌ Error during recount: {str(e)}")
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
    ("🎶 Worship & Music", "WorshipMusic"),
    ("🏠 Family Issues", "Family"),
    ("🙌 Testimony", "Testimony"),
    ("💊 Addiction & Recovery", "AddictionRecovery"),
    ("📖 Bible Question", "BibleQuestion"),
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

def build_multi_category_keyboard(selected_codes):
    """Return InlineKeyboardMarkup with checkboxes for given selected codes."""
    keyboard = []
    row = []
    for display, code in CATEGORIES:
        if code in selected_codes:
            button_text = f"✅ {display}"
        else:
            button_text = display
            
        row.append(InlineKeyboardButton(button_text, callback_data=f"cat_toggle_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    # Action row
    keyboard.append([
        InlineKeyboardButton("✅ Done", callback_data="cat_done"),
        InlineKeyboardButton("🔄 Reset", callback_data="cat_reset")
    ])
    keyboard.append([
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_input")
    ])
    return InlineKeyboardMarkup(keyboard)


# Initialize Flask app for Render health checks
flask_app = Flask(__name__, static_folder='static')

# ==================== FLASK ROUTES ====================

# Root shows mini app
# Root shows mini app with token check
@flask_app.route('/')
def main_page():
    """Show mini app with authentication check"""
    # Check if there's a token in the URL
    token = request.args.get('token')
    
    if not token:
        # No token - redirect to login page
        return redirect('/login')
    
    # Verify the token
    try:
        response = requests.get(f'{request.host_url}api/verify-token/{token}')
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                # Token is valid, show mini app with user info
                return mini_app_page()
    except Exception as e:
        logger.error(f"Error verifying token: {e}")
    
    # Invalid token or error - redirect to login
    return redirect('/login')

# Login page for mini app
@flask_app.route('/login')
def login_page():
    """Show login page for mini app with brand colors"""
    bot_username = BOT_USERNAME
    primary = PRIMARY_COLOR
    secondary = SECONDARY_COLOR
    card_bg = CARD_BG_COLOR
    border = BORDER_COLOR
    text_color = TEXT_COLOR
    primary_rgb = PRIMARY_RGB

    html = '''<!DOCTYPE html>
<html>
<head>
    <title>Christian Vent - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --primary: __PRIMARY__;
            --primary-rgb: __PRIMARY_RGB__;
            --secondary: __SECONDARY__;
            --card-bg: __CARD_BG__;
            --border: __BORDER__;
            --text: __TEXT_COLOR__;
        }
        * {
            box-sizing: border-box;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, var(--secondary) 0%, rgba(var(--primary-rgb), 0.1) 100%);
            color: var(--text);
            margin: 0;
            padding: 20px;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .login-container {
            background: rgba(var(--card-bg), 0.7);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            padding: 2.5rem;
            border-radius: 20px;
            border: 1px solid rgba(var(--primary-rgb), 0.15);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
            max-width: 440px;
            width: 100%;
            text-align: center;
            animation: fadeIn 0.6s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .brand {
            margin-bottom: 24px;
        }
        .logo {
            width: 72px;
            height: auto;
            border-radius: 18px;
            margin-bottom: 16px;
            box-shadow: 0 6px 16px rgba(var(--primary-rgb), 0.25);
        }
        .title {
            color: var(--primary);
            font-size: 1.4rem;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            margin: 0 0 8px 0;
        }
        .subtitle {
            opacity: 0.75;
            font-size: 0.95rem;
            line-height: 1.5;
            margin: 0;
        }
        .telegram-btn {
            background: #0088cc;
            background: linear-gradient(135deg, #0088cc, #0077b3);
            color: white;
            border: none;
            padding: 14px 28px;
            border-radius: 12px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            margin-bottom: 16px;
            text-decoration: none;
            display: inline-block;
            transition: all 0.3s ease;
            box-shadow: 0 4px 12px rgba(0, 136, 204, 0.25);
        }
        .telegram-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(0, 136, 204, 0.4);
            background: linear-gradient(135deg, #0099e6, #0088cc);
        }
        .bot-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: 600;
            transition: opacity 0.2s;
        }
        .bot-link:hover {
            opacity: 0.8;
            text-decoration: underline;
        }
        .features {
            text-align: left;
            margin-top: 32px;
            background: rgba(var(--primary-rgb), 0.04);
            padding: 20px;
            border-radius: 14px;
            border: 1px solid rgba(var(--primary-rgb), 0.08);
        }
        .features h3 {
            color: var(--primary);
            margin: 0 0 12px 0;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 700;
        }
        .features ul {
            padding-left: 20px;
            margin: 0;
            font-size: 0.9rem;
            opacity: 0.85;
            line-height: 1.7;
        }
        .features li {
            margin-bottom: 8px;
        }
        .features li:last-child {
            margin-bottom: 0;
        }
        .footer-text {
            margin-top: 24px;
            font-size: 0.8rem;
            opacity: 0.5;
            line-height: 1.5;
        }
        
        /* Auth Screen Styles */
        .auth-container {
            display: flex; 
            justify-content: center; 
            align-items: center; 
            height: 100vh; 
            background: linear-gradient(135deg, var(--secondary) 0%, rgba(var(--primary-rgb), 0.1) 100%); 
            color: var(--text); 
            flex-direction: column;
            font-family: 'Inter', sans-serif;
            animation: fadeIn 0.4s ease-out;
        }
        .auth-spinner {
            width: 44px;
            height: 44px;
            border: 3px solid rgba(var(--primary-rgb), 0.15);
            border-radius: 50%;
            border-top-color: var(--primary);
            animation: spin 1s ease-in-out infinite;
            margin-bottom: 24px;
        }
        .auth-title {
            color: var(--primary); 
            font-size: 1.1rem; 
            font-weight: 600; 
            letter-spacing: 1.5px;
            margin: 0 0 8px 0;
            text-transform: uppercase;
        }
        .auth-subtitle {
            opacity: 0.6;
            font-size: 0.9rem;
            margin: 0;
            font-weight: 500;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="brand">
            <img src="/static/images/vent%20logo.png" class="logo" alt="Christian Vent Logo">
            <h1 class="title">Christian Vent</h1>
            <p class="subtitle">Share your thoughts anonymously</p>
        </div>
        
        <p style="font-size: 0.9rem; opacity: 0.8; margin-bottom: 16px;">Please authenticate with the Telegram bot:</p>
        <a href="https://t.me/__BOT_USERNAME__" class="telegram-btn" target="_blank">Open Telegram Bot</a>
        <p style="font-size: 0.9rem; margin-top: 0;">Or use: <a href="https://t.me/__BOT_USERNAME__" class="bot-link" target="_blank">@__BOT_USERNAME__</a></p>
        
        <div class="features">
            <h3>Features</h3>
            <ul>
                <li>Share anonymous vents and prayers</li>
                <li>Join community discussions</li>
                <li>View the leaderboard</li>
                <li>Manage profile settings</li>
            </ul>
        </div>
        <p class="footer-text">
            After opening the bot, use the /webapp command to get authenticated access to the mini app.
        </p>
    </div>

    <script>
        // Auto-login via Telegram WebApp initData
        const tg = window.Telegram?.WebApp;
        if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
            tg.ready();
            const userId = tg.initDataUnsafe.user.id;
            
            // Show a premium temporary loading state
            document.body.innerHTML = `
                <div class="auth-container">
                    <div class="auth-spinner"></div>
                    <h2 class="auth-title">Authenticating</h2>
                    <p class="auth-subtitle">Securing your connection...</p>
                </div>
            `;
            
            fetch('/api/generate-token/' + userId)
                .then(r => r.json())
                .then(data => {
                    if (data.success && data.token) {
                        window.location.replace('/?token=' + data.token);
                    }
                })
                .catch(e => console.error("Auto-login failed:", e));
        }
    </script>
</body>
</html>'''

    html = html.replace('__PRIMARY__', primary)
    html = html.replace('__PRIMARY_RGB__', primary_rgb)
    html = html.replace('__SECONDARY__', secondary)
    html = html.replace('__CARD_BG__', card_bg)
    html = html.replace('__BORDER__', border)
    html = html.replace('__TEXT_COLOR__', text_color)
    html = html.replace('__BOT_USERNAME__', bot_username)
    return html
# Generate token for mini app (called by bot)
@flask_app.route('/api/generate-token/<user_id>')
def generate_token(user_id):
    """Generate a token for mini app authentication"""
    try:
        # Create JWT token that expires in 30 days
        token = jwt.encode(
            {
                'user_id': user_id,
                'exp': datetime.now(timezone.utc) + timedelta(days=30)
            },
            TOKEN,  # Use your bot token as secret key
            algorithm='HS256'
        )
        
        return jsonify({
            'success': True,
            'token': token
        })
    except Exception as e:
        logger.error(f"Error generating token: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Verify token
@flask_app.route('/api/verify-token/<token>')
def verify_token(token):
    """Verify JWT token - SIMPLIFIED VERSION"""
    try:
        # Try to decode the token
        decoded = jwt.decode(token, TOKEN, algorithms=['HS256'])
        user_id = decoded.get('user_id')
        
        if not user_id:
            return jsonify({'success': False, 'error': 'Invalid token format'}), 401
        
        # Check if user exists
        user = db_fetch_one("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 401
        
        return jsonify({
            'success': True,
            'user_id': user_id
        })
        
    except jwt.ExpiredSignatureError:
        return jsonify({'success': False, 'error': 'Token expired'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'success': False, 'error': 'Invalid token'}), 401
    except Exception as e:
        logger.error(f"Error verifying token: {e}")
        return jsonify({'success': False, 'error': 'Token verification failed'}), 500
@flask_app.route('/test-api')
def test_api():
    """Test if API endpoints are working"""
    return jsonify({
        'status': 'OK',
        'endpoints': {
            'submit_vent': '/api/mini-app/submit-vent (POST)',
            'get_posts': '/api/mini-app/get-posts (GET)',
            'leaderboard': '/api/mini-app/leaderboard (GET)',
            'profile': '/api/mini-app/profile/<user_id> (GET)',
            'verify_token': '/api/verify-token/<token> (GET)'
        }
    })
# Health check for Render
@flask_app.route('/health')
def health_check():
    return jsonify(status="OK", message="Christian Chat Bot is running")

# Handle favicon request
@flask_app.route('/favicon.ico')
def favicon():
    return '', 404  # Return empty 404 for favicon

# UptimeRobot ping
@flask_app.route('/ping')
def uptimerobot_ping():
    return jsonify(status="OK", message="Pong! Bot is alive")

# Serve static files
@flask_app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files"""
    try:
        return send_from_directory('static', filename)
    except Exception as e:
        return f"Error loading file: {e}", 404

# Helper to get dynamic main menu with token
def get_main_menu(user_id: str):
    """Generate the main menu keyboard with a dynamic user token for the Web App"""
    try:
        # Generate a secure JWT token (valid for 30 days)
        token = jwt.encode(
            {
                'user_id': str(user_id),
                'exp': datetime.now(timezone.utc) + timedelta(days=30)
            },
            TOKEN,
            algorithm='HS256'
        )
        
        render_url = os.getenv('RENDER_URL', 'https://your-render-url.onrender.com')
        mini_app_url = f"{render_url}/?token={token}"
        
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton("✍️ Share")],
                [KeyboardButton("👤 Profile"), KeyboardButton("📚 Posts")],
                [KeyboardButton("🏆 Top"), KeyboardButton("⚙️ Settings")],
                [KeyboardButton("🌐 Open App", web_app=WebAppInfo(url=mini_app_url))]
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
            input_field_placeholder="Choose option"
        )
    except Exception as e:
        logger.error(f"Error generating dynamic menu: {e}")
        # Fallback to menu without Web App button if something fails
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton("✍️ Share")],
                [KeyboardButton("👤 Profile"), KeyboardButton("📚 Posts")],
                [KeyboardButton("🏆 Top"), KeyboardButton("⚙️ Settings")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
            input_field_placeholder="Choose option"
        )

# Fallback for static contexts if needed (can be removed later)
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("✍️ Share")],
        [KeyboardButton("👤 Profile"), KeyboardButton("📚 Posts")],
        [KeyboardButton("🏆 Top"), KeyboardButton("⚙️ Settings")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True,
    input_field_placeholder="Choose option"
)


# Cancel-only menu for input states
cancel_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("❌ Cancel")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True
)


def create_anonymous_name(user_id):
    # Simply return "Anonymous" without numbers for all new users
    return "Anonymous"

@lru_cache(maxsize=1024)
def calculate_user_rating(user_id):
    # Weighted Scoring Logic:
    # Approved Posts: +10 | Comments: +2 | Likes: +1 | Dislikes: -2 | Blocks: -10
    
    # 1. Post Points (+10 per approved post)
    post_res = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE", (user_id,))
    post_points = (post_res['count'] if post_res else 0) * 10
    
    # 2. Comment Points (+2 per comment)
    comm_res = db_fetch_one("SELECT COUNT(*) as count FROM comments WHERE author_id = %s", (user_id,))
    comm_points = (comm_res['count'] if comm_res else 0) * 2
    
    # 3. Reactions Points (Likes +1, Dislikes -2)
    # We join with comments to find reactions ON the user's content
    rx_res = db_fetch_one("""
        SELECT 
            SUM(CASE WHEN r.type = 'like' THEN 1 ELSE 0 END) as likes,
            SUM(CASE WHEN r.type = 'dislike' THEN 1 ELSE 0 END) as dislikes
        FROM reactions r
        JOIN comments c ON r.comment_id = c.comment_id
        WHERE c.author_id = %s
    """, (user_id,))
    
    likes = rx_res['likes'] if rx_res and rx_res['likes'] else 0
    dislikes = rx_res['dislikes'] if rx_res and rx_res['dislikes'] else 0
    rx_points = (likes * 1) - (dislikes * 2)
    
    # 4. Block Points (-10 per block received)
    block_res = db_fetch_one("SELECT COUNT(*) as count FROM blocks WHERE blocked_id = %s", (user_id,))
    block_points = (block_res['count'] if block_res else 0) * -10
    
    return post_points + comm_points + rx_points + block_points

def calculate_top_weekly_contributors():
    """Calculate top 3 users by aura points earned in the last 7 days."""
    query = """
        SELECT u.user_id,
               (COALESCE(p.post_points, 0) + COALESCE(c.comment_points, 0) + COALESCE(r.rx_points, 0) - COALESCE(b.block_points, 0)) as weekly_points
        FROM users u
        LEFT JOIN (
            SELECT author_id, COUNT(*) * 10 as post_points
            FROM posts 
            WHERE approved = TRUE AND timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY author_id
        ) p ON u.user_id = p.author_id
        LEFT JOIN (
            SELECT author_id, COUNT(*) * 2 as comment_points
            FROM comments 
            WHERE timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY author_id
        ) c ON u.user_id = c.author_id
        LEFT JOIN (
            SELECT c.author_id, 
                   SUM(CASE WHEN r.type = 'like' THEN 1 ELSE 0 END) - SUM(CASE WHEN r.type = 'dislike' THEN 2 ELSE 0 END) as rx_points
            FROM reactions r
            JOIN comments c ON r.comment_id = c.comment_id
            WHERE r.timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY c.author_id
        ) r ON u.user_id = r.author_id
        LEFT JOIN (
            SELECT blocked_id, COUNT(*) * 10 as block_points
            FROM blocks 
            WHERE timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY blocked_id
        ) b ON u.user_id = b.blocked_id
        WHERE (COALESCE(p.post_points, 0) + COALESCE(c.comment_points, 0) + COALESCE(r.rx_points, 0) - COALESCE(b.block_points, 0)) > 0
        ORDER BY weekly_points DESC
        LIMIT 3
    """
    return db_fetch_all(query)

async def award_weekly_badges(context: ContextTypes.DEFAULT_TYPE):
    """Weekly job to announce top contributors."""
    try:
        logger.info("🏆 Starting weekly contributor announcement job...")
        
        # 1. Calculate top 3
        top_users = calculate_top_weekly_contributors()
        if not top_users:
            logger.info("ℹ️ No users earned points this week. No announcement made.")
            return

        badges = ["🥇", "🥈", "🥉"]
        winners_info = []
        today = datetime.now(timezone.utc).date()
        
        # 2. Record and format
        for idx, user_data in enumerate(top_users):
            user_id = user_data['user_id']
            points = user_data['weekly_points']
            rank = idx + 1
            badge = badges[idx]
            
            # Record in history (optional but good for tracking)
            db_execute("""
                INSERT INTO weekly_rankings (user_id, week_start, rank, points_earned, badge_emoji)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, week_start) DO UPDATE 
                SET rank = EXCLUDED.rank, points_earned = EXCLUDED.points_earned, badge_emoji = EXCLUDED.badge_emoji
            """, (user_id, today, rank, points, badge))
            
            # Get user info for announcement
            user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_id,))
            name = user['anonymous_name'] if user else "Contributor"
            winners_info.append(f"{badge} {name} – {points} pts")
            
            # Notify winner via DM
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎊 *Weekly Highlight!* 🎊\n\nYou are one of the *Top Contributors* this week with *{points} points*!\n\nThank you for your valuable contributions and for being a light in the community! 🙏",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as dm_e:
                logger.warning(f"Could not send DM to weekly winner {user_id}: {dm_e}")

        # 3. Announce in Channel
        if CHANNEL_ID and winners_info:
            announcement = "🏆 *Weekly Top Contributors* 🏆\n\n" + "\n".join(winners_info) + \
                          "\n\nCongratulations! Thank you for being such a blessing to this community. ✨"
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=announcement,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as ch_e:
                logger.error(f"Failed to announce weekly winners in channel: {ch_e}")
                
        logger.info(f"✅ Weekly contributors announced: {len(winners_info)} users.")
        
    except Exception as e:
        logger.error(f"❌ Error in award_weekly_badges job: {e}")


@lru_cache(maxsize=128)
def format_aura(rating):
    """Create aura based on weighted contribution points."""
    if rating < 0:
        return "🔴"  # Red aura for negative rank (Shame)
    elif rating >= 500:
        return "👑"  # Crown aura for legendary contributors (500+ points)
    elif rating >= 100:
        return "🟣"  # Purple aura for elite users (100-499 points)
    elif rating >= 50:
        return "🔵"  # Blue aura for advanced users (50-99 points)
    elif rating >= 25:
        return "🟢"  # Green aura for intermediate users (25-49 points)
    elif rating >= 10:
        return "🟡"  # Yellow aura for active users (10-24 points)
    else:
        return "⚪️"  # White aura for new/neutral users (0-9 points)


def count_all_comments(post_id):
    """Get the total number of comments for a post using a single query."""
    try:
        row = db_fetch_one("SELECT COUNT(*) as cnt FROM comments WHERE post_id = %s", (post_id,))
        return row['cnt'] if row else 0
    except Exception as e:
        logger.error(f"Error in count_all_comments: {e}")
        return 0
def get_cancel_reply_keyboard():
    """Create cancel button for reply keyboard (text) - ONLY for input states"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("❌ Cancel")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,  # Set to True so it disappears after use
    )

def get_display_name(user_data):
    """Helper to get user's display name with sex emoji"""
    if not user_data:
        return "Anonymous"
    
    emoji = user_data.get('avatar_emoji') or ""
    name = user_data.get('anonymous_name') or "Anonymous"
    
    if emoji:
        return f"{emoji} {name}"
    return name

def get_display_sex(user_data):
    if user_data and user_data.get('sex'):
        return user_data['sex']
    return '👤'

def get_user_rank(user_id):
    users = db_fetch_all('''
        SELECT user_id, 
               (
                (SELECT COUNT(*) FROM posts p WHERE p.author_id = u.user_id AND p.approved = TRUE) * 10 +
                (SELECT COUNT(*) FROM comments c WHERE c.author_id = u.user_id) * 2 +
                COALESCE((
                    SELECT SUM(CASE WHEN r.type = 'like' THEN 1 WHEN r.type = 'dislike' THEN -2 ELSE 0 END)
                    FROM reactions r
                    JOIN comments c2 ON r.comment_id = c2.comment_id
                    WHERE c2.author_id = u.user_id
                ), 0) -
                (SELECT COUNT(*) FROM blocks b WHERE b.blocked_id = u.user_id) * 10
               ) as total
        FROM users u
        WHERE u.is_admin = FALSE
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
            [InlineKeyboardButton(f"💬 Add/view Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
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
    chat_id = update.effective_chat.id
    
    # Show typing animation
    await typing_animation(context, chat_id, 0.5)
    
    # Show loading
    loading_msg = None
    try:
        if update.message:
            loading_msg = await update.message.reply_text("📊 Gathering statistics...")
        elif update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("📊 Gathering statistics...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Loading leaderboard", 3)
    
    # Get top 10 users with weighted aura
    top_users = db_fetch_all('''
        SELECT u.user_id, u.anonymous_name, u.sex, u.avatar_emoji,
               (
                (SELECT COUNT(*) FROM posts p WHERE p.author_id = u.user_id AND p.approved = TRUE) * 10 +
                (SELECT COUNT(*) FROM comments c WHERE c.author_id = u.user_id) * 2 +
                COALESCE((
                    SELECT SUM(CASE WHEN r.type = 'like' THEN 1 WHEN r.type = 'dislike' THEN -2 ELSE 0 END)
                    FROM reactions r
                    JOIN comments c2 ON r.comment_id = c2.comment_id
                    WHERE c2.author_id = u.user_id
                ), 0) -
                (SELECT COUNT(*) FROM blocks b WHERE b.blocked_id = u.user_id) * 10
               ) as total
        FROM users u
        WHERE u.is_admin = FALSE
        ORDER BY total DESC
        LIMIT 10
    ''')

    
    # Create clean header
    leaderboard_text = "*🏆 Christian Vent Leaderboard*\n\n"
    
    # Define medal emojis for top 3
    medal_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    
    # Format each user
    for idx, user in enumerate(top_users, start=1):
        safe_name = escape_markdown(user['anonymous_name'], version=2)
        safe_sex = escape_markdown(user['sex'], version=2)
        safe_total = escape_markdown(str(user['total']), version=2)
        safe_aura = escape_markdown(format_aura(user['total']), version=2)
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{user['user_id']}"
        
        # Create clean line
        if idx <= 3:
            rank_prefix = medal_emojis[idx]
        else:
            rank_prefix = f"{idx}."
        
        safe_rank = escape_markdown(rank_prefix, version=2)

        leaderboard_text += (
            f"{safe_rank} {safe_sex} "
            f"[{safe_name}]({profile_link})\n"
            f"   {safe_total} pts {safe_aura}\n\n"
        )

    
    # Add current user's rank
    user_id = str(update.effective_user.id)
    user_rank = get_user_rank(user_id)
    
    if user_rank:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = calculate_user_rating(user_id)
            safe_user_name = escape_markdown(user_data['anonymous_name'], version=2)
            safe_user_sex = escape_markdown(user_data['sex'], version=2)
            safe_user_aura = escape_markdown(format_aura(user_contributions), version=2)
            safe_user_pts = escape_markdown(str(user_contributions), version=2)
            safe_user_rank = escape_markdown(str(user_rank), version=2)
            
            leaderboard_text += f"*Your position:* {safe_user_rank}\n"
            leaderboard_text += f"{safe_user_sex} {safe_user_name} • {safe_user_pts} pts {safe_user_aura}\n\n"
    
    # Add subtle footer
    leaderboard_text += "_Click names to view profiles • Updated daily_"

    
    # Create clean buttons
    keyboard = [
        [InlineKeyboardButton("📱 Menu", callback_data='menu')],
        [InlineKeyboardButton("👤 My Profile", callback_data='profile')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Replace loading message with content
    try:
        if loading_msg:
            await animated_loading(loading_msg, "Finalizing", 1)
            await loading_msg.edit_text(
                leaderboard_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        else:
            if update.message:
                await update.message.reply_text(
                    leaderboard_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
            elif update.callback_query:
                try:
                    await update.callback_query.edit_message_text(
                        leaderboard_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True
                    )
                except BadRequest:
                    await update.callback_query.message.reply_text(
                        leaderboard_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True
                    )
    except Exception as e:
        logger.error(f"Error showing leaderboard: {e}")
        if loading_msg:
            try:
                await loading_msg.edit_text("❌ Error loading leaderboard. Please try again.")
            except:
                pass

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
                InlineKeyboardButton("🚫 Blocked Users", callback_data='list_blocked')
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
        thread_post = db_fetch_one("SELECT content, channel_message_id FROM posts WHERE post_id = %s", (thread_from_post_id,))
        if thread_post:
            thread_preview = thread_post['content'][:100] + '...' if len(thread_post['content']) > 100 else thread_post['content']
            if thread_post['channel_message_id']:
                thread_text = f"🔄 *Thread continuation from your previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
            else:
                thread_text = f"🔄 *Threading from previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
    
    # Format categories for preview
    category_list = category.split(',') if category else []
    cat_display = ", ".join(category_list)
    
    preview_text = (
        f"{thread_text}📝 *Post Preview* [{escape_markdown(cat_display, 2)}]\n\n"
        f"{escape_markdown(post_content, version=2)}\n\n"
        f"Please confirm your post\\:"
    )

    
    context.user_data['pending_post'] = {
        'content': post_content,
        'category': category, # Keep as comma-separated string
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
                # For media messages, edit the caption instead of text
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
        
        # Fallback for callback queries with media
        if update.callback_query and media_type != 'text':
            try:
                # Try to send as a new message instead
                await update.callback_query.message.reply_text(
                    f"📝 *Post Preview* [{cat_display}]\n\n"
                    f"{escape_markdown(post_content, version=2)}\n\n"
                    f"Please confirm your post:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e2:
                logger.error(f"Fallback also failed: {e2}")
                
        elif update.message:
            await update.message.reply_text("❌ Error showing confirmation. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error showing confirmation. Please try again.")
async def notify_vent_author_of_comment(context: ContextTypes.DEFAULT_TYPE, post_id: int, commenter_id: str):
    """Notify the post author when a new top‑level comment is added."""
    try:
        post = db_fetch_one("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        
        author_id = post['author_id']
        if author_id == commenter_id:
            return
        
        author = db_fetch_one("SELECT user_id, notifications_enabled FROM users WHERE user_id = %s", (author_id,))
        if not author or not author['notifications_enabled']:
            return
        
        commenter = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (commenter_id,))
        commenter_name = get_display_name(commenter)
        
        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
        
        # Use HTML parsing – no need to escape markdown special characters
        import html
        safe_commenter_name = html.escape(commenter_name)
        safe_post_preview = html.escape(post_preview)
        
        notification_text = (
            f"💬 <b>New comment on your vent!</b>\n\n"
            f"👤 {safe_commenter_name} commented:\n\n"
            f"📝 <b>Your vent:</b> {safe_post_preview}\n\n"
            f"🔗 <a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>"
        )
        
        await context.bot.send_message(
            chat_id=author_id,
            text=notification_text,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Error notifying vent author: {e}")
async def notify_user_of_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, comment_id: int, replier_id: str):
    try:
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        if not comment:
            return
        
        original_author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (comment['author_id'],))
        if not original_author or not original_author['notifications_enabled']:
            return
        
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
            
        # === FIX: Vent author anonymization in reply notification ===
        if str(replier_id) == str(post['author_id']):
            replier_display = "Vent author"
            safe_replier_name = replier_display
        else:
            replier = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (replier_id,))
            replier_name = get_display_name(replier)
            safe_replier_name = escape_markdown(replier_name, version=2)
        
        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
        
        safe_post_preview = escape_markdown(post_preview, version=2)
        safe_comment_preview = escape_markdown(comment['content'][:100], version=2)

        notification_text = (
            f"💬 {safe_replier_name} replied to your comment\\:\n\n"
            f"🗨 {safe_comment_preview}\n\n"
            f"📝 Post\\: {safe_post_preview}\n\n"
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

# Update the submit vent endpoint to use this
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
        
        safe_sender_name = escape_markdown(sender_name, version=2)
        safe_preview_content = escape_markdown(preview_content, version=2)

        notification_text = (
            f"📩 *New Private Message*\n\n"
            f"👤 From: {safe_sender_name}\n\n"
            f"💬 {safe_preview_content}\n\n"
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
    
    # Get statistics for display
    pending_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = FALSE")
    pending_count = pending_posts['count'] if pending_posts else 0
    
    total_users = db_fetch_one("SELECT COUNT(*) as count FROM users")
    users_count = total_users['count'] if total_users else 0
    
    active_today = db_fetch_one('''
        SELECT COUNT(DISTINCT user_id) as count 
        FROM (
            SELECT author_id as user_id FROM posts WHERE DATE(timestamp) = CURRENT_DATE
            UNION 
            SELECT author_id as user_id FROM comments WHERE DATE(timestamp) = CURRENT_DATE
        ) AS active_users
    ''')
    active_count = active_today['count'] if active_today else 0
    
    keyboard = [
        [InlineKeyboardButton(f"📝 Pending Posts ({pending_count})", callback_data='admin_pending')],
        [InlineKeyboardButton(f"👥 Users: {users_count}", callback_data='admin_users')],
        [InlineKeyboardButton("📊 Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("📢 Send Broadcast", callback_data='admin_broadcast')],
        [InlineKeyboardButton("📋 Pending Reports", callback_data='admin_reports')],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data='menu')]
    ]
    
    text = (
        f"🛠 *Admin Panel*\n\n"
        f"📊 *Quick Stats:*\n"
        f"• Pending Posts: {pending_count}\n"
        f"• Total Users: {users_count}\n"
        f"• Active Today: {active_count}\n\n"
        f"Select an option below:"
    )
    
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Error in admin_panel: {e}")
        if update.message:
            await update.message.reply_text("❌ Error loading admin panel.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("❌ Error loading admin panel.")

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the broadcast process"""
    query = update.callback_query
    # Redundant answer removed to fix mobile toast bugs
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("❌ You don't have permission to access this.", show_alert=True)
        return
    
    # Set broadcast state
    context.user_data['broadcasting'] = True
    context.user_data['broadcast_step'] = 'waiting_for_content'
    
    # Show broadcast options
    keyboard = [
        [
            InlineKeyboardButton("📝 Text Broadcast", callback_data='broadcast_text'),
            InlineKeyboardButton("🖼️ Photo Broadcast", callback_data='broadcast_photo')
        ],
        [
            InlineKeyboardButton("🎵 Voice Broadcast", callback_data='broadcast_voice'),
            InlineKeyboardButton("📎 Other Media", callback_data='broadcast_other')
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data='admin_panel')
        ]
    ]
    
    text = (
        "📢 *Send Broadcast Message*\n\n"
        "Choose the type of broadcast you want to send:\n\n"
        "📝 *Text* - Send a text message to all users\n"
        "🖼️ *Photo* - Send a photo with caption\n"
        "🎵 *Voice* - Send a voice message\n"
        "📎 *Other* - Send other media types\n\n"
        "_All users will receive this message._"
    )
    
    await query.message.reply_text(
        text,
        reply_markup=cancel_menu,
        parse_mode=ParseMode.MARKDOWN
    )
    # Edit the original message to show options
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_broadcast_type(update: Update, context: ContextTypes.DEFAULT_TYPE, broadcast_type: str):
    """Handle broadcast type selection"""
    query = update.callback_query
    # Redundant answer removed to fix mobile toast bugs
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("❌ You don't have permission to access this.", show_alert=True)
        return
    
    # Set broadcast type
    context.user_data['broadcast_type'] = broadcast_type
    context.user_data['broadcast_step'] = 'waiting_for_content'
    
    # Ask for content based on type
    if broadcast_type == 'text':
        prompt = "✍️ *Please type your broadcast message:*\n\nYou can use markdown formatting."
    elif broadcast_type == 'photo':
        prompt = "🖼️ *Please send a photo with caption:*\n\nSend a photo and add a caption (optional)."
    elif broadcast_type == 'voice':
        prompt = "🎵 *Please send a voice message:*\n\nSend a voice message with optional caption."
    else:  # other
        prompt = "📎 *Please send your media:*\n\nYou can send any media type (photo, video, document, etc.) with optional caption."
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data='admin_panel')]]
    
    await query.message.reply_text(
        prompt,
        reply_markup=cancel_menu,
        parse_mode=ParseMode.MARKDOWN
    )
    # Edit the original message to show options
    await query.edit_message_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )


async def confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show broadcast confirmation with preview"""
    # Check if this is a callback query or regular message
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        user_id = str(query.from_user.id)
        is_callback = True
    else:
        # Handle case when called from handle_message
        user_id = str(update.effective_user.id)
        is_callback = False
    
    broadcast_data = context.user_data.get('broadcast_data', {})
    
    if not broadcast_data:
        if is_callback:
            await update.callback_query.answer("❌ No broadcast data found.", show_alert=True)
        else:
            await update.message.reply_text("❌ No broadcast data found.")
        return
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if is_callback:
            await update.callback_query.answer("❌ You don't have permission to access this.", show_alert=True)
        else:
            await update.message.reply_text("❌ You don't have permission to access this.")
        return
    
    # Get user count for confirmation
    total_users = db_fetch_one("SELECT COUNT(*) as count FROM users")
    users_count = total_users['count'] if total_users else 0
    
    text = (
        f"📢 *Broadcast Confirmation*\n\n"
        f"📊 *Recipients:* {users_count} users\n"
        f"📋 *Type:* {broadcast_data.get('type', 'text').title()}\n\n"
        f"📝 *Preview:*\n"
    )
    
    # Add content preview
    content = broadcast_data.get('content', '') or broadcast_data.get('caption', '')
    if content:
        if len(content) > 200:
            preview = content[:197] + "..."
        else:
            preview = content
        text += f"{preview}\n\n"
    
    text += "_Are you sure you want to send this broadcast to all users?_"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Send Broadcast", callback_data='execute_broadcast'),
            InlineKeyboardButton("✏️ Edit", callback_data='admin_broadcast')
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data='admin_panel')
        ]
    ]
    
    if is_callback:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def execute_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute the broadcast to all users"""
    # Check if this is a callback query
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        status_message = query.message
    else:
        # This shouldn't happen from messages, but handle it
        await update.message.reply_text("❌ This action can only be triggered from the confirmation menu.")
        return
    
    user_id = str(update.effective_user.id)
    broadcast_data = context.user_data.get('broadcast_data', {})
    
    if not broadcast_data:
        await query.answer("❌ No broadcast data found.", show_alert=True)
        return
    
    # Show processing message
    status_message = await query.edit_message_text(
        "📤 *Starting Broadcast...*\n\nPreparing to send to all users...",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Get all users (exclude the sender)
    all_users = db_fetch_all("SELECT user_id FROM users WHERE user_id != %s", (user_id,))
    total_users = len(all_users)
    
    if total_users == 0:
        await status_message.edit_text(
            "❌ No users to broadcast to.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Track statistics
    success_count = 0
    failed_count = 0
    blocked_count = 0
    
    # Prepare message based on type
    message_type = broadcast_data.get('type', 'text')
    content = broadcast_data.get('content', '')
    media_id = broadcast_data.get('media_id')
    caption = broadcast_data.get('caption', '')
    
    # Send to users in batches
    batch_size = 30  # Telegram rate limit
    
    for i, user in enumerate(all_users):
        try:
            # Update progress every batch
            if i % batch_size == 0:
                current_batch = i // batch_size + 1
                total_batches = (total_users + batch_size - 1) // batch_size
                progress = int((i / total_users) * 100)
                
                await status_message.edit_text(
                    f"📤 *Broadcasting...*\n\n"
                    f"📊 Progress: {progress}%\n"
                    f"✅ Sent: {success_count}\n"
                    f"❌ Failed: {failed_count}\n"
                    f"⏸️ Blocked: {blocked_count}\n"
                    f"🎯 Batch: {current_batch}/{total_batches}\n\n"
                    f"_Please wait..._",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            # Send based on message type
            if message_type == 'text':
                await context.bot.send_message(
                    chat_id=user['user_id'],
                    text=content,
                    parse_mode=ParseMode.MARKDOWN
                )
                
            elif message_type == 'photo' and media_id:
                await context.bot.send_photo(
                    chat_id=user['user_id'],
                    photo=media_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                
            elif message_type == 'voice' and media_id:
                await context.bot.send_voice(
                    chat_id=user['user_id'],
                    voice=media_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                
            elif message_type == 'document' and media_id:
                await context.bot.send_document(
                    chat_id=user['user_id'],
                    document=media_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )
                
            elif message_type == 'video' and media_id:
                await context.bot.send_video(
                    chat_id=user['user_id'],
                    video=media_id,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )
            
            success_count += 1
            
            # Small delay to respect rate limits
            if i % 10 == 0:
                await asyncio.sleep(0.1)
                
        except BadRequest as e:
            if "blocked" in str(e).lower() or "Forbidden" in str(e):
                blocked_count += 1
            else:
                failed_count += 1
                logger.error(f"Failed to send broadcast to {user['user_id']}: {e}")
        except Exception as e:
            failed_count += 1
            logger.error(f"Failed to send broadcast to {user['user_id']}: {e}")
    
    # Broadcast complete
    completion_time = datetime.now().strftime("%H:%M:%S")
    
    # Clean up
    if 'broadcasting' in context.user_data:
        del context.user_data['broadcasting']
    if 'broadcast_step' in context.user_data:
        del context.user_data['broadcast_step']
    if 'broadcast_type' in context.user_data:
        del context.user_data['broadcast_type']
    if 'broadcast_data' in context.user_data:
        del context.user_data['broadcast_data']
    
    # Show final report
    report_text = (
        f"✅ *Broadcast Complete!*\n\n"
        f"📅 Completed: {completion_time}\n"
        f"👥 Total Users: {total_users}\n"
        f"✅ Successfully Sent: {success_count}\n"
        f"❌ Failed: {failed_count}\n"
        f"⏸️ Blocked/Inactive: {blocked_count}\n"
        f"📈 Success Rate: {((success_count / total_users) * 100):.1f}%\n\n"
        f"🎯 _Broadcast delivered to {success_count} active users._"
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 Send Another", callback_data='admin_broadcast')],
        [InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    
    await status_message.edit_text(
        report_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
async def advanced_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Advanced broadcast with targeting options"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("❌ You don't have permission to access this.", show_alert=True)
        return
    
    # Get user statistics for targeting
    total_users = db_fetch_one("SELECT COUNT(*) as count FROM users")
    active_users = db_fetch_one('''
        SELECT COUNT(DISTINCT user_id) as count 
        FROM (
            SELECT author_id as user_id FROM posts WHERE DATE(timestamp) >= CURRENT_DATE - INTERVAL '7 days'
            UNION 
            SELECT author_id as user_id FROM comments WHERE DATE(timestamp) >= CURRENT_DATE - INTERVAL '7 days'
        ) AS active_users
    ''')
    
    text = (
        "🎯 *Advanced Broadcast*\n\n"
        f"📊 *User Statistics:*\n"
        f"• Total Users: {total_users['count'] if total_users else 0}\n"
        f"• Active (7 days): {active_users['count'] if active_users else 0}\n\n"
        "*Select targeting options:*"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("🌍 All Users", callback_data='target_all'),
            InlineKeyboardButton("🎯 Active Users", callback_data='target_active')
        ],
        [
            InlineKeyboardButton("👤 Specific User", callback_data='target_specific'),
            InlineKeyboardButton("🏷️ By Category", callback_data='target_category')
        ],
        [
            InlineKeyboardButton("📝 Text Only", callback_data='broadcast_text'),
            InlineKeyboardButton("🖼️ With Media", callback_data='broadcast_photo')
        ],
        [
            InlineKeyboardButton("🔙 Simple Broadcast", callback_data='admin_broadcast'),
            InlineKeyboardButton("❌ Cancel", callback_data='admin_panel')
        ]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
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
    
    # Get pending posts (simplified - no JOIN with pending_notifications)
    posts = db_fetch_all("""
        SELECT p.post_id, p.content, u.anonymous_name, p.media_type, p.media_id,
               STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.approved = FALSE
        GROUP BY p.post_id, u.anonymous_name, p.media_type, p.media_id, p.content, p.timestamp
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
        
        # Use HTML for more reliable escaping
        preview = post['content'][:400] + '...' if len(post['content']) > 400 else post['content']
        safe_preview = html.escape(preview)
        safe_name = html.escape(post['anonymous_name'] or "Anonymous")
        safe_cats = html.escape(post['categories'] or 'Other')
        
        text = f"📝 <b>Pending Post</b> [{safe_cats}]\n\n{safe_preview}\n\n👤 <b>{safe_name}</b>"
        
        try:
            if post['media_type'] == 'text':
                if update.callback_query:
                    await update.callback_query.message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_text(
                        text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
            elif post['media_type'] == 'photo':
                if update.callback_query:
                    await update.callback_query.message.reply_photo(
                        photo=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_photo(
                        photo=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
            elif post['media_type'] == 'voice':
                if update.callback_query:
                    await update.callback_query.message.reply_voice(
                        voice=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_voice(
                        voice=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
        except Exception as e:
            logger.error(f"Error sending pending post {post['post_id']}: {e}")
            # Send as text if media fails
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    f"❌ Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text(
                    f"❌ Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
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
        # Get the next vent number FIRST
        max_vent = db_fetch_one("SELECT MAX(vent_number) as max_num FROM posts WHERE approved = TRUE")
        next_vent_number = (max_vent['max_num'] or 0) + 1
        
        # Get categories for this post
        cats_row = db_fetch_all("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,))
        categories = [row['category_code'] for row in cats_row]
        hashtags = ' '.join([f"#{cat}" for cat in categories]) if categories else "#Other"
        
        # Create the vent number text (copyable format)
        vent_display = f"Vent - {next_vent_number:03d}"
        
        caption_text = (
            f"`{vent_display}`\n\n"
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtags}\n"
            f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
        # Create the comments button
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Add/View Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ])
        
        # Check if this is a thread continuation
        reply_to_message_id = None
        if post['thread_from_post_id']:
            # Get the original post's channel message ID
            original_post = db_fetch_one(
                "SELECT channel_message_id FROM posts WHERE post_id = %s", 
                (post['thread_from_post_id'],)
            )
            if original_post and original_post['channel_message_id']:
                reply_to_message_id = original_post['channel_message_id']
        
        # Send post to channel based on media type
        safe_content = html.escape(post['content'])
        safe_hashtags = html.escape(hashtags)
        channel_text = (
            f"<code>{vent_display}</code>\n\n"
            f"{safe_content}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{safe_hashtags}\n"
            f"<a href='https://t.me/christianvent'>Telegram</a> | <a href='https://t.me/{BOT_USERNAME}'>Bot</a>"
        )

        if post['media_type'] == 'text':
            msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=channel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )
        elif post['media_type'] == 'photo':
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post['media_id'],
                caption=channel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        elif post['media_type'] == 'voice':
            msg = await context.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=post['media_id'],
                caption=channel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        else:
            await query.answer("❌ Unsupported media type.", show_alert=True)
            return
        
        # Update the post in database with vent number
        success = db_execute(
            "UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s, vent_number = %s WHERE post_id = %s",
            (user_id, msg.message_id, next_vent_number, post_id)
        )
        
        # Clear Aura Cache for real-time accuracy
        calculate_user_rating.cache_clear()
        format_aura.cache_clear()

        
        if not success:
            await query.answer("❌ Failed to update database.", show_alert=True)
            return
        
        # Notify the author in background
        asyncio.create_task(context.bot.send_message(
            chat_id=post['author_id'],
            text="✅ Your post has been approved and published!"
        ))
        
        # =============================================
        # CRITICAL FIX: Update the admin's original message to remove Approve/Reject buttons
        # =============================================
        try:
            # Format categories for display
            categories_display = ', '.join(categories) if categories else 'None'
            
            # Edit the original admin notification message to show it's approved
            safe_cats_display = html.escape(categories_display)
            safe_content_preview = html.escape(post['content'][:150])
            await query.edit_message_text(
                f"✅ <b>Post Approved and Published!</b>\n\n"
                f"<b>Vent Number:</b> <code>{vent_display}</code>\n"
                f"<b>Categories:</b> {safe_cats_display}\n"
                f"<b>Published to channel:</b> ✅\n\n"
                f"<b>Content Preview:</b>\n{safe_content_preview}...",
                parse_mode=ParseMode.HTML
            )
            
            # Alternative: You can also delete the admin notification message entirely
            # await query.message.delete()
            
        except BadRequest as e:
            # If editing fails, at least reply with success message
            logger.error(f"Error updating admin message: {e}")
            await query.answer("✅ Post approved and published!", show_alert=True)
            await query.message.reply_text(
                f"✅ Post #{post_id} approved and published as {vent_display}!",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # =============================================
        # END CRITICAL FIX
        # =============================================
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        try:
            await query.answer(f"❌ Failed to approve post: {str(e)}", show_alert=True)
        except:
            # Try to edit the message with error
            try:
                await query.edit_message_text("❌ Failed to approve post. Please try again.")
            except:
                pass

async def ask_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Ask the admin if they want to provide a rejection reason"""
    query = update.callback_query
    context.user_data['rejecting_post'] = post_id
    context.user_data['awaiting_rejection_reason'] = False # Not yet typing, just menu
    
    keyboard = [
        [InlineKeyboardButton("✏️ Type Reason", callback_data=f"reject_with_reason_{post_id}")],
        [InlineKeyboardButton("⏩ Skip Reason", callback_data=f"skip_rejection_{post_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_rejection")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.edit_message_text(
            "❌ *Reject Post*\n\nWould you like to provide a reason for rejecting this post?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error showing rejection menu: {e}")
        await query.message.reply_text(
            "❌ Rejection Reason Prompt\n\nWould you like to provide a reason?",
            reply_markup=reply_markup
        )

async def finalize_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int, reason: str = None):
    """Perform the final rejection after admin makes a choice"""
    user_id = str(update.effective_user.id)
    
    # Get the post details before deleting
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        logger.warning(f"Post {post_id} not found during finalize_rejection")
        return

    # Truncate reason if too long
    if reason and len(reason) > 200:
        reason = reason[:197] + "..."
        if update.message:
            await update.message.reply_text("⚠️ Reason was too long and has been truncated to 200 characters.")
        elif update.callback_query:
            await update.callback_query.answer("⚠️ Reason truncated to 200 chars", show_alert=True)

    try:
        # Notify the author in background
        notification_text = "❌ Your post was not approved by the admin."
        if reason:
            safe_reason = html.escape(reason)
            notification_text += f"\n\n<b>Reason:</b> {safe_reason}"
        
        asyncio.create_task(context.bot.send_message(
            chat_id=post['author_id'],
            text=notification_text,
            parse_mode=ParseMode.HTML if reason else None
        ))

        # Note: In a real system we might want to ARCHIVE instead of DELETE to keep the reason.
        # But the requirement says "Delete the post from DB (and optionally store rejection_reason)".
        # To store the reason, we'd need to keep the row but mark it as 'rejected'.
        # However, the current code deletes it. I will stick to deletion for consistency with existing code
        # but if we wanted to store it, we'd need a 'status' column.
        # Since I'm adding 'rejection_reason' column to 'posts', I should probably UPDATE it first if I want to keep it?
        # But if I delete it, the column is useless.
        # Let's assume the user wants to keep the post but MARK as rejected?
        # "Delete the post from DB" is what the user guide says.
        # I'll update it first, then delete? No, that makes no sense for the column.
        # Maybe the user meant "Move to rejected_posts"? 
        # I'll just follow the instruction: "Delete the post from DB".
        
        success = db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
        
        # Clear context flags
        context.user_data.pop('rejecting_post', None)
        context.user_data.pop('awaiting_rejection_reason', None)
        
        # Confirmation to admin
        confirm_text = f"✅ Post #{post_id} has been rejected."
        if reason:
            confirm_text += f"\nReason: {reason}"
            
        if update.callback_query:
            await update.callback_query.edit_message_text(confirm_text)
        else:
            await update.message.reply_text(confirm_text)
            
        # Return to admin panel after a short delay
        await asyncio.sleep(1)
        await admin_panel(update, context)

    except Exception as e:
        logger.error(f"Error in finalize_rejection: {e}")
        if update.callback_query:
            await update.callback_query.message.reply_text(f"❌ Error finalizing rejection: {e}")
        else:
            await update.message.reply_text(f"❌ Error finalizing rejection: {e}")

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
    
    # Instead of immediate deletion, ask for a reason
    await ask_rejection_reason(update, context, post_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    # Check if user exists and create if not
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        anon = create_anonymous_name(user_id)
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
                    f"{preview_text}\n\n✍️ Please type your comment or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
        elif arg.startswith("profileid_"):
            parts = arg.split("_")
            if len(parts) >= 2:
                target_user_id = parts[1]
                post_id = parts[2] if len(parts) >= 3 else None
                
                user_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (target_user_id,))
                if user_data:
                    followers = db_fetch_all("SELECT * FROM followers WHERE followed_id = %s", (user_data['user_id'],))
                    rating = calculate_user_rating(user_data['user_id'])
                    current_user_id = user_id
                    btn = []
                    
                    if user_data['user_id'] != current_user_id:
                        is_following = db_fetch_one(
                            "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
                            (current_user_id, user_data['user_id'])
                        )
                        # Check if blocked to show toggle
                        is_blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (current_user_id, user_data['user_id']))
                        
                        if is_following:
                            btn.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{user_data["user_id"]}')])
                            btn.append([InlineKeyboardButton("✉️ Request to Chat", callback_data=f'chatrequest_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{user_data["user_id"]}')])
                            btn.append([InlineKeyboardButton("✉️ Request to Chat", callback_data=f'chatrequest_{user_data["user_id"]}')])
                        
                        if is_blocked:
                            btn.append([InlineKeyboardButton("🔓 Unblock User", callback_data=f'unblock_user_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("⛔ Block User", callback_data=f'block_user_{user_data["user_id"]}')])
                
                # Contextual Anonymity Check
                display_name = get_display_name(user_data)
                if post_id:
                    post_info = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
                    if post_info and str(post_info['author_id']) == str(target_user_id) and str(target_user_id) != str(user_id):
                        display_name = "🛡 Vent Author"

                display_sex = get_display_sex(user_data)
                level = (rating // 10) + 1
                bio = user_data.get('bio', 'No bio set.')
                
                is_target_admin = user_data.get('is_admin', False)
                if is_target_admin:
                    # Standardize escaping for V2
                    safe_name = escape_markdown(display_name, version=2)
                    safe_sex = escape_markdown(display_sex, version=2)
                    safe_bio = escape_markdown(bio, version=2)
                    
                    profile_text = (
                        f"👤 *{safe_name}* {safe_sex}\n\n"
                        f"🛡 *Role:* Administrator\n"
                        f"👥 *Followers:* {len(followers)}\n\n"
                        f"📖 *About:*\n{safe_bio}\n"
                    )
                else:
                    # Standardize escaping for V2
                    safe_name = escape_markdown(display_name, version=2)
                    safe_sex = escape_markdown(display_sex, version=2)
                    safe_bio = escape_markdown(bio, version=2)
                    safe_level = escape_markdown(str(level), version=2)
                    safe_rating = escape_markdown(str(rating), version=2)
                    safe_aura = escape_markdown(format_aura(rating), version=2)

                    profile_text = (
                        f"👤 *{safe_name}* {safe_sex}\n\n"
                        f"✨ *Aura Level:* {safe_level} \\({safe_aura}\\)\n"
                        f"⭐️ *Points:* {safe_rating}\n"
                        f"👥 *Followers:* {len(followers)}\n\n"
                        f"📖 *About:*\n{safe_bio}\n"
                    )


                
                await update.message.reply_text(
                    profile_text,
                    reply_markup=InlineKeyboardMarkup(btn) if btn else None,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return
        
        elif arg == "inbox":
            await show_inbox(update, context)
            return
    
    # ----- NO INLINE KEYBOARD – only the reply menu -----
    await update.message.reply_text(
        "✝️ *እንኳን ወደ Christian vent በሰላም መጡ* \n\n"
        "ማንነታችሁ ሳይገለጽ ሃሳባችሁን ማጋራት ትችላላችሁ.\n\n",
        reply_markup=get_main_menu(user_id),
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Also send the reply keyboard (buttons above typing area)
    await update.message.reply_text(
        "You can also use the buttons below to navigate:",
        reply_markup=get_main_menu(user_id)
    )

async def show_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Show user's inbox with clean, modern UI"""
    user_id = str(update.effective_user.id)
    
    # Show loading
    loading_msg = None
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("📬 Checking inbox...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("📬 Checking inbox...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Loading", 1)
    
    # Get unread messages count
    unread_count_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE",
        (user_id,)
    )
    unread_count = unread_count_row['count'] if unread_count_row else 0
    
    # Pagination settings
    per_page = 7  # Show 7 messages per page
    offset = (page - 1) * per_page
    
    # Get messages with pagination
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
        # No messages - clean empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No messages")
            await asyncio.sleep(0.5)
        
        text = (
            "📭 *Your Inbox is Empty*\n\n"
            "No messages yet. When someone sends you a message, "
            "it will appear here.\n\n"
            "You can message other users by viewing their profile "
            "and clicking 'Send Message'."
        )
        
        keyboard = [
            [InlineKeyboardButton("🔍 View Leaderboard", callback_data='leaderboard')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            if loading_msg:
                await loading_msg.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            elif hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
        except Exception as e:
            logger.error(f"Error showing empty inbox: {e}")
        return
    
    # Build clean inbox header
    text = "📬 *Messages*\n"
    if unread_count > 0:
        text += f"🔴 {unread_count} unread\n\n"
    else:
        text += "\n"
    
    # Build keyboard with message previews
    keyboard = []
    
    for idx, msg in enumerate(messages, start=1):
        # Calculate message number
        _ = (page - 1) * per_page + idx  # position index (unused display var)
        
        # Determine read status icon
        status_icon = "🔴" if not msg['is_read'] else "⚪"
        
        # Format sender info (truncate if needed)
        sender_name = msg['sender_name'][:12] if len(msg['sender_name']) > 12 else msg['sender_name']
        
        # Format timestamp nicely
        if isinstance(msg['timestamp'], str):
            timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S')
        else:
            timestamp = msg['timestamp']
        
        # Calculate time difference
        now = datetime.now()
        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        
        time_diff = now - timestamp
        if time_diff.days == 0:
            # Same day - show time
            time_str = timestamp.strftime('%I:%M %p').lstrip('0')
        elif time_diff.days == 1:
            time_str = "Yesterday"
        elif time_diff.days < 7:
            time_str = timestamp.strftime('%a')
        else:
            time_str = timestamp.strftime('%b %d')
        
        # Create message preview (short and clean)
        preview = msg['content']
        if len(preview) > 25:
            preview = preview[:22] + '...'
        
        # Clean preview (remove markdown for button)
        clean_preview = preview.replace('*', '').replace('_', '').replace('`', '').strip()
        
        # Create button text
        button_text = f"{status_icon} {sender_name}: {clean_preview} • {time_str}"
        
        # Ensure button text isn't too long
        if len(button_text) > 40:
            button_text = button_text[:37] + "..."
        
        # Add button for each message
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"view_message_{msg['message_id']}_{page}")
        ])
    
    # Add pagination if needed
    if total_pages > 1:
        pagination_row = []
        
        if page > 1:
            pagination_row.append(InlineKeyboardButton("◀️", callback_data=f"inbox_page_{page-1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        pagination_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))
        
        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("▶️", callback_data=f"inbox_page_{page+1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        keyboard.append(pagination_row)
    
    # Add action buttons at bottom
    action_row = []
    if unread_count > 0:
        action_row.append(InlineKeyboardButton("✓ Mark All Read", callback_data="mark_all_read"))
    
    action_row.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"inbox_page_{page}"))
    keyboard.append(action_row)
    
    keyboard.append([
        InlineKeyboardButton("📱 Menu", callback_data='menu'),
        InlineKeyboardButton("👤 Profile", callback_data='profile')
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Add footer text
    text += f"_Showing {len(messages)} of {total_messages} messages_"
    
    # Replace loading message with content
    try:
        if loading_msg:
            await animated_loading(loading_msg, "Ready", 1)
            await loading_msg.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
    except Exception as e:
        logger.error(f"Error showing inbox: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Error loading inbox. Please try again.")
async def view_individual_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, from_page=1):
    """View an individual private message with clean, natural UI"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    # Show minimal loading
    await typing_animation(context, query.message.chat_id, 0.3)
    
    # Get message details
    message = db_fetch_one('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex, u.user_id as sender_id
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.message_id = %s AND pm.receiver_id = %s
    ''', (message_id, user_id))
    
    if not message:
        try:
            await query.message.edit_text(
                "❌ Message not found or you don't have permission to view it.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await query.message.reply_text("❌ Message not found.")
        return
    
    # Mark message as read
    db_execute(
        "UPDATE private_messages SET is_read = TRUE WHERE message_id = %s",
        (message_id,)
    )
    
    # Format timestamp naturally
    if isinstance(message['timestamp'], str):
        timestamp = datetime.strptime(message['timestamp'], '%Y-%m-%d %H:%M:%S')
    else:
        timestamp = message['timestamp']
    
    now = datetime.now()
    time_diff = now - timestamp
    
    if time_diff.days == 0:
        if time_diff.seconds < 60:
            time_ago = "just now"
        elif time_diff.seconds < 3600:
            minutes = time_diff.seconds // 60
            time_ago = f"{minutes}m ago"
        else:
            hours = time_diff.seconds // 3600
            time_ago = f"{hours}h ago"
    elif time_diff.days == 1:
        time_ago = "yesterday"
    elif time_diff.days < 7:
        time_ago = timestamp.strftime('%A')
    elif time_diff.days < 30:
        weeks = time_diff.days // 7
        time_ago = f"{weeks}w ago"
    else:
        time_ago = timestamp.strftime('%b %d')
    
    # Build clean message display
    text = (
        f"💬 *Message from {message['sender_name']}*\n"
        f"_{time_ago}_\n\n"
        f"{escape_markdown(message['content'], version=2)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Check if blocked for toggle
    is_blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (user_id, message['sender_id']))
    block_btn = InlineKeyboardButton("🔓 Unblock", callback_data=f"unblock_user_{message['sender_id']}") if is_blocked else InlineKeyboardButton("⛔ Block", callback_data=f"block_user_{message['sender_id']}")

    # Create clean action buttons (like WhatsApp/Telegram)
    keyboard = [
        [
            InlineKeyboardButton("💬 Reply", callback_data=f"reply_msg_{message['sender_id']}"),
            InlineKeyboardButton("👤 View Profile", url=f"https://t.me/{context.bot.username}?start=profileid_{message['sender_id']}")
        ],
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_message_{message_id}_{from_page}"),
            block_btn
        ],
        [
            InlineKeyboardButton("◀️ Back to Inbox", callback_data=f"inbox_page_{from_page}"),
            InlineKeyboardButton("📱 Menu", callback_data='menu')
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Error viewing message: {e}")
        try:
            await query.message.reply_text(
                f"💬 Message from {message['sender_name']}:\n\n"
                f"{message['content']}\n\n"
                f"_{time_ago}_",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await query.message.reply_text("❌ Error loading message.")
async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, from_page=1):
    """Show clean delete confirmation"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    # Get message preview for confirmation
    message = db_fetch_one('''
        SELECT pm.content, u.anonymous_name as sender_name
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.message_id = %s AND pm.receiver_id = %s
    ''', (message_id, user_id))
    
    if not message:
        await query.answer("❌ Message not found", show_alert=True)
        return
    
    # Create clean preview
    preview = message['content'][:50] + '...' if len(message['content']) > 50 else message['content']
    
    text = (
        f"🗑 *Delete Message?*\n\n"
        f"From: {message['sender_name']}\n"
        f"Preview: {preview}\n\n"
        f"This action cannot be undone."
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Delete", callback_data=f"confirm_delete_message_{message_id}_{from_page}"),
            InlineKeyboardButton("❌ Keep", callback_data=f"cancel_delete_message_{message_id}_{from_page}")
        ]
    ])
    
    await query.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
async def confirm_delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, from_page=1):
    """Confirm and delete message with clean feedback"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    # Show processing
    await query.message.edit_text("🗑 Deleting message...")
    await asyncio.sleep(0.5)
    
    # Delete the message
    success = db_execute(
        "DELETE FROM private_messages WHERE message_id = %s AND receiver_id = %s",
        (message_id, user_id)
    )
    
    if success:
        # Show success and return to inbox
        await query.message.edit_text(
            "✅ Message deleted successfully.",
            parse_mode=ParseMode.MARKDOWN
        )
        await asyncio.sleep(0.7)
        await show_inbox(update, context, from_page)
    else:
        await query.answer("❌ Error deleting message", show_alert=True)
        await query.message.edit_text(
            "❌ Could not delete message. Please try again.",
            parse_mode=ParseMode.MARKDOWN
        )

async def mark_all_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark all messages as read"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    
    # Mark all as read
    db_execute(
        "UPDATE private_messages SET is_read = TRUE WHERE receiver_id = %s",
        (user_id,)
    )
    
    await query.answer("✅ All messages marked as read")
    await show_inbox(update, context, 1)  # Refresh inbox
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
        messages_text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
    
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
            viewer_id = str(update.effective_user.id) if update.effective_user else None
            await update.message.reply_text("❌ Post not found.", reply_markup=get_main_menu(viewer_id) if viewer_id else None)

        return

    comment_count = count_all_comments(post_id)
    keyboard = [
        [InlineKeyboardButton(f"👁 View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_{page}")],
        [InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")],
        [InlineKeyboardButton("🚨 Report Post", callback_data=f"report_post_{post_id}")]
    ]

    post_text = post['content']
    escaped_text = escape_markdown(post_text, version=2)

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(
            f"💬\n{escaped_text}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )

def escape_markdown_v2(text):
    """Escape all special characters for MarkdownV2"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, '\\' + char)
    return text

async def send_comment_message(context, chat_id, comment, author_text, reply_to_message_id=None, pre_fetched_data=None):
    """Helper function to send comments with proper media handling and pre-fetched data support"""
    comment_id = comment['comment_id']
    comment_type = comment['type']
    file_id = comment['file_id']
    content = comment['content']
    
    # Get user reaction for buttons
    user_id = getattr(context, '_user_id', None)
    
    if pre_fetched_data:
        likes = pre_fetched_data.get('likes', 0)
        dislikes = pre_fetched_data.get('dislikes', 0)
        user_reaction_type = pre_fetched_data.get('user_reaction')
    else:
        # Fallback to individual DB queries if no pre-fetched data
        user_reaction = None
        if user_id:
            user_reaction = db_fetch_one(
                "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                (comment_id, user_id)
            )
        user_reaction_type = user_reaction['type'] if user_reaction else None
        
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

    like_emoji = "👍" if user_reaction_type == 'like' else "👍"
    dislike_emoji = "👎" if user_reaction_type == 'dislike' else "👎"

    # Build keyboard
    kb_buttons = [
        [
            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
            InlineKeyboardButton("Reply", callback_data=f"reply_{comment['post_id']}_{comment_id}")
        ],
        [InlineKeyboardButton("🚨 Report", callback_data=f"report_comment_{comment_id}")]
    ]
    
    # Add edit/delete buttons only for comment author and only for text comments
    if comment['author_id'] == user_id:
        if comment_type == 'text':
            kb_buttons.append([
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
            ])
        else:
            kb_buttons.append([
                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
            ])
    
    kb = InlineKeyboardMarkup(kb_buttons)

    # Send message based on comment type
    try:
        escaped_content = escape_markdown_v2(content) if content else ""
        message_text = f"{escaped_content}\n\n{author_text}"
        
        msg = None
        if comment_type == 'text':
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )
            
        elif comment_type == 'voice' and file_id:
            msg = await context.bot.send_voice(
                chat_id=chat_id,
                voice=file_id,
                caption=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id
            )
            
        elif comment_type == 'gif' and file_id:
            msg = await context.bot.send_animation(
                chat_id=chat_id,
                animation=file_id,
                caption=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id
            )
            
        elif comment_type == 'sticker' and file_id:
            msg = await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=file_id,
                reply_to_message_id=reply_to_message_id
            )
            
        else:
            # Fallback for unknown types
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )

        if msg:
            # FIX: Store message ID in database for threading
            db_execute(
                "UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s",
                (msg.message_id, comment_id)
            )
            return msg.message_id
            
    except Exception as e:
        logger.error(f"Error sending comment {comment_id}: {e}")
        # Fallback to text without markdown on error
        try:
            message_text = f"[Media] {content}\n\n{author_text}"
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )
            if msg:
                db_execute(
                    "UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s",
                    (msg.message_id, comment_id)
                )
                return msg.message_id
        except Exception as e2:
            logger.error(f"Fallback also failed: {e2}")
            return None
async def show_comments_page(update, context, post_id, page=1, reply_pages=None):
    if update.effective_chat is None:
        logger.error("Cannot determine chat from update: %s", update)
        return
    chat_id = update.effective_chat.id

    # Show typing animation
    await typing_animation(context, chat_id, 0.5)
    
    # Show loading message
    loading_msg = None
    if page == 1:
        try:
            if hasattr(update, 'callback_query') and update.callback_query:
                loading_msg = await update.callback_query.message.edit_text("💬 Loading comments...")
            elif hasattr(update, 'message') and update.message:
                loading_msg = await context.bot.send_message(chat_id, "💬 Loading comments...")
        except:
            pass

    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        if loading_msg:
            try: await loading_msg.delete()
            except: pass
        await context.bot.send_message(chat_id, "❌ Post not found.")
        return

    post_author_id = post['author_id']
    per_page = 10
    offset = (page - 1) * per_page

    # OPTIMIZED: Batch load comments and user data using a JOIN
    comments = db_fetch_all("""
        SELECT c.*, u.sex, u.avatar_emoji, u.anonymous_name, u.is_admin
        FROM comments c
        LEFT JOIN users u ON c.author_id = u.user_id
        WHERE c.post_id = %s
        ORDER BY c.timestamp ASC
        LIMIT %s OFFSET %s
    """, (post_id, per_page, offset))

    # Count all comments for pagination
    total_comments = count_all_comments(post_id)
    total_pages = (total_comments + per_page - 1) // per_page

    user_id = str(update.effective_user.id)
    if not comments and page == 1:
        if loading_msg:
            try: await loading_msg.delete()
            except: pass
        await context.bot.send_message(chat_id, "_No comments yet._", parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Delete loading message if it exists
    if loading_msg:
        try: await loading_msg.delete()
        except: pass

    # PRE-FETCH: Batch load reactions and parent message IDs
    comment_ids = [c['comment_id'] for c in comments]
    reaction_data = {}
    parent_msg_ids = {}

    if comment_ids:
        # Batch counts
        counts = db_fetch_all("""
            SELECT comment_id, type, COUNT(*) as cnt 
            FROM reactions WHERE comment_id IN %s GROUP BY comment_id, type
        """, (tuple(comment_ids),))
        for row in counts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            if row['type'] == 'like': reaction_data[cid]['likes'] = row['cnt']
            else: reaction_data[cid]['dislikes'] = row['cnt']

        # Batch user reactions
        u_reacts = db_fetch_all("SELECT comment_id, type FROM reactions WHERE comment_id IN %s AND user_id = %s", (tuple(comment_ids), user_id))
        for row in u_reacts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            reaction_data[cid]['user_reaction'] = row['type']

        # Batch parent message IDs for threading
        parent_ids = [c['parent_comment_id'] for c in comments if c.get('parent_comment_id', 0) != 0]
        if parent_ids:
            p_rows = db_fetch_all("SELECT comment_id, telegram_message_id FROM comments WHERE comment_id IN %s", (tuple(parent_ids),))
            for row in p_rows: parent_msg_ids[row['comment_id']] = row['telegram_message_id']

    context._user_id = user_id
    msg_ids = {}

    for comment in comments:
        comment_id = comment['comment_id']
        parent_id = comment.get('parent_comment_id', 0)
        
        # User cached or joined data
        rating = calculate_user_rating(comment['author_id'])
        is_author = str(comment['author_id']) == str(post_author_id)
        
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{comment['author_id']}_{post_id}"
        aura_text = f"⚡ _Aura_ {rating} {format_aura(rating)}" if not comment['is_admin'] else ""
        
        author_label = f"✅ _[vent author]({escape_markdown(profile_link, version=2)})_" if is_author else f"_[{escape_markdown(comment['anonymous_name'] or 'Anonymous', version=2)}]({profile_link})_"
        author_text = f"{comment['sex'] or '👤'} {author_label} {aura_text}".strip()

        # Threading logic
        reply_to_id = parent_msg_ids.get(parent_id)
        
        # Pre-fetched data for button builder
        pref = reaction_data.get(comment_id, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
        
        new_msg_id = await send_comment_message(context, chat_id, comment, author_text, reply_to_id, pre_fetched_data=pref)
        if new_msg_id:
            msg_ids[comment_id] = new_msg_id
    
    # Pagination
    if total_pages > 1:
        buttons = []
        if page > 1: buttons.append(InlineKeyboardButton("⬅️ Older", callback_data=f"viewcomments_{post_id}_{page-1}"))
        if page < total_pages: buttons.append(InlineKeyboardButton("Newer ➡️", callback_data=f"viewcomments_{post_id}_{page+1}"))
        await context.bot.send_message(chat_id, f"📄 Page {page}/{total_pages}", reply_markup=InlineKeyboardMarkup([buttons]))
async def send_reply_message(context, chat_id, reply, post_author_id, post_id, reply_to_message_id, pre_fetched_data=None):
    """Send a single reply message with proper formatting using pre-fetched user data if available"""
    # Use joined data if available, else fetch
    is_admin = reply.get('is_admin')
    if is_admin is None: # Not pre-fetched
        reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (reply['author_id'],))
        is_admin = reply_user.get('is_admin', False)
        display_sex = get_display_sex(reply_user)
        display_name = get_display_name(reply_user)
    else:
        display_sex = reply.get('sex') or '👤'
        display_name = reply.get('anonymous_name') or 'Anonymous'
        
    rating_reply = calculate_user_rating(reply['author_id'])
    reply_profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply['author_id']}_{post_id}"
    aura_text = f"⚡ _Aura_ {rating_reply} {format_aura(rating_reply)}" if not is_admin else ""
    
    # Check if reply author is the vent author
    if str(reply['author_id']) == str(post_author_id):
        author_label = f"✅ _[vent author]({reply_profile_link})_"
    else:
        author_label = f"_[{escape_markdown(display_name, version=2)}]({reply_profile_link})_"
        
    reply_author_text = f"{display_sex} {author_label} {aura_text}".strip()

    # Pass pre-fetched reaction data if available (e.g. from show_more_replies)
    return await send_comment_message(context, chat_id, reply, reply_author_text, reply_to_message_id, pre_fetched_data=pre_fetched_data)

async def show_more_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, comment_id: int, page: int):
    """Show additional replies for a comment (paginated)"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Get the comment to find its post
    comment = db_fetch_one("SELECT post_id FROM comments WHERE comment_id = %s", (comment_id,))
    if not comment:
        await query.answer("❌ Comment not found", show_alert=True)
        return
    
    post_id = comment['post_id']
    post = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
    post_author_id = post['author_id'] if post else None
    
    # Pagination for replies
    replies_per_page = 5
    # Skip the first 3 replies already shown in the comment view
    offset = 3 + (page - 1) * replies_per_page
    
    # Get replies for this page with user data JOINed
    try:
        replies = db_fetch_all("""
            WITH RECURSIVE comment_tree AS (
                SELECT * FROM comments WHERE parent_comment_id = %s
                UNION ALL
                SELECT c.* FROM comments c
                JOIN comment_tree ct ON c.parent_comment_id = ct.comment_id
            )
            SELECT ct.*, u.sex, u.anonymous_name, u.is_admin, u.avatar_emoji
            FROM comment_tree ct
            LEFT JOIN users u ON ct.author_id = u.user_id
            ORDER BY ct.timestamp ASC LIMIT %s OFFSET %s
        """, (comment_id, replies_per_page, offset))
    except Exception as e:
        logger.error(f"Error fetching more replies for comment {comment_id}: {e}")
        await query.answer("❌ Error loading replies", show_alert=True)
        return
    
    # Pre-fetch reaction data for replies
    reply_ids = [r['comment_id'] for r in replies]
    reaction_data = {}
    parent_msg_ids = {}
    user_id = str(update.effective_user.id)
    
    if reply_ids:
        # Batch counts
        counts = db_fetch_all("SELECT comment_id, type, COUNT(*) as cnt FROM reactions WHERE comment_id IN %s GROUP BY comment_id, type", (tuple(reply_ids),))
        for row in counts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            if row['type'] == 'like': reaction_data[cid]['likes'] = row['cnt']
            else: reaction_data[cid]['dislikes'] = row['cnt']
            
        # Batch user reactions
        u_reacts = db_fetch_all("SELECT comment_id, type FROM reactions WHERE comment_id IN %s AND user_id = %s", (tuple(reply_ids), user_id))
        for row in u_reacts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            reaction_data[cid]['user_reaction'] = row['type']

        # Batch parent message IDs
        p_ids = [r['parent_comment_id'] for r in replies]
        if p_ids:
            p_rows = db_fetch_all("SELECT comment_id, telegram_message_id FROM comments WHERE comment_id IN %s", (tuple(p_ids),))
            for row in p_rows: parent_msg_ids[row['comment_id']] = row['telegram_message_id']

    # Delete the "Show more replies" button
    try: await query.message.delete()
    except: pass
    
    msg_ids = {comment_id: base_reply_to_id}

    for reply in replies:
        try:
            pid = reply.get('parent_comment_id')
            target_msg_id = msg_ids.get(pid) or parent_msg_ids.get(pid) or base_reply_to_id
            
            pref = reaction_data.get(reply['comment_id'], {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            reply_msg_id = await send_reply_message(context, chat_id, reply, post_author_id, post_id, target_msg_id, pre_fetched_data=pref)
            
            if reply_msg_id:
                msg_ids[reply['comment_id']] = reply_msg_id
        except Exception as e:
            logger.error(f"Error sending reply {reply.get('comment_id')}: {e}")
    
    # If there are more replies, show another "Show more" button
    if page < total_pages:
        remaining = total_replies - (3 + page * replies_per_page)
        if remaining > 0:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📨 Show even more replies ({remaining} more)", 
                    callback_data=f"show_more_replies_{comment_id}_{page + 1}"
                )]
            ])
            
            # Try to get the reply_to_message_id safely
            reply_to_id = None
            if query.message and query.message.reply_to_message:
                reply_to_id = query.message.reply_to_message.message_id
                
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🗨 *Even more replies below:*",
                    reply_markup=keyboard,
                    reply_to_message_id=reply_to_id,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error sending additional replies button: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🗨 *Even more replies below:*",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If called from a callback query, answer it first
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "📱 *Main Menu*\nUse the buttons below:",
            reply_markup=get_main_menu(str(update.effective_user.id)),
            parse_mode=ParseMode.MARKDOWN
        )

        # Optional: delete the old inline message to avoid clutter
        try:
            await update.callback_query.message.delete()
        except:
            pass
    else:
        await update.message.reply_text(
            "📱 *Main Menu*\nUse the buttons below:",
            reply_markup=get_main_menu(str(update.effective_user.id)),
            parse_mode=ParseMode.MARKDOWN
        )


async def send_updated_profile(user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        return
    
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = calculate_user_rating(user_id)
    
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = %s",
        (user_id,)
    )
    
    bio = user.get('bio', 'No bio set.')
    level = (rating // 10) + 1
    follower_count = len(followers)
    
    # PREMIUM Grid Layout
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Name", callback_data='edit_name'),
            InlineKeyboardButton("⚧️ Sex", callback_data='edit_sex'),
            InlineKeyboardButton("📝 Bio", callback_data='edit_bio')
        ],
        [
            InlineKeyboardButton("🎭 Avatar", callback_data='select_avatar'),
            InlineKeyboardButton("📚 Content", callback_data='my_content_menu')
        ],
        [
            InlineKeyboardButton("📭 Inbox", callback_data='inbox'),
            InlineKeyboardButton("⚙️ Settings", callback_data='settings')
        ],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    
    is_admin = user.get('is_admin', False)
    
    # Standardize escaping for V2
    safe_name = escape_markdown(display_name, version=2)
    safe_sex = escape_markdown(display_sex, version=2)
    safe_bio = escape_markdown(bio, version=2)
    safe_level = escape_markdown(str(level), version=2)
    safe_rating = escape_markdown(str(rating), version=2)
    safe_aura = escape_markdown(format_aura(rating), version=2)
    follower_count = len(followers)

    
    if is_admin:
        profile_text = (
            f"👤 *{safe_name}* {safe_sex}\n\n"
            f"🛡 *Role:* Administrator\n"
            f"👥 *Followers:* {follower_count}\n\n"
            f"📖 *About:*\n{safe_bio}\n"
            f"_Use /menu to return_"
        )
    else:
        profile_text = (
            f"👤 *{safe_name}* {safe_sex}\n\n"
            f"✨ *Aura Level:* {safe_level} \\({safe_aura}\\)\n"
            f"⭐️ *Points:* {safe_rating}\n"
            f"👥 *Followers:* {follower_count}\n\n"
            f"📖 *About:*\n{safe_bio}\n"
            f"_Use /menu to return_"
        )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=profile_text,
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def show_avatar_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a grid of emojis for the user to select as an avatar"""
    query = update.callback_query
    await query.answer()

    emojis = [
        "🦁", "🦊", "🐉", "🐼", "🦄", 
        "🌈", "✨", "🔥", "💎", "🛡",
        "🦅", "🦉", "🦋", "🌸", "🌙",
        "🍎", "🍀", "⛪️", "🎗", "🎖"
    ]
    
    keyboard = []
    # Create a 5x4 grid
    for i in range(0, len(emojis), 5):
        row = [InlineKeyboardButton(e, callback_data=f"set_avatar_{e}") for e in emojis[i:i+5]]
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton("❌ Remove Emoji", callback_data="clear_avatar")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Profile", callback_data="profile")])
    
    text = (
        "🎭 *Select Avatar Emoji*\n\n"
        "Choose an emoji to display next to your name:\n\n"
        "_This will appear on your profile, comments, and the leaderboard\\._"
    )

    
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2
    )

# UPDATED: Function to show user's previous posts with NEW CLEAN UI
# UPDATED: Function to show user's previous posts with CHRONOLOGICAL ORDER and NEW STRUCTURE
# UPDATED: Function to show user's previous posts with CHRONOLOGICAL ORDER and NEW STRUCTURE
async def show_previous_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Show user's previous posts as clickable snippets"""
    
    # Show loading message
    loading_msg = None
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("📝 Loading your posts...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("📝 Loading your posts...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Searching posts", 2)
    
    user_id = str(update.effective_user.id)
    
    per_page = 8  # Show 8 posts per page
    offset = (page - 1) * per_page
    
    # Get user's posts with pagination (newest first)
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
        # Show empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No posts found")
            await asyncio.sleep(0.5)
        
        text = "📝 *My Posts*\n\nYou haven't posted anything yet or your posts are pending approval."
        keyboard = [
            [InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask')],
            [InlineKeyboardButton("📚 Back to My Content", callback_data='my_content_menu')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            if loading_msg:
                await loading_msg.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            elif hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
        except Exception as e:
            logger.error(f"Error showing previous posts: {e}")
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text("❌ Error loading your posts. Please try again.")
        return
    
    # Show posts as clickable buttons
    text = f"📝 *My Posts* ({total_posts} total)\n\n*Click on a post to view details:*\n\n"
    
    # Build keyboard with post buttons
    keyboard = []
    
    for idx, post in enumerate(posts, start=1):
        # Calculate actual post number (considering pagination)
        post_number = (page - 1) * per_page + idx
        
        # Create snippet (first 40 characters)
        snippet = post['content'][:40]
        if len(post['content']) > 40:
            snippet += '...'
        
        # Clean snippet for button text
        clean_snippet = snippet.replace('*', '').replace('_', '').replace('`', '').strip()
        
        # Get comment count for this post
        comment_count = count_all_comments(post['post_id'])
        
        # Create button for each post with post number and snippet
        button_text = f"#{post_number} - {clean_snippet} ({comment_count}💬)"
        
        # Truncate button text if too long
        if len(button_text) > 60:
            button_text = button_text[:57] + "..."
        
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"viewpost_{post['post_id']}_{page}")
        ])
    
    # Add pagination if needed
    if total_pages > 1:
        pagination_row = []
        
        # Previous page button
        if page > 1:
            pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"my_posts_{page-1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        # Current page indicator (non-clickable)
        pagination_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        
        # Next page button
        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"my_posts_{page+1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        keyboard.append(pagination_row)
    
    # Add navigation buttons
    keyboard.append([
        InlineKeyboardButton("📚 Back to My Content", callback_data='my_content_menu'),
        InlineKeyboardButton("📱 Main Menu", callback_data='menu')
    ])
    
    # Create the reply markup
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Replace loading message with content
    try:
        if loading_msg:
            await animated_loading(loading_msg, "Finalizing", 1)
            await loading_msg.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
    except Exception as e:
        logger.error(f"Error showing previous posts: {e}")
        if loading_msg:
            try:
                await loading_msg.edit_text("❌ Error loading your posts. Please try again.")
            except:
                pass

# NEW: Function to view a specific post
# NEW: Function to view a specific post in detail
# NEW: Function to show menu for My Content
async def show_my_content_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show menu for My Content (Posts and Comments)"""
    
    # Show quick loading (very fast)
    loading_msg = None
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("⏳ Loading menu...")
    except:
        pass
    
    keyboard = [
        [InlineKeyboardButton("📝 My Posts", callback_data='my_posts_1')],
        [InlineKeyboardButton("💬 My Comments", callback_data='my_comments_1')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ]
    
    text = "📚 *My Content*\n\nChoose what you want to view:"
    
    try:
        if loading_msg:
            await loading_msg.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
    except Exception as e:
        logger.error(f"Error showing my content menu: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Error loading content menu. Please try again.")

# NEW: Function to show a single post with action buttons
async def view_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int, from_page=1):
    """Show a specific post with action buttons"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Show typing animation
    await typing_animation(context, chat_id, 0.3)
    
    # Show animated loading
    loading_msg = await query.message.edit_text("📄 Loading post details...")
    await animated_loading(loading_msg, "Loading", 2)
    
    # Get post details with categories
    post = db_fetch_one("""
        SELECT p.*, STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.post_id = %s
        GROUP BY p.post_id
    """, (post_id,))
    
    if not post:
        await replace_with_error(loading_msg, "Post not found")
        return
    
    user_id = str(update.effective_user.id)
    
    # Verify ownership
    if post['author_id'] != user_id:
        await replace_with_error(loading_msg, "You can only view your own posts")
        return
    
    # Format the post content
    escaped_content = escape_markdown(post['content'], version=2)
    escaped_categories = escape_markdown(post['categories'] or 'None', version=2)
    
    # Format timestamp
    if isinstance(post['timestamp'], str):
        timestamp = datetime.strptime(post['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d, %Y at %H:%M')
    else:
        timestamp = post['timestamp'].strftime('%b %d, %Y at %H:%M')
    
    # Get comment count
    comment_count = count_all_comments(post_id)
    
    # Build the post detail text
    text = (
        f"📝 *Post Details*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 **Post ID:** \\#{post['post_id']}\n"
        f"📌 **Categories:** {escaped_categories}\n"
        f"📅 **Posted on:** {escape_markdown(timestamp, version=2)}\n"
        f"💬 **Comments:** {comment_count}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Content:**\n\n"
        f"{escaped_content}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Create action buttons for this post
    keyboard = [
        [InlineKeyboardButton("💬 View Comments", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton("🧵 Continue Thread", callback_data=f"continue_post_{post_id}")],
        [
            InlineKeyboardButton("🗑 Delete Post", callback_data=f"delete_post_{post_id}_{from_page}"),
            InlineKeyboardButton("🔙 Back to List", callback_data=f"my_posts_{from_page}")
        ],
        [
            InlineKeyboardButton("📚 Back to My Content", callback_data='my_content_menu'),
            InlineKeyboardButton("📱 Main Menu", callback_data='menu')
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        # Final animation before showing content
        await animated_loading(loading_msg, "Almost ready", 1)
        await loading_msg.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Error viewing post: {e}")
        await replace_with_error(loading_msg, "Error loading post")
# NEW: Function to show user's comments
async def show_my_comments(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    """Show user's previous comments with pagination"""
    
    # Show loading message
    loading_msg = None
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("💭 Loading your comments...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("💭 Loading your comments...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Searching comments", 2)
    
    user_id = str(update.effective_user.id)
    
    per_page = 10
    offset = (page - 1) * per_page
    
    # Get user's comments with post info
    comments = db_fetch_all('''
        SELECT c.*, p.content as post_content, p.post_id, p.category
        FROM comments c
        JOIN posts p ON c.post_id = p.post_id
        WHERE c.author_id = %s
        ORDER BY c.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset))
    
    total_comments_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
        (user_id,)
    )
    total_comments = total_comments_row['count'] if total_comments_row else 0
    total_pages = (total_comments + per_page - 1) // per_page
    
    if not comments:
        # Show empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No comments found")
            await asyncio.sleep(0.5)
        
        text = "💬 \\*My Comments\\*\n\nYou haven't made any comments yet\\."
        keyboard = [
            [InlineKeyboardButton("📚 Back to My Content", callback_data='my_content_menu')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        safe_page = escape_markdown(str(page), version=2)
        safe_total_pages = escape_markdown(str(total_pages), version=2)
        text = f"💬 *My Comments* \\(Page {safe_page}/{safe_total_pages}\\)\n\n"
        
        for idx, comment in enumerate(comments):
            comment_num = (page - 1) * per_page + idx + 1
            safe_num = escape_markdown(str(comment_num), version=2)
            
            # Truncate content
            comment_preview = comment['content'][:80] + '...' if len(comment['content']) > 80 else comment['content']
            safe_comment_preview = escape_markdown(comment_preview, version=2)
            
            text += f"**{safe_num}.** {safe_comment_preview}\n\n"

        
        # Build keyboard
        keyboard = []
        
        # Add pagination
        if total_pages > 1:
            pagination_row = []
            
            if page > 1:
                pagination_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"my_comments_{page-1}"))
            else:
                pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
            
            pagination_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
            
            if page < total_pages:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"my_comments_{page+1}"))
            else:
                pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
            
            keyboard.append(pagination_row)
        
        # Add navigation buttons
        keyboard.append([
            InlineKeyboardButton("📝 My Posts", callback_data='my_posts_1'),
            InlineKeyboardButton("📚 Back to My Content", callback_data='my_content_menu')
        ])
        keyboard.append([InlineKeyboardButton("📱 Main Menu", callback_data='menu')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Replace loading message with content
    try:
        if loading_msg:
            await animated_loading(loading_msg, "Finalizing", 1)
            await loading_msg.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.edit_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                if hasattr(update, 'message') and update.message:
                    await update.message.reply_text(
                        text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
    except Exception as e:
        logger.error(f"Error showing my comments: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ Error loading your comments. Please try again.")


# ==================== REPORTING FEATURE ====================

def create_report(reporter_id: str, target_type: str, target_id: int, reason: str):
    """Insert a new report. Returns report_id, None (duplicate), or -1 (rate limited)."""
    # Prevent duplicate reports from the same user on the same content
    existing = db_fetch_one(
        "SELECT report_id FROM reports WHERE reporter_id = %s AND target_type = %s AND target_id = %s",
        (reporter_id, target_type, target_id)
    )
    if existing:
        return None

    # Rate limit: max 5 reports per 24 hours
    today_count = db_fetch_one(
        "SELECT COUNT(*) as cnt FROM reports WHERE reporter_id = %s AND created_at >= NOW() - INTERVAL '1 day'",
        (reporter_id,)
    )
    if today_count and today_count['cnt'] >= 5:
        return -1

    result = db_execute(
        "INSERT INTO reports (reporter_id, target_type, target_id, reason) VALUES (%s, %s, %s, %s) RETURNING report_id",
        (reporter_id, target_type, target_id, reason),
        fetchone=True
    )
    return result['report_id'] if result else None


def get_pending_reports(offset: int = 0, limit: int = 5):
    """Fetch paginated pending reports with reporter name."""
    return db_fetch_all(
        """SELECT r.*, u.anonymous_name as reporter_name
           FROM reports r
           LEFT JOIN users u ON r.reporter_id = u.user_id
           WHERE r.status = 'pending'
           ORDER BY r.created_at ASC
           LIMIT %s OFFSET %s""",
        (limit, offset)
    )


def get_report_content_preview(target_type: str, target_id: int):
    """Return (preview_text, author_id) for a reported post or comment."""
    if target_type == 'post':
        row = db_fetch_one("SELECT content, author_id FROM posts WHERE post_id = %s", (target_id,))
        if row:
            return row['content'][:100], row['author_id']
    elif target_type == 'comment':
        row = db_fetch_one("SELECT content, author_id FROM comments WHERE comment_id = %s", (target_id,))
        if row:
            return (row['content'] or '[media]')[:100], row['author_id']
    return None, None


def resolve_report(report_id: int, admin_id: str, status: str, action_taken: str = None):
    """Mark a report as resolved with the given status and optional action."""
    db_execute(
        """UPDATE reports SET status = %s, reviewed_by = %s, reviewed_at = NOW(), action_taken = %s
           WHERE report_id = %s""",
        (status, admin_id, action_taken, report_id)
    )


async def show_admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    """Show paginated pending reports to admin."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if query:
            await query.answer("❌ No permission.", show_alert=True)
        return

    per_page = 5
    offset = (page - 1) * per_page
    reports = get_pending_reports(offset=offset, limit=per_page)

    total_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reports WHERE status = 'pending'")
    total = total_row['cnt'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    nav_keyboard = []

    if not reports:
        text = "📋 *Pending Reports*\n\n✅ No pending reports at this time."
        nav_keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data='admin_panel')]]
        try:
            if query:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error showing empty reports: {e}")
        return

    lines = [f"📋 *Pending Reports* \\(Page {page}/{total_pages}\\)\n"]
    keyboard = []

    for rep in reports:
        preview, _ = get_report_content_preview(rep['target_type'], rep['target_id'])
        preview = (preview or '[deleted]')[:60]
        type_label = "Post" if rep['target_type'] == 'post' else "Comment"
        reporter_name = rep.get('reporter_name') or 'Anonymous'
        safe_preview = escape_markdown(preview, version=2)
        safe_reporter = escape_markdown(reporter_name, version=2)
        safe_reason = escape_markdown(rep['reason'], version=2)

        lines.append(
            f"🆔 *Report \\#{rep['report_id']}* \\- {type_label}\n"
            f"📝 _{safe_preview}_\n"
            f"👤 By: {safe_reporter}\n"
            f"💬 Reason: {safe_reason}\n"
        )
        keyboard.append([
            InlineKeyboardButton("👁 View", callback_data=f"report_view_{rep['report_id']}"),
            InlineKeyboardButton("✅ Dismiss", callback_data=f"report_dismiss_{rep['report_id']}"),
        ])
        keyboard.append([
            InlineKeyboardButton("❌ Delete Content", callback_data=f"report_delete_{rep['report_id']}"),
            InlineKeyboardButton("⚠️ Warn User", callback_data=f"report_warn_{rep['report_id']}"),
        ])

    # Pagination row
    pag_row = []
    if page > 1:
        pag_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admin_reports_{page - 1}"))
    pag_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        pag_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"admin_reports_{page + 1}"))
    if pag_row:
        keyboard.append(pag_row)
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data='admin_panel')])

    text = "\n".join(lines)
    try:
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error showing admin reports: {e}")
        try:
            back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]])
            if query:
                await query.message.reply_text("❌ Error loading reports.", reply_markup=back)
        except Exception:
            pass


async def notify_admin_of_new_report(
    context: ContextTypes.DEFAULT_TYPE,
    report_id: int,
    reporter_id: str,
    target_type: str,
    reason: str
):
    """DM the admin when a new report is created."""
    if not ADMIN_ID:
        return
    try:
        reporter = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (reporter_id,))
        reporter_name = reporter['anonymous_name'] if reporter else 'Anonymous'
        type_label = "Post" if target_type == 'post' else "Comment"
        safe_reason = escape_markdown(reason, version=2)
        safe_name = escape_markdown(reporter_name, version=2)
        text = (
            f"🚨 *New Report \\#{report_id}*\n"
            f"Type: {type_label}\n"
            f"Reason: {safe_reason}\n"
            f"By: {safe_name}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👁 Review Reports", callback_data='admin_reports')]
        ])
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Error notifying admin of report: {e}")

async def send_reaction_notification(context: ContextTypes.DEFAULT_TYPE, comment: dict, reactor_id: str, reaction_type: str, post_id: int):
    """Background helper to send interaction notification"""
    try:
        # Resolve identities
        post = db_fetch_one("SELECT content, author_id FROM posts WHERE post_id = %s", (post_id,))
        comment_author = db_fetch_one("SELECT user_id, anonymous_name FROM users WHERE user_id = %s", (comment['author_id'],))
        
        # Don't notify yourself
        if str(reactor_id) == str(comment['author_id']):
            return

        # Anonymization: If the person reacting is the post author
        if post and str(reactor_id) == str(post['author_id']):
            reactor_display = "Vent author"
        else:
            reactor = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (reactor_id,))
            reactor_display = reactor['anonymous_name'] if reactor else "Anonymous"
        
        # Content formatting
        post_preview = post['content'][:50] + '...' if post and len(post['content']) > 50 else (post['content'] if post else "")
        reaction_label = "liked 👍" if reaction_type == 'like' else "disliked 👎"
        reaction_icon = "✨" if reaction_type == 'like' else "⚠️"
        
        notification_text = (
            f"{reaction_icon} *New Interaction\\!*\n\n"
            f"👤 {escape_markdown(reactor_display, version=2)} *{reaction_label}* your comment\\:\n\n"
            f"🗨 _{escape_markdown((comment['content'] or '[media]')[:150], version=2)}_\n\n"
            f"📝 *Post Context\\:*\n{escape_markdown(post_preview, version=2)}\n\n"
            f"🔗 [View Discussion](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
        )
        
        await context.bot.send_message(
            chat_id=comment_author['user_id'],
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Reaction notification failed: {e}")

# ==================== END REPORTING HELPERS ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # We will call query.answer() with specific text in the branches below
    # to show the premium "black toast" loading animations.
    
    user_id = str(query.from_user.id)
    
    # Log the callback data for debugging
    logger.info(f"Callback data received: {query.data} from user {user_id}")
    
    try:
        # ... rest of your code
        # FIXED: Handle noop callback (do nothing for separator buttons)
        if query.data == 'noop':
            return  # Do nothing and exit the function
            
        if query.data == 'ask':
            context.user_data['selected_categories'] = set()
            await query.message.reply_text(
                "📚 *Select categories (you can choose multiple):*",
                reply_markup=build_multi_category_keyboard(set()),
                parse_mode=ParseMode.MARKDOWN
            )
            await query.answer()

        elif query.data.startswith("cat_toggle_"):
            # Extract category code
            code = query.data.split("_", 2)[2]
            # Get current selection set (default to empty set)
            selected = context.user_data.get('selected_categories', set())
            if not isinstance(selected, set):
                selected = set(selected) if selected else set()
                
            if code in selected:
                selected.remove(code)
            else:
                selected.add(code)
            context.user_data['selected_categories'] = selected
            
            # Rebuild keyboard with updated selection
            new_markup = build_multi_category_keyboard(selected)
            
            # Edit the reply markup of the original message
            await query.message.edit_reply_markup(reply_markup=new_markup)
            
            # Answer callback to remove loading state
            await query.answer()
            return

        elif query.data == "cat_reset":
            context.user_data['selected_categories'] = set()
            new_markup = build_multi_category_keyboard(set())
            await query.message.edit_reply_markup(reply_markup=new_markup)
            await query.answer("Selection reset", show_alert=False)

        elif query.data == "cat_done":
            selected = context.user_data.get('selected_categories', set())
            if not selected:
                await query.answer("❌ Please select at least one category.", show_alert=True)
                return
            
            # Store selected categories in user's DB record
            db_execute(
                "UPDATE users SET selected_categories = %s, waiting_for_post = TRUE WHERE user_id = %s",
                (','.join(selected), user_id)
            )
            
            await query.message.reply_text(
                f"✍️ *Selected: {', '.join(selected)}*\n\nNow send your post content (text, photo, or voice).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )
            try:
                await query.message.delete()  # Remove category selection message
            except:
                pass
            await query.answer()
            return
        
        elif query.data == 'menu':
            await query.answer("📱 Opening Menu...", show_alert=False)
            await query.message.reply_text(
                "📱 Main Menu\nUse the buttons below:",
                reply_markup=get_main_menu(user_id),
                parse_mode=ParseMode.MARKDOWN
            )

            # Delete the old inline message to keep chat clean
            try:
                await query.message.delete()
            except:
                pass

        # Handle cancel input button
        elif query.data == 'cancel_input':
            # Reset all waiting states and restore main menu
            await reset_user_waiting_states(
                user_id, 
                query.message.chat_id, 
                context
            )
            
            # Send confirmation
            await query.answer("❌ Input cancelled")
            
            # Try to delete the input prompt message if it's an inline message
            try:
                await query.message.delete()
            except: pass
            
            return

        elif query.data == 'profile':
            await query.answer("👤 Loading Profile...", show_alert=False)
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data == 'leaderboard':
            await query.answer("🏆 Loading Leaderboard...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            await show_leaderboard(update, context)

        elif query.data == 'settings':
            await query.answer("⚙️ Loading Settings...", show_alert=False)
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
            await query.answer("ℹ️ Loading Help...", show_alert=False)
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
            await query.answer("ℹ️ Loading About...", show_alert=False)
            about_text = (
                "👤 Creator: Yididiya Tamiru\n\n"
                "🔗 Telegram: @YIDIDIYATAMIRUU\n"
                "🙏 This bot helps you share your thoughts anonymously with the Christian community."
            )
            keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
            await query.message.reply_text(about_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'edit_name':
            await query.answer("✏️ Renaming...", show_alert=False)
            db_execute(
                "UPDATE users SET awaiting_name = TRUE WHERE user_id = %s",
                (user_id,)
            )
            await query.message.reply_text(
                "✏️ Please type your new anonymous name:\n\nTap ❌ Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

        elif query.data == 'edit_bio':
            await query.answer("📝 Opening Bio Editor...", show_alert=False)
            db_execute(
                "UPDATE users SET awaiting_bio = TRUE WHERE user_id = %s",
                (user_id,)
            )
            await query.message.reply_text(
                "📝 *Please type your new bio:*\n\nKeep it short and interesting (max 150 chars).\n\nTap ❌ Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

        elif query.data == 'edit_sex':
            await query.answer("⚧️ Changing sex...", show_alert=False)
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
            await query.answer("👤 Updating Follow...", show_alert=False)
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
            await query.answer("🔄 Loading comments...", show_alert=False)
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
            await query.answer("✍️ Opening Writer...", show_alert=False)
            post_id_str = query.data.split('_', 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s",
                    (post_id, user_id)
                )
                
                await query.message.reply_text(
                    "✍️ Please type your comment or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML
                )
                return
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
                
                # Clear Aura Cache
                calculate_user_rating.cache_clear()
                format_aura.cache_clear()

                
                # Clear rating cache for consistency
                calculate_user_rating.cache_clear()
                format_aura.cache_clear()


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
                    "SELECT post_id, parent_comment_id, author_id, type, content FROM comments WHERE comment_id = %s",
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
                    
                    # Add edit/delete buttons only for comment author and only for text comments
                    if comment['author_id'] == user_id:
                        if comment['type'] == 'text':
                            kb_buttons.append([
                                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
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
                    
                    # Add edit/delete buttons only for reply author and only for text comments
                    if comment['author_id'] == user_id:
                        if comment['type'] == 'text':
                            kb_buttons.append([
                                InlineKeyboardButton("✏️ Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("🗑 Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
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
                
                # Send notification in background
                if not existing_reaction or existing_reaction['type'] != reaction_type:
                    asyncio.create_task(send_reaction_notification(context, comment, user_id, reaction_type, post_id))
            except Exception as e:
                logger.error(f"Error processing reaction: {e}")
                await query.answer("❌ Error updating reaction", show_alert=True)

        # NEW: Handle edit comment
        elif query.data.startswith("edit_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
            
            if comment and comment['author_id'] == user_id:
                if comment['type'] != 'text':
                    await query.answer("❌ Only text comments can be edited", show_alert=True)
                    return
                    
                context.user_data['editing_comment'] = comment_id
                await query.message.reply_text(
                    f"✏️ *Editing your comment:*\n\n{escape_markdown(comment['content'], version=2)}\n\nPlease type your new comment:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_input')]
                    ]),
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
                
                # Orphan Adoption: Become top-level first
                db_execute("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (comment_id,))
                
                # Delete the comment and its reactions
                db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment_id,))
                db_execute("DELETE FROM comments WHERE comment_id = %s", (comment_id,))
                
                await query.answer("✅ Comment deleted")
                await query.message.delete()
                
                # Update comment count with orphan check
                await adopt_orphaned_replies(context, post_id)
            else:
                await query.answer("❌ You can only delete your own comments", show_alert=True)

        # NEW: Handle delete post
        elif query.data.startswith("delete_post_"):
            try:
                parts = query.data.split('_')
                post_id = int(parts[2])
                
                # Get the page number (default to 1 if not provided)
                from_page = 1
                if len(parts) > 3:
                    from_page = int(parts[3])
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                
                if post and post['author_id'] == user_id:
                    # Ask for confirmation with page info
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_post_{post_id}_{from_page}"),
                            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_delete_post_{post_id}_{from_page}")
                        ]
                    ])
                    
                    await query.message.edit_text(
                        "🗑 *Delete Post*\n\nAre you sure you want to delete this post? This action cannot be undone.",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await query.answer("❌ You can only delete your own posts", show_alert=True)
            except Exception as e:
                logger.error(f"Error in delete_post handler: {e}")
                await query.answer("❌ Error processing request", show_alert=True)

        elif query.data.startswith("confirm_delete_post_"):
            try:
                parts = query.data.split('_')
                post_id = int(parts[3])
                from_page = int(parts[4]) if len(parts) > 4 else 1
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                
                if post and post['author_id'] == user_id:
                    # Delete the post (same logic as before)
                    if post['channel_message_id']:
                        try:
                            await context.bot.delete_message(
                                chat_id=CHANNEL_ID,
                                message_id=post['channel_message_id']
                            )
                        except Exception as e:
                            logger.error(f"Error deleting channel message: {e}")
                    
                    # Delete all comments and reactions for this post
                    comments = db_fetch_all("SELECT comment_id FROM comments WHERE post_id = %s", (post_id,))
                    for comment in comments:
                        db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment['comment_id'],))
                    
                    db_execute("DELETE FROM comments WHERE post_id = %s", (post_id,))
                    db_execute("DELETE FROM posts WHERE post_id = %s", (post_id,))
                    
                    await query.answer("✅ Post deleted successfully")
                    await query.message.edit_text(
                        "✅ Post has been deleted successfully.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    
                    # Return to the post list at the same page
                    await show_previous_posts(update, context, from_page)
                else:
                    await query.answer("❌ You can only delete your own posts", show_alert=True)
            except Exception as e:
                logger.error(f"Error deleting post: {e}")
                await query.answer("❌ Error deleting post", show_alert=True)

        elif query.data.startswith("cancel_delete_post_"):
            try:
                parts = query.data.split('_')
                post_id = int(parts[3])
                from_page = int(parts[4]) if len(parts) > 4 else 1
                
                # Return to the post view
                await view_post(update, context, post_id, from_page)
            except (IndexError, ValueError):
                # Fallback to post list
                await show_previous_posts(update, context, 1)

        
        elif query.data.startswith('chatrequest_'):
            target_id = query.data.split('_')[1]
            if target_id == user_id:
                await query.answer("❌ You cannot chat with yourself.", show_alert=True)
                return

            # Check for existing request
            existing = db_fetch_one(
                "SELECT status FROM chat_requests WHERE sender_id = %s AND receiver_id = %s",
                (user_id, target_id)
            )
            
            if existing:
                if existing['status'] == 'accepted':
                    await query.answer("✅ Request already accepted!", show_alert=False)
                    db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
                    await query.message.reply_text("✉️ Type your message below:", reply_markup=cancel_menu)
                else:
                    await query.answer("⏳ Chat request is still pending...", show_alert=True)
                return

            # Create new request
            try:
                db_execute(
                    "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'pending')",
                    (user_id, target_id)
                )
                await query.answer("✉️ Chat request sent!", show_alert=False)
                
                # Notify receiver
                sender_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                sender_name = get_display_name(sender_data)
                
                receiver_text = (
                    f"🔔 *New Chat Request\\!*\n"
                    f"_{escape_markdown(sender_name, version=2)}_ wants to chat with you\\."
                )
                receiver_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Accept", callback_data=f'acceptchat_{user_id}'),
                        InlineKeyboardButton("❌ Ignore", callback_data=f'declinechat_{user_id}')
                    ],
                    [InlineKeyboardButton("👤 View Profile", url=f'https://t.me/{BOT_USERNAME}?start=profileid_{user_id}')]
                ])
                
                await context.bot.send_message(
                    chat_id=target_id,
                    text=receiver_text,
                    reply_markup=receiver_kb,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e:
                logger.error(f"ChatRequest error: {e}")
                await query.answer("❌ Failed to send request.", show_alert=True)

        elif query.data.startswith('acceptchat_'):
            sender_id = query.data.split('_')[1]
            db_execute(
                "UPDATE chat_requests SET status = 'accepted' WHERE sender_id = %s AND receiver_id = %s",
                (sender_id, user_id)
            )
            # Mutual chat permission
            db_execute(
                "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'accepted') ON CONFLICT DO NOTHING",
                (user_id, sender_id)
            )
            
            await query.answer("✅ Request accepted!", show_alert=False)
            await query.message.edit_text("✅ *You accepted the chat request\\!*", parse_mode=ParseMode.MARKDOWN_V2)
            
            receiver_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
            receiver_name = get_display_name(receiver_data)
            try:
                await context.bot.send_message(
                    chat_id=sender_id,
                    text=f"✅ *{escape_markdown(receiver_name, version=2)}* accepted your chat request\\! You can now send messages from their profile\\.",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except: pass

        elif query.data.startswith('declinechat_'):
            sender_id = query.data.split('_')[1]
            db_execute("DELETE FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (sender_id, user_id))
            await query.answer("Request ignored.", show_alert=False)
            await query.message.edit_text("🗑️ *Chat request ignored\\.*", parse_mode=ParseMode.MARKDOWN_V2)

        elif query.data.startswith('message_'):
            target_id = query.data.split('_')[1]
            check = db_fetch_one("SELECT status FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (user_id, target_id))
            
            if not check or check['status'] != 'accepted':
                await query.answer("❌ You must send a chat request first!", show_alert=True)
                return

            await query.answer("✉️ Opening Chat...", show_alert=False)
            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            await query.message.reply_text("✉️ *Please type your private message:*\n\nTap ❌ Cancel to return to menu.", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)
        
        elif query.data.startswith('reply_msg_'):
            # Existing reply logic (requires accepted chat as well)
            target_id = query.data[len('reply_msg_'):]
            if not target_id or not target_id.isdigit():
                await query.answer("❌ Invalid ID", show_alert=True)
                return
                
            check = db_fetch_one("SELECT status FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (user_id, target_id))
            if not check or check['status'] != 'accepted':
                await query.answer("❌ No active chat permission.", show_alert=True)
                return

            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            await query.message.reply_text(f"↩️ *Replying to {target_user['anonymous_name']}*\n\nPlease type your message:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)

        elif query.data.startswith("reply_"):
            parts = query.data.split("_")
            if len(parts) == 3:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s",
                    (post_id, comment_id, user_id)
                )
                
                await query.message.reply_text(
                    "↩️ Please type your reply or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML
                )
                
        elif query.data.startswith("replytoreply_"):
            parts = query.data.split("_")
            if len(parts) == 4:
                post_id = int(parts[1])
                comment_id = int(parts[3])
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s, comment_idx = %s WHERE user_id = %s",
                    (post_id, comment_id, user_id)
                )
                
                await query.message.reply_text(
                    "↩️ Please type your reply or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML
                )
        # UPDATED: Handle Previous Posts pagination
        elif query.data.startswith('show_more_replies_'):
            try:
                parts = query.data.split('_')
                comment_id = int(parts[3])
                page = int(parts[4])
                await show_more_replies(update, context, comment_id, page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing show_more_replies: {e}")
                await query.answer("❌ Error loading more replies", show_alert=True)
        elif query.data.startswith("previous_posts_"):
            try:
                page = int(query.data.split('_')[2])
                await show_previous_posts(update, context, page)
            except (IndexError, ValueError):
                await show_previous_posts(update, context, 1)

        # UPDATED: Handle Previous Posts button
        elif query.data == 'my_content_menu':
            await show_my_content_menu(update, context)

        elif query.data.startswith("my_posts_"):
            await query.answer("📚 Loading your posts...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            try:
                page = int(query.data.split('_')[2])
                await show_previous_posts(update, context, page)
            except (IndexError, ValueError):
                await show_previous_posts(update, context, 1)

        elif query.data == 'my_posts':
            await show_previous_posts(update, context, 1)

        elif query.data.startswith("viewpost_"):
            await query.answer("📄 Loading vent...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            try:
                parts = query.data.split('_')
                if len(parts) >= 3:
                    post_id = int(parts[1])
                    from_page = int(parts[2])
                    await view_post(update, context, post_id, from_page)
                else:
                    post_id = int(parts[1])
                    await view_post(update, context, post_id, 1)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing viewpost callback: {e}")
                await query.answer("❌ Error loading post", show_alert=True)

        elif query.data.startswith('my_comments_'):
            await query.answer("🗨️ Loading your comments...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            try:
                page = int(query.data.split('_')[2])
                await show_my_comments(update, context, page)
            except (IndexError, ValueError):
                await show_my_comments(update, context, 1)
        
        elif query.data == 'my_comments':
            await show_my_comments(update, context, 1)

        # NEW: Handle My Content Menu
        elif query.data == 'my_content_menu':
            await show_my_content_menu(update, context)
        
        # NEW: Handle My Comments pagination
        elif query.data.startswith('my_comments_'):
            try:
                page = int(query.data.split('_')[2])
                await show_my_comments(update, context, page)
            except (IndexError, ValueError):
                await show_my_comments(update, context, 1)
        
        # NEW: Handle My Comments button
        elif query.data == 'my_comments':
            await show_my_comments(update, context, 1)
        
        # NEW: Handle view comment details
        elif query.data.startswith('view_comment_'):
            try:
                comment_id = int(query.data.split('_')[2])
                comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
                
                if comment and comment['author_id'] == user_id:
                    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (comment['post_id'],))
                    
                    if post:
                        keyboard = [
                            [InlineKeyboardButton("🔍 View in Post", callback_data=f"viewcomments_{post['post_id']}_1")],
                            [InlineKeyboardButton("🗑 Delete Comment", callback_data=f"delete_comment_{comment_id}")],
                            [InlineKeyboardButton("📚 Back to My Comments", callback_data='my_comments')]
                        ]
                        
                        # Show comment details
                        comment_preview = comment['content'][:200] + '...' if len(comment['content']) > 200 else comment['content']
                        post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                        
                        text = (
                            f"💬 *Comment Details*\n\n"
                            f"📄 **Post:** {escape_markdown(post_preview, version=2)}\n\n"
                            f"🗨 **Your Comment:**\n{escape_markdown(comment_preview, version=2)}\n\n"
                            f"📅 **Posted on:** {comment['timestamp'].strftime('%Y-%m-%d %H:%M') if not isinstance(comment['timestamp'], str) else comment['timestamp'][:16]}"
                        )
                        
                        await query.message.edit_text(
                            text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                else:
                    await query.answer("❌ Comment not found or not yours", show_alert=True)
            except Exception as e:
                logger.error(f"Error viewing comment: {e}")
                await query.answer("❌ Error viewing comment", show_alert=True)

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
                # Handle both text and media messages
                try:
                    await query.message.edit_text("❌ Post data not found. Please start over.")
                except BadRequest:
                    try:
                        await query.message.edit_caption("❌ Post data not found. Please start over.")
                    except:
                        await query.message.reply_text("❌ Post data not found. Please start over.")
                return
            
            if query.data == 'edit_post':
                if time.time() - pending_post.get('timestamp', 0) > 300:
                    # Handle both text and media messages for expiration
                    try:
                        await query.message.edit_text("❌ Edit time expired. Please start a new post.")
                    except BadRequest:
                        await query.message.edit_caption("❌ Edit time expired. Please start a new post.")
                    del context.user_data['pending_post']
                    return
                    
                # Store that we're in edit mode
                context.user_data['editing_post'] = True
                
                # Edit based on message type
                try:
                    await query.message.edit_text(
                        f"✏️ *Edit your post:*\n\n{escape_markdown(pending_post['content'], version=2)}\n\nPlease type your edited post:",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel", callback_data='cancel_input')]
                        ]),
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                except BadRequest:
                    # If it's a media message, edit the caption
                    await query.message.edit_caption(
                        caption=f"✏️ *Edit your post:*\n\n{escape_markdown(pending_post['content'], version=2)}\n\nPlease type your edited post:",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ Cancel", callback_data='cancel_input')]
                        ]),
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                return
            
            elif query.data == 'cancel_post':
                # Handle both text and media messages for cancellation
                try:
                    await query.message.edit_text("❌ Post cancelled.")
                except BadRequest:
                    await query.message.edit_caption("❌ Post cancelled.")
                if 'pending_post' in context.user_data:
                    del context.user_data['pending_post']
                if 'thread_from_post_id' in context.user_data:
                    del context.user_data['thread_from_post_id']
                if 'editing_post' in context.user_data:
                    del context.user_data['editing_post']
                return
            
            elif query.data == 'confirm_post':
                await query.answer()
                
                # Show typing animation
                await typing_animation(context, query.message.chat_id, 0.5)
                
                # Show loading - handle both text and media
                try:
                    loading_msg = await query.message.edit_text("📤 Submitting your post...")
                except BadRequest:
                    loading_msg = await query.message.edit_caption("📤 Submitting your post...")
                
                await animated_loading(loading_msg, "Processing", 3)
                
                pending_post = context.user_data.get('pending_post')
                if not pending_post:
                    # Handle both text and media for error
                    try:
                        await loading_msg.edit_text("❌ Post data not found. Please start over.")
                    except:
                        await loading_msg.edit_caption("❌ Post data not found. Please start over.")
                    return
                
                category = pending_post['category']
                post_content = pending_post['content']
                media_type = pending_post.get('media_type', 'text')
                media_id = pending_post.get('media_id')
                thread_from_post_id = pending_post.get('thread_from_post_id')
                
                # Insert post (without 'category' column which was dropped)
                if thread_from_post_id:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, media_type, media_id, thread_from_post_id) VALUES (%s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id, thread_from_post_id),
                        fetchone=True
                    )
                else:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, media_type, media_id) VALUES (%s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id),
                        fetchone=True
                    )
                
                if post_row:
                    post_id = post_row['post_id']
                    
                    # Insert categories into junction table
                    category_list = category.split(',') if category else []
                    for cat_code in category_list:
                        db_execute(
                            "INSERT INTO post_categories (post_id, category_code) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (post_id, cat_code.strip())
                        )
                
                # Clean up user data
                if 'pending_post' in context.user_data:
                    del context.user_data['pending_post']
                if 'thread_from_post_id' in context.user_data:
                    del context.user_data['thread_from_post_id']
                if 'editing_post' in context.user_data:
                    del context.user_data['editing_post']
                
                if post_row:
                    post_id = post_row['post_id']
                    await notify_admin_of_new_post(context, post_id)
                    
                    # Replace loading with success animation
                    try:
                        success_msg = await loading_msg.edit_text("✅ Post submitted for approval!")
                    except:
                        success_msg = await loading_msg.edit_caption("✅ Post submitted for approval!")
                    
                    await asyncio.sleep(1)
                    
                    keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
                    try:
                        await success_msg.edit_text(
                            "✅ Your post has been submitted for admin approval!\nYou'll be notified when it's approved and published.",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    except:
                        await success_msg.edit_caption(
                            "✅ Your post has been submitted for admin approval!\nYou'll be notified when it's approved and published.",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                else:
                    try:
                        await loading_msg.edit_text("❌ Failed to submit post. Please try again.")
                    except:
                        await loading_msg.edit_caption("❌ Failed to submit post. Please try again.")
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
        # Admin broadcast handlers
        elif query.data == 'admin_broadcast':
            await start_broadcast(update, context)
            
        elif query.data.startswith('broadcast_'):
            # Handle broadcast type selection
            broadcast_type = query.data.split('_', 1)[1]
            await handle_broadcast_type(update, context, broadcast_type)
            
        elif query.data == 'execute_broadcast':
            await execute_broadcast(update, context)    
                
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

        elif query.data.startswith('reject_with_reason_'):
            try:
                post_id = int(query.data.split('_')[-1])
                context.user_data['awaiting_rejection_reason'] = True
                context.user_data['rejecting_post'] = post_id
                await query.edit_message_text(
                    "📝 *Provide Rejection Reason*\n\nPlease type the reason for rejection and send it as a message.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error in reject_with_reason_ handler: {e}")
                await query.answer("❌ Error processing request", show_alert=True)
                
        elif query.data.startswith('skip_rejection_'):
            try:
                post_id = int(query.data.split('_')[-1])
                await finalize_rejection(update, context, post_id, reason=None)
            except Exception as e:
                logger.error(f"Error in skip_rejection_ handler: {e}")
                await query.answer("❌ Error skipping reason", show_alert=True)
                
        elif query.data == 'cancel_rejection':
            context.user_data.pop('rejecting_post', None)
            context.user_data.pop('awaiting_rejection_reason', None)
            try:
                await query.edit_message_text("❌ Rejection cancelled.")
                await admin_panel(update, context)
            except Exception as e:
                logger.error(f"Error in cancel_rejection handler: {e}")
                await query.message.reply_text("❌ Rejection cancelled.")
                await admin_panel(update, context)
        
        elif query.data == 'inbox':
            await show_inbox(update, context, 1)
            
        elif query.data.startswith('inbox_page_'):
            try:
                page = int(query.data.split('_')[2])
                await show_inbox(update, context, page)
            except (IndexError, ValueError):
                await show_inbox(update, context, 1)
                
        elif query.data.startswith('view_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 3:
                    message_id = int(parts[2])
                    from_page = int(parts[3]) if len(parts) > 3 else 1
                    await view_individual_message(update, context, message_id, from_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing view_message: {e}")
                await query.answer("❌ Error loading message", show_alert=True)
                
        elif query.data == 'mark_all_read':
            await mark_all_read(update, context)
            
        elif query.data.startswith('delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 3:
                    message_id = int(parts[2])
                    from_page = int(parts[3]) if len(parts) > 3 else 1
                    await delete_message(update, context, message_id, from_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing delete_message: {e}")
                await query.answer("❌ Error", show_alert=True)
                
        elif query.data.startswith('confirm_delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 4:
                    message_id = int(parts[3])
                    from_page = int(parts[4]) if len(parts) > 4 else 1
                    await confirm_delete_message(update, context, message_id, from_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing confirm_delete: {e}")
                await query.answer("❌ Error", show_alert=True)
                
        elif query.data.startswith('cancel_delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 4:
                    message_id = int(parts[3])
                    from_page = int(parts[4]) if len(parts) > 4 else 1
                    await view_individual_message(update, context, message_id, from_page)
            except (IndexError, ValueError):
                await show_inbox(update, context, 1)
            
            
        
                    
        # Add this in the button_handler function where you handle other callbacks
        elif query.data == 'refresh_mini_app':
            await query.answer("Refreshing...")
            await mini_app_command(update, context)
        elif query.data.startswith("viewpost_"):
            post_id = int(query.data.split('_')[1])
            await view_post(update, context, post_id)    
        elif query.data == 'select_avatar':
            await show_avatar_selection(update, context)
            
        elif query.data.startswith('set_avatar_'):
            emoji = query.data.split('_', 2)[2]
            db_execute("UPDATE users SET avatar_emoji = %s WHERE user_id = %s", (emoji, user_id))
            await query.answer(f"✅ Avatar set to {emoji}!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'clear_avatar':
            db_execute("UPDATE users SET avatar_emoji = NULL WHERE user_id = %s", (user_id))
            await query.answer("✅ Avatar removed!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'list_blocked':
            await query.answer("🚫 Loading blocked users...", show_alert=False)
            blocked = db_fetch_all(
                """SELECT u.user_id, u.anonymous_name, u.sex 
                FROM blocks b JOIN users u ON b.blocked_id = u.user_id 
                WHERE b.blocker_id = %s""",
                (user_id,)
            )
            
            if not blocked:
                await query.message.edit_text(
                    "🚫 *Your Block List is Empty*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Settings", callback_data='settings')]]),
                    parse_mode=ParseMode.MARKDOWN
                )
                return
                
            text = "🚫 *Your Blocked Users*\n\n"
            kb = []
            for b_user in blocked:
                name = get_display_name(b_user)
                text += f"• {escape_markdown(name, version=2)}\n"
                kb.append([InlineKeyboardButton(f"🔓 Unblock {name}", callback_data=f"unblock_user_{b_user['user_id']}")])
            
            kb.append([InlineKeyboardButton("◀️ Back to Settings", callback_data='settings')])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)

        elif query.data.startswith('unblock_user_'):
            target_id = query.data.split('_', 2)[2]
            db_execute("DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (user_id, target_id))
            
            # Clear Aura Cache for real-time accuracy
            calculate_user_rating.cache_clear()
            format_aura.cache_clear()
            
            await query.answer("✅ User unblocked!", show_alert=False)
            
            # Refresh view (either profiles or list)
            if "Blocked Users" in query.message.text:
                # If we are in the list, refresh the list
                blocked = db_fetch_all(
                    "SELECT u.user_id, u.anonymous_name, u.sex FROM blocks b JOIN users u ON b.blocked_id = u.user_id WHERE b.blocker_id = %s",
                    (user_id,)
                )
                if not blocked:
                    await query.message.edit_text("🚫 List empty.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data='settings')]]))
                else:
                    text = "🚫 *Your Blocked Users (Updated)*\n\n"
                    kb = []
                    for b_user in blocked:
                        name = get_display_name(b_user)
                        text += f"• {escape_markdown(name, version=2)}\n"
                        kb.append([InlineKeyboardButton(f"🔓 Unblock {name}", callback_data=f"unblock_user_{b_user['user_id']}")])
                    kb.append([InlineKeyboardButton("◀️ Back", callback_data='settings')])
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # If we are in a message or profile, show success and button refresh
                await query.message.reply_text("✅ User has been unblocked.")
                # We can't easily refresh the profile here without sender data, so a simple message is enough or let user re-open.

        elif query.data.startswith('block_user_'):
            target_id = query.data.split('_', 2)[2]
            
            # Add to blocks table
            try:
                db_execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_id, target_id)
                )
                
                # Clear Aura Cache for real-time accuracy
                calculate_user_rating.cache_clear()
                format_aura.cache_clear()
                
                await query.message.reply_text("✅ User has been blocked. They can no longer send you messages.")

            except psycopg2.IntegrityError:
                await query.message.reply_text("❌ User is already blocked.")

        # ==================== REPORTING CALLBACKS ====================

        elif query.data.startswith('report_post_'):
            try:
                post_id = int(query.data.split('_')[2])
                post = db_fetch_one("SELECT post_id FROM posts WHERE post_id = %s", (post_id,))
                if not post:
                    await query.answer("❌ Post not found.", show_alert=True)
                    return
                context.user_data['reporting'] = {'type': 'post', 'id': post_id}
                await query.answer()
                await query.message.reply_text(
                    "🚨 *Report Post*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap ❌ Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
            except Exception as e:
                logger.error(f"Error in report_post handler: {e}")
                await query.answer("❌ Error processing request", show_alert=True)

        elif query.data.startswith('report_comment_'):
            try:
                comment_id = int(query.data.split('_')[2])
                comment = db_fetch_one("SELECT comment_id FROM comments WHERE comment_id = %s", (comment_id,))
                if not comment:
                    await query.answer("❌ Comment not found.", show_alert=True)
                    return
                context.user_data['reporting'] = {'type': 'comment', 'id': comment_id}
                await query.answer()
                await query.message.reply_text(
                    "🚨 *Report Comment*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap ❌ Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
            except Exception as e:
                logger.error(f"Error in report_comment handler: {e}")
                await query.answer("❌ Error processing request", show_alert=True)

        elif query.data == 'admin_reports':
            await query.answer("📋 Loading reports...", show_alert=False)
            await show_admin_reports(update, context, page=1)

        elif query.data.startswith('admin_reports_'):
            try:
                page = int(query.data.split('_')[2])
                await show_admin_reports(update, context, page=page)
            except (IndexError, ValueError):
                await show_admin_reports(update, context, page=1)

        elif query.data.startswith('report_view_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = db_fetch_one("SELECT * FROM reports WHERE report_id = %s", (report_id,))
                if not report:
                    await query.answer("❌ Report not found.", show_alert=True)
                    return
                preview, author_id = get_report_content_preview(report['target_type'], report['target_id'])
                type_label = "Post" if report['target_type'] == 'post' else "Comment"
                preview_text = html.escape(preview or '[Content deleted]')
                safe_reason = html.escape(report['reason'])
                reporter = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (report['reporter_id'],))
                reporter_name = html.escape(reporter['anonymous_name'] if reporter else 'Anonymous')
                view_text = (
                    f"🔍 <b>Report #{report_id}</b>\n"
                    f"Type: {type_label}\n"
                    f"Reporter: {reporter_name}\n"
                    f"Reason: {safe_reason}\n\n"
                    f"<b>Content Preview:</b>\n{preview_text}"
                )
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Dismiss", callback_data=f"report_dismiss_{report_id}"),
                        InlineKeyboardButton("❌ Delete Content", callback_data=f"report_delete_{report_id}"),
                    ],
                    [InlineKeyboardButton("⚠️ Warn User", callback_data=f"report_warn_{report_id}")],
                    [InlineKeyboardButton("🔙 Back to Reports", callback_data='admin_reports')]
                ]
                try:
                    await query.edit_message_text(view_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
                except Exception:
                    await query.message.reply_text(view_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Error in report_view handler: {e}")
                await query.answer("❌ Error loading report", show_alert=True)

        elif query.data.startswith('report_dismiss_'):
            try:
                report_id = int(query.data.split('_')[2])
                resolve_report(report_id, user_id, 'dismissed', None)
                await query.answer("✅ Report dismissed.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_dismiss handler: {e}")
                await query.answer("❌ Error dismissing report", show_alert=True)

        elif query.data.startswith('report_delete_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = db_fetch_one("SELECT * FROM reports WHERE report_id = %s", (report_id,))
                if not report:
                    await query.answer("❌ Report not found.", show_alert=True)
                    return
                preview, author_id = get_report_content_preview(report['target_type'], report['target_id'])
                # Delete the reported content
                if report['target_type'] == 'post':
                    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (report['target_id'],))
                    if post:
                        if post['channel_message_id']:
                            try:
                                await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=post['channel_message_id'])
                            except Exception:
                                pass
                        comments_list = db_fetch_all("SELECT comment_id FROM comments WHERE post_id = %s", (report['target_id'],))
                        for c in comments_list:
                            db_execute("DELETE FROM reactions WHERE comment_id = %s", (c['comment_id'],))
                        db_execute("DELETE FROM comments WHERE post_id = %s", (report['target_id'],))
                        db_execute("DELETE FROM post_categories WHERE post_id = %s", (report['target_id'],))
                        db_execute("DELETE FROM posts WHERE post_id = %s", (report['target_id'],))
                elif report['target_type'] == 'comment':
                    comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (report['target_id'],))
                    if comment:
                        post_id_for_count = comment['post_id']
                        db_execute("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (report['target_id'],))
                        db_execute("DELETE FROM reactions WHERE comment_id = %s", (report['target_id'],))
                        db_execute("DELETE FROM comments WHERE comment_id = %s", (report['target_id'],))
                        await adopt_orphaned_replies(context, post_id_for_count)
                resolve_report(report_id, user_id, 'action_taken', 'deleted')
                # Notify the content author (without revealing the reporter)
                if author_id:
                    try:
                        await context.bot.send_message(
                            chat_id=author_id,
                            text="⚠️ Your content was reviewed and removed by an admin due to a community report. Please ensure your posts follow our community guidelines."
                        )
                    except Exception:
                        pass
                await query.answer("✅ Content deleted.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_delete handler: {e}")
                await query.answer("❌ Error deleting content", show_alert=True)

        elif query.data.startswith('report_warn_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = db_fetch_one("SELECT * FROM reports WHERE report_id = %s", (report_id,))
                if not report:
                    await query.answer("❌ Report not found.", show_alert=True)
                    return
                _, author_id = get_report_content_preview(report['target_type'], report['target_id'])
                resolve_report(report_id, user_id, 'action_taken', 'warned')
                if author_id:
                    # Increment warning count
                    db_execute(
                        "UPDATE users SET warning_count = COALESCE(warning_count, 0) + 1 WHERE user_id = %s",
                        (author_id,)
                    )
                    try:
                        await context.bot.send_message(
                            chat_id=author_id,
                            text=(
                                "⚠️ *Warning from Admin*\n\n"
                                "Your content has been reported and reviewed by an admin. "
                                "Please ensure your posts and comments follow our community guidelines.\n\n"
                                "Repeated violations may result in content removal or other actions."
                            ),
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        pass
                await query.answer("✅ Warning sent to user.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_warn handler: {e}")
                await query.answer("❌ Error sending warning", show_alert=True)

        # ==================== END REPORTING CALLBACKS ====================
            
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
    

    # Handle cancel command or main menu buttons while in an input state
    main_menu_buttons = ["✍️ Share", "👤 Profile", "📚 Posts", "🏆 Top", "⚙️ Settings", "🌐 Open App", "❌ Cancel", "/cancel"]
    
    if text in main_menu_buttons or text.lower() == "cancel":
        # Check if user is in any input/waiting state
        in_waiting_state = user and (
            user.get('waiting_for_post') or 
            user.get('waiting_for_comment') or 
            user.get('awaiting_name') or 
            user.get('waiting_for_private_message') or 
            user.get('awaiting_bio') or
            context.user_data.get('broadcasting') or
            context.user_data.get('editing_comment') or
            context.user_data.get('editing_post') or
            context.user_data.get('awaiting_rejection_reason') or
            context.user_data.get('reporting')
        )
        
        if in_waiting_state:
            # Reset all waiting states
            await reset_user_waiting_states(
                user_id, 
                update.message.chat.id, 
                context
            )
            
            # Clear any context data
            context_keys = ['editing_comment', 'editing_post', 'thread_from_post_id', 
                           'pending_post', 'broadcasting', 'broadcast_step', 'broadcast_type', 'reporting']
            for key in context_keys:
                if key in context.user_data:
                    del context.user_data[key]
            
            if text in ["❌ Cancel", "/cancel"] or text.lower() == "cancel":
                await update.message.reply_text(
                    "❌ Input cancelled.",
                    reply_markup=get_main_menu(user_id)
                )
                return
            else:
                # User clicked another menu button (like "Share") while in an input state
                # Fall through to let the normal button handlers process it after state reset
                pass
        elif text in ["❌ Cancel", "/cancel"] or text.lower() == "cancel":
            await update.message.reply_text(
                "You're not currently in an input state.",
                reply_markup=get_main_menu(user_id)
            )
            return

    # NEW: Handle rejection reason capture from admin
    if context.user_data.get('awaiting_rejection_reason'):
        post_id = context.user_data.get('rejecting_post')
        if post_id:
            logger.info(f"Admin {user_id} providing reason for post {post_id}")
            await finalize_rejection(update, context, post_id, reason=text)
            return

    # NEW: Handle report reason capture from user
    if context.user_data.get('reporting'):
        reporting = context.user_data.get('reporting')
        reason = text.strip() if text else ""

        if not reason:
            await update.message.reply_text("❌ Please provide a reason (at least 1 character).")
            return

        if len(reason) > 200:
            await update.message.reply_text(
                "❌ Reason is too long (max 200 characters). Please shorten it and try again."
            )
            return

        target_type = reporting['type']
        target_id = reporting['id']

        report_id = create_report(user_id, target_type, target_id, reason)

        if report_id is None:
            await update.message.reply_text(
                "⚠️ You have already reported this content. An admin will review it.",
                reply_markup=get_main_menu(user_id)
            )
        elif report_id == -1:
            await update.message.reply_text(
                "⚠️ You've reached the daily report limit (5 per day). Please try again tomorrow.",
                reply_markup=get_main_menu(user_id)
            )
        else:
            await update.message.reply_text(
                "✅ Thank you. An admin will review your report.",
                reply_markup=get_main_menu(user_id)
            )
            # Notify admin of new report
            await notify_admin_of_new_report(context, report_id, user_id, target_type, reason)

        # Clear reporting state regardless of outcome
        if 'reporting' in context.user_data:
            del context.user_data['reporting']
        return

    
    # Rest of your handle_message code...

    # NEW: Handle comment editing

    if 'editing_comment' in context.user_data:
        comment_id = context.user_data['editing_comment']
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        
        if comment and comment['author_id'] == user_id and comment['type'] == 'text':
            # Update the comment
            db_execute(
                "UPDATE comments SET content = %s WHERE comment_id = %s",
                (text, comment_id)
            )
            
            # Clean up
            del context.user_data['editing_comment']
            
            await update.message.reply_text(
                "✅ Comment updated successfully!",
                reply_markup=get_main_menu(user_id)
            )
            return
        else:
            del context.user_data['editing_comment']
            await update.message.reply_text(
                "❌ Error updating comment. Please try again.",
                reply_markup=get_main_menu(user_id)
            )
            return


    # FIX: Handle pending post editing (NEW CODE STARTS HERE)
    if 'editing_post' in context.user_data and context.user_data['editing_post']:
        pending_post = context.user_data.get('pending_post')
        if pending_post:
            # Update the pending post content
            pending_post['content'] = text
            pending_post['timestamp'] = time.time()  # Reset edit timer
            context.user_data['pending_post'] = pending_post
            
            # Remove editing flag
            del context.user_data['editing_post']
            
            # Resend the confirmation with updated content
            await send_post_confirmation(
                update, context, 
                pending_post['content'], 
                pending_post['category'], 
                pending_post.get('media_type', 'text'), 
                pending_post.get('media_id'),
                pending_post.get('thread_from_post_id')
            )
            return
        else:
            del context.user_data['editing_post']
            await update.message.reply_text(
                "❌ No pending post found. Please start over.",
                reply_markup=get_main_menu(user_id)
            )


            return
    # FIX: Handle pending post editing (NEW CODE ENDS HERE)

    # If user doesn't exist, create them
    # FIX: only create user if not exists
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
        category = user.get('selected_categories')
        if not category:
            category = user.get('selected_category') # Fallback for transition
            
        if not category:
            await update.message.reply_text("❌ No categories selected. Please start over.", reply_markup=get_main_menu(user_id))
            db_execute("UPDATE users SET waiting_for_post = FALSE WHERE user_id = %s", (user_id,))
            return

        post_content = ""
        media_type = 'text'
        media_id = None
        
        try:
            if update.message.text:
                post_content = update.message.text
                media_type = 'text'
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
                # Handle other media types or show error
                return

            
            # FIX: Reset user state for BOTH text and media posts
            db_execute(
                "UPDATE users SET waiting_for_post = FALSE, selected_categories = NULL, selected_category = NULL WHERE user_id = %s",
                (user_id,)
            )
            
            # Send confirmation
            await send_post_confirmation(update, context, post_content, category, media_type, media_id, thread_from_post_id=thread_from_post_id)
            return
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            await update.message.reply_text(
                "❌ Error processing your media. Please try again.",
                reply_markup=get_main_menu(user_id)

            )
            # Reset state on error
            db_execute(
                "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
                (user_id,)
            )
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
        content = ""
    
        if update.message.text:
            content = update.message.text
            comment_type = 'text'
        elif update.message.voice:
            voice = update.message.voice
            file_id = voice.file_id
            comment_type = 'voice'
            content = update.message.caption or ""
        elif update.message.animation:  # GIF
            animation = update.message.animation
            file_id = animation.file_id
            comment_type = 'gif'
            content = update.message.caption or ""
        elif update.message.sticker:
            sticker = update.message.sticker
            file_id = sticker.file_id
            comment_type = 'sticker'
            content = ""  # Stickers don't have text content
        elif update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            comment_type = 'photo'
            content = update.message.caption or ""
        else:
            await update.message.reply_text("❌ Unsupported comment type. Please send text, voice, GIF, sticker, or photo.")
            return
    
        # Insert new comment
        db_execute(
            """INSERT INTO comments
            (post_id, parent_comment_id, author_id, content, type, file_id)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id)
        )
        
        # Clear Aura Cache
        calculate_user_rating.cache_clear()
        format_aura.cache_clear()

    
        # Reset state
        db_execute(
            "UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL WHERE user_id = %s",
            (user_id,)
        )
    
        await update.message.reply_text("✅ Your comment has been posted!", reply_markup=get_main_menu(user_id))

        
        # Update comment count in background
        asyncio.create_task(update_channel_post_comment_count(context, post_id))
        
        # Notify vent author if this is a top‑level comment
        if parent_comment_id == 0:
            await notify_vent_author_of_comment(context, post_id, user_id)
        
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
                reply_markup=get_main_menu(user_id)
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
            reply_markup=get_main_menu(user_id)
        )


        return

    if user and user.get('awaiting_name'):
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s",
                (new_name, user_id)
            )
            await update.message.reply_text(
                f"✅ Name updated to *{new_name}*!", 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )


            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle main menu buttons
    if text == "✍️ Share":
        context.user_data['selected_categories'] = set()
        await update.message.reply_text(
            "📚 *Select categories (you can choose multiple):*",
            reply_markup=build_multi_category_keyboard(set()),
            parse_mode=ParseMode.MARKDOWN
        )
        return 

    elif text == "👤 Profile":
        await send_updated_profile(user_id, update.message.chat.id, context)
        return
        
    if user and user.get('awaiting_bio'):
        if not text:
            await update.message.reply_text("❌ Bio must be text. Please try again.")
            return
            
        if len(text) > 200:
             await update.message.reply_text("❌ Bio is too long (max 200 chars). Please shorten it.")
             return
             
        db_execute("UPDATE users SET bio = %s, awaiting_bio = FALSE WHERE user_id = %s", (text, user_id))
        await update.message.reply_text("✅ Bio updated successfully!", reply_markup=get_main_menu(user_id))

        await send_updated_profile(user_id, update.message.chat.id, context)
        return 

    elif text == "🏆 Top":
        await show_leaderboard(update, context)
        return

    elif text == "⚙️ Settings":
        await show_settings(update, context)
        return

    elif text == "📚 Posts":
        await show_my_content_menu(update, context)  # Show menu instead of direct posts
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

    elif text == "🌐 Open App":
        await mini_app_command(update, context)
        return


    # If none of the above, show main menu
    await update.message.reply_text(
        "How can I help you?",
        reply_markup=get_main_menu(user_id)

    )
async def handle_private_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text

    user = db_fetch_one(
        "SELECT waiting_for_private_message, private_message_target FROM users WHERE user_id = %s",
        (user_id,)
    )

    if not user or not user["waiting_for_private_message"]:
        return  # Not replying to a private message

    receiver_id = user["private_message_target"]

    # Prevent sending message to self
    if receiver_id == user_id:
        await update.message.reply_text("❌ You cannot message yourself.")
        return

    # Save message
    msg = db_execute(
        """
        INSERT INTO private_messages (sender_id, receiver_id, content)
        VALUES (%s, %s, %s)
        RETURNING message_id
        """,
        (user_id, receiver_id, text),
        fetchone=True
    )

    # Reset reply state
    db_execute(
        """
        UPDATE users
        SET waiting_for_private_message = FALSE,
            private_message_target = NULL
        WHERE user_id = %s
        """,
        (user_id,)
    )

    # Notify receiver
    await notify_user_of_private_message(
        context,
        sender_id=user_id,
        receiver_id=receiver_id,
        message_content=text,
        message_id=msg["message_id"]
    )

    await update.message.reply_text("✅ Message sent!")

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("webapp", "🌐 Open Web App"),
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
    
    # Set the bot-level menu button to default behavior
    # This ensures the bottom-left button triggers the keyboard/commands instead of opening the app directly
    try:
        from telegram import MenuButtonDefault
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonDefault()
        )
        logger.info("✅ Bot menu button set to Default (Trigger Keyboard)")
    except Exception as e:
        logger.warning(f"Could not set menu button: {e}")


async def mini_app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the mini app link with authentication token — opens natively inside Telegram"""
    user_id = str(update.effective_user.id)
    
    # Generate a secure JWT token valid 30 days
    token = jwt.encode(
        {
            'user_id': user_id,
            'exp': datetime.now(timezone.utc) + timedelta(days=30)
        },
        TOKEN,
        algorithm='HS256'
    )
    
    render_url = os.getenv('RENDER_URL', 'https://your-render-url.onrender.com')
    mini_app_url = f"{render_url}/?token={token}"
    
    # Primary: native WebApp button (opens inside Telegram without leaving the app)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Christian Vent App", web_app=WebAppInfo(url=mini_app_url))],
        [InlineKeyboardButton("📱 Open in Browser", url=mini_app_url)],
    ])
    
    await update.message.reply_text(
        "🌐 *Christian Vent Web App*\n\n"
        "Tap *Open Christian Vent App* to launch the app right here inside Telegram — no browser needed!\n\n"
        "📋 *You can:*\n"
        "• Share anonymous vents & prayers\n"
        "• Read & respond to the community\n"
        "• Check the leaderboard\n"
        "• Manage your profile\n\n"
        "_Your access is valid for 30 days._",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

def main():
    # Initialize database before starting the bot
    try:
        init_db()
        logger.info("Database initialized successfully")
        
        # Assign vent numbers to existing posts
        assign_vent_numbers_to_existing_posts()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return



    
    # Create and run Telegram bot
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    
    # Add your handlers
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("webapp", mini_app_command))
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("inbox", show_inbox))
    app.add_handler(CommandHandler("fixventnumbers", fix_vent_numbers))
    app.add_handler(CommandHandler("recount_comments", recount_comments))
    app.add_handler(CommandHandler("reset_weekly_badges", reset_weekly_badges_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_message_text))
    
    app.add_error_handler(error_handler)
    
    
    
    # Start Flask server in a separate thread for Render
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    logger.info(f"✅ Flask health check server started on port {port}")
    
    # Schedule Weekly Badges (Every Monday at 00:00 UTC)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_daily(
            award_weekly_badges,
            time=time(0, 0, tzinfo=timezone.utc),
            days=(0,)  # Monday = 0
        )
        logger.info("📅 Weekly badge awarding job scheduled for Mondays at 00:00 UTC")

    # Start polling
    logger.info("Starting bot polling...")
    app.run_polling()

# In bot.py, replace the simple /mini_app route with this:

@flask_app.route('/mini_app')
def mini_app_page():
    """Complete Mini App - returns the old UI style with new features integrated."""

    # All these are already loaded globally in bot.py via load_dotenv()
    _bot      = BOT_USERNAME
    _primary  = PRIMARY_COLOR        # e.g. "#c9a84c"
    _secondary= SECONDARY_COLOR      # e.g. "#e8c97a"
    _card_bg  = CARD_BG_COLOR        # e.g. "#161410"
    _border   = BORDER_COLOR         # e.g. "#1e1c18"
    _text     = TEXT_COLOR           # e.g. "#e8e0d0"
    _rgb      = PRIMARY_RGB          # e.g. "201, 168, 76"

    html = ("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Christian Vent</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    /* ===== CSS RESET & VARIABLES ===== */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --primary: SLOT_PRIMARY;
      --primary-dim: rgba(SLOT_RGB, 0.15);
      --bg-color: #0b0a08;
      --card-bg: rgba(22, 20, 16, 0.6);
      --border: SLOT_BORDER;
      --text: SLOT_TEXT;
      --text-dim: rgba(255, 255, 255, 0.5);
      --font-family: 'Inter', sans-serif;
      --radius: 12px;
      --nav-h: 65px;
    }
    body {
      font-family: var(--font-family);
      background-color: var(--bg-color);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
      padding-bottom: calc(var(--nav-h) + 20px);
    }
    canvas#particleCanvas {
      position: fixed;
      top: 0; left: 0; width: 100%; height: 100%;
      z-index: -1;
      pointer-events: none;
    }

    /* ===== SCROLLBAR ===== */
    ::-webkit-scrollbar { width: 4px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

    /* ===== LAYOUT ===== */
    .page { display: none; padding: 16px; max-width: 600px; margin: 0 auto; }
    .page.active { display: block; }

    /* ===== HEADER ===== */
    .app-header {
      text-align: center;
      padding: 20px 16px;
    }
    .app-title {
      font-size: 1.6rem;
      color: var(--primary);
      font-weight: 700;
      letter-spacing: -0.5px;
    }
    .app-subtitle {
      font-size: 0.85rem;
      color: var(--text-dim);
      margin-top: 4px;
    }

    /* ===== BOTTOM NAV ===== */
    .bottom-nav {
      position: fixed;
      bottom: 0; left: 0; right: 0;
      height: var(--nav-h);
      background: rgba(11, 10, 8, 0.85);
      backdrop-filter: blur(15px);
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-around;
      align-items: center;
      z-index: 1000;
      padding-bottom: env(safe-area-inset-bottom, 0);
    }
    .nav-btn {
      background: none; border: none;
      color: var(--text-dim);
      font-family: var(--font-family);
      font-size: 0.75rem;
      display: flex; flex-direction: column; align-items: center; gap: 4px;
      cursor: pointer;
      flex: 1;
      padding: 8px 0;
      transition: color 0.2s;
    }
    .nav-btn.active { color: var(--primary); }
    .nav-icon { font-size: 1.4rem; }

    /* ===== CARDS & GLASSMORPHISM ===== */
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin-bottom: 16px;
    }
    .card-title {
      font-size: 1.1rem;
      color: var(--primary);
      font-weight: 600;
      margin-bottom: 6px;
    }
    .card-sub { font-size: 0.8rem; color: var(--text-dim); margin-bottom: 12px; }

    /* ===== FORM ELEMENTS ===== */
    .vent-textarea {
      width: 100%;
      min-height: 120px;
      background: rgba(0,0,0,0.3);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      color: var(--text);
      font-family: var(--font-family);
      font-size: 0.9rem;
      resize: vertical;
      outline: none;
      transition: border-color 0.2s;
      margin-bottom: 8px;
    }
    .vent-textarea:focus { border-color: var(--primary); }
    
    .btn-primary {
      width: 100%;
      padding: 14px;
      background: var(--primary);
      color: #000;
      border: none;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.95rem;
      font-family: var(--font-family);
      cursor: pointer;
      transition: opacity 0.2s;
    }
    .btn-primary:active { opacity: 0.8; }
    .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

    .btn-ghost {
      background: transparent;
      border: 1px solid var(--primary);
      color: var(--primary);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.8rem;
      cursor: pointer;
    }

    /* ===== CATEGORY GRID ===== */
    .categories-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 16px;
      max-height: 200px;
      overflow-y: auto;
    }
    .cat-btn {
      display: flex; align-items: center; gap: 6px;
      background: rgba(0,0,0,0.2);
      border: 1px solid var(--border);
      padding: 8px 10px;
      border-radius: 6px;
      color: var(--text);
      font-size: 0.8rem;
      cursor: pointer;
      text-align: left;
      transition: all 0.2s;
    }
    .cat-btn.selected {
      background: var(--primary-dim);
      border-color: var(--primary);
    }
    .cat-icon-check {
      width: 14px; height: 14px;
      border: 1px solid var(--text-dim);
      border-radius: 3px;
      display: flex; align-items: center; justify-content: center;
      font-size: 10px;
      flex-shrink: 0;
    }
    .cat-btn.selected .cat-icon-check {
      background: var(--primary);
      border-color: var(--primary);
      color: #000;
    }

    /* ===== FEED POSTS ===== */
    .search-bar {
      width: 100%;
      background: rgba(0,0,0,0.3);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--text);
      font-family: var(--font-family);
      margin-bottom: 16px;
      outline: none;
    }
    .search-bar:focus { border-color: var(--primary); }

    .post-header {
      display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
    }
    .avatar {
      width: 36px; height: 36px;
      background: var(--primary-dim);
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 18px;
    }
    .post-author { font-weight: 600; font-size: 0.9rem; }
    .post-time { font-size: 0.75rem; color: var(--text-dim); margin-left: auto; }
    
    .cat-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
    .cat-badge {
      background: rgba(0,0,0,0.4);
      border: 1px solid var(--border);
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.7rem;
      color: var(--text-dim);
    }
    
    .post-content {
      font-size: 0.9rem; line-height: 1.5; margin-bottom: 12px; word-break: break-word;
    }
    .post-content.truncated {
      display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
    }
    
    .post-footer {
      display: flex; align-items: center; justify-content: space-between;
      border-top: 1px solid rgba(255,255,255,0.05);
      padding-top: 12px;
    }
    .unread-badge {
      background: var(--primary); color: #000; font-size: 0.65rem; font-weight: 700;
      padding: 2px 6px; border-radius: 10px; margin-left: 6px;
    }
    
    /* ===== COMMENTS ===== */
    .comment-list { margin-top: 16px; }
    .comment-item {
      display: flex; gap: 10px; margin-bottom: 16px; position: relative;
    }
    .comment-item.is-reply { margin-left: 32px; }
    .comment-item.is-reply::before {
      content: ''; position: absolute; left: -16px; top: 0; bottom: 0;
      width: 2px; background: var(--border); border-radius: 2px;
    }
    .comment-body {
      flex: 1; background: rgba(0,0,0,0.2); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px;
    }
    .comment-actions {
      display: flex; gap: 12px; margin-top: 8px; font-size: 0.75rem; color: var(--text-dim);
    }
    .action-btn { background: none; border: none; color: inherit; cursor: pointer; padding: 0; }
    .action-btn:hover { color: var(--primary); }

    .inline-reply-box { display: none; margin-top: 8px; }
    .inline-reply-box.open { display: block; }
    
    /* ===== MISC ===== */
    .skeleton {
      height: 100px; background: rgba(255,255,255,0.05); border-radius: 8px; margin-bottom: 12px;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse { 0% { opacity: 0.5; } 50% { opacity: 1; } 100% { opacity: 0.5; } }
    
    .toast {
      position: fixed; bottom: 85px; left: 50%; transform: translateX(-50%);
      background: var(--primary); color: #000; padding: 10px 20px; border-radius: 20px;
      font-size: 0.85rem; font-weight: 600; opacity: 0; pointer-events: none; transition: opacity 0.3s;
      z-index: 2000;
    }
    .toast.show { opacity: 1; }
    
    .toggle-switch {
      position: relative; display: inline-block; width: 40px; height: 22px;
    }
    .toggle-switch input { opacity: 0; width: 0; height: 0; }
    .toggle-slider {
      position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
      background-color: rgba(255,255,255,0.1); transition: .4s; border-radius: 22px;
    }
    .toggle-slider:before {
      position: absolute; content: ""; height: 16px; width: 16px; left: 3px; bottom: 3px;
      background-color: var(--text-dim); transition: .4s; border-radius: 50%;
    }
    input:checked + .toggle-slider { background-color: var(--primary-dim); }
    input:checked + .toggle-slider:before { transform: translateX(18px); background-color: var(--primary); }
    
    .emoji-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
    .emoji-item {
      font-size: 1.5rem; text-align: center; padding: 8px; background: rgba(0,0,0,0.2);
      border-radius: 8px; cursor: pointer; border: 1px solid transparent;
    }
    .emoji-item.selected { border-color: var(--primary); background: var(--primary-dim); }
    
    #authScreen {
      position: fixed; top:0; left:0; width:100%; height:100%;
      background: var(--bg-color); z-index: 9999;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
    }
    .spinner {
      width: 40px; height: 40px; border: 3px solid rgba(255,255,255,0.1);
      border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>

<canvas id="particleCanvas"></canvas>

<div id="authScreen">
  <div class="spinner"></div>
  <h2 style="margin-top: 20px; color: var(--primary);">Authenticating...</h2>
</div>

<div id="mainApp" style="display:none;">

  <header class="app-header">
    <div class="app-title">Christian Vent</div>
    <div class="app-subtitle">Share securely & anonymously</div>
  </header>

  <!-- VENT PAGE -->
  <section id="page-vent" class="page active">
    <div class="card">
      <div class="card-title">Share Your Heart</div>
      <div class="card-sub">Pick categories that match your vent:</div>
      <div class="categories-grid" id="categoriesGrid"></div>
      
      <textarea id="ventInput" class="vent-textarea" placeholder="What's on your heart?" maxlength="5000"></textarea>
      <div style="text-align:right; font-size:0.75rem; color:var(--text-dim); margin-bottom:12px;" id="charCount">0/5000</div>
      
      <button class="btn-primary" id="submitVentBtn">Post Anonymously</button>
    </div>
  </section>

  <!-- FEED PAGE -->
  <section id="page-feed" class="page">
    <input type="text" id="searchInput" class="search-bar" placeholder="Search vents...">
    <div id="feedContainer"></div>
    <div id="loadMoreArea" style="text-align:center; display:none; padding: 10px;">
      <button class="btn-ghost" id="loadMoreBtn">Load More</button>
    </div>
  </section>

  <!-- POST DETAIL PAGE -->
  <section id="page-detail" class="page">
    <button class="btn-ghost" onclick="switchPage('feed')" style="margin-bottom:16px; border:none; padding:0;">← Back</button>
    <div id="detailPostBox"></div>
    
    <div class="card" style="margin-top:16px; padding: 12px;">
      <textarea id="commentInput" class="vent-textarea" style="min-height:70px; margin-bottom:8px;" placeholder="Offer a response..."></textarea>
      <button class="btn-primary" id="postCommentBtn" style="padding: 10px;">Send Response</button>
    </div>
    
    <div id="detailCommentsBox" class="comment-list"></div>
  </section>

  <!-- LEADERBOARD PAGE -->
  <section id="page-leaderboard" class="page">
    <div class="card">
      <div class="card-title">Top Contributors</div>
      <div id="leaderboardContainer" style="margin-top: 16px;"></div>
    </div>
  </section>

  <!-- PROFILE PAGE -->
  <section id="page-profile" class="page">
    <div id="profileContainer"></div>
  </section>

  <!-- EDIT PROFILE PAGE -->
  <section id="page-edit-profile" class="page">
    <button class="btn-ghost" onclick="switchPage('profile')" style="margin-bottom:16px; border:none; padding:0;">← Back</button>
    <div class="card">
      <div class="card-title">Edit Profile</div>
      
      <label style="font-size:0.8rem; color:var(--primary); margin-bottom:4px; display:block;">Anonymous Name</label>
      <input type="text" id="edit-name" class="vent-textarea" style="min-height:40px; margin-bottom:16px;">
      
      <label style="font-size:0.8rem; color:var(--primary); margin-bottom:4px; display:block;">Bio</label>
      <textarea id="edit-bio" class="vent-textarea" style="min-height:80px; margin-bottom:16px;"></textarea>
      
      <label style="font-size:0.8rem; color:var(--primary); margin-bottom:8px; display:block;">Avatar</label>
      <div id="emoji-grid" class="emoji-grid" style="margin-bottom:20px;"></div>
      
      <button class="btn-primary" id="saveProfileBtn">Save Profile</button>
    </div>
  </section>

  <!-- SETTINGS PAGE -->
  <section id="page-settings" class="page">
    <div class="card">
      <div class="card-title">Settings</div>
      
      <div style="display:flex; justify-content:space-between; align-items:center; margin: 20px 0;">
        <div>
          <div style="font-weight:500;">Push Notifications</div>
          <div style="font-size:0.75rem; color:var(--text-dim);">Alerts for replies</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="set-notifications">
          <span class="toggle-slider"></span>
        </label>
      </div>
      
      <div style="display:flex; justify-content:space-between; align-items:center; margin: 20px 0;">
        <div>
          <div style="font-weight:500;">Public Profile</div>
          <div style="font-size:0.75rem; color:var(--text-dim);">Others see your stats</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="set-privacy">
          <span class="toggle-slider"></span>
        </label>
      </div>
      
      <button class="btn-primary" id="saveSettingsBtn">Apply</button>
    </div>
  </section>

  <!-- BOTTOM NAV -->
  <nav class="bottom-nav">
    <button class="nav-btn active" data-page="vent"><span class="nav-icon">✍️</span>Vent</button>
    <button class="nav-btn" data-page="feed"><span class="nav-icon">🌍</span>Feed</button>
    <button class="nav-btn" data-page="leaderboard"><span class="nav-icon">🏆</span>Top</button>
    <button class="nav-btn" data-page="profile"><span class="nav-icon">👤</span>Me</button>
    <button class="nav-btn" data-page="settings"><span class="nav-icon">⚙️</span>Settings</button>
  </nav>

</div>

<div id="toast" class="toast"></div>

<script>
'use strict';

const CONFIG = {
  botUsername: 'SLOT_BOT',
  apiBase: window.location.origin,
  categories: [
    ['PrayForMe', '🙏 Pray For Me'], ['Bible', '📖 Bible Study'], ['WorkLife', '💼 Work & Life'],
    ['SpiritualLife', '🕊 Spiritual Life'], ['ChristianChallenges', '⚔️ Challenges'], 
    ['Relationship', '❤️ Relationship'], ['Marriage', '💍 Marriage'], ['Youth', '🧑‍🤝‍🧑 Youth'],
    ['Finance', '💰 Finance'], ['WorshipMusic', '🎶 Worship'], ['Family', '🏠 Family'],
    ['Testimony', '🙌 Testimony'], ['AddictionRecovery', '💊 Recovery'], ['BibleQuestion', '📖 Bible Q&A'],
    ['Other', '🔖 Other']
  ],
  emojis: ['👨', '👩', '🕊️', '🙏', '✝️', '📖', '❤️', '🌟', '🛡️', '⚔️', '⛪', '🎹', '👶', '🧑', '👴']
};

const state = {
  userId: null, currentPage: 'vent', feedPage: 1, feedHasMore: true, feedLoading: false,
  searchQuery: '', currentPostId: null, selectedCategories: new Set(),
  profileData: null, selectedEmoji: null
};

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str || '';
  return d.innerHTML;
}
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), 3000);
}
async function apiFetch(path, opts = {}) {
  const res = await fetch(CONFIG.apiBase + path, { headers: {'Content-Type': 'application/json'}, ...opts });
  const data = await res.json();
  if(!res.ok || !data.success) throw new Error(data.error || 'HTTP ' + res.status);
  return data;
}

// NAVIGATION
function switchPage(name) {
  state.currentPage = name;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const t = document.getElementById('page-' + name);
  if(t) t.classList.add('active');
  
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  
  if(name === 'feed' && state.feedPage === 1) loadFeed();
  if(name === 'leaderboard') loadLeaderboard();
  if(name === 'profile' && state.userId) loadProfile();
  if(name === 'settings' && state.userId) loadSettings();
  window.scrollTo(0,0);
}

// VENT PAGE
function renderCategories() {
  const grid = document.getElementById('categoriesGrid');
  grid.innerHTML = CONFIG.categories.map(([code, label]) => `
    <div class="cat-btn" data-code="${code}">
      <div class="cat-icon-check"></div>
      ${esc(label)}
    </div>
  `).join('');
  
  grid.querySelectorAll('.cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const c = btn.dataset.code;
      if(state.selectedCategories.has(c)) {
        state.selectedCategories.delete(c); btn.classList.remove('selected');
      } else {
        state.selectedCategories.add(c); btn.classList.add('selected');
      }
      btn.querySelector('.cat-icon-check').textContent = state.selectedCategories.has(c) ? '✓' : '';
    });
  });
}

async function submitVent() {
  const text = document.getElementById('ventInput').value.trim();
  const cats = Array.from(state.selectedCategories);
  if(!text) return toast('Please write something');
  if(!cats.length) return toast('Select at least one category');
  
  const btn = document.getElementById('submitVentBtn');
  btn.disabled = true; btn.textContent = 'Posting...';
  
  try {
    await apiFetch('/api/mini-app/submit-vent', {
      method: 'POST', body: JSON.stringify({ user_id: state.userId, content: text, categories: cats })
    });
    toast('✅ Vent submitted for approval!');
    document.getElementById('ventInput').value = '';
    state.selectedCategories.clear();
    document.querySelectorAll('.cat-btn').forEach(b => { b.classList.remove('selected'); b.querySelector('.cat-icon-check').textContent=''; });
    document.getElementById('charCount').textContent = '0/5000';
    state.feedPage = 1;
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Post Anonymously'; }
}

// FEED PAGE
async function loadFeed(append = false) {
  if(state.feedLoading) return;
  state.feedLoading = true;
  const container = document.getElementById('feedContainer');
  const loadMore = document.getElementById('loadMoreArea');
  
  if(!append) { container.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>'; loadMore.style.display='none'; }
  
  try {
    let url = `/api/mini-app/get-posts?page=${state.feedPage}&user_id=${state.userId}`;
    if(state.searchQuery) url = `/api/mini-app/search?q=${encodeURIComponent(state.searchQuery)}&page=${state.feedPage}&user_id=${state.userId}`;
    
    const data = await apiFetch(url);
    const posts = data.data || [];
    state.feedHasMore = data.has_more;
    
    if(!append) container.innerHTML = '';
    if(posts.length === 0 && !append) container.innerHTML = '<div style="text-align:center; padding:20px; color:var(--text-dim);">No posts found.</div>';
    
    posts.forEach(p => {
      const cats = (p.categories||[]).map(c => `<span class="cat-badge">${esc(c)}</span>`).join('');
      const unread = p.unread_comments > 0 ? `<span class="unread-badge">${p.unread_comments} new</span>` : '';
      
      container.insertAdjacentHTML('beforeend', `
        <div class="card" style="cursor:pointer;" onclick="openPost(${p.id})">
          <div class="post-header">
            <div class="avatar">${esc(p.author?.avatar || p.author?.sex || '👤')}</div>
            <div>
              <div class="post-author">${esc(p.author?.name || 'Anonymous')} ${esc(p.author?.aura||'')}</div>
              <div class="post-time">${esc(p.time_ago)}</div>
            </div>
          </div>
          <div class="cat-badges">${cats}</div>
          <div class="post-content truncated">${esc(p.content)}</div>
          <div class="post-footer">
            <span style="font-size:0.8rem; color:var(--text-dim);">💬 ${p.comments} replies ${unread}</span>
            <span style="font-size:0.8rem; color:var(--primary);">Read →</span>
          </div>
        </div>
      `);
    });
    
    loadMore.style.display = state.feedHasMore ? 'block' : 'none';
    if(state.feedHasMore) state.feedPage++;
  } catch(e) {
    if(!append) container.innerHTML = '<div style="text-align:center; color:var(--text-dim);">Failed to load.</div>';
    toast('Error loading feed');
  } finally { state.feedLoading = false; }
}

// POST DETAIL
async function openPost(id) {
  state.currentPostId = id;
  switchPage('detail');
  const box = document.getElementById('detailPostBox');
  const commBox = document.getElementById('detailCommentsBox');
  box.innerHTML = '<div class="skeleton"></div>'; commBox.innerHTML = '';
  
  try {
    const data = await apiFetch(`/api/mini-app/post/${id}`);
    const p = data.data;
    const cats = (p.categories||[]).map(c => `<span class="cat-badge">${esc(c)}</span>`).join('');
    
    box.innerHTML = `
      <div class="card">
        <div class="post-header">
          <div class="avatar">${esc(p.author?.avatar || p.author?.sex || '👤')}</div>
          <div>
            <div class="post-author">${esc(p.author?.name || 'Anonymous')} ${esc(p.author?.aura||'')}</div>
            <div class="post-time">${esc(p.time_ago)}</div>
          </div>
        </div>
        <div class="cat-badges">${cats}</div>
        <div class="post-content">${esc(p.content)}</div>
      </div>
    `;
    await loadComments(id);
  } catch(e) { box.innerHTML = 'Error loading post.'; }
}

async function loadComments(id) {
  const box = document.getElementById('detailCommentsBox');
  box.innerHTML = '<div class="skeleton"></div>';
  try {
    const data = await apiFetch(`/api/mini-app/post/${id}/comments`);
    const comments = data.data || [];
    
    if(!comments.length) { box.innerHTML = '<div style="text-align:center; color:var(--text-dim);">No replies yet.</div>'; return; }
    
    const map = {}; const roots = [];
    comments.forEach(c => map[c.id] = {...c, children: []});
    comments.forEach(c => {
      if(c.parent_id && map[c.parent_id]) map[c.parent_id].children.push(map[c.id]);
      else roots.push(map[c.id]);
    });
    
    const renderC = (c, depth) => {
      const isReply = depth > 0 ? 'is-reply' : '';
      const isMine = String(c.author?.id || c.author_id) === String(state.userId) || c.author?.is_me;
      
      let actions = `<button class="action-btn" onclick="toggleReply(${c.id})">Reply</button>`;
      if(isMine) {
        actions += `
          <button class="action-btn" onclick="editComment(${c.id}, '${esc(c.content.replace(/'/g, "\\'"))}')">Edit</button>
          <button class="action-btn" style="color:#e74c3c;" onclick="deleteComment(${c.id})">Delete</button>
        `;
      }
      
      const children = c.children.map(ch => renderC(ch, depth+1)).join('');
      return `
        <div class="comment-item ${isReply}">
          <div class="avatar" style="width:28px; height:28px; font-size:14px;">${esc(c.author?.avatar || '👤')}</div>
          <div class="comment-body">
            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
              <span style="font-size:0.8rem; font-weight:600; color:var(--primary);">${esc(c.author?.name)} ${esc(c.author?.aura)}</span>
              <span style="font-size:0.7rem; color:var(--text-dim);">${esc(c.time_ago)}</span>
            </div>
            <div style="font-size:0.85rem; line-height:1.4;" id="comment-content-${c.id}">${esc(c.content)}</div>
            <div class="comment-actions">${actions}</div>
            
            <div class="inline-reply-box" id="reply-box-${c.id}">
              <textarea class="vent-textarea" style="min-height:50px; margin-bottom:4px;" id="reply-text-${c.id}"></textarea>
              <div style="text-align:right;">
                <button class="btn-ghost" style="padding:4px 8px; font-size:0.7rem;" onclick="toggleReply(${c.id})">Cancel</button>
                <button class="btn-primary" style="padding:4px 12px; width:auto; font-size:0.7rem; display:inline-block;" onclick="sendReply(${c.id})">Send</button>
              </div>
            </div>
          </div>
        </div>
        ${children}
      `;
    };
    box.innerHTML = roots.map(c => renderC(c, 0)).join('');
  } catch(e) { box.innerHTML = 'Error loading comments.'; }
}

async function postComment() {
  const text = document.getElementById('commentInput').value.trim();
  if(!text) return toast('Write something');
  const btn = document.getElementById('postCommentBtn');
  btn.disabled = true;
  try {
    await apiFetch(`/api/mini-app/post/${state.currentPostId}/comment`, {
      method: 'POST', body: JSON.stringify({ user_id: state.userId, content: text, parent_comment_id: 0 })
    });
    document.getElementById('commentInput').value = '';
    toast('Reply posted');
    await loadComments(state.currentPostId);
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; }
}

function toggleReply(id) {
  const box = document.getElementById('reply-box-'+id);
  box.classList.toggle('open');
}

async function sendReply(parentId) {
  const text = document.getElementById('reply-text-'+parentId).value.trim();
  if(!text) return toast('Write something');
  try {
    await apiFetch(`/api/mini-app/post/${state.currentPostId}/comment`, {
      method: 'POST', body: JSON.stringify({ user_id: state.userId, content: text, parent_comment_id: parentId })
    });
    toast('Reply posted');
    await loadComments(state.currentPostId);
  } catch(e) { toast(e.message); }
}

async function editComment(id, oldText) {
  const newText = prompt("Edit comment:", oldText);
  if(newText === null || newText.trim() === '' || newText.trim() === oldText) return;
  try {
    await apiFetch(`/api/mini-app/comment/${id}`, {
      method: 'PUT', body: JSON.stringify({ user_id: state.userId, content: newText.trim() })
    });
    toast('Comment edited');
    await loadComments(state.currentPostId);
  } catch(e) { toast(e.message); }
}

async function deleteComment(id) {
  if(!confirm("Delete this comment?")) return;
  try {
    await apiFetch(`/api/mini-app/comment/${id}`, {
      method: 'DELETE', body: JSON.stringify({ user_id: state.userId })
    });
    toast('Comment deleted');
    await loadComments(state.currentPostId);
  } catch(e) { toast(e.message); }
}

// LEADERBOARD
async function loadLeaderboard() {
  const box = document.getElementById('leaderboardContainer');
  box.innerHTML = '<div class="skeleton"></div>';
  try {
    const data = await apiFetch('/api/mini-app/leaderboard');
    box.innerHTML = (data.data||[]).map((u,i) => `
      <div style="display:flex; align-items:center; gap:12px; padding:10px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
        <div style="width:24px; font-weight:700; color:${i===0?'#FFD700':i===1?'#C0C0C0':i===2?'#CD7F32':'var(--text-dim)'};">${i<3?['👑','🥈','🥉'][i]:i+1}</div>
        <div class="avatar">${esc(u.avatar||'👤')}</div>
        <div style="flex:1;">
          <div style="font-weight:600; font-size:0.9rem;">${esc(u.name)}</div>
          <div style="font-size:0.75rem; color:var(--text-dim);">${esc(u.aura)}</div>
        </div>
        <div style="font-weight:700; color:var(--primary);">${u.points} pts</div>
      </div>
    `).join('');
  } catch(e) { box.innerHTML = 'Error loading leaderboard.'; }
}

// PROFILE
async function loadProfile() {
  const box = document.getElementById('profileContainer');
  box.innerHTML = '<div class="skeleton" style="height:200px;"></div>';
  try {
    const data = await apiFetch(`/api/mini-app/profile/${state.userId}`);
    const p = data.data; state.profileData = p;
    
    box.innerHTML = `
      <div class="card" style="text-align:center;">
        <button class="btn-ghost" onclick="setupEditProfile()" style="position:absolute; right:16px; top:16px;">Edit</button>
        <div class="avatar" style="width:80px; height:80px; font-size:32px; margin:0 auto 12px; border:2px solid var(--primary);">${esc(p.avatar||'👤')}</div>
        <div style="font-size:1.3rem; font-weight:700; color:var(--primary);">${esc(p.name)}</div>
        <div style="font-size:0.85rem; color:var(--text-dim); margin-bottom:16px;">${esc(p.aura)} ${p.rating} pts</div>
        ${p.bio ? `<div style="font-style:italic; font-size:0.9rem; margin-bottom:20px;">"${esc(p.bio)}"</div>` : ''}
        
        <div style="display:flex; justify-content:space-around; border-top:1px solid var(--border); padding-top:16px;">
          <div><div style="font-size:1.2rem; font-weight:700; color:var(--primary);">${p.stats?.posts||0}</div><div style="font-size:0.7rem; color:var(--text-dim);">Vents</div></div>
          <div><div style="font-size:1.2rem; font-weight:700; color:var(--primary);">${p.stats?.comments||0}</div><div style="font-size:0.7rem; color:var(--text-dim);">Replies</div></div>
          <div><div style="font-size:1.2rem; font-weight:700; color:var(--primary);">${p.stats?.followers||0}</div><div style="font-size:0.7rem; color:var(--text-dim);">Followers</div></div>
        </div>
      </div>
    `;
  } catch(e) { box.innerHTML = 'Error loading profile.'; }
}

function setupEditProfile() {
  switchPage('edit-profile');
  const p = state.profileData; if(!p) return;
  document.getElementById('edit-name').value = p.name || '';
  document.getElementById('edit-bio').value = p.bio || '';
  state.selectedEmoji = p.avatar;
  
  const grid = document.getElementById('emoji-grid');
  grid.innerHTML = CONFIG.emojis.map(e => `<div class="emoji-item ${e===state.selectedEmoji?'selected':''}" data-e="${e}">${e}</div>`).join('');
  grid.querySelectorAll('.emoji-item').forEach(el => el.onclick = () => {
    grid.querySelectorAll('.emoji-item').forEach(i => i.classList.remove('selected'));
    el.classList.add('selected'); state.selectedEmoji = el.dataset.e;
  });
}

async function saveProfile() {
  const name = document.getElementById('edit-name').value.trim();
  const bio = document.getElementById('edit-bio').value.trim();
  if(!name) return toast('Name required');
  
  const btn = document.getElementById('saveProfileBtn'); btn.disabled = true;
  try {
    await apiFetch(`/api/mini-app/profile/${state.userId}`, {
      method: 'PUT', body: JSON.stringify({ name, bio, avatar: state.selectedEmoji })
    });
    toast('Profile updated');
    switchPage('profile');
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; }
}

// SETTINGS
async function loadSettings() {
  try {
    const data = await apiFetch(`/api/mini-app/settings/${state.userId}`);
    document.getElementById('set-notifications').checked = data.data.notifications;
    document.getElementById('set-privacy').checked = data.data.privacy_public;
  } catch(e) {}
}

async function saveSettings() {
  const btn = document.getElementById('saveSettingsBtn'); btn.disabled = true;
  try {
    await apiFetch(`/api/mini-app/settings/${state.userId}`, {
      method: 'POST', body: JSON.stringify({
        notifications: document.getElementById('set-notifications').checked,
        privacy_public: document.getElementById('set-privacy').checked
      })
    });
    toast('Settings saved');
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; }
}

// INIT
function initParticles() {
  const canvas = document.getElementById('particleCanvas');
  const ctx = canvas.getContext('2d');
  let w = canvas.width = window.innerWidth;
  let h = canvas.height = window.innerHeight;
  const particles = [];
  
  for(let i=0; i<40; i++) {
    particles.push({ x: Math.random()*w, y: Math.random()*h, r: Math.random()*2+0.5, vx: (Math.random()-0.5)*0.3, vy: (Math.random()-0.5)*0.3 });
  }
  
  function draw() {
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
    particles.forEach(p => {
      p.x += p.vx; p.y += p.vy;
      if(p.x < 0) p.x = w; if(p.x > w) p.x = 0;
      if(p.y < 0) p.y = h; if(p.y > h) p.y = 0;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI*2); ctx.fill();
    });
    requestAnimationFrame(draw);
  }
  draw();
  window.addEventListener('resize', () => { w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight; });
}

async function init() {
  initParticles();
  renderCategories();
  
  document.getElementById('ventInput').addEventListener('input', function() {
    document.getElementById('charCount').textContent = this.value.length + '/5000';
  });
  
  let searchTimeout;
  document.getElementById('searchInput').addEventListener('input', function() {
    clearTimeout(searchTimeout);
    state.searchQuery = this.value.trim();
    searchTimeout = setTimeout(() => { state.feedPage = 1; loadFeed(); }, 500);
  });
  
  document.querySelectorAll('.nav-btn').forEach(b => b.onclick = () => switchPage(b.dataset.page));
  document.getElementById('submitVentBtn').onclick = submitVent;
  document.getElementById('loadMoreBtn').onclick = () => loadFeed(true);
  document.getElementById('postCommentBtn').onclick = postComment;
  document.getElementById('saveProfileBtn').onclick = saveProfile;
  document.getElementById('saveSettingsBtn').onclick = saveSettings;
  
  // Auth
  const tg = window.Telegram?.WebApp;
  if(tg) {
    try { tg.expand(); tg.ready(); } catch(e){}
    const user = tg.initDataUnsafe?.user;
    if(user?.id) { state.userId = String(user.id); }
  }
  if(!state.userId) {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if(token) {
      try {
        const res = await fetch(CONFIG.apiBase + '/api/verify-token/' + token);
        const data = await res.json();
        if(data.success) state.userId = String(data.user_id);
      } catch(e){}
    }
  }
  
  if(state.userId) {
    document.getElementById('authScreen').style.display = 'none';
    document.getElementById('mainApp').style.display = 'block';
    loadFeed();
  } else {
    document.getElementById('authScreen').innerHTML = `
      <div style="font-size:2rem; margin-bottom:10px;">🔒</div>
      <h2 style="color:var(--primary);">Access Required</h2>
      <p style="color:var(--text-dim); text-align:center; padding:20px;">Please open the app via Telegram bot.</p>
    `;
  }
}

document.addEventListener('DOMContentLoaded', init);
</script>
</body>
</html>""")
    
    html = html.replace('SLOT_PRIMARY', _primary).replace('SLOT_BORDER', _border).replace('SLOT_TEXT', _text).replace('SLOT_RGB', _rgb).replace('SLOT_BOT', _bot)
    return html


# ==================== MINI APP API ENDPOINTS ====================

# ==================== MINI APP API ENDPOINTS ====================

@flask_app.route('/api/mini-app/submit-vent', methods=['POST'])
def mini_app_submit_vent():
    """API endpoint for submitting vents from mini app - Supports Multiple Categories"""
    try:
        # Get data from request
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        user_id = data.get('user_id')
        content = data.get('content', '').strip()
        categories = data.get('categories', []) # Expected as array
        
        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
        
        if not content:
            return jsonify({'success': False, 'error': 'Content cannot be empty'}), 400
            
        if not categories:
            return jsonify({'success': False, 'error': 'At least one category is required'}), 400
        
        # Check if user exists
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Insert the post (no category column)
        post_row = db_execute(
            "INSERT INTO posts (content, author_id, media_type, approved) VALUES (%s, %s, 'text', FALSE) RETURNING post_id",
            (content, user_id),
            fetchone=True
        )
        
        if post_row:
            post_id = post_row['post_id']
            
            # Insert each category into junction table
            for cat_code in categories:
                db_execute(
                    "INSERT INTO post_categories (post_id, category_code) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (post_id, cat_code)
                )
            
            # Log it
            logger.info(f"📝 Mini App Multi-Cat Post submitted: ID {post_id} by {user_id}")
            
            # Notify admin immediately
            notify_admin_of_new_post_sync(post_id)
            
            return jsonify({
                'success': True,
                'message': '✅ Your vent has been submitted for admin approval!',
                'post_id': post_id
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to create post'}), 500
            
    except Exception as e:
        logger.error(f"Error in mini-app submit vent: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def notify_admin_of_new_post_sync(post_id):
    """Sync version of notify_admin_of_new_post"""
    try:
        if not ADMIN_ID:
            return
        
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        
        author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (post['author_id'],))
        author_name = get_display_name(author)
        
        post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
        
        logger.info(f"🆕 Mini App Post awaiting approval from {author_name}: {post_preview}")
        
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_ID,
            "text": f"🆕 New post awaiting approval from {author_name}:\n\n{post_preview}",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"approve_post_{post_id}"},
                        {"text": "❌ Reject", "callback_data": f"reject_post_{post_id}"}
                    ]
                ]
            }
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Error in sync admin notification: {e}")

def update_channel_post_comment_count_sync(post_id):
    """Sync version of update_channel_post_comment_count for the mini app"""
    try:
        post = db_fetch_one("SELECT channel_message_id FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post['channel_message_id']:
            return
            
        total_comments = count_all_comments(post_id)
        
        url = f"https://api.telegram.org/bot{TOKEN}/editMessageReplyMarkup"
        payload = {
            "chat_id": CHANNEL_ID,
            "message_id": post['channel_message_id'],
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": f"💬 Add/view Comments ({total_comments})", "url": f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}"}]
                ]
            }
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Error in sync channel comment update: {e}")

@flask_app.route('/api/mini-app/get-posts', methods=['GET'])
def mini_app_get_posts():
    """API endpoint for getting posts from mini app - With Pagination and Unread Counts"""
    try:
        user_id = request.args.get('user_id')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        offset = (page - 1) * per_page
        
        # Get approved posts
        posts = db_fetch_all('''
            SELECT 
                p.post_id,
                p.content,
                p.timestamp,
                p.comment_count,
                p.media_type,
                u.user_id as author_id,
                u.sex as author_sex,
                u.avatar_emoji as author_avatar,
                u.anonymous_name as author_name,
                STRING_AGG(DISTINCT pc.category_code, ',') as categories,
                COALESCE((
                    SELECT COUNT(*) 
                    FROM comments c2 
                    WHERE c2.post_id = p.post_id 
                    AND c2.timestamp > COALESCE((
                        SELECT last_viewed FROM post_views pv 
                        WHERE pv.user_id = %s AND pv.post_id = p.post_id
                    ), '1970-01-01')
                ), 0) as unread_comments
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = TRUE
            GROUP BY p.post_id, u.user_id, u.sex, u.avatar_emoji, u.anonymous_name
            ORDER BY p.timestamp DESC
            LIMIT %s OFFSET %s
        ''', (user_id, per_page, offset))
        
        formatted_posts = []
        for post in posts:
            if isinstance(post['timestamp'], str):
                post_time = datetime.strptime(post['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                post_time = post['timestamp']
            
            now = datetime.now()
            time_diff = now - post_time
            
            if time_diff.days > 0:
                time_ago = f"{time_diff.days}d ago"
            elif time_diff.seconds > 3600:
                time_ago = f"{time_diff.seconds // 3600}h ago"
            elif time_diff.seconds > 60:
                time_ago = f"{time_diff.seconds // 60}m ago"
            else:
                time_ago = "Just now"
            
            # Truncate content
            content_preview = post['content']
            if len(content_preview) > 300:
                content_preview = content_preview[:297] + '...'
            
            rating = calculate_user_rating(post['author_id'])
            aura_sticker = format_aura(rating)
            
            category_list = post['categories'].split(',') if post['categories'] else ['Other']
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'full_content': post['content'],
                'categories': category_list,
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'unread_comments': post['unread_comments'],
                'author': {
                    'name': 'Anonymous',
                    'sex': post['author_sex'] or '👤',
                    'avatar': post['author_avatar'] or "",
                    'aura': aura_sticker,
                    'is_me': str(post['author_id']) == str(user_id)
                },
                'has_media': post['media_type'] != 'text'
            })

        total_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = TRUE")
        
        return jsonify({
            'success': True,
            'data': formatted_posts,
            'page': page,
            'total_posts': total_posts['count'] if total_posts else 0,
            'has_more': len(posts) == per_page,
            'next_page': page + 1 if len(posts) == per_page else None
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app get posts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/post/<int:post_id>', methods=['GET'])
def mini_app_get_single_post(post_id):
    """API endpoint for fetching a single full vent natively in the Mini App"""
    try:
        post = db_fetch_one('''
            SELECT 
                p.post_id, p.content, p.timestamp, p.comment_count, p.media_type,
                u.user_id as author_id, u.sex as author_sex, u.avatar_emoji as author_avatar, u.anonymous_name as author_name,
                STRING_AGG(pc.category_code, ', ') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.post_id = %s AND p.approved = TRUE
            GROUP BY p.post_id, u.user_id, u.sex, u.avatar_emoji, u.anonymous_name
        ''', (post_id,))
        
        if not post:
            return jsonify({'success': False, 'error': 'Post not found or pending approval'}), 404
            
        # Format time
        if isinstance(post['timestamp'], str):
            post_time = datetime.strptime(post['timestamp'], '%Y-%m-%d %H:%M:%S')
        else:
            post_time = post['timestamp']
            
        now = datetime.now()
        time_diff = now - post_time
        
        if time_diff.days > 0:
            time_ago = f"{time_diff.days}d ago"
        elif time_diff.seconds > 3600:
            time_ago = f"{time_diff.seconds // 3600}h ago"
        elif time_diff.seconds > 60:
            time_ago = f"{time_diff.seconds // 60}m ago"
        else:
            time_ago = "Just now"
            
        rating = calculate_user_rating(post['author_id'])
        
        # Parse categories
        category_list = post['categories'].split(',') if post['categories'] else ['Other']
        
        formatted_post = {
            'id': post['post_id'],
            'content': post['content'],
            'categories': category_list,
            'time_ago': time_ago,
            'comments': post['comment_count'] or 0,
            'author': {
                'name': 'Anonymous',
                'sex': post['author_sex'] or '👤',
                'avatar': post['author_avatar'] or "",
                'aura': format_aura(rating)
            }
        }
        return jsonify({'success': True, 'data': formatted_post})

    except Exception as e:
        logger.error(f"Error compiling single post {post_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/post/<int:post_id>/comments', methods=['GET'])
def mini_app_get_post_comments(post_id):
    """API endpoint for fetching a post's comments with threading support"""
    try:
        comments = db_fetch_all('''
            SELECT 
                c.comment_id,
                c.parent_comment_id,
                c.content,
                c.timestamp as time_ago,
                u.user_id as author_id,
                u.sex as author_sex,
                u.avatar_emoji as author_avatar,
                u.anonymous_name as author_name
            FROM comments c
            JOIN users u ON c.author_id = u.user_id
            WHERE c.post_id = %s
            ORDER BY c.timestamp ASC
        ''', (post_id,))

        formatted_comments = []
        now = datetime.now()
        for c in comments:
            if isinstance(c['time_ago'], str):
                c_time = datetime.strptime(c['time_ago'], '%Y-%m-%d %H:%M:%S')
            else:
                c_time = c['time_ago']

            tdiff = now - c_time
            if tdiff.days > 0:
                calc_time = f"{tdiff.days}d ago"
            elif tdiff.seconds > 3600:
                calc_time = f"{tdiff.seconds // 3600}h ago"
            elif tdiff.seconds > 60:
                calc_time = f"{tdiff.seconds // 60}m ago"
            else:
                calc_time = "Just now"

            rating = calculate_user_rating(c['author_id'])

            formatted_comments.append({
                'id': c['comment_id'],
                'parent_id': c['parent_comment_id'] or 0,
                'content': c['content'],
                'time_ago': calc_time,
                'author': {
                    'name': c['author_name'] or 'Anonymous',
                    'sex': c['author_sex'] or '👤',
                    'avatar': c['author_avatar'] or "",
                    'aura': format_aura(rating)
                }
            })

        return jsonify({'success': True, 'data': formatted_comments})
    except Exception as e:
        logger.error(f"Error fetching comments for {post_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/post/<int:post_id>/comment', methods=['POST'])
def mini_app_submit_comment(post_id):
    """API endpoint for appending a comment natively, supports parent_comment_id for threading"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        content = data.get('content', '').strip()
        parent_comment_id = data.get('parent_comment_id', 0) or 0

        if not user_id:
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401
        if not content:
            return jsonify({'success': False, 'error': 'Empty response'}), 400

        db_execute(
            "INSERT INTO comments (post_id, author_id, content, parent_comment_id) VALUES (%s, %s, %s, %s)",
            (post_id, user_id, content, parent_comment_id)
        )
        db_execute(
            "UPDATE posts SET comment_count = COALESCE(comment_count, 0) + 1 WHERE post_id = %s",
            (post_id,)
        )

        # Update Channel Message Inline Keyboard immediately
        update_channel_post_comment_count_sync(post_id)

        return jsonify({'success': True, 'message': 'Reply posted successfully!'})
    except Exception as e:
        logger.error(f"Failed to post native comment: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/leaderboard', methods=['GET'])
def mini_app_leaderboard():
    """API endpoint for leaderboard data"""
    try:
        # Get top 10 users with weighted aura
        top_users = db_fetch_all('''
            SELECT 
                u.user_id,
                u.anonymous_name,
                u.sex,
                u.avatar_emoji,
                (
                    (SELECT COUNT(*) FROM posts p WHERE p.author_id = u.user_id AND p.approved = TRUE) * 10 +
                    (SELECT COUNT(*) FROM comments c WHERE c.author_id = u.user_id) * 2 +
                    COALESCE((
                        SELECT SUM(CASE WHEN r.type = 'like' THEN 1 WHEN r.type = 'dislike' THEN -2 ELSE 0 END)
                        FROM reactions r
                        JOIN comments c2 ON r.comment_id = c2.comment_id
                        WHERE c2.author_id = u.user_id
                    ), 0) -
                    (SELECT COUNT(*) FROM blocks b WHERE b.blocked_id = u.user_id) * 10
                ) as total
            FROM users u
            WHERE u.is_admin = FALSE
            ORDER BY total DESC
            LIMIT 10
        ''')

        
        # Format users
        formatted_users = []
        for idx, user in enumerate(top_users, start=1):
            formatted_users.append({
                'rank': idx,
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'avatar': user['avatar_emoji'] or "",
                'points': user['total'],
                'aura': format_aura(user['total'])
            })

        
        return jsonify({
            'success': True,
            'data': formatted_users
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app leaderboard: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/profile/<user_id>', methods=['GET'])
def mini_app_profile(user_id):
    """API endpoint for user profile"""
    try:
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        rating = calculate_user_rating(user_id)
        
        followers = db_fetch_one(
            "SELECT COUNT(*) as count FROM followers WHERE followed_id = %s",
            (user_id,)
        )
        
        posts = db_fetch_one(
            "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
            (user_id,)
        )
        
        comments = db_fetch_one(
            "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
            (user_id,)
        )
        
        rating = calculate_user_rating(user_id)
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'avatar': user['avatar_emoji'] or "",
                'rating': rating,
                'aura': format_aura(rating),

                'stats': {
                    'followers': followers['count'] if followers else 0,
                    'posts': posts['count'] if posts else 0,
                    'comments': comments['count'] if comments else 0
                }
            }
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app profile: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/admin/pending-posts', methods=['GET'])
def mini_app_admin_pending_posts():
    """API endpoint for admin to get pending posts"""
    try:
        # Check if admin (you'll need to implement proper authentication)
        # For now, we'll just return data
        
        posts = db_fetch_all('''
            SELECT 
                p.post_id,
                p.content,
                p.timestamp,
                p.media_type,
                u.anonymous_name as author_name,
                u.sex as author_sex,
                STRING_AGG(pc.category_code, ',') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = FALSE
            GROUP BY p.post_id, u.anonymous_name, u.sex, p.content, p.timestamp, p.media_type
            ORDER BY p.timestamp
        ''')
        
        return jsonify({
            'success': True,
            'data': posts
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app admin pending posts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/admin/approve-post', methods=['POST'])
def mini_app_admin_approve_post():
    """API endpoint for admin to approve posts"""
    try:
        data = request.get_json()
        post_id = data.get('post_id')
        
        if not post_id:
            return jsonify({'success': False, 'error': 'Post ID required'}), 400
        
        # Update the post to approved
        success = db_execute(
            "UPDATE posts SET approved = TRUE WHERE post_id = %s",
            (post_id,)
        )
        
        if success:
            return jsonify({'success': True, 'message': 'Post approved'})
        else:
            return jsonify({'success': False, 'error': 'Failed to approve post'}), 500
            
    except Exception as e:
        logger.error(f"Error in mini-app approve post: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/search', methods=['GET'])
def mini_app_search():
    """API endpoint for searching vents"""
    try:
        query = request.args.get('q', '').strip()
        category = request.args.get('category', '')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        offset = (page - 1) * per_page
        
        sql = '''
            SELECT p.post_id, p.content, p.timestamp, p.comment_count,
                   u.user_id as author_id, u.sex as author_sex, u.avatar_emoji as author_avatar, u.anonymous_name as author_name,
                   STRING_AGG(DISTINCT pc.category_code, ',') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = TRUE
        '''
        params = []
        
        if query:
            # Check if search_vector column exists (Postgres FTS)
            # Otherwise fallback to ILIKE
            sql += " AND (p.search_vector @@ plainto_tsquery('english', %s) OR p.content ILIKE %s)"
            params.extend([query, f"%{query}%"])
            
        if category:
            sql += " AND EXISTS (SELECT 1 FROM post_categories pc2 WHERE pc2.post_id = p.post_id AND pc2.category_code = %s)"
            params.append(category)
            
        sql += " GROUP BY p.post_id, u.user_id ORDER BY p.timestamp DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        posts = db_fetch_all(sql, tuple(params))
        
        formatted_posts = []
        for post in posts:
            rating = calculate_user_rating(post['author_id'])
            formatted_posts.append({
                'id': post['post_id'],
                'content': post['content'][:300] + '...' if len(post['content']) > 300 else post['content'],
                'categories': post['categories'].split(',') if post['categories'] else [],
                'comments': post['comment_count'] or 0,
                'author': {
                    'name': 'Anonymous',
                    'avatar': post['author_avatar'] or "",
                    'aura': format_aura(rating)
                }
            })
            
        return jsonify({'success': True, 'data': formatted_posts})
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/profile/<user_id>', methods=['PUT'])
def mini_app_update_profile(user_id):
    """API endpoint for updating user profile"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        bio = data.get('bio', '').strip()
        avatar = data.get('avatar', '').strip()
        
        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
            
        db_execute(
            "UPDATE users SET anonymous_name = %s, bio = %s, avatar_emoji = %s WHERE user_id = %s",
            (name, bio, avatar, user_id)
        )
        
        return jsonify({'success': True, 'message': 'Profile updated successfully'})
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/comment/<int:comment_id>', methods=['PUT'])
def mini_app_update_comment(comment_id):
    """API endpoint for editing a comment"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        content = data.get('content', '').strip()
        
        if not content:
            return jsonify({'success': False, 'error': 'Content required'}), 400
            
        comment = db_fetch_one("SELECT author_id FROM comments WHERE comment_id = %s", (comment_id,))
        if not comment or str(comment['author_id']) != str(user_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
            
        db_execute("UPDATE comments SET content = %s WHERE comment_id = %s", (content, comment_id))
        return jsonify({'success': True, 'message': 'Comment updated'})
    except Exception as e:
        logger.error(f"Comment update error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/comment/<int:comment_id>', methods=['DELETE'])
def mini_app_delete_comment(comment_id):
    """API endpoint for deleting a comment"""
    try:
        user_id = request.args.get('user_id')
        comment = db_fetch_one("SELECT author_id, post_id FROM comments WHERE comment_id = %s", (comment_id,))
        
        if not comment or str(comment['author_id']) != str(user_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
            
        post_id = comment['post_id']
        
        # Cascade re-parent child comments
        db_execute("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (comment_id,))
        # Delete reactions and comment
        db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment_id,))
        db_execute("DELETE FROM comments WHERE comment_id = %s", (comment_id,))
        
        # Update post comment count
        db_execute("UPDATE posts SET comment_count = (SELECT COUNT(*) FROM comments WHERE post_id = %s) WHERE post_id = %s", (post_id, post_id))
        update_channel_post_comment_count_sync(post_id)
        
        return jsonify({'success': True, 'message': 'Comment deleted'})
    except Exception as e:
        logger.error(f"Comment delete error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/post/<int:post_id>/view', methods=['POST'])
def mini_app_mark_post_viewed(post_id):
    """API endpoint to mark a post as viewed by a user"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
            
        db_execute(
            """INSERT INTO post_views (user_id, post_id, last_viewed) 
               VALUES (%s, %s, CURRENT_TIMESTAMP) 
               ON CONFLICT (user_id, post_id) 
               DO UPDATE SET last_viewed = CURRENT_TIMESTAMP""",
            (user_id, post_id)
        )
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error marking post as viewed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
@flask_app.route('/api/mini-app/settings/<user_id>', methods=['GET'])
def mini_app_get_settings(user_id):
    """API endpoint for fetching user settings"""
    try:
        user = db_fetch_one("SELECT notifications_enabled, privacy_public FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
            
        return jsonify({
            'success': True,
            'data': {
                'notifications': user['notifications_enabled'],
                'privacy_public': user['privacy_public']
            }
        })
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/settings/<user_id>', methods=['POST'])
def mini_app_update_settings(user_id):
    """API endpoint for updating user settings"""
    try:
        data = request.get_json()
        notifications = data.get('notifications')
        privacy_public = data.get('privacy_public')
        
        updates = []
        params = []
        
        if notifications is not None:
            updates.append("notifications_enabled = %s")
            params.append(notifications)
            
        if privacy_public is not None:
            updates.append("privacy_public = %s")
            params.append(privacy_public)
            
        if not updates:
            return jsonify({'success': False, 'error': 'No settings to update'}), 400
            
        params.append(user_id)
        db_execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s", tuple(params))
        
        return jsonify({'success': True, 'message': 'Settings updated'})
    except Exception as e:
        logger.error(f"Error updating settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == "__main__": 
    # The main() function already handles initializing the DB, 
    # starting the Flask server, and running the bot polling.
    main()
