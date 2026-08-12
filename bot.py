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
import html
from types import SimpleNamespace
from functools import lru_cache

# FIX: moved logger setup to top
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# How long a "reporting" state (waiting for the user to type a report reason)
# stays valid before it's treated as stale and cleared automatically.
REPORTING_TIMEOUT_SECONDS = 300  # 5 minutes

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
                    thread_from_post_id BIGINT DEFAULT NULL,
                    deleted BOOLEAN DEFAULT FALSE
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

                # Check for privacy columns in users
                privacy_columns = [
                    ('hide_aura', 'BOOLEAN DEFAULT FALSE'),
                    ('hide_bio', 'BOOLEAN DEFAULT FALSE'),
                    ('hide_follower_count', 'BOOLEAN DEFAULT FALSE'),
                    ('hide_role', 'BOOLEAN DEFAULT FALSE')
                ]
                for col_name, col_type in privacy_columns:
                    c.execute(f"""
                        SELECT column_name FROM information_schema.columns 
                        WHERE table_name='users' AND column_name='{col_name}'
                    """)
                    if not c.fetchone():
                        logger.info(f"Adding missing column: {col_name} to users table")
                        c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")

                # Add timestamp to reactions
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='reactions' AND column_name='timestamp'
                """)
                if not c.fetchone():
                    c.execute("ALTER TABLE reactions ADD COLUMN timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    logger.info("Added timestamp column to reactions table")

                # Add post_id to reactions table and update indexes
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='reactions' AND column_name='post_id'
                """)
                if not c.fetchone():
                    c.execute("ALTER TABLE reactions ALTER COLUMN comment_id DROP NOT NULL")
                    c.execute("ALTER TABLE reactions ADD COLUMN post_id INTEGER REFERENCES posts(post_id) DEFAULT NULL")
                    logger.info("Added post_id column to reactions table")
                    
                    c.execute("ALTER TABLE reactions DROP CONSTRAINT IF EXISTS reactions_comment_id_user_id_key")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reactions_post_user ON reactions (post_id, user_id) WHERE post_id IS NOT NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reactions_comment_user ON reactions (comment_id, user_id) WHERE comment_id IS NOT NULL")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_reactions_lookup ON reactions (post_id, comment_id, type)")
                    
                    c.execute("CREATE INDEX IF NOT EXISTS idx_pm_lookup ON private_messages (sender_id, receiver_id, timestamp DESC)")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_pm_unread ON private_messages (receiver_id, is_read)")

                # Private messages media columns
                c.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='private_messages' AND column_name='media_type'
                """)
                if not c.fetchone():
                    logger.info("Adding missing media columns to private_messages table")
                    c.execute("ALTER TABLE private_messages ADD COLUMN media_type TEXT DEFAULT 'text'")
                    c.execute("ALTER TABLE private_messages ADD COLUMN media_id TEXT")

                # Add timestamp to blocks
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='blocks' AND column_name='timestamp'
                """)
                if not c.fetchone():
                    c.execute("ALTER TABLE blocks ADD COLUMN timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    logger.info("Added timestamp column to blocks table")

                # Add weekly_badge to users
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='weekly_badge'
                """)
                if not c.fetchone():
                    c.execute("ALTER TABLE users ADD COLUMN weekly_badge TEXT DEFAULT NULL")
                    logger.info("Added weekly_badge column to users table")


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

                # Check for 'thread_context_post_id' column in users
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='users' AND column_name='thread_context_post_id'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: thread_context_post_id to users table")
                    c.execute("ALTER TABLE users ADD COLUMN thread_context_post_id BIGINT DEFAULT NULL")

                # FIX: Added telegram_message_id to comments for cross-page threading
                c.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name='comments' AND column_name='telegram_message_id'
                """)
                if not c.fetchone():
                    logger.info("Adding telegram_message_id column to comments table")
                    c.execute("ALTER TABLE comments ADD COLUMN telegram_message_id BIGINT DEFAULT NULL")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_telegram_message_id ON comments(telegram_message_id)")

                # Check for 'deleted' column in posts
                c.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='posts' AND column_name='deleted'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: deleted to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN deleted BOOLEAN DEFAULT FALSE")

                # Check for 'explicit' column in posts
                c.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='posts' AND column_name='explicit'
                """)
                if not c.fetchone():
                    logger.info("Adding missing column: explicit to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN explicit BOOLEAN DEFAULT FALSE")

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
        await update.message.reply_text("You don't have permission to use this command.")
        return
    
    await update.message.reply_text("Reassigning vent numbers to all approved posts...")
    
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
        
        await update.message.reply_text(f"Successfully assigned vent numbers to {count} posts.")
        
    except Exception as e:
        logger.error(f"Error in fix_vent_numbers: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

async def fix_missing_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix missing sex emoji for users with avatars"""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user.get('is_admin'):
        await update.message.reply_text("Admin only.")
        return

    # Fix users where sex is NULL or empty but avatar_emoji exists
    rows_fixed = db_execute("""
        UPDATE users 
        SET sex = '👤' 
        WHERE (sex IS NULL OR sex = '') 
        AND avatar_emoji IS NOT NULL
    """)
    
    await update.message.reply_text(f"Fixed missing sex for {rows_fixed} users.")


async def reset_weekly_badges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger weekly badge awarding."""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    
    if not user or not user['is_admin']:
        await update.message.reply_text("You don't have permission to use this command.")
        return
    
    await update.message.reply_text("Recalculating weekly contributors and announcing...")
    await award_weekly_badges(context)
    await update.message.reply_text("Weekly contributors have been announced.")
def is_media_message(message):
    """Check if a message contains media"""
    return (message.photo or message.voice or message.video or 
            message.document or message.audio or message.sticker or 
            message.animation)
async def show_loading(update_or_message, loading_text="Processing...", edit_message=True):
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
        success_msg = await loading_msg.edit_text(f"{success_text}")
        await asyncio.sleep(1)
        return success_msg
    except:
        return loading_msg

async def replace_with_error(loading_msg, error_text):
    """Replace loading message with error message"""
    try:
        await loading_msg.edit_text(f"{error_text}")
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
    logging.info("Database connection pool created successfully")
except Exception as e:
    logging.error(f"Failed to create database pool: {e}")
    db_pool = None


def db_execute(query, params=(), fetch=False, fetchone=False):
    """Execute a SQL query using the global connection pool. Raises on error."""
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
        raise   # <-- IMPORTANT: re-raise so caller knows it failed
    finally:
        if conn:
            db_pool.putconn(conn)


def db_fetch_one(query, params=()):
    return db_execute(query, params, fetchone=True)

def db_fetch_all(query, params=()):
    return db_execute(query, params, fetch=True)
def get_admin_conversations(limit=20, offset=0, search=None):
    """List distinct conversation pairs, most recently active first."""
    where_extra = ""
    params = []
    if search:
        where_extra = "WHERE ua.anonymous_name ILIKE %s OR ub.anonymous_name ILIKE %s OR p.user_a = %s OR p.user_b = %s"
        like = f"%{search}%"
        params = [like, like, search, search]

    query = f"""
        WITH pairs AS (
            SELECT LEAST(sender_id, receiver_id) AS user_a,
                   GREATEST(sender_id, receiver_id) AS user_b,
                   MAX(timestamp) AS last_ts,
                   COUNT(*) AS msg_count
            FROM private_messages
            GROUP BY LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id)
        )
        SELECT p.user_a, p.user_b, p.last_ts, p.msg_count,
               ua.anonymous_name AS name_a, ua.sex AS sex_a, ua.avatar_emoji AS avatar_a,
               ub.anonymous_name AS name_b, ub.sex AS sex_b, ub.avatar_emoji AS avatar_b,
               lm.content AS last_content, lm.sender_id AS last_sender_id, lm.media_type AS last_media_type
        FROM pairs p
        JOIN users ua ON ua.user_id = p.user_a
        JOIN users ub ON ub.user_id = p.user_b
        JOIN LATERAL (
            SELECT content, sender_id, media_type
            FROM private_messages m
            WHERE (m.sender_id = p.user_a AND m.receiver_id = p.user_b)
               OR (m.sender_id = p.user_b AND m.receiver_id = p.user_a)
            ORDER BY m.timestamp DESC LIMIT 1
        ) lm ON true
        {where_extra}
        ORDER BY p.last_ts DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    return db_fetch_all(query, tuple(params))


def get_admin_conversations_count(search=None):
    where_extra = ""
    params = []
    if search:
        where_extra = "WHERE ua.anonymous_name ILIKE %s OR ub.anonymous_name ILIKE %s OR p.user_a = %s OR p.user_b = %s"
        like = f"%{search}%"
        params = [like, like, search, search]

    query = f"""
        WITH pairs AS (
            SELECT LEAST(sender_id, receiver_id) AS user_a, GREATEST(sender_id, receiver_id) AS user_b
            FROM private_messages
            GROUP BY LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id)
        )
        SELECT COUNT(*) as cnt
        FROM pairs p
        JOIN users ua ON ua.user_id = p.user_a
        JOIN users ub ON ub.user_id = p.user_b
        {where_extra}
    """
    row = db_fetch_one(query, tuple(params))
    return row['cnt'] if row else 0


def get_admin_conversation_transcript(user_a, user_b, limit=50):
    """Most recent `limit` messages between two users, returned oldest-first."""
    return db_fetch_all("""
        SELECT * FROM (
            SELECT pm.*, u.anonymous_name as sender_name
            FROM private_messages pm
            JOIN users u ON pm.sender_id = u.user_id
            WHERE (pm.sender_id = %s AND pm.receiver_id = %s)
               OR (pm.sender_id = %s AND pm.receiver_id = %s)
            ORDER BY pm.timestamp DESC
            LIMIT %s
        ) sub
        ORDER BY timestamp ASC
    """, (user_a, user_b, user_b, user_a, limit))

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
            private_message_target = NULL,
            thread_context_post_id = NULL
        WHERE user_id = %s
    ''', (user_id,))
    
    # Reset context flags
    if context:
        context_keys = ['editing_comment', 'editing_post', 'thread_from_post_id', 
                       'pending_post', 'pending_explicit_check', 'broadcasting', 'broadcast_step', 'broadcast_type',
                       'rejecting_post', 'awaiting_rejection_reason', 'reporting',
                       'editing_categories_for_pending', 'selected_categories', 'pending_comment_edit']
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
            await update.message.reply_text("You don't have permission to use this command.")
        return
        
    status_msg = await update.message.reply_text("Scanning all posts and fixing comment counts...")
    
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
            f"*Comment Recount Complete*\n\n"
            f"• Posts Scanned: {posts_scanned}\n"
            f"• Posts Updated: {posts_fixed}\n"
            f"• Orphans Adopted: {orphans_adopted}"
        )
        await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error in recount_comments: {e}")
        await status_msg.edit_text(f"Error during recount: {str(e)}")
# Categories
CATEGORIES = [
    ("Pray For Me", "PrayForMe"),
    ("Bible", "Bible"),
    ("Work and Life", "WorkLife"),
    ("Spiritual Life", "SpiritualLife"),
    ("Christian Challenges", "ChristianChallenges"),
    ("Relationship", "Relationship"),
    ("Marriage", "Marriage"),
    ("Youth", "Youth"),
    ("Finance", "Finance"),
    ("Other", "Other"),
    ("Worship & Music", "WorshipMusic"),
    ("Family Issues", "Family"),
    ("Testimony", "Testimony"),
    ("Addiction & Recovery", "AddictionRecovery"),
    ("Bible Question", "BibleQuestion"),
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
            button_text = f"⬜ {display}"
            
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
            <img src="/static/images/vent logo.png" class="logo" alt="Christian Vent Logo">
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
                [KeyboardButton("Share"), KeyboardButton("Chat Requests")],
                [KeyboardButton("Profile"), KeyboardButton("Posts")],
                [KeyboardButton("Top"), KeyboardButton("Settings")],
                [KeyboardButton("Open App", web_app=WebAppInfo(url=mini_app_url))]
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
                [KeyboardButton("Share"), KeyboardButton("Chat Requests")],
                [KeyboardButton("Profile"), KeyboardButton("Posts")],
                [KeyboardButton("Top"), KeyboardButton("Settings")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
            input_field_placeholder="Choose option"
        )

# Fallback for static contexts if needed (can be removed later)
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Share"), KeyboardButton("Chat Requests")],
        [KeyboardButton("Profile"), KeyboardButton("Posts")],
        [KeyboardButton("Top"), KeyboardButton("Settings")]
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
    
    # 3. Reactions Points (Dynamic Weights on both Comments and Posts)
    comment_rx = db_fetch_all("""
        SELECT r.type, COUNT(*) as count
        FROM reactions r
        JOIN comments c ON r.comment_id = c.comment_id
        WHERE c.author_id = %s AND r.comment_id IS NOT NULL
        GROUP BY r.type
    """, (user_id,))
    
    post_rx = db_fetch_all("""
        SELECT r.type, COUNT(*) as count
        FROM reactions r
        JOIN posts p ON r.post_id = p.post_id
        WHERE p.author_id = %s AND r.post_id IS NOT NULL
        GROUP BY r.type
    """, (user_id,))
    
    weights = {
        'like': 1,
        'dislike': -2,
        '': 2,
        '': 2,
        '': 2,
        '': 1,
        '': -2,
        '': -2
    }
    
    rx_points = 0
    for row in (comment_rx or []) + (post_rx or []):
        r_type = row['type']
        r_count = row['count']
        rx_points += r_count * weights.get(r_type, 1)
    
    # 4. Block Points (-10 per block received)
    block_res = db_fetch_one("SELECT COUNT(*) as count FROM blocks WHERE blocked_id = %s", (user_id,))
    block_points = (block_res['count'] if block_res else 0) * -10
    
    # 5. Follower Bonus (+2 per follower)
    follower_res = db_fetch_one("SELECT COUNT(*) as cnt FROM followers WHERE followed_id = %s", (user_id,))
    follower_points = (follower_res['cnt'] if follower_res else 0) * 2

    return post_points + comm_points + rx_points + block_points + follower_points

def calculate_top_weekly_contributors():
    """Calculate top 3 users by aura points earned in the last 7 days."""
    query = """
        SELECT 
            u.user_id,
            COALESCE(p.post_points, 0) + 
            COALESCE(c.comment_points, 0) + 
            COALESCE(r.reaction_points, 0) - 
            COALESCE(b.block_points, 0) AS weekly_points
        FROM users u
        LEFT JOIN (
            SELECT author_id, COUNT(*) * 10 AS post_points
            FROM posts
            WHERE approved = TRUE AND timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY author_id
        ) p ON u.user_id = p.author_id
        LEFT JOIN (
            SELECT author_id, COUNT(*) * 2 AS comment_points
            FROM comments
            WHERE timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY author_id
        ) c ON u.user_id = c.author_id
        LEFT JOIN (
            SELECT 
                c.author_id,
                SUM(CASE WHEN r.type = 'like' THEN 1 ELSE 0 END) - 
                SUM(CASE WHEN r.type = 'dislike' THEN 2 ELSE 0 END) AS reaction_points
            FROM reactions r
            JOIN comments c ON r.comment_id = c.comment_id
            WHERE r.timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY c.author_id
        ) r ON u.user_id = r.author_id
        LEFT JOIN (
            SELECT blocked_id, COUNT(*) * 10 AS block_points
            FROM blocks
            WHERE timestamp >= NOW() - INTERVAL '7 days'
            GROUP BY blocked_id
        ) b ON u.user_id = b.blocked_id
        WHERE u.is_admin = FALSE
          AND (COALESCE(p.post_points,0) + COALESCE(c.comment_points,0) + COALESCE(r.reaction_points,0) - COALESCE(b.block_points,0)) > 0
        ORDER BY weekly_points DESC
        LIMIT 3
    """
    return db_fetch_all(query)



async def award_weekly_badges(context: ContextTypes.DEFAULT_TYPE):
    """
    Weekly job to announce top contributors.
    Returns a summary dict if called manually by admin.
    """
    summary = {
        'success': False,
        'winners_count': 0,
        'dms_sent': 0,
        'announcement_sent': False,
        'error': None
    }
    
    try:
        logger.info("Starting weekly contributor announcement job...")
        
        # Clear previous badges
        db_execute("UPDATE users SET weekly_badge = NULL")
        
        top_users = calculate_top_weekly_contributors()
        if not top_users:
            logger.info("No users earned points this week.")
            summary['success'] = True
            return summary

        badges = ["", "", ""]
        winners_info = []
        today = datetime.now(timezone.utc).date()
        
        for idx, user_data in enumerate(top_users):
            user_id = user_data['user_id']
            points = user_data['weekly_points']
            rank = idx + 1
            badge_emoji = badges[rank-1]
            
            # Store in history
            db_execute("""
                INSERT INTO weekly_rankings (user_id, week_start, rank, points_earned, badge_emoji)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, week_start) DO UPDATE 
                SET rank = EXCLUDED.rank, points_earned = EXCLUDED.points_earned, badge_emoji = EXCLUDED.badge_emoji
            """, (user_id, today, rank, points, badge_emoji))
            
            # Update current badge in users table
            db_execute("UPDATE users SET weekly_badge = %s WHERE user_id = %s", (badge_emoji, user_id))
            
            # Get user info for announcement
            user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_id,))
            name = user['anonymous_name'] if user else "Contributor"
            winners_info.append(f"{badge_emoji} {name} – {points} pts")
            
            summary['winners_count'] += 1
            
            # DM winner
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"*Weekly Highlight!*\n\nYou are one of the *Top Contributors* this week with *{points} points*!\n\nThank you for your valuable contributions and for being a light in the community!",
                    parse_mode=ParseMode.MARKDOWN
                )
                summary['dms_sent'] += 1
            except Exception as dm_e:
                logger.warning(f"Could not send DM to weekly winner {user_id}: {dm_e}")

        # Announce in channel
        if CHANNEL_ID and winners_info:
            announcement = "*Weekly Top Contributors*\n\n" + "\n".join(winners_info) + \
                          "\n\nCongratulations! Thank you for being such a blessing to this community."
            try:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=announcement,
                    parse_mode=ParseMode.MARKDOWN
                )
                summary['announcement_sent'] = True
            except Exception as ch_e:
                logger.error(f"Failed to announce weekly winners in channel: {ch_e}")
        
        summary['success'] = True
        return summary
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"CRITICAL ERROR in award_weekly_badges:\n{error_trace}")
        summary['error'] = str(e)
        return summary



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
        return "⚪"  # White aura for new/neutral users (0-9 points)


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
        if user_data['sex'] in ('👨', '👩'):
            return user_data['sex']
    return ""

def format_time_ago(timestamp):
    """Human-friendly relative time string, e.g. '5m ago', 'yesterday'."""
    if not timestamp:
        return ""
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return ""

    now = datetime.now()
    time_diff = now - timestamp
    if time_diff.days == 0:
        if time_diff.seconds < 60:
            return "just now"
        elif time_diff.seconds < 3600:
            return f"{time_diff.seconds // 60}m ago"
        else:
            return f"{time_diff.seconds // 3600}h ago"
    elif time_diff.days == 1:
        return "yesterday"
    elif time_diff.days < 7:
        return timestamp.strftime('%A')
    elif time_diff.days < 30:
        return f"{time_diff.days // 7}w ago"
    else:
        return timestamp.strftime('%b %d')

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

def build_channel_post_keyboard(post_id: int, comment_count: int, explicit: bool = False):
    """Inline keyboard attached to a post in the channel.

    Explicit posts get an extra "View Post" button since their content is
    hidden in the channel message itself — otherwise there'd be no direct
    way to see the post without first tapping into the comments flow.
    """
    comments_button = InlineKeyboardButton(
        f"Add/View Comments ({comment_count})",
        url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}"
    )
    if explicit:
        view_button = InlineKeyboardButton(
            "View Post",
            url=f"https://t.me/{BOT_USERNAME}?start=viewpost_{post_id}"
        )
        return InlineKeyboardMarkup([[view_button], [comments_button]])
    return InlineKeyboardMarkup([[comments_button]])

async def update_channel_post_comment_count(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Update the comment count on the channel post"""
    try:
        # Get the post details
        post = db_fetch_one("SELECT channel_message_id, comment_count, explicit FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post['channel_message_id']:
            return
        
        # Count all comments for this post
        total_comments = count_all_comments(post_id)
        
        # Update the database with the new count
        db_execute("UPDATE posts SET comment_count = %s WHERE post_id = %s", (total_comments, post_id))
        
        # Update the channel message button
        keyboard = build_channel_post_keyboard(post_id, total_comments, post.get('explicit', False))
        
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
            loading_msg = await update.message.reply_text("Gathering statistics...")
        elif update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("Gathering statistics...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Loading leaderboard", 3)
    
    # Get top 10 users with weighted aura
    top_users = db_fetch_all('''
        SELECT u.user_id, u.anonymous_name, u.sex, u.avatar_emoji, u.weekly_badge,
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
    leaderboard_text = "*Christian Vent Leaderboard*\n\n"
    
    # Define medal emojis for top 3
    medal_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    
    # Format each user
    for idx, user in enumerate(top_users, start=1):
        display_name = user['anonymous_name']
        if user.get('weekly_badge'):
            display_name = f"{user['weekly_badge']} {display_name}"
            
        safe_name = escape_markdown(display_name, version=2)
        sex_val = user['sex'] if user['sex'] in ('👨', '👩') else ""
        safe_sex = escape_markdown(sex_val, version=2)
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
            f"{safe_rank}{' ' + safe_sex if safe_sex else ''} "
            f"[{safe_name}]({profile_link})\n"
            f"   {safe_total} pts {safe_aura}\n\n"
        )


    
    # Add current user's rank
    user_id = str(update.effective_user.id)
    user_rank = get_user_rank(user_id)
    
    if user_rank:
        user_data = db_fetch_one("SELECT anonymous_name, sex, is_admin FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = calculate_user_rating(user_id)
            safe_user_name = escape_markdown(user_data['anonymous_name'], version=2)
            user_sex_val = user_data['sex'] if user_data['sex'] in ('👨', '👩') else ""
            safe_user_sex = escape_markdown(user_sex_val, version=2)
            user_aura_val = "" if user_data.get('is_admin') else format_aura(user_contributions)
            safe_user_aura = escape_markdown(user_aura_val, version=2)
            safe_user_pts = escape_markdown(str(user_contributions), version=2)
            safe_user_rank = escape_markdown(str(user_rank), version=2)
            
            leaderboard_text += f"*Your position:* {safe_user_rank}\n"
            leaderboard_text += f"{safe_user_sex}{' ' if safe_user_sex else ''}{safe_user_name} • {safe_user_pts} pts {safe_user_aura}\n\n"
    
    # Add subtle footer
    leaderboard_text += "_Click names to view profiles • Updated daily_"

    
    # Create clean buttons
    keyboard = [
        [InlineKeyboardButton("Menu", callback_data='menu')],
        [InlineKeyboardButton("My Profile", callback_data='profile')]
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
                await loading_msg.edit_text("Error loading leaderboard. Please try again.")
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
        
        notifications_status = "ON" if user['notifications_enabled'] else "OFF"
        privacy_status = "Public" if user['privacy_public'] else "Private"

        pending_requests_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM chat_requests WHERE receiver_id = %s AND status = 'pending'",
            (user_id,)
        )
        pending_requests = pending_requests_row['cnt'] if pending_requests_row else 0
        requests_label = f"📨 Chat Requests ({pending_requests})" if pending_requests else "📨 Chat Requests"
        
        keyboard = [
            [
                InlineKeyboardButton(f"Notifications: {notifications_status}", 
                                   callback_data='toggle_notifications')
            ],
            [
                InlineKeyboardButton(f"Privacy: {privacy_status}", 
                                   callback_data='toggle_privacy')
            ],
            [
                InlineKeyboardButton("Privacy Controls", callback_data='privacy_settings')
            ],
            [
                InlineKeyboardButton(requests_label, callback_data='chat_requests')
            ],
            [
                InlineKeyboardButton("Blocked Users", callback_data='list_blocked')
            ],
            [
                InlineKeyboardButton("Main Menu", callback_data='menu'),
                InlineKeyboardButton("Profile", callback_data='profile')
            ]
        ]
        
        # Add admin panel button if user is admin
        if user['is_admin']:
            keyboard.insert(0, [InlineKeyboardButton("Admin Panel", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    "*Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.callback_query.message.reply_text(
                    "*Settings Menu*",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text(
                "*Settings Menu*",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Error in show_settings: {e}")
        if update.message:
            await update.message.reply_text("Error loading settings. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("Error loading settings. Please try again.")

async def show_privacy_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the privacy toggle menu"""
    user_id = str(update.effective_user.id)
    query = update.callback_query
    
    user = db_fetch_one("""
        SELECT hide_aura, hide_bio, hide_follower_count, hide_role 
        FROM users WHERE user_id = %s
    """, (user_id,))
    
    if not user:
        await query.answer("User not found.", show_alert=True)
        return

    # Helper for status text
    def s(val): return "HIDDEN" if val else "VISIBLE"
    
    keyboard = [
        [InlineKeyboardButton(f"Aura & Points: {s(user['hide_aura'])}", callback_data='toggle_hide_aura')],
        [InlineKeyboardButton(f"Bio: {s(user['hide_bio'])}", callback_data='toggle_hide_bio')],
        [InlineKeyboardButton(f"Follower Count: {s(user['hide_follower_count'])}", callback_data='toggle_hide_follower_count')],
        [InlineKeyboardButton(f"Role: {s(user['hide_role'])}", callback_data='toggle_hide_role')],
        [InlineKeyboardButton("Back to Settings", callback_data='settings')]
    ]
    
    text = (
        "*Privacy Controls*\n\n"
        "Toggle which metrics are visible to other users when they view your profile\\.\n"
        "Note: You and administrators will always see your full profile\\."
    )
    
    try:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error in show_privacy_settings: {e}")

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None, thread_from_post_id: int = None, explicit: bool = False):
    keyboard = [
        [
            InlineKeyboardButton("Edit Text", callback_data='edit_post'),
            InlineKeyboardButton("Edit Categories", callback_data='edit_categories')
        ]
    ]

    if thread_from_post_id:
        keyboard.append([
            InlineKeyboardButton("Change Thread", callback_data='select_thread_post'),
            InlineKeyboardButton("Remove Thread", callback_data='clear_thread_post')
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("Thread to Previous Post", callback_data='select_thread_post')
        ])

    keyboard.append([
        InlineKeyboardButton("❌ Cancel", callback_data='cancel_post'),
        InlineKeyboardButton("✅ Submit", callback_data='confirm_post')
    ])
    
    thread_text = ""
    if thread_from_post_id:
        thread_post = db_fetch_one("SELECT content, channel_message_id FROM posts WHERE post_id = %s", (thread_from_post_id,))
        if thread_post:
            thread_preview = thread_post['content'][:100] + '...' if len(thread_post['content']) > 100 else thread_post['content']
            if thread_post['channel_message_id']:
                thread_text = f"*Thread continuation from your previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
            else:
                thread_text = f"*Threading from previous post:*\n{escape_markdown(thread_preview, version=2)}\n\n"
    
    # Format categories for preview
    category_list = category.split(',') if category else []
    cat_display = ", ".join(category_list)
    
    explicit_tag = "*Marked as explicit content*\n\n" if explicit else ""
    
    preview_text = (
        f"{thread_text}{explicit_tag}*Post Preview* [{escape_markdown(cat_display, 2)}]\n\n"
        f"{escape_markdown(post_content, version=2)}\n\n"
        f"Please confirm your post\\:"
    )

    
    context.user_data['pending_post'] = {
        'content': post_content,
        'category': category, # Keep as comma-separated string
        'media_type': media_type,
        'media_id': media_id,
        'thread_from_post_id': thread_from_post_id,
        'explicit': explicit,
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
                elif media_type == 'audio':
                    await update.message.reply_audio(
                        audio=media_id,
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
                    f"*Post Preview* [{cat_display}]\n\n"
                    f"{escape_markdown(post_content, version=2)}\n\n"
                    f"Please confirm your post:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e2:
                logger.error(f"Fallback also failed: {e2}")
                
        elif update.message:
            await update.message.reply_text("Error showing confirmation. Please try again.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("Error showing confirmation. Please try again.")
async def _send_notification_with_media(context: ContextTypes.DEFAULT_TYPE, chat_id, text, parse_mode, reply_markup=None, media_type=None, media_id=None):
    """Send a notification. If real media (photo/voice/gif) is available, attach it with the
    text as caption so the person can see/hear the actual reply, not just a label. Falls back
    to a plain text message on any failure so a notification is never silently dropped."""
    try:
        if media_id and media_type and media_type != 'text':
            caption = text[:1024]  # Telegram's caption hard limit
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=chat_id, photo=media_id, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                return
            if media_type == 'voice':
                await context.bot.send_voice(chat_id=chat_id, voice=media_id, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                return
            if media_type == 'gif':
                await context.bot.send_animation(chat_id=chat_id, animation=media_id, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup)
                return
            if media_type == 'sticker':
                # Stickers don't support captions — send the sticker, then the text+button as a follow-up
                await context.bot.send_sticker(chat_id=chat_id, sticker=media_id)
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
                return
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"_send_notification_with_media failed, falling back to text: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as e2:
            logger.error(f"_send_notification_with_media text fallback also failed: {e2}")

async def notify_vent_author_of_comment(context: ContextTypes.DEFAULT_TYPE, post_id: int, commenter_id: str, comment_id: int = None, comment_content: str = None, media_type: str = 'text', media_id: str = None):
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

        media_label = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🩹 Sticker', 'photo': '🖼 Photo'}.get(media_type)
        if comment_content:
            safe_comment_text = html.escape(comment_content[:500])
        else:
            safe_comment_text = media_label or ""

        lines = [
            "<b>New comment on your vent!</b>",
            "",
            f"<b>{safe_commenter_name}</b> wrote:",
        ]
        if safe_comment_text:
            lines.append(f"“{safe_comment_text}”")
        lines += [
            "",
            f"<b>Your vent:</b> {safe_post_preview}",
            "",
            f"<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>",
        ]
        notification_text = "\n".join(lines)

        reply_markup = None
        if comment_id:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Reply", callback_data=f"reply_{post_id}_{comment_id}")]
            ])
        
        await _send_notification_with_media(
            context, author_id, notification_text, ParseMode.HTML,
            reply_markup=reply_markup, media_type=media_type, media_id=media_id
        )
    except Exception as e:
        logger.error(f"Error notifying vent author: {e}")
async def notify_user_of_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, comment_id: int, replier_id: str, new_comment_id: int = None, comment_content: str = None, media_type: str = 'text', media_id: str = None):
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
        safe_parent_preview = escape_markdown((comment['content'] or '[media]')[:100], version=2)

        media_label = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🩹 Sticker', 'photo': '🖼 Photo'}.get(media_type)
        if comment_content:
            safe_reply_text = escape_markdown(comment_content[:500], version=2)
        else:
            safe_reply_text = escape_markdown(media_label, version=2) if media_label else ""

        lines = [
            f"{safe_replier_name} replied to your comment\\:",
            f"_{safe_parent_preview}_",
            "",
        ]
        if safe_reply_text:
            lines += [f"*Their reply:*", safe_reply_text, ""]
        lines += [
            f"Post\\: {safe_post_preview}",
            "",
            f"[View conversation](https://t.me/{BOT_USERNAME}?start=comments_{post_id})",
        ]
        notification_text = "\n".join(lines)

        reply_markup = None
        if new_comment_id:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Reply", callback_data=f"replytoreply_{post_id}_{comment_id}_{new_comment_id}")]
            ])
        
        await _send_notification_with_media(
            context, original_author['user_id'], notification_text, ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup, media_type=media_type, media_id=media_id
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
    
    # Increased to 4000 characters for full admin review (respects Telegram's 4096 limit)
    post_preview = post['content'][:4000] + ('...' if len(post['content']) > 4000 else '')
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"approve_post_{post_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_post_{post_id}")
        ],
        [
            InlineKeyboardButton(
                "Unmark Explicit" if post.get('explicit') else "Mark Explicit",
                callback_data=f"toggle_explicit_{post_id}"
            )
        ]
    ])
    
    explicit_line = "Marked as explicit\n\n" if post.get('explicit') else ""
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"New post awaiting approval from {author_name}:\n\n{explicit_line}{post_preview}",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error notifying admin: {e}")

# Update the submit vent endpoint to use this
async def notify_user_of_private_message(context: ContextTypes.DEFAULT_TYPE, sender_id: str, receiver_id: str, message_content: str, message_id: int):
    try:
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (receiver_id, sender_id)
        )
        if is_blocked:
            return

        receiver = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (receiver_id,))
        if not receiver or not receiver['notifications_enabled']:
            return

        sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
        sender_name = get_display_name(sender)
        safe_sender_name = escape_markdown(sender_name, version=2)

        media_type, media_id = 'text', None
        if message_id:
            media_row = db_fetch_one(
                "SELECT media_type, media_id FROM private_messages WHERE message_id = %s",
                (message_id,)
            )
            if media_row:
                media_type = media_row.get('media_type') or 'text'
                media_id = media_row.get('media_id')

        preview_content = message_content[:200] + '...' if message_content and len(message_content) > 200 else (message_content or "")
        safe_preview_content = escape_markdown(preview_content, version=2) if preview_content else ""

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Reply", callback_data=f"reply_msg_{sender_id}"),
                InlineKeyboardButton("Block", callback_data=f"block_user_{sender_id}")
            ]
        ])

        header_lines = ["*New Private Message*", "", "From: " + safe_sender_name, ""]
        header = "\n".join(header_lines)

        if media_id and media_type != 'text':
            caption_lines = [header, safe_preview_content, "", "_Use /inbox to view all messages_"]
            caption = "\n".join(caption_lines)
            if len(caption) > 1000:
                caption = caption[:997] + "..."
            try:
                if media_type == 'photo':
                    await context.bot.send_photo(chat_id=receiver_id, photo=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'voice':
                    await context.bot.send_voice(chat_id=receiver_id, voice=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'audio':
                    await context.bot.send_audio(chat_id=receiver_id, audio=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'video':
                    await context.bot.send_video(chat_id=receiver_id, video=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'document':
                    await context.bot.send_document(chat_id=receiver_id, document=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'gif':
                    await context.bot.send_animation(chat_id=receiver_id, animation=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                else:
                    raise ValueError("Unhandled media_type: " + str(media_type))
                return
            except Exception as media_err:
                logger.error("Failed to deliver media private message, falling back to text notice: " + str(media_err))

        fallback_body = safe_preview_content if safe_preview_content else "_\\\\[attachment\\\\]_"
        notification_lines = [header, fallback_body, "", "_Use /inbox to view all messages_"]
        notification_text = "\n".join(notification_lines)
        await context.bot.send_message(
            chat_id=receiver_id,
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error("Error sending private message notification: " + str(e))




# ==================== WEEKLY TOOLS & DIAGNOSTICS ====================

async def show_admin_weekly_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the weekly tools sub-menu for admins"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Test Weekly Calculation", callback_data='weekly_test')],
        [InlineKeyboardButton("Force Weekly Announcement", callback_data='weekly_force')],
        [InlineKeyboardButton("View Last Winners", callback_data='weekly_last')],
        [InlineKeyboardButton("Fix Weekly Schedule", callback_data='weekly_fix_schedule')],
        [InlineKeyboardButton("View Job Status", callback_data='weekly_status')],
        [InlineKeyboardButton("Back to Admin Panel", callback_data='admin_panel')]
    ]
    
    text = (
        "*Weekly Contributor Tools*\n\n"
        "Use these tools to debug and manage the weekly badge distribution job."
    )
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def weekly_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Test weekly calculation (no announcement)"""
    query = update.callback_query
    await query.answer("Calculating...")
    
    top_users = calculate_top_weekly_contributors()
    if not top_users:
        await query.message.reply_text("No users earned points in the last 7 days.")
        return

    winners_info = []
    badges = ["", "", ""]
    for idx, user_data in enumerate(top_users):
        u = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_data['user_id'],))
        name = u['anonymous_name'] if u else "Anonymous"
        winners_info.append(f"{badges[idx]} {name} – {user_data['weekly_points']} pts")

    text = "*Weekly Points (Last 7 days)*\n\n" + "\n".join(winners_info) + "\n\n_Admin only – no announcement sent._"
    await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def weekly_force_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Force weekly announcement"""
    query = update.callback_query
    await query.answer("Starting job...")
    
    status_msg = await query.message.reply_text("Forcing weekly announcement job... please wait.")
    summary = await award_weekly_badges(context)
    
    if summary['success']:
        report = (
            "*Weekly job completed.*\n"
            f"• Winners announced: {'' if summary['announcement_sent'] else ''}\n"
            f"• DMs sent: {summary['dms_sent']}\n"
            f"• Badges updated: {summary['winners_count']}"
        )
    else:
        report = f"*Weekly job failed:*\n`{summary['error']}`"
    
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def weekly_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: View last week's winners"""
    query = update.callback_query
    await query.answer()
    
    last_date, winners = get_last_week_winners()
    if not winners:
        await query.message.reply_text("No winners recorded in weekly_rankings.")
        return
    
    winners_info = []
    for w in winners:
        winners_info.append(f"{w['badge_emoji']} {w['anonymous_name']} – {w['points_earned']} pts")
    
    text = f"*Last Week's Winners* (week starting {last_date})\n\n" + "\n".join(winners_info)
    await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def weekly_fix_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Reschedule the weekly job"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.edit_message_text("Admin only.")
        return

    job_queue = context.application.job_queue
    if job_queue is None:
        await query.edit_message_text("Job queue not available. Please restart the bot.")
        return

    # Remove existing job with the same name (if any)
    existing_jobs = job_queue.jobs()
    for job in existing_jobs:
        if job.name == "weekly_badges":
            job.schedule_removal()
            logger.info("Removed existing weekly job")

    # Reschedule
    job_queue.run_daily(
        award_weekly_badges,
        time=time(0, 0, tzinfo=timezone.utc),
        days=(0,),
        name="weekly_badges"
    )
    await query.edit_message_text(
        "Weekly job rescheduled.\nNext run: Monday at 00:00 UTC.",
        parse_mode=ParseMode.MARKDOWN
    )

async def weekly_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: Show job scheduling status"""
    query = update.callback_query
    await query.answer()
    
    job_queue = context.application.job_queue
    if not job_queue:
        await query.message.reply_text("JobQueue is not initialized!")
        return

    # Search for job by name
    job = next((j for j in job_queue.jobs() if j.name == "weekly_badges"), None)
    
    if job:
        next_run = job.next_t
        await query.message.reply_text(
            f"*Weekly Job Status*\n\n"
            f"• Scheduled: Yes\n"
            f"• Next run: `{next_run.strftime('%Y-%m-%d %H:%M:%S')} UTC`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.message.reply_text("*Weekly Job Status*\n\n• Scheduled: No", parse_mode=ParseMode.MARKDOWN)

# Re-implement command versions (proxies to callbacks logic or vice versa)
async def test_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']: return
    
    top_users = calculate_top_weekly_contributors()
    if not top_users:
        await update.message.reply_text("No users earned points in the last 7 days.")
        return
    winners_info = []
    badges = ["", "", ""]
    for idx, user_data in enumerate(top_users):
        u = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_data['user_id'],))
        name = u['anonymous_name'] if u else "Anonymous"
        winners_info.append(f"{badges[idx]} {name} – {user_data['weekly_points']} pts")
    text = "*Weekly Points (Last 7 days)*\n\n" + "\n".join(winners_info) + "\n\n_Admin only – no announcement sent._"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def force_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']: return
    status_msg = await update.message.reply_text("Forcing weekly announcement job...")
    summary = await award_weekly_badges(context)
    if summary['success']:
        report = f"*Weekly job completed.*\n• DMs sent: {summary['dms_sent']}\n• Badges updated: {summary['winners_count']}"
    else:
        report = f"*Weekly job failed:*\n`{summary['error']}`"
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def weekly_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']: return
    job = next((j for j in context.application.job_queue.jobs() if j.name == "weekly_badges"), None)
    if job:
        await update.message.reply_text(f"*Weekly Job Status*\n• Scheduled:\n• Next run: `{job.next_t.strftime('%Y-%m-%d %H:%M:%S')} UTC`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("*Weekly Job Status*\n• Scheduled:", parse_mode=ParseMode.MARKDOWN)

def get_last_week_winners():
    """Fetch the most recent winners from weekly_rankings"""
    last_week = db_fetch_one("SELECT MAX(week_start) as last_date FROM weekly_rankings")
    if not last_week or not last_week['last_date']: return None, []
    last_date = last_week['last_date']
    winners = db_fetch_all("""
        SELECT r.points_earned, r.badge_emoji, u.anonymous_name
        FROM weekly_rankings r
        JOIN users u ON r.user_id = u.user_id
        WHERE r.week_start = %s
        ORDER BY r.rank ASC
    """, (last_date,))
    return last_date, winners

# ==================== ADMIN PANEL ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
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
        [InlineKeyboardButton(f"Pending Posts ({pending_count})", callback_data='admin_pending')],
        [InlineKeyboardButton(f"Users: {users_count}", callback_data='admin_users')],
        [InlineKeyboardButton("Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("Send Broadcast", callback_data='admin_broadcast')],
        [InlineKeyboardButton("Weekly Tools", callback_data='admin_weekly_tools')],
        [InlineKeyboardButton("Pending Reports", callback_data='admin_reports')],
        [InlineKeyboardButton("Monitor Chats", callback_data='admin_chats_1')],
        [InlineKeyboardButton("Back to Menu", callback_data='menu')]
    ]
    
    text = (
        f"*Admin Panel*\n\n"
        f"*Quick Stats:*\n"
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
            await update.message.reply_text("Error loading admin panel.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("Error loading admin panel.")

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the broadcast process"""
    query = update.callback_query
    # Redundant answer removed to fix mobile toast bugs
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to access this.", show_alert=True)
        return
    
    # Set broadcast state
    context.user_data['broadcasting'] = True
    context.user_data['broadcast_step'] = 'waiting_for_content'
    
    # Show broadcast options
    keyboard = [
        [
            InlineKeyboardButton("Text Broadcast", callback_data='broadcast_text'),
            InlineKeyboardButton("Photo Broadcast", callback_data='broadcast_photo')
        ],
        [
            InlineKeyboardButton("Voice Broadcast", callback_data='broadcast_voice'),
            InlineKeyboardButton("Other Media", callback_data='broadcast_other')
        ],
        [
            InlineKeyboardButton("Cancel", callback_data='admin_panel')
        ]
    ]
    
    text = (
        "*Send Broadcast Message*\n\n"
        "Choose the type of broadcast you want to send:\n\n"
        "*Text* - Send a text message to all users\n"
        "*Photo* - Send a photo with caption\n"
        "*Voice* - Send a voice message\n"
        "*Other* - Send other media types\n\n"
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
        await query.answer("You don't have permission to access this.", show_alert=True)
        return
    
    # Set broadcast type
    context.user_data['broadcast_type'] = broadcast_type
    context.user_data['broadcast_step'] = 'waiting_for_content'
    
    # Ask for content based on type
    if broadcast_type == 'text':
        prompt = "*Please type your broadcast message:*\n\nYou can use markdown formatting."
    elif broadcast_type == 'photo':
        prompt = "*Please send a photo with caption:*\n\nSend a photo and add a caption (optional)."
    elif broadcast_type == 'voice':
        prompt = "*Please send a voice message:*\n\nSend a voice message with optional caption."
    else:  # other
        prompt = "*Please send your media:*\n\nYou can send any media type (photo, video, document, etc.) with optional caption."
    
    keyboard = [[InlineKeyboardButton("Cancel", callback_data='admin_panel')]]
    
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
            await update.callback_query.answer("No broadcast data found.", show_alert=True)
        else:
            await update.message.reply_text("No broadcast data found.")
        return
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if is_callback:
            await update.callback_query.answer("You don't have permission to access this.", show_alert=True)
        else:
            await update.message.reply_text("You don't have permission to access this.")
        return
    
    # Get user count for confirmation
    total_users = db_fetch_one("SELECT COUNT(*) as count FROM users")
    users_count = total_users['count'] if total_users else 0
    
    text = (
        f"*Broadcast Confirmation*\n\n"
        f"*Recipients:* {users_count} users\n"
        f"*Type:* {broadcast_data.get('type', 'text').title()}\n\n"
        f"*Preview:*\n"
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
            InlineKeyboardButton("Send Broadcast", callback_data='execute_broadcast'),
            InlineKeyboardButton("Edit", callback_data='admin_broadcast')
        ],
        [
            InlineKeyboardButton("Cancel", callback_data='admin_panel')
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
        await update.message.reply_text("This action can only be triggered from the confirmation menu.")
        return
    
    user_id = str(update.effective_user.id)
    broadcast_data = context.user_data.get('broadcast_data', {})
    
    if not broadcast_data:
        await query.answer("No broadcast data found.", show_alert=True)
        return
    
    # Show processing message
    status_message = await query.edit_message_text(
        "*Starting Broadcast...*\n\nPreparing to send to all users...",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Get all users (exclude the sender)
    all_users = db_fetch_all("SELECT user_id FROM users WHERE user_id != %s", (user_id,))
    total_users = len(all_users)
    
    if total_users == 0:
        await status_message.edit_text(
            "No users to broadcast to.",
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
                    f"*Broadcasting...*\n\n"
                    f"Progress: {progress}%\n"
                    f"Sent: {success_count}\n"
                    f"Failed: {failed_count}\n"
                    f"Blocked: {blocked_count}\n"
                    f"Batch: {current_batch}/{total_batches}\n\n"
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
        f"*Broadcast Complete!*\n\n"
        f"Completed: {completion_time}\n"
        f"Total Users: {total_users}\n"
        f"Successfully Sent: {success_count}\n"
        f"Failed: {failed_count}\n"
        f"Blocked/Inactive: {blocked_count}\n"
        f"Success Rate: {((success_count / total_users) * 100):.1f}%\n\n"
        f"_Broadcast delivered to {success_count} active users._"
    )
    
    keyboard = [
        [InlineKeyboardButton("Send Another", callback_data='admin_broadcast')],
        [InlineKeyboardButton("Admin Panel", callback_data='admin_panel')],
        [InlineKeyboardButton("Main Menu", callback_data='menu')]
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
        await query.answer("You don't have permission to access this.", show_alert=True)
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
        "*Advanced Broadcast*\n\n"
        f"*User Statistics:*\n"
        f"• Total Users: {total_users['count'] if total_users else 0}\n"
        f"• Active (7 days): {active_users['count'] if active_users else 0}\n\n"
        "*Select targeting options:*"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("All Users", callback_data='target_all'),
            InlineKeyboardButton("Active Users", callback_data='target_active')
        ],
        [
            InlineKeyboardButton("Specific User", callback_data='target_specific'),
            InlineKeyboardButton("By Category", callback_data='target_category')
        ],
        [
            InlineKeyboardButton("Text Only", callback_data='broadcast_text'),
            InlineKeyboardButton("With Media", callback_data='broadcast_photo')
        ],
        [
            InlineKeyboardButton("Simple Broadcast", callback_data='admin_broadcast'),
            InlineKeyboardButton("Cancel", callback_data='admin_panel')
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
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
        return
    
    # Get pending posts (simplified - no JOIN with pending_notifications)
    posts = db_fetch_all("""
        SELECT p.post_id, p.content, u.anonymous_name, p.media_type, p.media_id, p.explicit,
               STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.approved = FALSE
        GROUP BY p.post_id, u.anonymous_name, p.media_type, p.media_id, p.content, p.timestamp, p.explicit
        ORDER BY p.timestamp
    """)
    
    if not posts:
        if update.callback_query:
            await update.callback_query.message.reply_text("No pending posts!")
        else:
            await update.message.reply_text("No pending posts!")
        return
    
    # Send each pending post to admin
    for post in posts[:10]:  # Limit to 10 posts to avoid flooding
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"approve_post_{post['post_id']}"),
                InlineKeyboardButton("Reject", callback_data=f"reject_post_{post['post_id']}")
            ],
            [
                InlineKeyboardButton(
                    "Unmark Explicit" if post.get('explicit') else "Mark Explicit",
                    callback_data=f"toggle_explicit_{post['post_id']}"
                )
            ]
        ])
        
        # Use HTML for more reliable escaping. Increased to 2000 for better admin review.
        preview = post['content'][:2000] + ('...' if len(post['content']) > 2000 else '')
        safe_preview = html.escape(preview)
        safe_name = html.escape(post['anonymous_name'] or "Anonymous")
        safe_cats = html.escape(post['categories'] or 'Other')
        explicit_line = "<b>Marked as explicit</b>\n\n" if post.get('explicit') else ""
        
        text = f"<b>Pending Post</b> [{safe_cats}]\n\n{explicit_line}{safe_preview}\n\n<b>{safe_name}</b>"
        
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
            elif post['media_type'] == 'audio':
                if update.callback_query:
                    await update.callback_query.message.reply_audio(
                        audio=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_audio(
                        audio=post['media_id'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML
                    )
        except Exception as e:
            logger.error(f"Error sending pending post {post['post_id']}: {e}")
            # Send as text if media fails
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    f"Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text(
                    f"Error loading media for post {post['post_id']}\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )

async def toggle_post_explicit(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Admin flags or unflags a post as explicit — works for posts still pending
    review as well as posts already published to the channel (in which case the
    live channel message content and keyboard are updated too)."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to do this.", show_alert=True)
        return

    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        await query.answer("Post not found.", show_alert=True)
        return

    new_explicit = not post.get('explicit')
    db_execute("UPDATE posts SET explicit = %s WHERE post_id = %s", (new_explicit, post_id))

    # If already live in the channel, update the channel message content + keyboard too
    if post.get('approved') and post.get('channel_message_id'):
        try:
            cats_row = db_fetch_all("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,))
            categories = [row['category_code'] for row in cats_row]
            hashtags = ' '.join([f"#{cat}" for cat in categories]) if categories else "#Other"
            safe_hashtags = html.escape(hashtags)
            vent_display = f"Vent - {post['vent_number']:03d}" if post.get('vent_number') else f"Post #{post_id}"

            if new_explicit:
                body_html = (
                    "This post is marked as explicit content and may not be suitable for all members.\n"
                    "Tap \"View Post\" below if you'd like to read it."
                )
            else:
                body_html = html.escape(post['content'])

            channel_text = (
                f"<code>{vent_display}</code>\n\n"
                f"{body_html}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{safe_hashtags}\n"
                f"<a href='https://t.me/christianvent'>Telegram</a> | <a href='https://t.me/{BOT_USERNAME}'>Bot</a>"
            )

            new_kb = build_channel_post_keyboard(post_id, post.get('comment_count', 0) or 0, new_explicit)

            if post['media_type'] == 'text':
                await context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=post['channel_message_id'],
                    text=channel_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=new_kb,
                    disable_web_page_preview=True
                )
            else:
                await context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=post['channel_message_id'],
                    caption=channel_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=new_kb
                )
        except Exception as e:
            logger.error(f"Error updating channel message explicit state for post {post_id}: {e}")

    # Refresh the toggle button label on whichever admin message this was pressed from
    try:
        new_buttons = list(query.message.reply_markup.inline_keyboard)
        for row in new_buttons:
            for i, btn in enumerate(row):
                if btn.callback_data == f"toggle_explicit_{post_id}":
                    row[i] = InlineKeyboardButton(
                        "Unmark Explicit" if new_explicit else "Mark Explicit",
                        callback_data=f"toggle_explicit_{post_id}"
                    )
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception as e:
        logger.error(f"Error updating admin keyboard after explicit toggle: {e}")

    await query.answer("Marked as explicit" if new_explicit else "Unmarked as explicit")

async def approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        try:
            await query.answer("You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("You don't have permission to do this.")
        return
    
    # Get the post
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await query.answer("Post not found.", show_alert=True)
        except:
            await query.edit_message_text("Post not found.")
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
        
        # Create the channel keyboard (View Post + Comments for explicit posts, Comments only otherwise)
        kb = build_channel_post_keyboard(post_id, 0, post.get('explicit', False))
        
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
        if post.get('explicit'):
            body_html = (
                "የዚህ post ይዘት ለሁሉም አባላት ተገቢ አይደለም። በራስዎ ሃላፊነት  ይህንን ፖስት ማንበብ ከፈለጉ፣ ከታች ያለውን\n"
                "\"View Post\" የሚለውን ይጫኑ።"
            )
        else:
            body_html = html.escape(post['content'])
        safe_hashtags = html.escape(hashtags)
        channel_text = (
            f"<code>{vent_display}</code>\n\n"
            f"{body_html}\n\n"
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
        elif post['media_type'] == 'audio':
            msg = await context.bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=post['media_id'],
                caption=channel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        else:
            await query.answer("Unsupported media type.", show_alert=True)
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
            await query.answer("Failed to update database.", show_alert=True)
            return
        
        # Notify the author in background
        asyncio.create_task(context.bot.send_message(
            chat_id=post['author_id'],
            text="Your post has been approved and published!"
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
                f"<b>Post Approved and Published!</b>\n\n"
                f"<b>Vent Number:</b> <code>{vent_display}</code>\n"
                f"<b>Categories:</b> {safe_cats_display}\n"
                f"<b>Published to channel:</b>\n\n"
                f"<b>Content Preview:</b>\n{safe_content_preview}...",
                parse_mode=ParseMode.HTML
            )
            
            # Alternative: You can also delete the admin notification message entirely
            # await query.message.delete()
            
        except BadRequest as e:
            # If editing fails, at least reply with success message
            logger.error(f"Error updating admin message: {e}")
            await query.answer("Post approved and published!", show_alert=True)
            await query.message.reply_text(
                f"Post #{post_id} approved and published as {vent_display}!",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # =============================================
        # END CRITICAL FIX
        # =============================================
        
    except Exception as e:
        logger.error(f"Error approving post: {e}")
        try:
            await query.answer(f"Failed to approve post: {str(e)}", show_alert=True)
        except:
            # Try to edit the message with error
            try:
                await query.edit_message_text("Failed to approve post. Please try again.")
            except:
                pass

async def ask_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Ask the admin if they want to provide a rejection reason"""
    query = update.callback_query
    context.user_data['rejecting_post'] = post_id
    context.user_data['awaiting_rejection_reason'] = False # Not yet typing, just menu
    
    keyboard = [
        [InlineKeyboardButton("Type Reason", callback_data=f"reject_with_reason_{post_id}")],
        [InlineKeyboardButton("Skip Reason", callback_data=f"skip_rejection_{post_id}")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rejection")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.edit_message_text(
            "*Reject Post*\n\nWould you like to provide a reason for rejecting this post?",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error showing rejection menu: {e}")
        await query.message.reply_text(
            "Rejection Reason Prompt\n\nWould you like to provide a reason?",
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
            await update.message.reply_text("Reason was too long and has been truncated to 200 characters.")
        elif update.callback_query:
            await update.callback_query.answer("Reason truncated to 200 chars", show_alert=True)

    try:
        # Notify the author in background
        notification_text = "Your post was not approved by the admin."
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
        confirm_text = f"Post #{post_id} has been rejected."
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
            await update.callback_query.message.reply_text(f"Error finalizing rejection: {e}")
        else:
            await update.message.reply_text(f"Error finalizing rejection: {e}")

async def reject_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    query = update.callback_query
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        try:
            await query.answer("You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("You don't have permission to do this.")
        return
    
    # Get the post
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        try:
            await query.answer("Post not found.", show_alert=True)
        except:
            await query.edit_message_text("Post not found.")
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
            await update.message.reply_text("Error creating user profile. Please try again.")
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

        elif arg.startswith("viewpost_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                await show_comments_menu(update, context, post_id, page=1, force_reveal=True)
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
                    preview_text = f"*Replying to:*\n{escape_markdown(content, version=2)}"
                
                await update.message.reply_text(
                    f"{preview_text}\n\nPlease type your comment or send a voice message, GIF, or sticker:\n\nTap Cancel to return to menu.",
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
                if not user_data:
                    await update.message.reply_text("User not found.")
                    return

                followers = db_fetch_all("SELECT * FROM followers WHERE followed_id = %s", (user_data['user_id'],))
                rating = calculate_user_rating(user_data['user_id'])
                current_user_id = user_id

                # Determine if this is a vent author context (viewing from a post)
                is_vent_author = False
                if post_id:
                    post_info = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
                    if post_info and str(post_info['author_id']) == str(target_user_id) and str(target_user_id) != str(current_user_id):
                        is_vent_author = True

                # Build buttons
                btn = []
                if user_data['user_id'] != current_user_id:
                    # Check chat request status
                    accepted_request = db_fetch_one(
                        "SELECT status FROM chat_requests WHERE "
                        "((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)) AND status = 'accepted'",
                        (current_user_id, user_data['user_id'], user_data['user_id'], current_user_id)
                    )

                    chat_btn_text = "Chat" if accepted_request else "Request to Chat"
                    chat_btn_callback = f'message_{user_data["user_id"]}' if accepted_request else f'chatrequest_{user_data["user_id"]}'

                    # For vent authors, only show chat and block/unblock (no follow/unfollow)
                    if is_vent_author:
                        btn.append([InlineKeyboardButton(chat_btn_text, callback_data=chat_btn_callback)])
                        # Check block status
                        is_blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (current_user_id, user_data['user_id']))
                        if is_blocked:
                            btn.append([InlineKeyboardButton("Unblock User", callback_data=f'unblock_user_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Block User", callback_data=f'block_user_{user_data["user_id"]}')])
                    else:
                        # Normal profile: show follow/unfollow, chat, block
                        is_following = db_fetch_one(
                            "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
                            (current_user_id, user_data['user_id'])
                        )
                        if is_following:
                            btn.append([InlineKeyboardButton("Unfollow", callback_data=f'unfollow_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Follow", callback_data=f'follow_{user_data["user_id"]}')])

                        btn.append([InlineKeyboardButton(chat_btn_text, callback_data=chat_btn_callback)])

                        is_blocked = db_fetch_one("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (current_user_id, user_data['user_id']))
                        if is_blocked:
                            btn.append([InlineKeyboardButton("Unblock User", callback_data=f'unblock_user_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Block User", callback_data=f'block_user_{user_data["user_id"]}')])

                # Prepare display variables
                display_sex = get_display_sex(user_data)
                bio = user_data.get('bio', 'No bio set.')
                is_owner = str(current_user_id) == str(target_user_id)

                # For vent author, we override display name and hide all stats
                if is_vent_author:
                    display_name = "Vent author"
                    # Hide stats – we will not include them in the text
                    # We also don't show bio for vent author to keep minimal
                    profile_text = f"*{escape_markdown(display_name, version=2)}*{' ' + escape_markdown(display_sex, version=2) if display_sex else ''}\n\n"
                    # Only add a note if not self? But we already handle self above.
                    # Add a simple spacer
                    profile_text += "_This is the author of the vent_\n"
                else:
                    # Normal profile (including self)
                    display_name = get_display_name(user_data)
                    weekly_badge = user_data.get('weekly_badge')
                    if weekly_badge:
                        display_name = f"{weekly_badge} {display_name}"

                    level = (rating // 10) + 1

                    # Privacy filters
                    viewer_data = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (current_user_id,))
                    is_viewer_admin = viewer_data['is_admin'] if viewer_data else False

                    if not is_viewer_admin and not is_owner:
                        if user_data.get('hide_aura'):
                            rating_str = "Hidden"
                            level_str = "Hidden"
                            aura_str = "Hidden"
                        else:
                            rating_str = str(rating)
                            level_str = str(level)
                            is_target_admin = user_data.get('is_admin', False)
                            aura_str = "" if is_target_admin else format_aura(rating)

                        if user_data.get('hide_bio'):
                            bio = "_[Hidden by user]_"

                        if user_data.get('hide_follower_count'):
                            follower_count = "Hidden"
                            following_count = "Hidden"
                        else:
                            follower_count = str(len(followers))
                            following_row = db_fetch_one(
                                "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (target_user_id,)
                            )
                            following_count = str(following_row['count'] if following_row else 0)

                        hide_role = user_data.get('hide_role')
                    else:
                        rating_str = str(rating)
                        level_str = str(level)
                        is_target_admin = user_data.get('is_admin', False)
                        aura_str = "" if is_target_admin else format_aura(rating)
                        follower_count = str(len(followers))
                        following_row = db_fetch_one(
                            "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (target_user_id,)
                        )
                        following_count = str(following_row['count'] if following_row else 0)
                        hide_role = False

                    is_target_admin = user_data.get('is_admin', False)
                    safe_name = escape_markdown(display_name, version=2)
                    safe_sex = escape_markdown(display_sex, version=2)
                    safe_bio = escape_markdown(bio, version=2)

                    if is_target_admin:
                        role_display = "Administrator"
                        if hide_role and not is_viewer_admin and not is_owner:
                            role_display = "Hidden"
                        profile_text = (
                            f"*{safe_name}*{' ' + safe_sex if safe_sex else ''}\n\n"
                            f"*Role:* {role_display}\n"
                            f"*Followers:* {follower_count} \u2022 *Following:* {following_count}\n\n"
                            f"*About:*\n{safe_bio}\n"
                        )
                    else:
                        safe_level = escape_markdown(level_str, version=2)
                        safe_rating = escape_markdown(rating_str, version=2)
                        safe_aura = escape_markdown(aura_str, version=2)
                        profile_text = (
                            f"*{safe_name}*{' ' + safe_sex if safe_sex else ''}\n\n"
                            f"*Aura Level:* {safe_level} \\({safe_aura}\\)\n"
                            f"*Points:* {safe_rating}\n"
                            f"*Followers:* {follower_count} \u2022 *Following:* {following_count}\n\n"
                            f"*About:*\n{safe_bio}\n"
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
        "*እንኳን ወደ Christian vent በሰላም መጡ* \n\n"
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
    """Show the user's inbox grouped by conversation partner, so they can pick who to open
    instead of scrolling through every message in one flat list."""
    user_id = str(update.effective_user.id)

    # Show loading
    loading_msg = None
    try:
        if hasattr(update, 'callback_query') and update.callback_query:
            loading_msg = await update.callback_query.message.edit_text("Checking inbox...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("Checking inbox...")
    except:
        pass

    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Loading", 1)

    # Get unread messages count (across all conversations)
    unread_count_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE",
        (user_id,)
    )
    unread_count = unread_count_row['count'] if unread_count_row else 0

    # Pagination settings — one row per conversation partner
    per_page = 7
    offset = (page - 1) * per_page

    # Group messages by sender so each row represents one person, not one message
    conversations = db_fetch_all('''
        SELECT pm.sender_id,
               u.anonymous_name AS sender_name,
               u.sex AS sender_sex,
               MAX(pm.timestamp) AS last_timestamp,
               COUNT(*) AS message_count,
               SUM(CASE WHEN pm.is_read = FALSE THEN 1 ELSE 0 END) AS unread_in_convo
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s
        GROUP BY pm.sender_id, u.anonymous_name, u.sex
        ORDER BY last_timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset))

    total_conv_row = db_fetch_one(
        "SELECT COUNT(DISTINCT sender_id) as count FROM private_messages WHERE receiver_id = %s",
        (user_id,)
    )
    total_conversations = total_conv_row['count'] if total_conv_row else 0
    total_pages = max(1, (total_conversations + per_page - 1) // per_page)

    if not conversations:
        # No messages - clean empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No messages")
            await asyncio.sleep(0.5)

        text = (
            "*Your Inbox is Empty*\n\n"
            "No messages yet. When someone sends you a message, "
            "it will appear here.\n\n"
            "You can message other users by viewing their profile "
            "and clicking 'Send Message'."
        )

        keyboard = [
            [InlineKeyboardButton("View Leaderboard", callback_data='leaderboard')],
            [InlineKeyboardButton("Main Menu", callback_data='menu')]
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
    text = "*Messages*\n"
    if unread_count > 0:
        text += f"{unread_count} unread\n\n"
    else:
        text += "\n"

    # Build keyboard — one button per conversation partner
    keyboard = []

    for convo in conversations:
        unread_in_convo = convo['unread_in_convo'] or 0
        status_icon = "" if unread_in_convo > 0 else ""

        sender_name = convo['sender_name'][:14] if len(convo['sender_name']) > 14 else convo['sender_name']

        # Format timestamp nicely
        timestamp = convo['last_timestamp']
        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')

        now = datetime.now()
        time_diff = now - timestamp
        if time_diff.days == 0:
            time_str = timestamp.strftime('%I:%M %p').lstrip('0')
        elif time_diff.days == 1:
            time_str = "Yesterday"
        elif time_diff.days < 7:
            time_str = timestamp.strftime('%a')
        else:
            time_str = timestamp.strftime('%b %d')

        count_label = f" ({convo['message_count']})" if convo['message_count'] > 1 else ""
        unread_label = f" • {unread_in_convo} new" if unread_in_convo > 0 else ""

        button_text = f"{status_icon} {sender_name}{count_label}{unread_label} • {time_str}"
        if len(button_text) > 40:
            button_text = button_text[:37] + "..."

        # Selecting a conversation opens that person's thread (open_conv_<sender_id>_<list_page>)
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"open_conv_{convo['sender_id']}_{page}")
        ])

    # Add pagination if needed
    if total_pages > 1:
        pagination_row = []

        if page > 1:
            pagination_row.append(InlineKeyboardButton("◀", callback_data=f"inbox_page_{page-1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))

        pagination_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))

        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("▶", callback_data=f"inbox_page_{page+1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))

        keyboard.append(pagination_row)

    # Add action buttons at bottom
    action_row = []
    if unread_count > 0:
        action_row.append(InlineKeyboardButton("Mark All Read", callback_data="mark_all_read"))

    action_row.append(InlineKeyboardButton("Refresh", callback_data=f"inbox_page_{page}"))
    keyboard.append(action_row)

    keyboard.append([
        InlineKeyboardButton("Menu", callback_data='menu'),
        InlineKeyboardButton("Profile", callback_data='profile')
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Add footer text
    convo_word = "conversation" if total_conversations == 1 else "conversations"
    text += f"_Showing {len(conversations)} of {total_conversations} {convo_word}_"

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
            await update.message.reply_text("Error loading inbox. Please try again.")


async def show_chat_requests(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    """Show the current user's incoming pending chat requests, with Accept/Reject
    per request and pagination. This is the persistent home for chat requests so
    a receiver who missed the original notification can still find and act on it,
    and a sender's request is never silently lost."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    per_page = 5
    if page < 1:
        page = 1
    offset = (page - 1) * per_page

    requests = db_fetch_all(
        """
        SELECT cr.sender_id, cr.timestamp, u.anonymous_name, u.sex, u.avatar_emoji, u.weekly_badge
        FROM chat_requests cr
        JOIN users u ON u.user_id = cr.sender_id
        WHERE cr.receiver_id = %s AND cr.status = 'pending'
        ORDER BY cr.timestamp DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, per_page, offset)
    )

    total_row = db_fetch_one(
        "SELECT COUNT(*) as cnt FROM chat_requests WHERE receiver_id = %s AND status = 'pending'",
        (user_id,)
    )
    total = total_row['cnt'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    # If this page is now empty (e.g. the last item on it was just accepted/rejected)
    # but earlier pages still have items, fall back a page instead of showing a dead end.
    if not requests and page > 1:
        await show_chat_requests(update, context, page=page - 1)
        return

    if not requests:
        text = "📭 *My Chat Requests*\n\nYou have no pending chat requests right now\\."
        keyboard = [[InlineKeyboardButton("⬅️ Back to Settings", callback_data='settings')]]
        markup = InlineKeyboardMarkup(keyboard)
        try:
            if query:
                await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
            elif hasattr(update, 'message') and update.message:
                await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.error(f"Error showing empty chat requests: {e}")
        return

    lines = [f"📬 *My Chat Requests* \\(Page {page}/{total_pages}\\)\n"]
    keyboard = []

    for req in requests:
        display_name = get_display_name(req)
        safe_name = escape_markdown(display_name, version=2)
        safe_time = escape_markdown(format_time_ago(req['timestamp']), version=2)
        lines.append(f"👤 *{safe_name}* wants to chat • _{safe_time}_")
        keyboard.append([
            InlineKeyboardButton("✅ Accept", callback_data=f"reqaccept_{req['sender_id']}_{page}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reqreject_{req['sender_id']}_{page}"),
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"View {display_name}'s Profile",
                url=f"https://t.me/{BOT_USERNAME}?start=profileid_{req['sender_id']}"
            )
        ])

    # Pagination row
    pag_row = []
    if page > 1:
        pag_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"chat_requests_{page - 1}"))
    pag_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        pag_row.append(InlineKeyboardButton("Next ▶", callback_data=f"chat_requests_{page + 1}"))
    if pag_row:
        keyboard.append(pag_row)

    keyboard.append([InlineKeyboardButton("⬅️ Back to Settings", callback_data='settings')])

    text = "\n\n".join(lines)
    markup = InlineKeyboardMarkup(keyboard)
    try:
        if query:
            await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.error(f"Error showing chat requests: {e}")
    except Exception as e:
        logger.error(f"Error showing chat requests: {e}")
        try:
            if query:
                await query.message.reply_text("Error loading chat requests. Please try again.")
        except Exception:
            pass


async def show_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, sender_id: str, page=1, list_page=1):
    """Show every message from one specific person (a single thread), so the user can
    browse a conversation without it being mixed in with everyone else's messages."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = str(update.effective_user.id)

    sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
    sender_name = get_display_name(sender) if sender else "Unknown User"

    per_page = 6
    offset = (page - 1) * per_page

    messages = db_fetch_all('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s AND pm.sender_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, sender_id, per_page, offset))

    total_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND sender_id = %s",
        (user_id, sender_id)
    )
    total_messages = total_row['count'] if total_row else 0
    total_pages = max(1, (total_messages + per_page - 1) // per_page)

    safe_name = escape_markdown(sender_name, version=2)

    if not messages:
        text = f"*No messages from {safe_name}*\n\nThey may have been deleted\\."
        keyboard = [[InlineKeyboardButton("Back to Inbox", callback_data="inbox_page_1")]]
        try:
            if query:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            elif hasattr(update, 'message') and update.message:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"Error showing empty conversation: {e}")
        return

    is_blocked = db_fetch_one(
        "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
        (user_id, sender_id)
    )

    text = f"*Conversation with {safe_name}*\n"
    text += f"_{total_messages} message{'s' if total_messages != 1 else ''}_\n\n"

    keyboard = []
    for msg in messages:
        status_icon = "" if not msg['is_read'] else ""

        timestamp = msg['timestamp']
        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')

        now = datetime.now()
        time_diff = now - timestamp
        if time_diff.days == 0:
            time_str = timestamp.strftime('%I:%M %p').lstrip('0')
        elif time_diff.days == 1:
            time_str = "Yesterday"
        elif time_diff.days < 7:
            time_str = timestamp.strftime('%a')
        else:
            time_str = timestamp.strftime('%b %d')

        preview = msg['content'] or '[attachment]'
        if len(preview) > 26:
            preview = preview[:23] + '...'
        clean_preview = preview.replace('*', '').replace('_', '').replace('`', '').strip()

        button_text = f"{status_icon} {clean_preview} • {time_str}"
        if len(button_text) > 40:
            button_text = button_text[:37] + "..."

        # from_page (page) here doubles as "which thread page to return to after viewing"
        keyboard.append([
            InlineKeyboardButton(button_text, callback_data=f"view_message_{msg['message_id']}_{sender_id}_{page}")
        ])

    # Pagination within this one thread
    if total_pages > 1:
        pagination_row = []
        if page > 1:
            pagination_row.append(InlineKeyboardButton("◀", callback_data=f"open_conv_{sender_id}_{list_page}_{page-1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))

        pagination_row.append(InlineKeyboardButton(f"Page {page}/{total_pages}", callback_data="noop"))

        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("▶", callback_data=f"open_conv_{sender_id}_{list_page}_{page+1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))

        keyboard.append(pagination_row)

    # Quick actions for this person
    action_row = [InlineKeyboardButton("Reply", callback_data=f"reply_msg_{sender_id}")]
    if is_blocked:
        action_row.append(InlineKeyboardButton("Unblock", callback_data=f"unblock_user_{sender_id}"))
    else:
        action_row.append(InlineKeyboardButton("Block", callback_data=f"block_user_{sender_id}"))
    keyboard.append(action_row)

    keyboard.append([InlineKeyboardButton("Back to Inbox", callback_data=f"inbox_page_{list_page}")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if query:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error showing conversation: {e}")
        if query:
            await query.message.reply_text("Error loading conversation. Please try again.")


async def view_individual_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, sender_id: str, from_page=1, list_page=1):
    """View an individual private message with clean, natural UI — now renders attachments too"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    await typing_animation(context, query.message.chat_id, 0.3)

    message = db_fetch_one('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex, u.user_id as sender_id
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.message_id = %s AND pm.receiver_id = %s
    ''', (message_id, user_id))

    if not message:
        try:
            await query.message.edit_text(
                "Message not found or you don't have permission to view it.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await query.message.reply_text("Message not found.")
        return

    db_execute("UPDATE private_messages SET is_read = TRUE WHERE message_id = %s", (message_id,))

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
            time_ago = f"{time_diff.seconds // 60}m ago"
        else:
            time_ago = f"{time_diff.seconds // 3600}h ago"
    elif time_diff.days == 1:
        time_ago = "yesterday"
    elif time_diff.days < 7:
        time_ago = timestamp.strftime('%A')
    elif time_diff.days < 30:
        time_ago = f"{time_diff.days // 7}w ago"
    else:
        time_ago = timestamp.strftime('%b %d')

    media_type = message.get('media_type') or 'text'
    media_id = message.get('media_id')

    body_text = escape_markdown(message['content'], version=2) if message['content'] else ""
    text_lines = [
        "*Message from " + escape_markdown(message['sender_name'], version=2) + "*",
        "_" + escape_markdown(time_ago, version=2) + "_",
        "",
        body_text
    ]
    text = "\n".join(text_lines)

    is_blocked = db_fetch_one(
        "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
        (user_id, message['sender_id'])
    )
    block_btn = (
        InlineKeyboardButton("Unblock", callback_data=f"unblock_user_{message['sender_id']}")
        if is_blocked else
        InlineKeyboardButton("Block", callback_data=f"block_user_{message['sender_id']}")
    )

    keyboard = [
        [
            InlineKeyboardButton("Reply", callback_data=f"reply_msg_{message['sender_id']}"),
            InlineKeyboardButton("View Profile", url=f"https://t.me/{context.bot.username}?start=profileid_{message['sender_id']}")
        ],
        [
            InlineKeyboardButton("Delete", callback_data=f"delete_message_{message_id}_{sender_id}_{from_page}_{list_page}"),
            block_btn
        ],
        [
            InlineKeyboardButton("Back to Conversation", callback_data=f"open_conv_{sender_id}_{list_page}_{from_page}"),
            InlineKeyboardButton("Menu", callback_data='menu')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if media_id and media_type != 'text':
            # Media can't be shown by editing a text message — send it fresh and drop the old bubble.
            try:
                await query.message.delete()
            except:
                pass

            caption = text[:1000] if len(text) > 1000 else text
            send_kwargs = dict(chat_id=query.message.chat_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)

            if media_type == 'photo':
                await context.bot.send_photo(photo=media_id, **send_kwargs)
            elif media_type == 'voice':
                await context.bot.send_voice(voice=media_id, **send_kwargs)
            elif media_type == 'audio':
                await context.bot.send_audio(audio=media_id, **send_kwargs)
            elif media_type == 'video':
                await context.bot.send_video(video=media_id, **send_kwargs)
            elif media_type == 'document':
                await context.bot.send_document(document=media_id, **send_kwargs)
            elif media_type == 'gif':
                await context.bot.send_animation(animation=media_id, **send_kwargs)
            else:
                await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
        else:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error viewing message: {e}")
        try:
            await query.message.reply_text(
                f"Message from {message['sender_name']}:\n\n"
                f"{message['content'] or '[attachment]'}\n\n"
                f"_{time_ago}_",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await query.message.reply_text("Error loading message.")
async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, sender_id: str, from_page=1, list_page=1):
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
        await query.answer("Message not found", show_alert=True)
        return

    # Create clean preview
    preview = message['content'][:50] + '...' if message['content'] and len(message['content']) > 50 else message['content']

    text = (
        f"*Delete Message?*\n\n"
        f"From: {message['sender_name']}\n"
        f"Preview: {preview}\n\n"
        f"This action cannot be undone."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Delete", callback_data=f"confirm_delete_message_{message_id}_{sender_id}_{from_page}_{list_page}"),
            InlineKeyboardButton("Keep", callback_data=f"cancel_delete_message_{message_id}_{sender_id}_{from_page}_{list_page}")
        ]
    ])

    await query.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
async def confirm_delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, sender_id: str, from_page=1, list_page=1):
    """Confirm and delete message with clean feedback"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)

    # Show processing
    await query.message.edit_text("Deleting message...")
    await asyncio.sleep(0.5)

    # Delete the message
    success = db_execute(
        "DELETE FROM private_messages WHERE message_id = %s AND receiver_id = %s",
        (message_id, user_id)
    )

    if success:
        # Show success and return to the conversation thread this message belonged to
        await query.message.edit_text(
            "Message deleted successfully.",
            parse_mode=ParseMode.MARKDOWN
        )
        await asyncio.sleep(0.7)
        await show_conversation(update, context, sender_id, from_page, list_page)
    else:
        await query.answer("Error deleting message", show_alert=True)
        await query.message.edit_text(
            "Could not delete message. Please try again.",
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

    await query.answer("All messages marked as read")
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
                "*Your Messages*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(
                "*Your Messages*\n\nYou don't have any messages yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    messages_text = f"*Your Messages* (Page {page}/{total_pages})\n\n"
    
    for msg in messages:
        # Handle timestamp whether it's string or datetime object
        if isinstance(msg['timestamp'], str):
            timestamp = datetime.strptime(msg['timestamp'], '%Y-%m-%d %H:%M:%S').strftime('%b %d, %H:%M')
        else:
            timestamp = msg['timestamp'].strftime('%b %d, %H:%M')
        sender_sex = msg['sender_sex'] if msg['sender_sex'] in ('👨', '👩') else ""
        messages_text += f"*{msg['sender_name']}*{' ' + sender_sex if sender_sex else ''} ({timestamp}):\n"
        messages_text += f"{escape_markdown(msg['content'], version=2)}\n\n"
        messages_text += "━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Build keyboard with pagination and reply options
    keyboard_buttons = []
    
    # Pagination buttons
    pagination_row = []
    if page > 1:
        pagination_row.append(InlineKeyboardButton("Previous", callback_data=f"messages_page_{page-1}"))
    if page < total_pages:
        pagination_row.append(InlineKeyboardButton("Next", callback_data=f"messages_page_{page+1}"))
    if pagination_row:
        keyboard_buttons.append(pagination_row)
    
    # Reply and block buttons for each message
    for msg in messages:
        keyboard_buttons.append([
            InlineKeyboardButton(f"Reply to {msg['sender_name']}", callback_data=f"reply_msg_{msg['sender_id']}"),
            InlineKeyboardButton(f"Block {msg['sender_name']}", callback_data=f"block_user_{msg['sender_id']}")
        ])
    
    keyboard_buttons.append([InlineKeyboardButton("Main Menu", callback_data='menu')])
    
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
            await update.message.reply_text("Error loading messages. Please try again.")

async def show_comments_menu(update, context, post_id, page=1, force_reveal=False, auto_show_comments=False):
    """Entry point for viewing a post: shows the post content (or an explicit-content
    warning) with "View Comments" / "Write Comment" buttons. Comments are only loaded
    once the user taps "View Comments" (or immediately if auto_show_comments=True,
    which is used right after a user posts a new comment so they can see it land)."""
    post = db_fetch_one("""
        SELECT p.*, STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.post_id = %s
        GROUP BY p.post_id
    """, (post_id,))
    if not post:
        if hasattr(update, 'message') and update.message:
            viewer_id = str(update.effective_user.id) if update.effective_user else None
            await update.message.reply_text("Post not found.", reply_markup=get_main_menu(viewer_id) if viewer_id else None)
        return

    viewer_id = str(update.effective_user.id) if update.effective_user else None
    viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (viewer_id,)) if viewer_id else None
    is_admin_viewer = bool(viewer_row and viewer_row.get('is_admin'))
    is_owner = viewer_id is not None and str(post['author_id']) == viewer_id

    target_message = None
    if hasattr(update, 'message') and update.message:
        target_message = update.message
    elif hasattr(update, 'callback_query') and update.callback_query:
        target_message = update.callback_query.message

    # Explicit-content gate: authors and admins see it directly; everyone else must
    # tap through a warning first. Deleted posts skip the gate (nothing to reveal).
    if post.get('explicit') and not post.get('deleted') and not is_owner and not is_admin_viewer and not force_reveal:
        comment_count = count_all_comments(post_id)
        reveal_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("View Post & Comments", callback_data=f"revealexplicit_{post_id}_{page}")]
        ])
        warning_text = (
            "*Explicit Content Warning*\n\n"
            "This post contains explicit or sexual content that may not be suitable for all members\\.\n\n"
            f"{comment_count} comment\\(s\\)\n\n"
            "Tap below if you still wish to view it\\."
        )
        if target_message:
            await target_message.reply_text(warning_text, reply_markup=reveal_kb, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Build the post header
    if post.get('deleted'):
        post_text = "This content has been deleted by the author."
    else:
        post_text = post['content']
    escaped_text = escape_markdown(post_text, version=2)

    categories_display = post['categories'] or 'Other'
    escaped_categories = escape_markdown(categories_display, version=2)

    if post.get('vent_number'):
        vent_display = f"Vent - {post['vent_number']:03d}"
    else:
        vent_display = f"Post #{post_id}"
    escaped_vent = escape_markdown(vent_display, version=2)

    explicit_tag = "_Explicit content_\n" if post.get('explicit') else ""

    header_text = (
        f"*{escaped_vent}*\n"
        f"{explicit_tag}"
        f"{escaped_categories}\n\n"
        f"{escaped_text}"
    )

    comment_count = count_all_comments(post_id)
    header_kb = [
        [InlineKeyboardButton(f"View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton("Write Comment", callback_data=f"writecomment_{post_id}")]
    ]
    if not post.get('deleted'):
        header_kb.append([InlineKeyboardButton("Report Post", callback_data=f"report_post_{post_id}")])

    if target_message:
        await target_message.reply_text(
            header_text,
            reply_markup=InlineKeyboardMarkup(header_kb),
            parse_mode=ParseMode.MARKDOWN_V2
        )

    # Comments only load once the user taps "View Comments" — unless we were asked
    # to auto-show them (e.g. right after the user posts a new comment).
    if auto_show_comments:
        await show_comments_page(update, context, post_id, page)

def escape_markdown_v2(text):
    """Escape all special characters for MarkdownV2"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, '\\' + char)
    return text

# Fragments of our own "copy this" prompts that users sometimes paste back to us
# by accident (e.g. selecting the whole message bubble instead of just the code block).
_EDIT_INSTRUCTION_ARTIFACTS = [
    "copy the text below (tap the box to copy only the text):",
    "copy the text below:",
    "copy the text below",
]

def sanitize_pasted_edit(raw_text: str):
    """
    Strip a leading 'Copy the text below...' instruction line if a user accidentally
    copied it along with the content they meant to edit.
    Returns (cleaned_text, was_cleaned).
    """
    if not raw_text:
        return raw_text, False

    cleaned = raw_text.strip()
    if cleaned.startswith(""):
        cleaned = cleaned.lstrip("").strip()

    lowered = cleaned.lower()
    for artifact in _EDIT_INSTRUCTION_ARTIFACTS:
        if lowered.startswith(artifact):
            cleaned = cleaned[len(artifact):]
            break
    else:
        # Also catch it as a standalone first line even with slightly different wording
        first_line, _, rest = cleaned.partition("\n")
        if "copy the text below" in first_line.lower():
            cleaned = rest

    cleaned = cleaned.strip(" :\n")
    return (cleaned if cleaned else raw_text.strip()), (cleaned.strip() != raw_text.strip())

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
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type NOT IN ('dislike', '👎', '😡')",
            (comment_id,)
        )
        likes = likes_row['cnt'] if likes_row else 0
        
        dislikes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type IN ('dislike', '👎', '😡')",
            (comment_id,)
        )
        dislikes = dislikes_row['cnt'] if dislikes_row else 0

    like_emoji = "👍"
    dislike_emoji = "👎"

    # Build keyboard
    kb_buttons = [
        [
            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
            InlineKeyboardButton("Reply", callback_data=f"reply_{comment['post_id']}_{comment_id}")
        ],
        [InlineKeyboardButton("Report", callback_data=f"report_comment_{comment_id}")]
    ]
    
    # Add edit/delete buttons only for comment author and only for text comments
    if comment['author_id'] == user_id:
        if comment_type == 'text':
            kb_buttons.append([
                InlineKeyboardButton("Edit", callback_data=f"edit_comment_{comment_id}"),
                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
            ])
        else:
            kb_buttons.append([
                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
            ])
    
    kb = InlineKeyboardMarkup(kb_buttons)

    # FIX: use dynamic kwargs for reply_to_message_id
    send_kwargs = {
        'chat_id': chat_id,
        'reply_markup': kb,
        'parse_mode': ParseMode.MARKDOWN_V2
    }
    
    if isinstance(reply_to_message_id, int) and reply_to_message_id > 0:
        send_kwargs['reply_to_message_id'] = reply_to_message_id

    # Send message based on comment type
    try:
        escaped_content = escape_markdown_v2(content) if content else ""
        # FIX: always use comment's own author fields (already built in author_text by callers)
        message_text = f"{escaped_content}\n\n{author_text}" if escaped_content else author_text
        
        msg = None
        if comment_type == 'text':
            send_kwargs['text'] = message_text
            send_kwargs['disable_web_page_preview'] = True
            msg = await context.bot.send_message(**send_kwargs)
            
        elif comment_type == 'voice' and file_id:
            send_kwargs['voice'] = file_id
            send_kwargs['caption'] = message_text
            msg = await context.bot.send_voice(**send_kwargs)
            
        elif comment_type in ('photo', 'gif') and file_id:
            # Photos and GIFs support captions, so attach the author info + buttons
            # directly to the media — same clean single-bubble layout as voice comments.
            send_kwargs['caption'] = message_text
            if comment_type == 'photo':
                msg = await context.bot.send_photo(photo=file_id, **send_kwargs)
            else:  # gif
                msg = await context.bot.send_animation(animation=file_id, **send_kwargs)

        elif comment_type == 'sticker' and file_id:
            # Stickers can't carry a caption on Telegram, so send the sticker first,
            # then the author info + buttons as a reply directly beneath it.
            media_kwargs = {
                'chat_id': chat_id,
                'reply_to_message_id': send_kwargs.get('reply_to_message_id')
            }
            media_msg = await context.bot.send_sticker(sticker=file_id, **media_kwargs)

            info_kwargs = send_kwargs.copy()
            info_kwargs['text'] = message_text
            info_kwargs['reply_to_message_id'] = media_msg.message_id
            info_kwargs['disable_web_page_preview'] = True

            msg = await context.bot.send_message(**info_kwargs)
            
        else:
            # Fallback for unknown types
            send_kwargs['text'] = message_text
            send_kwargs['disable_web_page_preview'] = True
            msg = await context.bot.send_message(**send_kwargs)

        if msg:
            # FIX: Store message ID in database for threading
            db_execute(
                "UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s",
                (msg.message_id, comment_id)
            )
            return msg.message_id
            
    except BadRequest as e:
        # FIX: Improved fallback logic for "Message to be replied not found"
        if "Message to be replied not found" in str(e) and 'reply_to_message_id' in send_kwargs:
            logger.warning(f"Threading failed for comment {comment_id}, retrying as standalone. Error: {e}")
            # Create a new dict WITHOUT reply_to_message_id
            fallback_kwargs = {k: v for k, v in send_kwargs.items() if k != 'reply_to_message_id'}
            try:
                if comment_type == 'text' or comment_type not in ('voice', 'gif', 'sticker', 'photo'):
                    msg = await context.bot.send_message(**fallback_kwargs)
                elif comment_type == 'voice':
                    msg = await context.bot.send_voice(**fallback_kwargs)
                elif comment_type in ('photo', 'gif'):
                    fallback_kwargs['caption'] = fallback_kwargs.pop('text', message_text)
                    if comment_type == 'photo':
                        msg = await context.bot.send_photo(photo=file_id, **fallback_kwargs)
                    else:  # gif
                        msg = await context.bot.send_animation(animation=file_id, **fallback_kwargs)
                elif comment_type == 'sticker':
                    # Fallback for sticker: media first (standalone), then info as reply
                    m_msg = await context.bot.send_sticker(sticker=file_id, chat_id=chat_id)
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=message_text,
                        reply_markup=kb,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=m_msg.message_id,
                        disable_web_page_preview=True
                    )
                
                if msg:
                    db_execute("UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s", (msg.message_id, comment_id))
                    return msg.message_id
            except Exception as e2:
                logger.error(f"Fallback also failed for comment {comment_id}: {e2}")
        else:
            logger.error(f"BadRequest sending comment {comment_id}: {e}")
            
    except Exception as e:
        logger.error(f"Error sending comment {comment_id}: {e}")
        # Final fallback to plain text without markdown
        try:
            message_text = f"[Media] {content}\n\n{author_text}"
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=kb,
                disable_web_page_preview=True
            )
            if msg:
                db_execute("UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s", (msg.message_id, comment_id))
                return msg.message_id
        except Exception as e2:
            logger.error(f"Final fallback failed for comment {comment_id}: {e2}")
    
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
            loading_msg = await context.bot.send_message(chat_id, "Loading comments...")
        except:
            pass

    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        if loading_msg:
            try: await loading_msg.delete()
            except: pass
        await context.bot.send_message(chat_id, "Post not found.")
        return

    post_author_id = post['author_id'] if not post.get('deleted') else None
    per_page = 10
    offset = (page - 1) * per_page

    # OPTIMIZED: Batch load comments and user data using a JOIN
    comments = db_fetch_all("""
        SELECT c.*, u.sex AS user_sex, u.avatar_emoji, u.anonymous_name, u.is_admin
        FROM comments c
        LEFT JOIN users u ON c.author_id = u.user_id
        WHERE c.post_id = %s
        ORDER BY c.timestamp ASC
        LIMIT %s OFFSET %s
    """, (post_id, per_page, offset))

    # FIX: Restore sex field from aliased user_sex
    for c in comments:
        c['sex'] = c.pop('user_sex', '👤') or '👤'

    # Count all comments for pagination
    total_comments = count_all_comments(post_id)
    total_pages = (total_comments + per_page - 1) // per_page

    user_id = str(update.effective_user.id)
    if not comments and page == 1:
        if loading_msg:
            try: await loading_msg.delete()
            except: pass
        first_comment_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Be the first to comment", callback_data=f"writecomment_{post_id}")]
        ])
        await context.bot.send_message(
            chat_id,
            "_No comments yet\\. Start the conversation\\!_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=first_comment_kb
        )
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
            SELECT comment_id, 
                   CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END as rgroup,
                   COUNT(*) as cnt 
            FROM reactions WHERE comment_id IN %s GROUP BY comment_id, CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END
        """, (tuple(comment_ids),))
        for row in counts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            if row['rgroup'] == 'like': reaction_data[cid]['likes'] = row['cnt']
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
        aura_text = f"_Aura_ ⚡ {rating} pts" if not comment['is_admin'] else ""
        
        if is_author:
            # Vent author: show sex emoji + clickable "Vent author" (no custom avatar, no aura)
            sex_emoji = comment.get('sex') or '👤'
            author_text = f"{sex_emoji} _[{escape_markdown('Vent author', version=2)}]({profile_link})_"
        else:
            # Normal user: show full display (sex + custom avatar + name + aura)
            sex_emoji = comment.get('sex') or '👤'
            avatar_emoji = comment.get('avatar_emoji')
            if sex_emoji in ('👨', '👩'):
                author_avatar = f"{sex_emoji} {avatar_emoji}" if avatar_emoji else sex_emoji
            else:
                author_avatar = avatar_emoji if avatar_emoji else '👤'
            author_label = f"_[{escape_markdown(comment['anonymous_name'] or 'Anonymous', version=2)}]({profile_link})_"
            author_text = f"{author_avatar} {author_label} {aura_text}".strip()

        # Threading logic - FIX: check current batch msg_ids first
        reply_to_id = msg_ids.get(parent_id) or parent_msg_ids.get(parent_id)
        
        # Pre-fetched data for button builder
        pref = reaction_data.get(comment_id, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
        
        new_msg_id = await send_comment_message(context, chat_id, comment, author_text, reply_to_id, pre_fetched_data=pref)
        if new_msg_id:
            msg_ids[comment_id] = new_msg_id
    
    # Pagination Add comment button
    is_last_page = page >= total_pages

    if total_pages > 1:
        nav_buttons = []
        if page > 1: nav_buttons.append(InlineKeyboardButton("Older", callback_data=f"viewcomments_{post_id}_{page-1}"))
        if page < total_pages: nav_buttons.append(InlineKeyboardButton("Newer", callback_data=f"viewcomments_{post_id}_{page+1}"))
        rows = [nav_buttons]
        if is_last_page:
            rows.append([InlineKeyboardButton("Add comment", callback_data=f"writecomment_{post_id}")])
        await context.bot.send_message(chat_id, f"Page {page}/{total_pages}", reply_markup=InlineKeyboardMarkup(rows))
    elif is_last_page:
        # Single page — standalone add comment button
        await context.bot.send_message(
            chat_id,
            "Add your thoughts to the conversation",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Add comment", callback_data=f"writecomment_{post_id}")]])
        )
async def send_reply_message(context, chat_id, reply, post_author_id, post_id, reply_to_message_id, pre_fetched_data=None):
    """Send a single reply message with proper formatting using pre-fetched user data if available"""
    # Use joined data if available, else fetch
    is_admin = reply.get('is_admin')
    if is_admin is None: # Not pre-fetched
        reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (reply['author_id'],))
        is_admin = reply_user.get('is_admin', False)
        display_sex = get_display_sex(reply_user)
        display_name = get_display_name(reply_user)
        avatar_emoji = reply_user.get('avatar_emoji')
    else:
        display_sex = reply.get('sex') or '👤'
        display_name = reply.get('anonymous_name') or 'Anonymous'
        avatar_emoji = reply.get('avatar_emoji')
        
    rating_reply = calculate_user_rating(reply['author_id'])
    reply_profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply['author_id']}_{post_id}"
    aura_text = f"_Aura_ ⚡ {rating_reply} pts" if not is_admin else ""
    
    # Check if reply author is the vent author
    if str(reply['author_id']) == str(post_author_id):
        # Vent author reply: clickable "Vent author" with sex emoji
        sex_emoji = display_sex or '👤'
        reply_author_text = f"{sex_emoji} _[{escape_markdown('Vent author', version=2)}]({reply_profile_link})_"
    else:
        # Normal user
        author_sex = display_sex or '👤'
        author_label = f"_[{escape_markdown(display_name, version=2)}]({reply_profile_link})_"
        if author_sex in ('👨', '👩'):
            author_avatar = f"{author_sex} {avatar_emoji}" if avatar_emoji else author_sex
        else:
            author_avatar = avatar_emoji if avatar_emoji else '👤'
        reply_author_text = f"{author_avatar} {author_label} {aura_text}".strip()

    # Pass pre-fetched reaction data if available (e.g. from show_more_replies)
    # FIX: Pass the full reply dict (already done, but ensured)
    return await send_comment_message(context, chat_id, reply, reply_author_text, reply_to_message_id, pre_fetched_data=pre_fetched_data)

async def show_more_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, comment_id: int, page: int):
    """Show additional replies for a comment (paginated)"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Get the comment to find its post and telegram_message_id
    comment = db_fetch_one("SELECT post_id, telegram_message_id FROM comments WHERE comment_id = %s", (comment_id,))
    if not comment:
        await query.answer("Comment not found", show_alert=True)
        return
    
    post_id = comment['post_id']
    base_reply_to_id = comment.get('telegram_message_id')
    post = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
    post_author_id = post['author_id'] if post else None
    
    # Pagination for replies
    replies_per_page = 5
    # Skip the first 3 replies already shown in the comment view
    offset = 3 + (page - 1) * replies_per_page
    
    # FIX: Get total replies for pagination
    total_replies_res = db_fetch_one("""
        WITH RECURSIVE comment_tree AS (
            SELECT comment_id FROM comments WHERE parent_comment_id = %s
            UNION ALL
            SELECT c.comment_id FROM comments c
            JOIN comment_tree ct ON c.parent_comment_id = ct.comment_id
        )
        SELECT COUNT(*) as cnt FROM comment_tree
    """, (comment_id,))
    total_replies = total_replies_res['cnt'] if total_replies_res else 0
    total_pages = (total_replies - 3 + replies_per_page - 1) // replies_per_page
    
    # Get replies for this page with user data JOINed
    try:
        replies = db_fetch_all("""
            WITH RECURSIVE comment_tree AS (
                SELECT * FROM comments WHERE parent_comment_id = %s
                UNION ALL
                SELECT c.* FROM comments c
                JOIN comment_tree ct ON c.parent_comment_id = ct.comment_id
            )
            SELECT ct.*, u.sex AS user_sex, u.anonymous_name, u.is_admin, u.avatar_emoji
            FROM comment_tree ct
            LEFT JOIN users u ON ct.author_id = u.user_id
            ORDER BY ct.timestamp ASC LIMIT %s OFFSET %s
        """, (comment_id, replies_per_page, offset))
        
        # FIX: Restore sex field from aliased user_sex
        for r in replies:
            r['sex'] = r.pop('user_sex', '👤') or '👤'
            
    except Exception as e:
        logger.error(f"Error fetching more replies for comment {comment_id}: {e}")
        await query.answer("Error loading replies", show_alert=True)
        return
    
    # Pre-fetch reaction data for replies
    reply_ids = [r['comment_id'] for r in replies]
    reaction_data = {}
    parent_msg_ids = {}
    user_id = str(update.effective_user.id)
    
    if reply_ids:
        # Batch counts
        counts = db_fetch_all("""
            SELECT comment_id, 
                   CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END as rgroup,
                   COUNT(*) as cnt 
            FROM reactions WHERE comment_id IN %s GROUP BY comment_id, CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END
        """, (tuple(reply_ids),))
        for row in counts:
            cid = row['comment_id']
            if cid not in reaction_data: reaction_data[cid] = {'likes': 0, 'dislikes': 0, 'user_reaction': None}
            if row['rgroup'] == 'like': reaction_data[cid]['likes'] = row['cnt']
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
                    f"Show even more replies ({remaining} more)", 
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
                    text="*Even more replies below:*",
                    reply_markup=keyboard,
                    reply_to_message_id=reply_to_id,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error sending additional replies button: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="*Even more replies below:*",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If called from a callback query, answer it first
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "*Main Menu*\nUse the buttons below:",
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
            "*Main Menu*\nUse the buttons below:",
            reply_markup=get_main_menu(str(update.effective_user.id)),
            parse_mode=ParseMode.MARKDOWN
        )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profile - shortcut to the same profile view as the 'Profile' menu button."""
    user_id = str(update.effective_user.id)
    await send_updated_profile(user_id, update.message.chat.id, context)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask - shortcut to the same category picker as the 'Share' menu button."""
    context.user_data['selected_categories'] = set()
    await update.message.reply_text(
        "*Select categories (you can choose multiple):*",
        reply_markup=build_multi_category_keyboard(set()),
        parse_mode=ParseMode.MARKDOWN
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help - quick guide to using the bot."""
    help_text = (
        "*How to use this bot*\n\n"
        "• *Share* - post an anonymous vent, pick one or more categories, then submit.\n"
        "• *Profile* - view your stats, aura level, and points.\n"
        "• *Posts* - browse posts from the community.\n"
        "• *Top* - see the leaderboard of top contributors.\n"
        "• *Chat Requests* - see anyone who wants to chat with you, and accept or reject.\n"
        "• *Settings* - manage notifications, privacy, and blocked users.\n"
        "• *Open App* - the full mini app experience with feed, comments, and voice messages.\n\n"
        "*Useful commands*\n"
        "/ask - start a new post\n"
        "/profile - view your profile\n"
        "/inbox - view private messages\n"
        "/requests - view pending chat requests\n"
        "/leaderboard - view top contributors\n"
        "/settings - open settings\n"
        "/about - learn more about this bot"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/about - what this bot is and how anonymity works."""
    about_text = (
        "*About this bot*\n\n"
        "This is a safe space to share what's on your mind anonymously, connect with others, "
        "and support one another.\n\n"
        "Your identity stays private unless you choose to reveal it - posts and comments are "
        "shown under an anonymous name and avatar.\n\n"
        "Use /help to see everything the bot can do."
    )
    await update.message.reply_text(about_text, parse_mode=ParseMode.MARKDOWN)


async def send_updated_profile(user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    if not user:
        return
    
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = calculate_user_rating(user_id)
    
    weekly_badge = user.get('weekly_badge')
    if weekly_badge:
        display_name = f"{weekly_badge} {display_name}"

    
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = %s",
        (user_id,)
    )
    
    bio = user.get('bio', 'No bio set.')
    level = (rating // 10) + 1
    follower_count = len(followers)

    # Fetch following count (users this person follows)
    following_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (user_id,)
    )
    following_count = following_row['count'] if following_row else 0
    
    # PREMIUM Grid Layout
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Name", callback_data='edit_name'),
            InlineKeyboardButton("Sex", callback_data='edit_sex'),
            InlineKeyboardButton("Bio", callback_data='edit_bio')
        ],
        [
            InlineKeyboardButton("Avatar", callback_data='select_avatar'),
            InlineKeyboardButton("Content", callback_data='my_content_menu')
        ],
        [
            InlineKeyboardButton("Followers", callback_data='list_followers_1'),
            InlineKeyboardButton("Following", callback_data='list_following_1')
        ],
        [
            InlineKeyboardButton("Inbox", callback_data='inbox'),
            InlineKeyboardButton("Settings", callback_data='settings')
        ],
        [InlineKeyboardButton("Main Menu", callback_data='menu')]
    ])
    
    is_admin = user.get('is_admin', False)
    
    # Standardize escaping for V2
    safe_name = escape_markdown(display_name, version=2)
    safe_sex = escape_markdown(display_sex, version=2)
    safe_bio = escape_markdown(bio, version=2)
    safe_level = escape_markdown(str(level), version=2)
    safe_rating = escape_markdown(str(rating), version=2)
    safe_aura = escape_markdown("" if is_admin else format_aura(rating), version=2)

    if is_admin:
        profile_text = (
            f"*{safe_name}*{' ' + safe_sex if safe_sex else ''}\n\n"
            f"*Role:* Administrator\n"
            f"*Followers:* {follower_count} \u2022 *Following:* {following_count}\n\n"
            f"*About:*\n{safe_bio}\n"
            f"_Use /menu to return_"
        )
    else:
        profile_text = (
            f"*{safe_name}*{' ' + safe_sex if safe_sex else ''}\n\n"
            f"*Aura Level:* {safe_level} \\({safe_aura}\\)\n"
            f"*Points:* {safe_rating}\n"
            f"*Followers:* {follower_count} \u2022 *Following:* {following_count}\n\n"
            f"*About:*\n{safe_bio}\n"
            f"_Use /menu to return_"
        )
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=profile_text,
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN_V2
    )

AVATAR_EMOJIS = [
    # Original set
    "🦁", "🦊", "🐉", "🐼", "🦄",
    "🌈", "✨", "🔥", "💎", "🛡",
    "🦅", "🦉", "🦋", "🌸", "🌙",
    "🍎", "🍀", "⛪️", "🎗", "🎖",
    # Faith
    "✝️", "🙏", "📿", "💒", "🕊️", "🕯️", "🌾",
    # Fire / light / energy
    "⚡", "💥", "🌟", "🔆", "🌠", "⭐",
    # Mood
    "😊", "😄", "😢", "😔", "😌", "😇", "🥲", "😴",
    # Activity / learning
    "🚶", "🏃", "📖", "📚", "📓", "🎨", "🎵", "🎣", "🧗",
    # Technology
    "💻", "📱", "⌚", "🖥️",
    # Medical
    "⚕️", "🩺", "💊", "🧬",
    # More nature
    "🐢", "🦌", "🐝", "🌊", "🌻",
    # Misc
    "🔑",
]

AVATAR_PAGE_SIZE = 20  # 4 rows x 5 columns

def _avatar_page_count():
    return (len(AVATAR_EMOJIS) + AVATAR_PAGE_SIZE - 1) // AVATAR_PAGE_SIZE

async def show_avatar_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Show a paginated grid of emojis for the user to select as an avatar"""
    query = update.callback_query
    await query.answer()

    page_count = _avatar_page_count()
    page = max(0, min(page, page_count - 1))
    start = page * AVATAR_PAGE_SIZE
    emojis = AVATAR_EMOJIS[start:start + AVATAR_PAGE_SIZE]

    keyboard = []
    # 5 emojis per row
    for i in range(0, len(emojis), 5):
        row = [InlineKeyboardButton(e, callback_data=f"set_avatar_{e}") for e in emojis[i:i + 5]]
        keyboard.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"avatar_page_{page - 1}"))
    if page_count > 1:
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data="noop"))
    if page < page_count - 1:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"avatar_page_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("Remove Emoji", callback_data="clear_avatar")])
    keyboard.append([InlineKeyboardButton("Back to Profile", callback_data="profile")])

    text = (
        "*Select Avatar Emoji*\n\n"
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
            loading_msg = await update.callback_query.message.edit_text("Loading your posts...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("Loading your posts...")
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
        "SELECT * FROM posts WHERE author_id = %s AND approved = TRUE AND deleted = FALSE ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    )
    
    total_posts_row = db_fetch_one(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE AND deleted = FALSE",
        (user_id,)
    )
    total_posts = total_posts_row['count'] if total_posts_row else 0
    total_pages = (total_posts + per_page - 1) // per_page
    
    if not posts:
        # Show empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No posts found")
            await asyncio.sleep(0.5)
        
        text = "*My Posts*\n\nYou haven't posted anything yet or your posts are pending approval."
        keyboard = [
            [InlineKeyboardButton("Share My Thoughts", callback_data='ask')],
            [InlineKeyboardButton("Back to My Content", callback_data='my_content_menu')],
            [InlineKeyboardButton("Main Menu", callback_data='menu')]
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
                await update.message.reply_text("Error loading your posts. Please try again.")
        return
    
    # Show posts as clickable buttons
    text = f"*My Posts* ({total_posts} total)\n\n*Click on a post to view details:*\n\n"
    
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
        button_text = f"#{post_number} - {clean_snippet} ({comment_count})"
        
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
            pagination_row.append(InlineKeyboardButton("Previous", callback_data=f"my_posts_{page-1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        # Current page indicator (non-clickable)
        pagination_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        
        # Next page button
        if page < total_pages:
            pagination_row.append(InlineKeyboardButton("Next", callback_data=f"my_posts_{page+1}"))
        else:
            pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
        
        keyboard.append(pagination_row)
    
    # Add navigation buttons
    keyboard.append([
        InlineKeyboardButton("Back to My Content", callback_data='my_content_menu'),
        InlineKeyboardButton("Main Menu", callback_data='menu')
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
                await loading_msg.edit_text("Error loading your posts. Please try again.")
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
            loading_msg = await update.callback_query.message.edit_text("Loading menu...")
    except:
        pass
    
    keyboard = [
        [InlineKeyboardButton("My Posts", callback_data='my_posts_1')],
        [InlineKeyboardButton("My Comments", callback_data='my_comments_1')],
        [InlineKeyboardButton("Main Menu", callback_data='menu')]
    ]
    
    text = "*My Content*\n\nChoose what you want to view:"
    
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
            await update.message.reply_text("Error loading content menu. Please try again.")

# NEW: Function to show a single post with action buttons
async def view_post(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int, from_page=1):
    """Show a specific post with action buttons"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Show typing animation
    await typing_animation(context, chat_id, 0.3)
    
    # Show animated loading
    loading_msg = await query.message.edit_text("Loading post details...")
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
        f"*Post Details*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Post ID:** \\#{post['post_id']}\n"
        f"**Categories:** {escaped_categories}\n"
        f"**Posted on:** {escape_markdown(timestamp, version=2)}\n"
        f"**Comments:** {comment_count}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Content:**\n\n"
        f"{escaped_content}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Create action buttons for this post
    keyboard = [
        [InlineKeyboardButton("View Comments", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton("Continue Thread", callback_data=f"continue_post_{post_id}")],
        [
            InlineKeyboardButton("Delete Post", callback_data=f"delete_post_{post_id}_{from_page}"),
            InlineKeyboardButton("Back to List", callback_data=f"my_posts_{from_page}")
        ],
        [
            InlineKeyboardButton("Back to My Content", callback_data='my_content_menu'),
            InlineKeyboardButton("Main Menu", callback_data='menu')
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
            loading_msg = await update.callback_query.message.edit_text("Loading your comments...")
        elif hasattr(update, 'message') and update.message:
            loading_msg = await update.message.reply_text("Loading your comments...")
    except:
        pass
    
    # Animate loading
    if loading_msg:
        await animated_loading(loading_msg, "Searching comments", 2)
    
    user_id = str(update.effective_user.id)
    
    per_page = 10
    offset = (page - 1) * per_page
    
    # Get user's comments with post info (p.category removed - multi-category migration)
    comments = db_fetch_all('''
        SELECT c.*, p.content as post_content, p.post_id
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
        
        text = "*My Comments*\n\nYou haven't made any comments yet\\."
        keyboard = [
            [InlineKeyboardButton("Back to My Content", callback_data='my_content_menu')],
            [InlineKeyboardButton("Main Menu", callback_data='menu')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        safe_page = escape_markdown(str(page), version=2)
        safe_total_pages = escape_markdown(str(total_pages), version=2)
        text = f"*My Comments* \\(Page {safe_page}/{safe_total_pages}\\)\n\n"
        
        for idx, comment in enumerate(comments):
            comment_num = (page - 1) * per_page + idx + 1
            safe_num = escape_markdown(str(comment_num), version=2)
            
            # Truncate content
            comment_preview = comment['content'][:80] + '...' if len(comment['content']) > 80 else comment['content']
            safe_comment_preview = escape_markdown(comment_preview, version=2)
            
            text += f"*{safe_num}\\.* {safe_comment_preview}\n\n"

        
        # Build keyboard
        keyboard = []
        
        # Add pagination
        if total_pages > 1:
            pagination_row = []
            
            if page > 1:
                pagination_row.append(InlineKeyboardButton("Previous", callback_data=f"my_comments_{page-1}"))
            else:
                pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
            
            pagination_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
            
            if page < total_pages:
                pagination_row.append(InlineKeyboardButton("Next", callback_data=f"my_comments_{page+1}"))
            else:
                pagination_row.append(InlineKeyboardButton("•", callback_data="noop"))
            
            keyboard.append(pagination_row)
        
        # Add navigation buttons
        keyboard.append([
            InlineKeyboardButton("My Posts", callback_data='my_posts_1'),
            InlineKeyboardButton("Back to My Content", callback_data='my_content_menu')
        ])
        keyboard.append([InlineKeyboardButton("Main Menu", callback_data='menu')])
        
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
            await update.message.reply_text("Error loading your comments. Please try again.")


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


# Tracks running live-monitor jobs so a second admin (or the same one re-opening
# a stale message) doesn't stack duplicate repeating jobs on the same message.
LIVE_MONITOR_JOBS = {}


async def show_admin_chats_list(update, context, page=1):
    query = update.callback_query
    admin_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    per_page = 8
    offset = (page - 1) * per_page
    convos = get_admin_conversations(limit=per_page, offset=offset)
    total = get_admin_conversations_count()
    total_pages = max(1, (total + per_page - 1) // per_page)

    kb = []
    if not convos:
        text = "*Chat Monitor*\n\nNo private conversations yet\\."
    else:
        lines = [f"*Chat Monitor* \\(Page {page}/{total_pages}\\)\n"]
        for c in convos:
            name_a = c['name_a'] or 'Anon'
            name_b = c['name_b'] or 'Anon'
            preview = (c['last_content'] or f"[{c['last_media_type'] or 'media'}]")[:40]
            lines.append(
                f"{escape_markdown(name_a, version=2)} ↔ {escape_markdown(name_b, version=2)}\n"
                f"{c['msg_count']} msgs — _{escape_markdown(preview, version=2)}_\n"
            )
            kb.append([InlineKeyboardButton(
                f"{name_a} ↔ {name_b}",
                callback_data=f"admin_chat_view_{c['user_a']}_{c['user_b']}_1"
            )])
        text = "\n".join(lines)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=f"admin_chats_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"admin_chats_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("Admin Panel", callback_data='admin_panel')])

    try:
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error showing admin chats: {e}")


def _format_transcript_text(user_a, user_b, live=False):
    msgs = get_admin_conversation_transcript(user_a, user_b, limit=40)
    name_a_row = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_a,))
    name_b_row = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_b,))
    name_a = name_a_row['anonymous_name'] if name_a_row else 'Anon'
    name_b = name_b_row['anonymous_name'] if name_b_row else 'Anon'

    header = "*LIVE*" if live else "*Transcript*"
    lines = [f"{header}: {escape_markdown(name_a, version=2)} ↔ {escape_markdown(name_b, version=2)}\n"]
    if live:
        lines.append("_auto\\-refreshing every 8s_\n")
    if not msgs:
        lines.append("_No messages yet\\._")
    else:
        for m in msgs:
            sender_label = name_a if str(m['sender_id']) == str(user_a) else name_b
            content = m['content'] or f"[{m.get('media_type') or 'media'}]"
            ts = m['timestamp']
            ts_str = ts[11:16] if isinstance(ts, str) else ts.strftime('%H:%M')
            lines.append(
                f"*{escape_markdown(sender_label, version=2)}* `{ts_str}`\n"
                f"{escape_markdown(content[:300], version=2)}\n"
            )
    text = "\n".join(lines)
    return text[-4000:] if len(text) > 4000 else text


async def show_admin_chat_transcript(update, context, user_a, user_b, page=1, live=False):
    query = update.callback_query
    admin_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    text = _format_transcript_text(user_a, user_b, live=live)
    live_label = "Stop Live" if live else "Go Live"
    live_cb = f"admin_chat_stoplive_{user_a}_{user_b}" if live else f"admin_chat_golive_{user_a}_{user_b}"
    kb = [
        [InlineKeyboardButton("Refresh", callback_data=f"admin_chat_view_{user_a}_{user_b}_{page}"),
         InlineKeyboardButton(live_label, callback_data=live_cb)],
        [InlineKeyboardButton("Chat List", callback_data='admin_chats_1')]
    ]
    try:
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.error(f"Error rendering transcript: {e}")


async def _live_monitor_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    d = job.data
    text = _format_transcript_text(d['user_a'], d['user_b'], live=True)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Stop Live", callback_data=f"admin_chat_stoplive_{d['user_a']}_{d['user_b']}")],
        [InlineKeyboardButton("Chat List", callback_data='admin_chats_1')]
    ])
    try:
        await context.bot.edit_message_text(
            chat_id=d['chat_id'], message_id=d['message_id'], text=text,
            reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2
        )
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            pass
        elif "not found" in msg or "can't be edited" in msg:
            job.schedule_removal()
            LIVE_MONITOR_JOBS.pop((d['chat_id'], d['message_id']), None)
        else:
            logger.error(f"Live monitor tick error: {e}")
    except Exception as e:
        logger.error(f"Live monitor tick error: {e}")


async def start_live_monitor(update, context, user_a, user_b):
    query = update.callback_query
    admin_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,))
    if not user or not user['is_admin']:
        await query.answer("No permission.", show_alert=True)
        return

    chat_id = query.message.chat_id
    message_id = query.message.message_id
    key = (chat_id, message_id)

    if key in LIVE_MONITOR_JOBS:
        LIVE_MONITOR_JOBS[key].schedule_removal()
        del LIVE_MONITOR_JOBS[key]

    job = context.application.job_queue.run_repeating(
        _live_monitor_tick, interval=8, first=0,
        data={'chat_id': chat_id, 'message_id': message_id, 'user_a': user_a, 'user_b': user_b},
        name=f"live_monitor_{chat_id}_{message_id}"
    )
    LIVE_MONITOR_JOBS[key] = job
    await query.answer("Live monitoring started")


async def stop_live_monitor(update, context, user_a, user_b):
    query = update.callback_query
    key = (query.message.chat_id, query.message.message_id)
    if key in LIVE_MONITOR_JOBS:
        LIVE_MONITOR_JOBS[key].schedule_removal()
        del LIVE_MONITOR_JOBS[key]
    await query.answer("Live monitoring stopped")
    await show_admin_chat_transcript(update, context, user_a, user_b, live=False)

async def show_admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    """Show paginated pending reports to admin."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    per_page = 5
    offset = (page - 1) * per_page
    reports = get_pending_reports(offset=offset, limit=per_page)

    total_row = db_fetch_one("SELECT COUNT(*) as cnt FROM reports WHERE status = 'pending'")
    total = total_row['cnt'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    nav_keyboard = []

    if not reports:
        text = "*Pending Reports*\n\nNo pending reports at this time."
        nav_keyboard = [[InlineKeyboardButton("Admin Panel", callback_data='admin_panel')]]
        try:
            if query:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Error showing empty reports: {e}")
        return

    lines = [f"*Pending Reports* \\(Page {page}/{total_pages}\\)\n"]
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
            f"*Report \\#{rep['report_id']}* \\- {type_label}\n"
            f"_{safe_preview}_\n"
            f"By: {safe_reporter}\n"
            f"Reason: {safe_reason}\n"
        )
        keyboard.append([
            InlineKeyboardButton("View", callback_data=f"report_view_{rep['report_id']}"),
            InlineKeyboardButton("Dismiss", callback_data=f"report_dismiss_{rep['report_id']}"),
            InlineKeyboardButton("Delete Content", callback_data=f"report_delete_{rep['report_id']}"),
            InlineKeyboardButton("Warn User", callback_data=f"report_warn_{rep['report_id']}"),
        ])

    # Pagination row
    pag_row = []
    if page > 1:
        pag_row.append(InlineKeyboardButton("Prev", callback_data=f"admin_reports_{page - 1}"))
    pag_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        pag_row.append(InlineKeyboardButton("Next", callback_data=f"admin_reports_{page + 1}"))
    if pag_row:
        keyboard.append(pag_row)
    keyboard.append([InlineKeyboardButton("Admin Panel", callback_data='admin_panel')])

    text = "\n".join(lines)
    try:
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error showing admin reports: {e}")
        try:
            back = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data='admin_panel')]])
            if query:
                await query.message.reply_text("Error loading reports.", reply_markup=back)
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
            f"*New Report \\#{report_id}*\n"
            f"Type: {type_label}\n"
            f"Reason: {safe_reason}\n"
            f"By: {safe_name}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Review Reports", callback_data='admin_reports')]
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
        reaction_label = "liked" if reaction_type == 'like' else "disliked"
        reaction_icon = "👍" if reaction_type == 'like' else "👎"
        
        notification_text = (
            f"{reaction_icon} *New Interaction\\!*\n\n"
            f"{escape_markdown(reactor_display, version=2)} *{reaction_label}* your comment\\:\n\n"
            f"_{escape_markdown((comment['content'] or '[media]')[:150], version=2)}_\n\n"
            f"*Post Context\\:*\n{escape_markdown(post_preview, version=2)}\n\n"
            f"[View Discussion](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
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
                "*Select categories (you can choose multiple):*",
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
            try:
                await query.message.edit_reply_markup(reply_markup=new_markup)
            except BadRequest as e:
                # Telegram raises this if the markup happens to be identical
                # to what's already shown (e.g. rapid double-taps) - safe to ignore
                if "not modified" not in str(e).lower():
                    raise
            
            # Answer callback to remove loading state
            await query.answer()
            return

        elif query.data == "cat_reset":
            context.user_data['selected_categories'] = set()
            new_markup = build_multi_category_keyboard(set())
            try:
                await query.message.edit_reply_markup(reply_markup=new_markup)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
            await query.answer("Selection reset", show_alert=False)

        elif query.data == "cat_done":
            selected = context.user_data.get('selected_categories', set())
            if not selected:
                await query.answer("Please select at least one category.", show_alert=True)
                return

            # If the user got here from "Edit Categories" on an existing preview,
            # just update the category on that pending post and go back to the preview —
            # don't discard their already-typed content and ask them to retype it.
            if context.user_data.get('editing_categories_for_pending'):
                del context.user_data['editing_categories_for_pending']
                pending_post = context.user_data.get('pending_post')
                await query.answer("Categories updated")
                try:
                    await query.message.delete()
                except Exception:
                    pass
                if not pending_post:
                    await query.message.reply_text(
                        "Post data not found. Please start over.",
                        reply_markup=get_main_menu(user_id)
                    )
                    return

                pending_post['category'] = ','.join(selected)
                context.user_data['pending_post'] = pending_post

                fake_update = SimpleNamespace(
                    callback_query=None,
                    message=query.message,
                    effective_user=update.effective_user,
                    effective_chat=update.effective_chat
                )
                await send_post_confirmation(
                    fake_update, context,
                    pending_post['content'], pending_post['category'],
                    pending_post.get('media_type', 'text'), pending_post.get('media_id'),
                    thread_from_post_id=pending_post.get('thread_from_post_id'),
                    explicit=pending_post.get('explicit', False)
                )
                return

            # Check if this is a thread continuation
            user_data = db_fetch_one("SELECT thread_context_post_id FROM users WHERE user_id = %s", (user_id,))
            if user_data and user_data.get('thread_context_post_id'):
                context.user_data['thread_from_post_id'] = user_data['thread_context_post_id']
            
            # Store selected categories in user's DB record
            db_execute(
                "UPDATE users SET selected_categories = %s, waiting_for_post = TRUE WHERE user_id = %s",
                (','.join(selected), user_id)
            )
            
            await query.message.reply_text(
                f"*Selected: {', '.join(selected)}*\n\nNow send your post content (text, photo, or voice).",
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
            # Navigating away cancels any in-progress report
            if 'reporting' in context.user_data:
                del context.user_data['reporting']
            await query.answer("Opening Menu...", show_alert=False)
            await query.message.reply_text(
                "Main Menu\nUse the buttons below:",
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
            await query.answer("Input cancelled")
            
            # Try to delete the input prompt message if it's an inline message
            try:
                await query.message.delete()
            except: pass
            
            return

        elif query.data == 'profile':
            # Navigating away cancels any in-progress report
            if 'reporting' in context.user_data:
                del context.user_data['reporting']
            await query.answer("Loading Profile...", show_alert=False)
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data == 'leaderboard':
            await query.answer("Loading Leaderboard...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            await show_leaderboard(update, context)

        elif query.data == 'settings':
            # Navigating away cancels any in-progress report
            if 'reporting' in context.user_data:
                del context.user_data['reporting']
            await query.answer("Loading Settings...", show_alert=False)
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

        elif query.data == 'privacy_settings':
            await show_privacy_settings(update, context)

        elif query.data.startswith('toggle_hide_'):
            metric = query.data.replace('toggle_hide_', '')
            col = f"hide_{metric}"
            
            # Simple toggle logic
            current = db_fetch_one(f"SELECT {col} FROM users WHERE user_id = %s", (user_id,))
            if current:
                new_val = not current[col]
                db_execute(f"UPDATE users SET {col} = %s WHERE user_id = %s", (new_val, user_id))
                status = "Hidden" if new_val else "Visible"
                await query.answer(f"{metric.replace('_', ' ').title()} is now {status}", show_alert=False)
            
            await show_privacy_settings(update, context)

        elif query.data == 'help':
            await query.answer("Loading Help...", show_alert=False)
            help_text = (
                "*የዚህ ቦት አጠቃቀም:*\n"
                "•  menu button በመጠቀም የተለያዩ አማራጮችን ማየት ይችላሉ.\n"
                "• 'Share My Thoughts' የሚለውን በመንካት በፈለጉት ነገር ጥያቄም ሆነ ሃሳብ መጻፍ ይችላሉ.\n"
                "•  category ወይም መደብ በመምረጥ በ ጽሁፍ፣ ፎቶ እና ድምጽ ሃሳቦን ማንሳት ይችላሉ.\n"
                "• እርስዎ ባነሱት ሃሳብ ላይ ሌሎች ሰዎች አስተያየት መጻፍ ይችላሉ\n"
                "• View your profile የሚለውን በመንካት ስም፣ ጾታዎን መቀየር እንዲሁም እርስዎን የሚከተሉ ሰዎች ብዛት ማየት ይችላሉ.\n"
                "• በተነሱ ጥያቄዎች ላይ ከቻናሉ comments የሚለድን በመጫን አስተያየትዎን መጻፍ ይችላሉ."
            )
            keyboard = [[InlineKeyboardButton("Main Menu", callback_data='menu')]]
            await query.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'about':
            await query.answer("Loading About...", show_alert=False)
            about_text = (
                "Creator: Yididiya Tamiru\n\n"
                "Telegram: @YIDIDIYATAMIRUU\n"
                "This bot helps you share your thoughts anonymously with the Christian community."
            )
            keyboard = [[InlineKeyboardButton("Main Menu", callback_data='menu')]]
            await query.message.reply_text(about_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

        elif query.data == 'edit_name':
            await query.answer("Renaming...", show_alert=False)
            db_execute(
                "UPDATE users SET awaiting_name = TRUE WHERE user_id = %s",
                (user_id,)
            )
            await query.message.reply_text(
                "Please type your new anonymous name:\n\nTap Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

        elif query.data == 'edit_bio':
            await query.answer("Opening Bio Editor...", show_alert=False)
            db_execute(
                "UPDATE users SET awaiting_bio = TRUE WHERE user_id = %s",
                (user_id,)
            )
            await query.message.reply_text(
                "*Please type your new bio:*\n\nKeep it short and interesting (max 150 chars).\n\nTap Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

        elif query.data == 'edit_sex':
            await query.answer("Changing sex...", show_alert=False)
            btns = [
                [InlineKeyboardButton("Male", callback_data='sex_male')],
                [InlineKeyboardButton("Female", callback_data='sex_female')],
                [InlineKeyboardButton("Remove/Hide Sex", callback_data='sex_hide')]
            ]
            await query.message.reply_text("Select your sex:", reply_markup=InlineKeyboardMarkup(btns))

        elif query.data.startswith('sex_'):
            if query.data == 'sex_male':
                sex = '👨'
            elif query.data == 'sex_female':
                sex = '👩'
            elif query.data == 'sex_hide':
                sex = '👤'
            else:
                sex = '👤'  # fallback
            
            db_execute(
                "UPDATE users SET sex = %s WHERE user_id = %s",
                (sex, user_id)
            )
            await query.message.reply_text("Sex updated!")
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data.startswith(('follow_', 'unfollow_')):
            await query.answer("Updating Follow...", show_alert=False)
            target_uid = query.data.split('_', 1)[1]
            if query.data.startswith('follow_'):
                try:
                    db_execute(
                        "INSERT INTO followers (follower_id, followed_id) VALUES (%s, %s)",
                        (user_id, target_uid)
                    )
                    # Notify the followed user if they have notifications enabled
                    followed_user = db_fetch_one(
                        "SELECT notifications_enabled FROM users WHERE user_id = %s", (target_uid,)
                    )
                    if followed_user and followed_user['notifications_enabled']:
                        follower_data = db_fetch_one(
                            "SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (user_id,)
                        )
                        if follower_data:
                            follower_name = follower_data.get('avatar_emoji') or ''
                            follower_name = f"{follower_name} {follower_data['anonymous_name']}".strip()
                            try:
                                await context.bot.send_message(
                                    chat_id=target_uid,
                                    text=(
                                        f"*New Follower!*\n"
                                        f"*{follower_name}* started following you.\n"
                                        f"View their profile: /start profileid_{user_id}"
                                    ),
                                    parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception as notify_err:
                                logger.warning(f"Could not notify user {target_uid} of follow: {notify_err}")
                except psycopg2.IntegrityError:
                    pass
            else:
                db_execute(
                    "DELETE FROM followers WHERE follower_id = %s AND followed_id = %s",
                    (user_id, target_uid)
                )
            calculate_user_rating.cache_clear()
            await query.message.reply_text("Successfully updated!")
            await send_updated_profile(target_uid, query.message.chat.id, context)
        
        elif query.data.startswith('list_followers_'):
            # Show paginated list of users who follow the current user
            try:
                page = int(query.data.split('_')[2])
            except (IndexError, ValueError):
                page = 1
            per_page = 10
            offset = (page - 1) * per_page
            rows = db_fetch_all(
                "SELECT u.user_id, u.anonymous_name, u.avatar_emoji FROM followers f "
                "JOIN users u ON f.follower_id = u.user_id "
                "WHERE f.followed_id = %s ORDER BY u.anonymous_name LIMIT %s OFFSET %s",
                (user_id, per_page, offset)
            )
            total_row = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM followers WHERE followed_id = %s", (user_id,)
            )
            total = total_row['cnt'] if total_row else 0
            total_pages = max(1, (total + per_page - 1) // per_page)

            if not rows:
                await query.answer("You have no followers yet.", show_alert=True)
            else:
                keyboard = []
                for r in rows:
                    label = f"{r['avatar_emoji']} {r['anonymous_name']}".strip() if r.get('avatar_emoji') else r['anonymous_name']
                    keyboard.append([InlineKeyboardButton(label, url=f"https://t.me/{context.bot.username}?start=profileid_{r['user_id']}" )])
                nav = []
                if page > 1:
                    nav.append(InlineKeyboardButton("Prev", callback_data=f"list_followers_{page-1}"))
                if page < total_pages:
                    nav.append(InlineKeyboardButton("Next", callback_data=f"list_followers_{page+1}"))
                if nav:
                    keyboard.append(nav)
                keyboard.append([InlineKeyboardButton("Back to Profile", callback_data="profile")])
                await query.message.edit_text(
                    f"*Your Followers* (Page {page}/{total_pages})\n_{total} total_",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )

        elif query.data.startswith('list_following_'):
            # Show paginated list of users the current user follows
            try:
                page = int(query.data.split('_')[2])
            except (IndexError, ValueError):
                page = 1
            per_page = 10
            offset = (page - 1) * per_page
            rows = db_fetch_all(
                "SELECT u.user_id, u.anonymous_name, u.avatar_emoji FROM followers f "
                "JOIN users u ON f.followed_id = u.user_id "
                "WHERE f.follower_id = %s ORDER BY u.anonymous_name LIMIT %s OFFSET %s",
                (user_id, per_page, offset)
            )
            total_row = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM followers WHERE follower_id = %s", (user_id,)
            )
            total = total_row['cnt'] if total_row else 0
            total_pages = max(1, (total + per_page - 1) // per_page)

            if not rows:
                await query.answer("You are not following anyone yet.", show_alert=True)
            else:
                keyboard = []
                for r in rows:
                    label = f"{r['avatar_emoji']} {r['anonymous_name']}".strip() if r.get('avatar_emoji') else r['anonymous_name']
                    keyboard.append([InlineKeyboardButton(label, url=f"https://t.me/{context.bot.username}?start=profileid_{r['user_id']}" )])
                nav = []
                if page > 1:
                    nav.append(InlineKeyboardButton("Prev", callback_data=f"list_following_{page-1}"))
                if page < total_pages:
                    nav.append(InlineKeyboardButton("Next", callback_data=f"list_following_{page+1}"))
                if nav:
                    keyboard.append(nav)
                keyboard.append([InlineKeyboardButton("Back to Profile", callback_data="profile")])
                await query.message.edit_text(
                    f"*Following* (Page {page}/{total_pages})\n_{total} total_",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )

        elif query.data.startswith('revealexplicit_'):
            try:
                parts = query.data.split('_')
                post_id = int(parts[1])
                page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                await query.answer()
                await show_comments_menu(update, context, post_id, page=page, force_reveal=True)
            except Exception as e:
                logger.error(f"RevealExplicit error: {e}")
                await query.answer("Error loading post", show_alert=True)

        elif query.data.startswith('viewcomments_'):
            await query.answer("Loading comments...", show_alert=False)
            try:
                parts = query.data.split('_')
                if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                    post_id = int(parts[1])
                    page = int(parts[2])
                    await show_comments_page(update, context, post_id, page)
            except Exception as e:
                logger.error(f"ViewComments error: {e}")
                await query.answer("Error loading comments")
  
        elif query.data.startswith('writecomment_'):
            await query.answer("Opening writer...", show_alert=False)
            post_id_str = query.data.split('_', 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = TRUE, comment_post_id = %s WHERE user_id = %s",
                    (post_id, user_id)
                )
                
                await query.message.reply_text(
                    "Type your comment, or send a voice message, GIF, or sticker.\n\nTap Cancel to return to the menu.",
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
                    is_existing_like = existing_reaction['type'] not in ('dislike', '👎', '😡')
                    is_new_like = reaction_type == 'like'
                    if is_existing_like == is_new_like:
                        # User is clicking the same reaction group - remove it (toggle off)
                        db_execute(
                            "DELETE FROM reactions WHERE comment_id = %s AND user_id = %s",
                            (comment_id, user_id)
                        )
                    else:
                        # User is changing reaction group - update it
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

                # Get updated counts
                likes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type NOT IN ('dislike', '👎', '😡')",
                    (comment_id,)
                )
                likes = likes_row['cnt'] if likes_row else 0
                
                dislikes_row = db_fetch_one(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type IN ('dislike', '👎', '😡')",
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

                like_emoji = "👍"
                dislike_emoji = "👎"

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
                                InlineKeyboardButton("Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
                                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
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
                                InlineKeyboardButton("Edit", callback_data=f"edit_comment_{comment_id}"),
                                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
                            ])
                        else:
                            kb_buttons.append([
                                InlineKeyboardButton("Delete", callback_data=f"delete_comment_{comment_id}")
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
                await query.answer("Error updating reaction", show_alert=True)

        # NEW: Handle edit comment
        elif query.data.startswith("edit_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
            
            if comment and comment['author_id'] == user_id:
                if comment['type'] != 'text':
                    await query.answer("Only text comments can be edited", show_alert=True)
                    return
                    
                context.user_data['editing_comment'] = comment_id
                
                # Message 1: ONLY the copyable content
                content_escaped = html.escape(comment['content'])
                
                await query.message.reply_text(
                    f"<pre>{content_escaped}</pre>",
                    parse_mode=ParseMode.HTML
                )
                
                # Message 2: Instructions
                await query.message.reply_text(
                    "<b>Edit your comment</b>\n\n"
                    "Make your changes and send the <b>entire corrected comment</b> as a new message.\n\n"
                    "Tap Cancel to abort.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Cancel", callback_data='cancel_input')]
                    ]),
                    parse_mode=ParseMode.HTML
                )
                return
            else:
                await query.answer("You can only edit your own comments", show_alert=True)

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
                
                await query.answer("Comment deleted")
                await query.message.delete()
                
                # Update comment count with orphan check
                await adopt_orphaned_replies(context, post_id)
            else:
                await query.answer("You can only delete your own comments", show_alert=True)

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
                            InlineKeyboardButton("Yes, Delete", callback_data=f"confirm_delete_post_{post_id}_{from_page}"),
                            InlineKeyboardButton("Cancel", callback_data=f"cancel_delete_post_{post_id}_{from_page}")
                        ]
                    ])
                    
                    await query.message.edit_text(
                        "*Delete Post*\n\nAre you sure you want to delete this post? This action cannot be undone.",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await query.answer("You can only delete your own posts", show_alert=True)
            except Exception as e:
                logger.error(f"Error in delete_post handler: {e}")
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith("confirm_delete_post_"):
            try:
                parts = query.data.split('_')
                post_id = int(parts[3])
                from_page = int(parts[4]) if len(parts) > 4 else 1
                
                post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                
                if post and post['author_id'] == user_id:
                    if post['channel_message_id']:
                        try:
                            if post.get('vent_number'):
                                vent_display = f"Vent - {post['vent_number']:03d}"
                            else:
                                vent_display = "Vent"

                            cats_row = db_fetch_all("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,))
                            categories = [row['category_code'] for row in cats_row]
                            hashtags = ' '.join([f"#{cat}" for cat in categories]) if categories else "#Other"
                            safe_hashtags = html.escape(hashtags)
                            deletion_notice = "This content has been deleted by the author."

                            channel_text = (
                                f"<code>{vent_display}</code>\n\n"
                                f"{deletion_notice}\n\n"
                                f"━━━━━━━━━━━━━━━\n"
                                f"{safe_hashtags}\n"
                                f"<a href='https://t.me/christianvent'>Telegram</a> | <a href='https://t.me/{BOT_USERNAME}'>Bot</a>"
                            )

                            comment_count = post.get('comment_count') or 0
                            keyboard = InlineKeyboardMarkup([
                                [InlineKeyboardButton(f"Add/view Comments ({comment_count})",
                                    url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                            ])

                            if post.get('media_type', 'text') == 'text':
                                await context.bot.edit_message_text(
                                    chat_id=CHANNEL_ID, message_id=post['channel_message_id'],
                                    text=channel_text, parse_mode=ParseMode.HTML,
                                    reply_markup=keyboard, disable_web_page_preview=True
                                )
                            else:
                                await context.bot.edit_message_caption(
                                    chat_id=CHANNEL_ID, message_id=post['channel_message_id'],
                                    caption=channel_text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                                )
                        except Exception as e:
                            logger.error(f"Error editing channel message: {e}")
                    
                    db_execute("UPDATE posts SET deleted = TRUE WHERE post_id = %s", (post_id,))
                    
                    await query.answer("Post deleted successfully")
                    await query.message.edit_text(
                        "Post has been deleted successfully.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    
                    # Return to the post list at the same page
                    await show_previous_posts(update, context, from_page)
                else:
                    await query.answer("You can only delete your own posts", show_alert=True)
            except Exception as e:
                logger.error(f"Error deleting post: {e}")
                await query.answer("Error deleting post", show_alert=True)

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
                await query.answer("You cannot chat with yourself.", show_alert=True)
                return

            # Check for existing request
            existing = db_fetch_one(
                "SELECT status, timestamp FROM chat_requests WHERE sender_id = %s AND receiver_id = %s",
                (user_id, target_id)
            )
            
            if existing:
                if existing['status'] == 'accepted':
                    await query.answer("Request already accepted!", show_alert=False)
                    db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
                    await query.message.reply_text("Type your message below:", reply_markup=cancel_menu)
                    return

                # Still pending. Rather than block the sender forever if the receiver
                # simply missed the original notification, allow a one-tap reminder
                # once enough time has passed since the last ping.
                REQUEST_REMINDER_COOLDOWN_HOURS = 24
                last_sent = existing.get('timestamp')
                hours_since = None
                if last_sent:
                    if isinstance(last_sent, str):
                        try:
                            last_sent = datetime.strptime(last_sent, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            last_sent = None
                    if last_sent:
                        hours_since = (datetime.now() - last_sent).total_seconds() / 3600

                if hours_since is None or hours_since < REQUEST_REMINDER_COOLDOWN_HOURS:
                    hours_left = REQUEST_REMINDER_COOLDOWN_HOURS - (hours_since or 0)
                    await query.answer(
                        f"Request already sent — still waiting on a response "
                        f"(you can send a reminder in ~{max(1, round(hours_left))}h). "
                        f"They can find it anytime in their Chat Requests menu.",
                        show_alert=True
                    )
                    return

                # Cooldown has passed — bump the timestamp and re-notify as a reminder.
                db_execute(
                    "UPDATE chat_requests SET timestamp = CURRENT_TIMESTAMP WHERE sender_id = %s AND receiver_id = %s",
                    (user_id, target_id)
                )
                await query.answer("🔔 Reminder sent!", show_alert=False)

                sender_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                sender_name = get_display_name(sender_data)
                reminder_text = (
                    f"*Chat Request Reminder\\!*\n"
                    f"_{escape_markdown(sender_name, version=2)}_ still wants to chat with you\\."
                )
                reminder_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Accept", callback_data=f'acceptchat_{user_id}'),
                        InlineKeyboardButton("❌ Ignore", callback_data=f'declinechat_{user_id}')
                    ],
                    [InlineKeyboardButton("View Profile", url=f'https://t.me/{BOT_USERNAME}?start=profileid_{user_id}')]
                ])
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=reminder_text,
                        reply_markup=reminder_kb,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                except Exception as e:
                    logger.error(f"Failed to send chat request reminder: {e}")
                return

            # Create new request
            try:
                db_execute(
                    "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'pending')",
                    (user_id, target_id)
                )
                await query.answer("Chat request sent!", show_alert=False)
                
                # Notify receiver
                sender_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                sender_name = get_display_name(sender_data)
                
                receiver_text = (
                    f"*New Chat Request\\!*\n"
                    f"_{escape_markdown(sender_name, version=2)}_ wants to chat with you\\.\n\n"
                    f"_You can find this anytime under Settings ➜ 📨 Chat Requests\\._"
                )
                receiver_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Accept", callback_data=f'acceptchat_{user_id}'),
                        InlineKeyboardButton("❌ Ignore", callback_data=f'declinechat_{user_id}')
                    ],
                    [InlineKeyboardButton("View Profile", url=f'https://t.me/{BOT_USERNAME}?start=profileid_{user_id}')]
                ])
                
                await context.bot.send_message(
                    chat_id=target_id,
                    text=receiver_text,
                    reply_markup=receiver_kb,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except Exception as e:
                logger.error(f"ChatRequest error: {e}")
                await query.answer("Failed to send request.", show_alert=True)

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
                    text=f"*{escape_markdown(receiver_name, version=2)}* accepted your chat request\\! You can now send messages from their profile\\.",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            except: pass

        elif query.data.startswith('declinechat_'):
            sender_id = query.data.split('_')[1]
            db_execute("DELETE FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (sender_id, user_id))
            await query.answer("Request ignored.", show_alert=False)
            await query.message.edit_text("❌ *Chat request ignored\\.*", parse_mode=ParseMode.MARKDOWN_V2)

        elif query.data == 'chat_requests':
            await query.answer()
            await show_chat_requests(update, context, page=1)

        elif query.data.startswith('chat_requests_'):
            try:
                page = int(query.data.split('_')[2])
            except (IndexError, ValueError):
                page = 1
            await query.answer()
            await show_chat_requests(update, context, page=page)

        elif query.data.startswith('reqaccept_') or query.data.startswith('reqreject_'):
            try:
                parts = query.data.split('_')
                is_accept = query.data.startswith('reqaccept_')
                sender_id = parts[1]
                page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

                if sender_id == user_id:
                    await query.answer("Invalid request.", show_alert=True)
                    return

                if is_accept:
                    db_execute(
                        "UPDATE chat_requests SET status = 'accepted' WHERE sender_id = %s AND receiver_id = %s",
                        (sender_id, user_id)
                    )
                    # Mutual chat permission, mirroring the acceptchat_ flow
                    db_execute(
                        "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'accepted') ON CONFLICT DO NOTHING",
                        (user_id, sender_id)
                    )
                    await query.answer("✅ Request accepted!", show_alert=False)

                    receiver_data = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
                    receiver_name = get_display_name(receiver_data)
                    try:
                        await context.bot.send_message(
                            chat_id=sender_id,
                            text=f"*{escape_markdown(receiver_name, version=2)}* accepted your chat request\\! You can now send messages from their profile\\.",
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                    except Exception:
                        pass
                else:
                    db_execute(
                        "DELETE FROM chat_requests WHERE sender_id = %s AND receiver_id = %s",
                        (sender_id, user_id)
                    )
                    await query.answer("❌ Request rejected.", show_alert=False)

                # Refresh the list in place so the user can keep working through it
                await show_chat_requests(update, context, page=page)
            except Exception as e:
                logger.error(f"Error in reqaccept/reqreject handler: {e}")
                await query.answer("Error processing request. Please try again.", show_alert=True)

        elif query.data.startswith('message_'):
            target_id = query.data.split('_')[1]
            check = db_fetch_one("SELECT status FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (user_id, target_id))
            
            if not check or check['status'] != 'accepted':
                await query.answer("You must send a chat request first!", show_alert=True)
                return

            await query.answer("Opening Chat...", show_alert=False)
            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            await query.message.reply_text("*Please type your private message:*\n\nTap Cancel to return to menu.", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)
        
        elif query.data.startswith('reply_msg_'):
            # Existing reply logic (requires accepted chat as well)
            target_id = query.data[len('reply_msg_'):]
            if not target_id or not target_id.isdigit():
                await query.answer("Invalid ID", show_alert=True)
                return
                
            check = db_fetch_one("""
                SELECT 1 FROM chat_requests 
                WHERE (sender_id = %s AND receiver_id = %s AND status = 'accepted')
                   OR (sender_id = %s AND receiver_id = %s AND status = 'accepted')
            """, (user_id, target_id, target_id, user_id))
            pm_check = db_fetch_one("""
                SELECT 1 FROM private_messages 
                WHERE (sender_id = %s AND receiver_id = %s)
                   OR (sender_id = %s AND receiver_id = %s)
            """, (user_id, target_id, target_id, user_id))
            
            if not check and not pm_check:
                await query.answer("No active chat permission.", show_alert=True)
                return

            db_execute("UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s", (target_id, user_id))
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            await query.message.reply_text(f"*Replying to {target_user['anonymous_name']}*\n\nPlease send your text,voice or picturemessage:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)

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
                    "Please type your reply or send a voice message, GIF, or sticker:\n\nTap Cancel to return to menu.",
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
                    "Please type your reply or send a voice message, GIF, or sticker:\n\nTap Cancel to return to menu.",
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
                await query.answer("Error loading more replies", show_alert=True)
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
            await query.answer("Loading your posts...", show_alert=False)
            await typing_animation(context, query.message.chat_id, 0.3)
            try:
                page = int(query.data.split('_')[2])
                await show_previous_posts(update, context, page)
            except (IndexError, ValueError):
                await show_previous_posts(update, context, 1)

        elif query.data == 'my_posts':
            await show_previous_posts(update, context, 1)

        elif query.data.startswith("viewpost_"):
            await query.answer("Loading vent...", show_alert=False)
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
                await query.answer("Error loading post", show_alert=True)

        elif query.data.startswith('my_comments_'):
            await query.answer("Loading your comments...", show_alert=False)
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
                            [InlineKeyboardButton("View in Post", callback_data=f"viewcomments_{post['post_id']}_1")],
                            [InlineKeyboardButton("Delete Comment", callback_data=f"delete_comment_{comment_id}")],
                            [InlineKeyboardButton("Back to My Comments", callback_data='my_comments')]
                        ]
                        
                        # Show comment details
                        comment_preview = comment['content'][:200] + '...' if len(comment['content']) > 200 else comment['content']
                        post_preview = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                        
                        text = (
                            f"*Comment Details*\n\n"
                            f"**Post:** {escape_markdown(post_preview, version=2)}\n\n"
                            f"**Your Comment:**\n{escape_markdown(comment_preview, version=2)}\n\n"
                            f"**Posted on:** {comment['timestamp'].strftime('%Y-%m-%d %H:%M') if not isinstance(comment['timestamp'], str) else comment['timestamp'][:16]}"
                        )
                        
                        await query.message.edit_text(
                            text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.MARKDOWN_V2
                        )
                else:
                    await query.answer("Comment not found or not yours", show_alert=True)
            except Exception as e:
                logger.error(f"Error viewing comment: {e}")
                await query.answer("Error viewing comment", show_alert=True)

        # UPDATED: Handle continue post (threading) - renamed from elaborate
        elif query.data.startswith("continue_post_"):
            post_id = int(query.data.split('_')[2])
            post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
            
            if post and post['author_id'] == user_id:
                context.user_data['thread_from_post_id'] = post_id
                # Save to DB for persistence
                db_execute("UPDATE users SET thread_context_post_id = %s WHERE user_id = %s", (post_id, user_id))
                # Use multi-category selection
                context.user_data['selected_categories'] = set()
                await query.message.reply_text(
                    "*Select categories for your continuation (you can choose multiple):*",
                    reply_markup=build_multi_category_keyboard(set()),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.answer("You can only continue your own posts", show_alert=True)
        
        elif query.data.startswith("replypage_"):
            parts = query.data.split("_")
            if len(parts) == 5:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                reply_page = int(parts[3])
                comment_page = int(parts[4])
                await show_comments_page(update, context, post_id, comment_page, reply_pages={comment_id: reply_page})
            return

        elif query.data in ('post_explicit_yes', 'post_explicit_no'):
            pending = context.user_data.get('pending_explicit_check')
            if not pending:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return
            await query.answer()
            explicit_flag = query.data == 'post_explicit_yes'
            del context.user_data['pending_explicit_check']

            # Remove the Yes/No buttons from the question message
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # Send the preview as a fresh message (not an edit) so photo/voice posts render correctly
            fake_update = SimpleNamespace(
                callback_query=None,
                message=query.message,
                effective_user=update.effective_user,
                effective_chat=update.effective_chat
            )
            await send_post_confirmation(
                fake_update, context,
                pending['content'], pending['category'],
                pending.get('media_type', 'text'), pending.get('media_id'),
                thread_from_post_id=pending.get('thread_from_post_id'),
                explicit=explicit_flag
            )
            return

        elif query.data == 'edit_categories':
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return

            if time.time() - pending_post.get('timestamp', 0) > 300:
                try:
                    await query.message.edit_text("Edit time expired. Please start a new post.")
                except BadRequest:
                    await query.message.edit_caption("Edit time expired. Please start a new post.")
                del context.user_data['pending_post']
                await query.answer()
                return

            await query.answer()

            # Pre-fill the category picker with whatever is currently selected
            current_categories = pending_post.get('category', '')
            selected = set(c.strip() for c in current_categories.split(',') if c.strip())
            context.user_data['selected_categories'] = selected

            # Flag that we're revising categories for a post that already has content,
            # so cat_done should return straight to the preview instead of asking to retype it.
            context.user_data['editing_categories_for_pending'] = True

            # Remove the buttons on the stale preview so it can't be submitted while categories are being edited
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            await query.message.reply_text(
                "*Update categories* (you can choose multiple):\n\nYour post text is kept as is.",
                reply_markup=build_multi_category_keyboard(selected),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif query.data == 'select_thread_post':
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return

            if time.time() - pending_post.get('timestamp', 0) > 300:
                try:
                    await query.message.edit_text("Edit time expired. Please start a new post.")
                except BadRequest:
                    await query.message.edit_caption("Edit time expired. Please start a new post.")
                del context.user_data['pending_post']
                await query.answer()
                return

            await query.answer()

            # Pull the user's most recent approved posts to thread from
            recent_posts = db_fetch_all(
                "SELECT post_id, content, vent_number FROM posts "
                "WHERE author_id = %s AND approved = TRUE AND deleted = FALSE "
                "ORDER BY timestamp DESC LIMIT 6",
                (user_id,)
            )

            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            if not recent_posts:
                await query.message.reply_text(
                    "You don't have any previous posts yet to thread from.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Preview", callback_data='thread_pick_cancel')]
                    ])
                )
                return

            thread_kb = []
            for p in recent_posts:
                label = p['content'][:40] + ('…' if len(p['content']) > 40 else '')
                num = p.get('vent_number')
                prefix = f"Vent-{num:03d}: " if num else ""
                thread_kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"thread_pick_{p['post_id']}")])

            thread_kb.append([InlineKeyboardButton("No Thread (Standalone)", callback_data="thread_pick_none")])
            thread_kb.append([InlineKeyboardButton("Back to Preview", callback_data="thread_pick_cancel")])

            await query.message.reply_text(
                "*Thread to Previous Post*\n\nPick one of your recent posts to continue as a thread, "
                "or keep this post standalone:",
                reply_markup=InlineKeyboardMarkup(thread_kb),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        elif query.data == 'clear_thread_post':
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return

            await query.answer("Thread removed")
            pending_post['thread_from_post_id'] = None
            context.user_data['pending_post'] = pending_post

            fake_update = SimpleNamespace(
                callback_query=None,
                message=query.message,
                effective_user=update.effective_user,
                effective_chat=update.effective_chat
            )
            await send_post_confirmation(
                fake_update, context,
                pending_post['content'], pending_post['category'],
                pending_post.get('media_type', 'text'), pending_post.get('media_id'),
                thread_from_post_id=None,
                explicit=pending_post.get('explicit', False)
            )
            return

        elif query.data.startswith('thread_pick_'):
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return

            choice = query.data[len('thread_pick_'):]
            await query.answer()

            try:
                await query.message.delete()
            except Exception:
                pass

            new_thread_id = None
            if choice == 'cancel':
                new_thread_id = pending_post.get('thread_from_post_id')
            elif choice == 'none':
                new_thread_id = None
            elif choice.isdigit():
                candidate_id = int(choice)
                owned_post = db_fetch_one(
                    "SELECT post_id FROM posts WHERE post_id = %s AND author_id = %s AND approved = TRUE AND deleted = FALSE",
                    (candidate_id, user_id)
                )
                if owned_post:
                    new_thread_id = candidate_id
                else:
                    await query.message.reply_text("That post is no longer available to thread from.")
                    new_thread_id = pending_post.get('thread_from_post_id')

            pending_post['thread_from_post_id'] = new_thread_id
            context.user_data['pending_post'] = pending_post

            fake_update = SimpleNamespace(
                callback_query=None,
                message=query.message,
                effective_user=update.effective_user,
                effective_chat=update.effective_chat
            )
            await send_post_confirmation(
                fake_update, context,
                pending_post['content'], pending_post['category'],
                pending_post.get('media_type', 'text'), pending_post.get('media_id'),
                thread_from_post_id=new_thread_id,
                explicit=pending_post.get('explicit', False)
            )
            return

        elif query.data in ('edit_post', 'cancel_post', 'confirm_post'):
            pending_post = context.user_data.get('pending_post')
            if not pending_post:
                # Handle both text and media messages
                try:
                    await query.message.edit_text("Post data not found. Please start over.")
                except BadRequest:
                    try:
                        await query.message.edit_caption("Post data not found. Please start over.")
                    except:
                        await query.message.reply_text("Post data not found. Please start over.")
                return
            
            if query.data == 'edit_post':
                if time.time() - pending_post.get('timestamp', 0) > 300:
                    # Handle both text and media messages for expiration
                    try:
                        await query.message.edit_text("Edit time expired. Please start a new post.")
                    except BadRequest:
                        await query.message.edit_caption("Edit time expired. Please start a new post.")
                    del context.user_data['pending_post']
                    return
                    
                # Store that we're in edit mode
                context.user_data['editing_post'] = True
                
                # Message 1: ONLY the copyable content — nothing else in this bubble,
                # so selecting/copying the whole message can't drag in any instruction text.
                content_escaped = html.escape(pending_post['content'])
                
                await query.message.reply_text(
                    f"<pre>{content_escaped}</pre>",
                    parse_mode=ParseMode.HTML
                )
                
                # Message 2: Instructions (kept separate from the content on purpose)
                await query.message.reply_text(
                    "<b>Edit your post</b>\n\n"
                    "Tap the box above to copy just your text, make your changes, then send the "
                    "<b>entire corrected post</b> back here as a new message.\n\n"
                    "Tap Cancel to abort.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Cancel", callback_data='cancel_input')]
                    ]),
                    parse_mode=ParseMode.HTML
                )
                return
            
            elif query.data == 'cancel_post':
                # Handle both text and media messages for cancellation
                try:
                    await query.message.edit_text("Post cancelled.")
                except BadRequest:
                    await query.message.edit_caption("Post cancelled.")
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
                    loading_msg = await query.message.edit_text("Submitting your post...")
                except BadRequest:
                    loading_msg = await query.message.edit_caption("Submitting your post...")
                
                await animated_loading(loading_msg, "Processing", 3)
                
                pending_post = context.user_data.get('pending_post')
                if not pending_post:
                    # Handle both text and media for error
                    try:
                        await loading_msg.edit_text("Post data not found. Please start over.")
                    except:
                        await loading_msg.edit_caption("Post data not found. Please start over.")
                    return
                
                category = pending_post['category']
                post_content = pending_post['content']
                media_type = pending_post.get('media_type', 'text')
                media_id = pending_post.get('media_id')
                thread_from_post_id = pending_post.get('thread_from_post_id')
                explicit_flag = pending_post.get('explicit', False)
                
                # Insert post (without 'category' column which was dropped)
                if thread_from_post_id:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, media_type, media_id, thread_from_post_id, explicit) VALUES (%s, %s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id, thread_from_post_id, explicit_flag),
                        fetchone=True
                    )
                else:
                    post_row = db_execute(
                        "INSERT INTO posts (content, author_id, media_type, media_id, explicit) VALUES (%s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id, explicit_flag),
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
                        success_msg = await loading_msg.edit_text("Post submitted for approval!")
                    except:
                        success_msg = await loading_msg.edit_caption("Post submitted for approval!")
                    
                    await asyncio.sleep(1)
                    
                    keyboard = [[InlineKeyboardButton("Main Menu", callback_data='menu')]]
                    try:
                        await success_msg.edit_text(
                            "Your post has been submitted for admin approval!\nYou'll be notified when it's approved and published.",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    except:
                        await success_msg.edit_caption(
                            "Your post has been submitted for admin approval!\nYou'll be notified when it's approved and published.",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                else:
                    try:
                        await loading_msg.edit_text("Failed to submit post. Please try again.")
                    except:
                        await loading_msg.edit_caption("Failed to submit post. Please try again.")
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
                await query.answer("Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in approve_post handler: {e}")
                await query.answer("Error approving post", show_alert=True)

        elif query.data.startswith('toggle_explicit_'):
            try:
                post_id = int(query.data.split('_')[-1])
                logger.info(f"Admin {user_id} toggling explicit flag on post {post_id}")
                await toggle_post_explicit(update, context, post_id)
            except ValueError:
                await query.answer("Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in toggle_post_explicit handler: {e}")
                await query.answer("Error toggling explicit flag", show_alert=True)
        # Admin broadcast handlers
        elif query.data == 'admin_broadcast':
            await start_broadcast(update, context)
            
        elif query.data == 'admin_weekly_tools':
            await show_admin_weekly_tools(update, context)
            
        elif query.data == 'weekly_test':
            await weekly_test_callback(update, context)

        elif query.data == 'weekly_force':
            await weekly_force_callback(update, context)

        elif query.data == 'weekly_last':
            await weekly_last_callback(update, context)

        elif query.data == 'weekly_fix_schedule':
            await weekly_fix_schedule(update, context)
            
        elif query.data == 'weekly_status':
            await weekly_status_callback(update, context)
            
        elif query.data == 'admin_panel':
            await admin_panel(update, context)
            await query.answer()
            
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
                await query.answer("Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in reject_post handler: {e}")
                await query.answer("Error rejecting post", show_alert=True)

        elif query.data.startswith('reject_with_reason_'):
            try:
                post_id = int(query.data.split('_')[-1])
                context.user_data['awaiting_rejection_reason'] = True
                context.user_data['rejecting_post'] = post_id
                await query.edit_message_text(
                    "*Provide Rejection Reason*\n\nPlease type the reason for rejection and send it as a message.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Error in reject_with_reason_ handler: {e}")
                await query.answer("Error processing request", show_alert=True)
                
        elif query.data.startswith('skip_rejection_'):
            try:
                post_id = int(query.data.split('_')[-1])
                await finalize_rejection(update, context, post_id, reason=None)
            except Exception as e:
                logger.error(f"Error in skip_rejection_ handler: {e}")
                await query.answer("Error skipping reason", show_alert=True)
                
        elif query.data == 'cancel_rejection':
            context.user_data.pop('rejecting_post', None)
            context.user_data.pop('awaiting_rejection_reason', None)
            try:
                await query.edit_message_text("Rejection cancelled.")
                await admin_panel(update, context)
            except Exception as e:
                logger.error(f"Error in cancel_rejection handler: {e}")
                await query.message.reply_text("Rejection cancelled.")
                await admin_panel(update, context)
        
        elif query.data == 'inbox':
            await show_inbox(update, context, 1)
            
        elif query.data.startswith('inbox_page_'):
            try:
                page = int(query.data.split('_')[2])
                await show_inbox(update, context, page)
            except (IndexError, ValueError):
                await show_inbox(update, context, 1)

        elif query.data.startswith('open_conv_'):
            # open_conv_<sender_id>_<list_page>              -> open that person's thread at page 1
            # open_conv_<sender_id>_<list_page>_<thread_page> -> open a specific page of that thread
            try:
                parts = query.data.split('_')
                sender_id = parts[2]
                list_page = int(parts[3]) if len(parts) > 3 else 1
                thread_page = int(parts[4]) if len(parts) > 4 else 1
                await show_conversation(update, context, sender_id, thread_page, list_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing open_conv: {e}")
                await show_inbox(update, context, 1)

        elif query.data.startswith('view_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 5:
                    message_id = int(parts[2])
                    sender_id = parts[3]
                    from_page = int(parts[4])
                    await view_individual_message(update, context, message_id, sender_id, from_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing view_message: {e}")
                await query.answer("Error loading message", show_alert=True)
                
        elif query.data == 'mark_all_read':
            await mark_all_read(update, context)
            
        elif query.data.startswith('delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 4:
                    message_id = int(parts[2])
                    sender_id = parts[3]
                    from_page = int(parts[4]) if len(parts) > 4 else 1
                    list_page = int(parts[5]) if len(parts) > 5 else 1
                    await delete_message(update, context, message_id, sender_id, from_page, list_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing delete_message: {e}")
                await query.answer("Error", show_alert=True)
                
        elif query.data.startswith('confirm_delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 5:
                    message_id = int(parts[3])
                    sender_id = parts[4]
                    from_page = int(parts[5]) if len(parts) > 5 else 1
                    list_page = int(parts[6]) if len(parts) > 6 else 1
                    await confirm_delete_message(update, context, message_id, sender_id, from_page, list_page)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing confirm_delete: {e}")
                await query.answer("Error", show_alert=True)
                
        elif query.data.startswith('cancel_delete_message_'):
            try:
                parts = query.data.split('_')
                if len(parts) >= 5:
                    sender_id = parts[4]
                    from_page = int(parts[5]) if len(parts) > 5 else 1
                    list_page = int(parts[6]) if len(parts) > 6 else 1
                    await show_conversation(update, context, sender_id, from_page, list_page)
                else:
                    await show_inbox(update, context, 1)
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
            await show_avatar_selection(update, context, page=0)

        elif query.data.startswith('avatar_page_'):
            page = int(query.data.split('_')[2])
            await show_avatar_selection(update, context, page=page)

        elif query.data == 'noop':
            await query.answer()

        elif query.data.startswith('set_avatar_'):
            emoji = query.data.split('_', 2)[2]
            db_execute("UPDATE users SET avatar_emoji = %s WHERE user_id = %s", (emoji, user_id))
            await query.answer(f"Avatar set to {emoji}!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'clear_avatar':
            db_execute("UPDATE users SET avatar_emoji = NULL WHERE user_id = %s", (user_id,))
            await query.answer("Avatar removed!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'list_blocked':
            await query.answer("Loading blocked users...", show_alert=False)
            blocked = db_fetch_all(
                """SELECT u.user_id, u.anonymous_name, u.sex 
                FROM blocks b JOIN users u ON b.blocked_id = u.user_id 
                WHERE b.blocker_id = %s""",
                (user_id,)
            )
            
            if not blocked:
                await query.message.edit_text(
                    "*Your Block List is Empty*",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Settings", callback_data='settings')]]),
                    parse_mode=ParseMode.MARKDOWN
                )
                return
                
            text = "*Your Blocked Users*\n\n"
            kb = []
            for b_user in blocked:
                name = get_display_name(b_user)
                text += f"• {escape_markdown(name, version=2)}\n"
                kb.append([InlineKeyboardButton(f"Unblock {name}", callback_data=f"unblock_user_{b_user['user_id']}")])
            
            kb.append([InlineKeyboardButton("Back to Settings", callback_data='settings')])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)

        elif query.data.startswith('unblock_user_'):
            target_id = query.data.split('_', 2)[2]
            db_execute("DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (user_id, target_id))
            
            # Clear Aura Cache for real-time accuracy
            calculate_user_rating.cache_clear()
            format_aura.cache_clear()
            
            await query.answer("User unblocked!", show_alert=False)
            
            # Refresh view (either profiles or list)
            if "Blocked Users" in query.message.text:
                # If we are in the list, refresh the list
                blocked = db_fetch_all(
                    "SELECT u.user_id, u.anonymous_name, u.sex FROM blocks b JOIN users u ON b.blocked_id = u.user_id WHERE b.blocker_id = %s",
                    (user_id,)
                )
                if not blocked:
                    await query.message.edit_text("List empty.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data='settings')]]))
                else:
                    text = "*Your Blocked Users (Updated)*\n\n"
                    kb = []
                    for b_user in blocked:
                        name = get_display_name(b_user)
                        text += f"• {escape_markdown(name, version=2)}\n"
                        kb.append([InlineKeyboardButton(f"Unblock {name}", callback_data=f"unblock_user_{b_user['user_id']}")])
                    kb.append([InlineKeyboardButton("Back", callback_data='settings')])
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                # If we are in a message or profile, show success and button refresh
                await query.message.reply_text("User has been unblocked.")
                # We can't easily refresh the profile here without sender data, so a simple message is enough or let user re-open.

        elif query.data.startswith('block_user_'):
            target_id = query.data.split('_', 2)[2]

            # Don't block silently — ask for confirmation first
            target_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (target_id,))
            target_name = get_display_name(target_user) if target_user else "this user"
            safe_name = escape_markdown(target_name, version=2)

            text = (
                f"*Block {safe_name}?*\n\n"
                f"They won't be able to send you messages anymore\\. "
                f"You can unblock them later from Settings\\."
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Yes, Block", callback_data=f"confirm_block_user_{target_id}"),
                    InlineKeyboardButton("Cancel", callback_data=f"cancel_block_user_{target_id}")
                ]
            ])
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)

        elif query.data.startswith('confirm_block_user_'):
            target_id = query.data.split('_', 3)[3]

            # Add to blocks table
            try:
                db_execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_id, target_id)
                )
                
                # Clear Aura Cache for real-time accuracy
                calculate_user_rating.cache_clear()
                format_aura.cache_clear()

                await query.answer("User blocked", show_alert=False)
                await query.message.edit_text("User has been blocked. They can no longer send you messages.")

            except psycopg2.IntegrityError:
                await query.answer("Already blocked", show_alert=False)
                await query.message.edit_text("User is already blocked.")

        elif query.data.startswith('cancel_block_user_'):
            await query.answer("Cancelled", show_alert=False)
            await query.message.edit_text("No changes made — that user hasn't been blocked.")

        # ==================== REPORTING CALLBACKS ====================

        elif query.data.startswith('report_post_'):
            try:
                post_id = int(query.data.split('_')[2])
                post = db_fetch_one("SELECT post_id FROM posts WHERE post_id = %s", (post_id,))
                if not post:
                    await query.answer("Post not found.", show_alert=True)
                    return
                context.user_data['reporting'] = {'type': 'post', 'id': post_id, 'timestamp': time.time()}
                await query.answer()
                await query.message.reply_text(
                    "*Report Post*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
            except Exception as e:
                logger.error(f"Error in report_post handler: {e}")
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('report_comment_'):
            try:
                comment_id = int(query.data.split('_')[2])
                comment = db_fetch_one("SELECT comment_id FROM comments WHERE comment_id = %s", (comment_id,))
                if not comment:
                    await query.answer("Comment not found.", show_alert=True)
                    return
                context.user_data['reporting'] = {'type': 'comment', 'id': comment_id, 'timestamp': time.time()}
                await query.answer()
                await query.message.reply_text(
                    "*Report Comment*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
            except Exception as e:
                logger.error(f"Error in report_comment handler: {e}")
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('admin_chats_'):
            try:
                page = int(query.data.split('_')[2])
            except (IndexError, ValueError):
                page = 1
            await show_admin_chats_list(update, context, page)

        elif query.data.startswith('admin_chat_view_'):
            parts = query.data.split('_')
            user_a, user_b, page = parts[3], parts[4], int(parts[5])
            await show_admin_chat_transcript(update, context, user_a, user_b, page=page)

        elif query.data.startswith('admin_chat_golive_'):
            parts = query.data.split('_')
            await start_live_monitor(update, context, parts[3], parts[4])

        elif query.data.startswith('admin_chat_stoplive_'):
            parts = query.data.split('_')
            await stop_live_monitor(update, context, parts[3], parts[4])

        elif query.data == 'admin_reports':
            await query.answer("Loading reports...", show_alert=False)
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
                    await query.answer("Report not found.", show_alert=True)
                    return
                preview, author_id = get_report_content_preview(report['target_type'], report['target_id'])
                type_label = "Post" if report['target_type'] == 'post' else "Comment"
                preview_text = html.escape(preview or '[Content deleted]')
                safe_reason = html.escape(report['reason'])
                reporter = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (report['reporter_id'],))
                reporter_name = html.escape(reporter['anonymous_name'] if reporter else 'Anonymous')
                view_text = (
                    f"<b>Report #{report_id}</b>\n"
                    f"Type: {type_label}\n"
                    f"Reporter: {reporter_name}\n"
                    f"Reason: {safe_reason}\n\n"
                    f"<b>Content Preview:</b>\n{preview_text}"
                )
                keyboard = [
                    [
                        InlineKeyboardButton("Dismiss", callback_data=f"report_dismiss_{report_id}"),
                        InlineKeyboardButton("Delete Content", callback_data=f"report_delete_{report_id}"),
                    ],
                    [InlineKeyboardButton("Warn User", callback_data=f"report_warn_{report_id}")],
                    [InlineKeyboardButton("Back to Reports", callback_data='admin_reports')]
                ]
                try:
                    await query.edit_message_text(view_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
                except Exception:
                    await query.message.reply_text(view_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Error in report_view handler: {e}")
                await query.answer("Error loading report", show_alert=True)

        elif query.data.startswith('report_dismiss_'):
            try:
                report_id = int(query.data.split('_')[2])
                resolve_report(report_id, user_id, 'dismissed', None)
                await query.answer("Report dismissed.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_dismiss handler: {e}")
                await query.answer("Error dismissing report", show_alert=True)

        elif query.data.startswith('report_delete_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = db_fetch_one("SELECT * FROM reports WHERE report_id = %s", (report_id,))
                if not report:
                    await query.answer("Report not found.", show_alert=True)
                    return
        
                target_type = report['target_type']
                target_id = report['target_id']
                author_id = None
        
                if target_type == 'post':
                    # ---------- DELETE POST ----------
                    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (target_id,))
                    if not post:
                        await query.answer("Post already deleted.", show_alert=True)
                        return
        
                    # 1. Try to delete or hide channel message
                    if post.get('channel_message_id'):
                        try:
                            await context.bot.delete_message(
                                chat_id=CHANNEL_ID,
                                message_id=post['channel_message_id']
                            )
                            logger.info(f"Deleted channel message for post {target_id}")
                        except Exception as e:
                            logger.error(f"Failed to delete channel message: {e}")
                            # Fallback: edit the message to show it's removed
                            try:
                                await context.bot.edit_message_text(
                                    chat_id=CHANNEL_ID,
                                    message_id=post['channel_message_id'],
                                    text="*This content has been removed by an admin.*",
                                    parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=None
                                )
                            except Exception as edit_err:
                                logger.error(f"Also failed to edit channel message: {edit_err}")
        
                    # 2. Delete all associated data (comments, reactions, categories)
                    db_execute("DELETE FROM reactions WHERE comment_id IN (SELECT comment_id FROM comments WHERE post_id = %s)", (target_id,))
                    db_execute("DELETE FROM comments WHERE post_id = %s", (target_id,))
                    db_execute("DELETE FROM post_categories WHERE post_id = %s", (target_id,))
                    # 3. Delete the post itself, verify it's gone
                    deleted = db_execute("DELETE FROM posts WHERE post_id = %s RETURNING post_id", (target_id,), fetchone=True)
                    if not deleted:
                        raise Exception("Post deletion from database failed (no rows returned)")
        
                    author_id = post.get('author_id')
                    logger.info(f"Post {target_id} deleted by admin {user_id}")
        
                elif target_type == 'comment':
                    # ---------- DELETE COMMENT ----------
                    comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (target_id,))
                    if not comment:
                        await query.answer("Comment already deleted.", show_alert=True)
                        return
        
                    post_id = comment['post_id']
                    # 1. Re‑parent child comments to top level
                    db_execute("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (target_id,))
                    # 2. Delete reactions and the comment itself
                    db_execute("DELETE FROM reactions WHERE comment_id = %s", (target_id,))
                    deleted = db_execute("DELETE FROM comments WHERE comment_id = %s RETURNING comment_id", (target_id,), fetchone=True)
                    if not deleted:
                        raise Exception("Comment deletion from database failed (no rows returned)")
        
                    # 3. Update comment count and channel button
                    await adopt_orphaned_replies(context, post_id)
        
                    author_id = comment.get('author_id')
                    logger.info(f"Comment {target_id} deleted by admin {user_id}")
        
                else:
                    await query.answer("Unknown target type.", show_alert=True)
                    return
        
                # ---------- AFTER DELETION: update report, clear caches, notify author ----------
                resolve_report(report_id, user_id, 'action_taken', 'deleted')
        
                # Clear aura caches (important for leaderboard updates)
                calculate_user_rating.cache_clear()
                format_aura.cache_clear()
        
                # Notify the content author (if we have an author_id and it's not the admin themselves)
                if author_id and str(author_id) != str(user_id):
                    try:
                        await context.bot.send_message(
                            chat_id=author_id,
                            text="Your content was reviewed and removed by an admin due to a community report. Please ensure your posts follow our community guidelines."
                        )
                    except Exception as notify_err:
                        logger.warning(f"Could not notify author {author_id}: {notify_err}")
        
                # Success feedback
                await query.answer("Content deleted.", show_alert=False)
                await show_admin_reports(update, context, page=1)
        
            except Exception as e:
                logger.error(f"Error in report_delete handler: {e}", exc_info=True)
                await query.answer(f"Deletion failed: {str(e)[:50]}", show_alert=True)
        elif query.data.startswith('report_warn_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = db_fetch_one("SELECT * FROM reports WHERE report_id = %s", (report_id,))
                if not report:
                    await query.answer("Report not found.", show_alert=True)
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
                                "Warning from Admin \n\n"
                                "Your content has been reported and reviewed by an admin. "
                                "Please ensure your posts and comments follow our community guidelines.\n\n"
                                "Repeated violations may result in content removal or other actions."
                            ),
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        pass
                await query.answer("Warning sent to user.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_warn handler: {e}")
                await query.answer("Error sending warning", show_alert=True)

        # ==================== END REPORTING CALLBACKS ====================
            
    except Exception as e:
        logger.error(f"Error in button_handler: {e}")
        try:
            await query.message.reply_text("An error occurred. Please try again.")
        except:
            pass

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (user_id,))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
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
        "*Bot Statistics*\n\n"
        f"Total Users: {stats['total_users']}\n"
        f"Approved Posts: {stats['approved_posts']}\n"
        f"Pending Posts: {stats['pending_posts']}\n"
        f"Total Comments: {stats['total_comments']}\n"
        f"Private Messages: {stats['total_messages']}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data='admin_panel')]
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
            await update.message.reply_text("Error loading statistics.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("Error loading statistics.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.effective_user.id)
    user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
    

    # Handle cancel command or main menu buttons while in an input state
    main_menu_buttons = ["Share", "Chat Requests", "Profile", "Posts", "Top", "Settings", "Open App", "❌ Cancel", "/cancel"]
    
    if text in main_menu_buttons or text.lower() in ("cancel", "❌ cancel"):
        # UNCONDITIONALLY reset all waiting states when a menu button is pressed
        # We pass None for chat_id to reset quietly, as we'll send the specific menu next
        await reset_user_waiting_states(user_id, None, context)
        
        # Reload user object from DB to ensure subsequent flags are FALSE
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        
        # Early exit for explicit cancellation
        if text in ["❌ Cancel", "/cancel"] or text.lower() in ("cancel", "❌ cancel"):
            await update.message.reply_text(
                "Input cancelled.",
                reply_markup=get_main_menu(user_id)
            )
            return
        
        # For other main menu buttons (e.g. "Share"), we fall through 
        # so the handlers below can process the command with a clean state.

    # NEW: Handle rejection reason capture from admin
    if context.user_data.get('awaiting_rejection_reason'):
        if text in main_menu_buttons: return
        post_id = context.user_data.get('rejecting_post')
        if post_id:
            logger.info(f"Admin {user_id} providing reason for post {post_id}")
            await finalize_rejection(update, context, post_id, reason=text)
            return

    # NEW: Handle report reason capture from user
    # IMPORTANT: comment flow always wins. If the user is mid-comment
    # (waiting_for_comment is TRUE in the DB) a lingering 'reporting' state
    # must NOT hijack their message — fall through and let the
    # waiting_for_comment branch further down handle it instead.
    if context.user_data.get('reporting') and not (user and user['waiting_for_comment']):
        if text in main_menu_buttons: return
        reporting = context.user_data.get('reporting')

        # Expire stale reporting state so it can never linger indefinitely
        started_at = reporting.get('timestamp', 0)
        if time.time() - started_at > REPORTING_TIMEOUT_SECONDS:
            del context.user_data['reporting']
            await update.message.reply_text(
                "Your report request timed out after 5 minutes. Tap Report again if you still want to report this.",
                reply_markup=get_main_menu(user_id)
            )
            return

        try:
            reason = text.strip() if text else ""

            if not reason:
                await update.message.reply_text(
                    "Please provide a reason (at least 1 character). Tap Report again to retry.",
                    reply_markup=get_main_menu(user_id)
                )
                return

            if len(reason) > 200:
                await update.message.reply_text(
                    "Reason is too long (max 200 characters). Tap Report again to retry.",
                    reply_markup=get_main_menu(user_id)
                )
                return

            target_type = reporting['type']
            target_id = reporting['id']

            report_id = create_report(user_id, target_type, target_id, reason)

            if report_id is None:
                await update.message.reply_text(
                    "You have already reported this content. An admin will review it.",
                    reply_markup=get_main_menu(user_id)
                )
            elif report_id == -1:
                await update.message.reply_text(
                    "You've reached the daily report limit (5 per day). Please try again tomorrow.",
                    reply_markup=get_main_menu(user_id)
                )
            else:
                await update.message.reply_text(
                    "Thank you. An admin will review your report.",
                    reply_markup=get_main_menu(user_id)
                )
                # Notify admin of new report
                await notify_admin_of_new_report(context, report_id, user_id, target_type, reason)
        finally:
            # ALWAYS clear reporting state here — success, failure, or
            # invalid input — so it can never linger into the next message.
            if 'reporting' in context.user_data:
                del context.user_data['reporting']
        return

    
    # Rest of your handle_message code...

    # NEW: Handle comment editing

    if 'editing_comment' in context.user_data:
        if text in main_menu_buttons: return
        comment_id = context.user_data['editing_comment']
        comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (comment_id,))
        
        if comment and comment['author_id'] == user_id and comment['type'] == 'text':
            # Guard against users accidentally pasting our own "copy the text below"
            # instruction along with the content they meant to edit.
            cleaned_text, was_cleaned = sanitize_pasted_edit(text)

            if was_cleaned:
                # Don't save silently — let the user confirm what actually got cleaned up.
                del context.user_data['editing_comment']
                context.user_data['pending_comment_edit'] = {
                    'comment_id': comment_id,
                    'content': cleaned_text,
                    'timestamp': time.time()
                }
                await update.message.reply_text(
                    "Looks like our copy instructions got pasted in too — here's your comment with those trimmed out:\n\n"
                    f"<pre>{html.escape(cleaned_text)}</pre>\n\n"
                    "Save this?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Save", callback_data='confirm_comment_edit'),
                         InlineKeyboardButton("Edit Again", callback_data='redo_comment_edit')],
                        [InlineKeyboardButton("Cancel", callback_data='cancel_input')]
                    ])
                )
                return

            # Update the comment
            db_execute(
                "UPDATE comments SET content = %s WHERE comment_id = %s",
                (cleaned_text, comment_id)
            )
            
            # Clean up
            del context.user_data['editing_comment']
            
            await update.message.reply_text(
                "Comment updated successfully!",
                reply_markup=get_main_menu(user_id)
            )
            return
        else:
            del context.user_data['editing_comment']
            await update.message.reply_text(
                "Error updating comment. Please try again.",
                reply_markup=get_main_menu(user_id)
            )
            return


    # FIX: Handle pending post editing (NEW CODE STARTS HERE)
    if 'editing_post' in context.user_data and context.user_data['editing_post']:
        if text in main_menu_buttons: return
        pending_post = context.user_data.get('pending_post')
        if pending_post:
            # Guard against users accidentally pasting our own "copy the text below"
            # instruction along with the content they meant to edit.
            cleaned_text, was_cleaned = sanitize_pasted_edit(text)
            if was_cleaned:
                await update.message.reply_text(
                    "Looks like our copy instructions got pasted in too — I've trimmed those out. "
                    "Check the preview below before submitting."
                )

            # Update the pending post content
            pending_post['content'] = cleaned_text
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
                pending_post.get('thread_from_post_id'),
                explicit=pending_post.get('explicit', False)
            )
            return
        else:
            del context.user_data['editing_post']
            await update.message.reply_text(
                "No pending post found. Please start over.",
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
    if not thread_from_post_id:
        # Fallback to database
        user_db = db_fetch_one("SELECT thread_context_post_id FROM users WHERE user_id = %s", (user_id,))
        if user_db and user_db.get('thread_context_post_id'):
            thread_from_post_id = user_db['thread_context_post_id']
            context.user_data['thread_from_post_id'] = thread_from_post_id
    
    if user and user['waiting_for_post']:
        if text in main_menu_buttons: return
        category = user.get('selected_categories')
        if not category:
            category = user.get('selected_category') # Fallback for transition
            
        if not category:
            await update.message.reply_text("No categories selected. Please start over.", reply_markup=get_main_menu(user_id))
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
            elif update.message.audio:
                audio = update.message.audio
                media_id = audio.file_id
                media_type = 'audio'
                post_content = update.message.caption or ""
            else:
                # Unsupported media type — let the user know instead of silently dropping it
                await update.message.reply_text(
                    "That file type isn't supported for vents yet. "
                    "You can share text, a photo, a voice note, or a music/audio file.",
                    reply_markup=get_main_menu(user_id)
                )
                return

            
            # FIX: Reset user state for BOTH text and media posts
            db_execute(
                "UPDATE users SET waiting_for_post = FALSE, selected_categories = NULL, selected_category = NULL WHERE user_id = %s",
                (user_id,)
            )
            
            # Ask whether the post contains explicit content before showing the preview
            context.user_data['pending_explicit_check'] = {
                'content': post_content,
                'category': category,
                'media_type': media_type,
                'media_id': media_id,
                'thread_from_post_id': thread_from_post_id,
            }
            explicit_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("No, safe for everyone", callback_data='post_explicit_no'),
                    InlineKeyboardButton("Yes, explicit", callback_data='post_explicit_yes')
                ]
            ])
            await update.message.reply_text(
                "Does this post contain explicit or sexual content?\n\n"
                "This means sexual content, graphic descriptions, or explicit profanity — "
                "not just a sensitive topic. It helps us show a content warning to other "
                "members before they view it.",
                reply_markup=explicit_kb
            )
            
            # Clear thread context from DB now that it's been captured for the pending post
            if thread_from_post_id:
                db_execute("UPDATE users SET thread_context_post_id = NULL WHERE user_id = %s", (user_id,))
            return
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            await update.message.reply_text(
                "Error processing your media. Please try again.",
                reply_markup=get_main_menu(user_id)

            )
            # Reset state on error
            db_execute(
                "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
                (user_id,)
            )
            return

    elif user and user['waiting_for_comment']:
        if text in main_menu_buttons: return
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
            await update.message.reply_text("Unsupported comment type. Please send text, voice, GIF, sticker, or photo.")
            return
    
        # Insert new comment
        new_comment_row = db_execute(
            """INSERT INTO comments
            (post_id, parent_comment_id, author_id, content, type, file_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING comment_id""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id),
            fetchone=True
        )
        new_comment_id = new_comment_row['comment_id'] if new_comment_row else None
        
        # Clear Aura Cache
        calculate_user_rating.cache_clear()
        format_aura.cache_clear()

    
        # Reset state
        db_execute(
            "UPDATE users SET waiting_for_comment = FALSE, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL WHERE user_id = %s",
            (user_id,)
        )
    
        await update.message.reply_text("Your comment has been posted!", reply_markup=get_main_menu(user_id))

        # Refresh the post + comments view so the new comment is visible right away
        try:
            total_comments_now = count_all_comments(post_id)
            per_page = 10
            last_page = max(1, (total_comments_now + per_page - 1) // per_page)
            await show_comments_menu(update, context, post_id, page=last_page, force_reveal=True, auto_show_comments=True)
        except Exception as e:
            logger.error(f"Error refreshing comments view after posting: {e}")

        
        # Update comment count in background
        asyncio.create_task(update_channel_post_comment_count(context, post_id))
        
        # Notify vent author if this is a top‑level comment
        if parent_comment_id == 0:
            await notify_vent_author_of_comment(
                context, post_id, user_id, new_comment_id,
                comment_content=content, media_type=comment_type, media_id=file_id
            )
        
        # Notify parent comment author if this is a reply
        if parent_comment_id != 0:
            await notify_user_of_reply(
                context, post_id, parent_comment_id, user_id, new_comment_id,
                comment_content=content, media_type=comment_type, media_id=file_id
            )
        return

    elif user and user['waiting_for_private_message']:
        if text in main_menu_buttons: return
        target_id = user['private_message_target']
        
        message_content = update.message.text or update.message.caption or ""
        media_type = 'text'
        media_id = None

        if update.message.photo:
            media_type = 'photo'
            media_id = update.message.photo[-1].file_id
        elif update.message.voice:
            media_type = 'voice'
            media_id = update.message.voice.file_id
        elif update.message.audio:
            media_type = 'audio'
            media_id = update.message.audio.file_id
        elif update.message.video:
            media_type = 'video'
            media_id = update.message.video.file_id
        elif update.message.document:
            media_type = 'document'
            media_id = update.message.document.file_id
        elif update.message.animation:
            media_type = 'gif'
            media_id = update.message.animation.file_id

        if not message_content and not media_id:
            await update.message.reply_text("Please send a message or media.")
            return
        
        # Check if blocked
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (target_id, user_id)
        )
        
        if is_blocked:
            await update.message.reply_text(
                "You cannot send messages to this user. They have blocked you.",
                reply_markup=get_main_menu(user_id)
            )


            db_execute(
                "UPDATE users SET waiting_for_private_message = FALSE, private_message_target = NULL WHERE user_id = %s",
                (user_id,)
            )
            return
        
        # Save message
        message_row = db_execute(
            "INSERT INTO private_messages (sender_id, receiver_id, content, media_type, media_id) VALUES (%s, %s, %s, %s, %s) RETURNING message_id",
            (user_id, target_id, message_content, media_type, media_id),
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
            "Your message has been sent!",
            reply_markup=get_main_menu(user_id)
        )


        return

    if user and user.get('awaiting_name'):
        if text in main_menu_buttons: return
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = %s, awaiting_name = FALSE WHERE user_id = %s",
                (new_name, user_id)
            )
            await update.message.reply_text(
                f"Name updated to *{new_name}*!", 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )


            await send_updated_profile(user_id, update.message.chat.id, context)
        else:
            await update.message.reply_text("Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle main menu buttons
    if text == "Share":
        context.user_data['selected_categories'] = set()
        await update.message.reply_text(
            "*Select categories (you can choose multiple):*",
            reply_markup=build_multi_category_keyboard(set()),
            parse_mode=ParseMode.MARKDOWN
        )
        return 

    elif text == "Chat Requests":
        await show_chat_requests(update, context, page=1)
        return

    elif text == "Profile":
        await send_updated_profile(user_id, update.message.chat.id, context)
        return
        
    if user and user.get('awaiting_bio'):
        if text in main_menu_buttons: return
        if not text:
            await update.message.reply_text("Bio must be text. Please try again.")
            return
            
        if len(text) > 200:
             await update.message.reply_text("Bio is too long (max 200 chars). Please shorten it.")
             return
             
        db_execute("UPDATE users SET bio = %s, awaiting_bio = FALSE WHERE user_id = %s", (text, user_id))
        await update.message.reply_text("Bio updated successfully!", reply_markup=get_main_menu(user_id))

        await send_updated_profile(user_id, update.message.chat.id, context)
        return 

    elif text == "Top":
        await show_leaderboard(update, context)
        return

    elif text == "Settings":
        await show_settings(update, context)
        return

    elif text == "Posts":
        await show_my_content_menu(update, context)  # Show menu instead of direct posts
        return

    elif text == "Help":
        help_text = (
            "*How to Use This Bot:*\n"
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

    elif text == "Open App":
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
        await update.message.reply_text("You cannot message yourself.")
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

    await update.message.reply_text("Message sent!")

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("webapp", "Open Web App"),
        BotCommand("menu", "Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Share your thoughts"),
        BotCommand("leaderboard", "View top contributors"),
        BotCommand("settings", "Configure your preferences"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
        BotCommand("inbox", "View your private messages"),
        BotCommand("requests", "View your pending chat requests"),
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
        logger.info("Bot menu button set to Default (Trigger Keyboard)")
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
        [InlineKeyboardButton("Open Christian Vent App", web_app=WebAppInfo(url=mini_app_url))],
        [InlineKeyboardButton("Open in Browser", url=mini_app_url)],
    ])
    
    await update.message.reply_text(
        "*Christian Vent Web App*\n\n"
        "Tap *Open Christian Vent App* to launch the app right here inside Telegram — no browser needed!\n\n"
        "*You can:*\n"
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
    app.add_handler(CommandHandler("requests", show_chat_requests))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("fixventnumbers", fix_vent_numbers))
    app.add_handler(CommandHandler("fix_missing_sex", fix_missing_sex))
    app.add_handler(CommandHandler("recount_comments", recount_comments))
    app.add_handler(CommandHandler("reset_weekly_badges", reset_weekly_badges_command))
    
    # Weekly Admin Diagnostics Commands
    app.add_handler(CommandHandler("test_weekly", test_weekly_command))
    app.add_handler(CommandHandler("force_weekly", force_weekly_command))
    app.add_handler(CommandHandler("weekly_status", weekly_status_command))
    
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
    
    logger.info(f"Flask health check server started on port {port}")
    
    # Schedule Weekly Badges (Every Monday at 00:00 UTC)
    from telegram.ext import JobQueue
    if app.job_queue is None:
        try:
            app.job_queue = JobQueue()
            app.job_queue.set_application(app)
            app.job_queue.start()
            logger.info("Job queue manually started.")
        except Exception as jq_e:
            logger.error(f"Failed to initialize JobQueue: {jq_e}")

    job_queue = app.job_queue
    if job_queue:
        # Check if already scheduled to avoid duplicates
        existing_jobs = job_queue.jobs()
        if not any(j.name == "weekly_badges" for j in existing_jobs):
            job_queue.run_daily(
                award_weekly_badges,
                time=time(0, 0, tzinfo=timezone.utc),
                days=(0,),  # Monday = 0
                name="weekly_badges"
            )
            logger.info("Weekly badge job scheduled for Mondays at 00:00 UTC")
        else:
            logger.info("Weekly badge job already scheduled.")
    else:
        logger.error("Failed to initialize job queue.")

    # Start polling
    logger.info("Starting bot polling...")
    app.run_polling()

# In bot.py, replace the simple /mini_app route with this:

@flask_app.route('/mini_app')
def mini_app_page():
    """Complete Mini App - returns the premium UI."""
    _bot = BOT_USERNAME
    _primary = PRIMARY_COLOR
    _secondary = SECONDARY_COLOR
    _card_bg = CARD_BG_COLOR
    _border = BORDER_COLOR
    _text = TEXT_COLOR
    _rgb = PRIMARY_RGB

    html = ("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Christian Vent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400&display=swap" rel="stylesheet">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/fix-webm-duration@1.0.5/fix-webm-duration.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --gold:#c9a84c;
  --gold2:#e8c97a;
  --gold3:#f5e4b0;
  --gold-rgb:201,168,76;
  --bg:#0c0b09;
  --bg2:#131210;
  --bg3:#1a1814;
  --glass:rgba(255,255,255,0.04);
  --glass2:rgba(255,255,255,0.07);
  --border:rgba(255,255,255,0.08);
  --border2:rgba(201,168,76,0.2);
  --text:#f0ede6;
  --text2:#a09880;
  --text3:#6b6355;
  --nav-h:72px;
  --radius:16px;
  --radius-sm:10px;
  --radius-xs:6px;
}
body.light {
  --bg:#f5f3f0;
  --bg2:#e8e4dd;
  --bg3:#ddd8cf;
  --glass:rgba(0,0,0,0.02);
  --glass2:rgba(0,0,0,0.04);
  --border:rgba(0,0,0,0.1);
  --border2:rgba(201,168,76,0.3);
  --text:#1a1a1a;
  --text2:#4a4a4a;
  --text3:#6b6b6b;
}
html,body{height:100%;overflow:hidden}
body{
  font-family:'Inter',sans-serif;
  background:var(--bg);
  color:var(--text);
  font-size:15px;
  -webkit-font-smoothing:antialiased;
  overscroll-behavior:none;
  transition:background 0.2s, color 0.2s;
}
#shell{
  position:fixed;inset:0;
  display:flex;flex-direction:column;
}
#pages{
  flex:1;overflow-y:auto;overflow-x:hidden;
  scroll-behavior:smooth;
  padding-bottom:calc(var(--nav-h) + 16px);
  -webkit-overflow-scrolling:touch;
}
#pages::-webkit-scrollbar{display:none}
.page{display:none;padding:0 0 8px}
.page.active{display:block}
#nav{
  flex-shrink:0;
  height:var(--nav-h);
  background:rgba(12,11,9,0.92);
  border-top:0.5px solid var(--border);
  display:flex;align-items:stretch;
  padding-bottom:env(safe-area-inset-bottom,0);
  backdrop-filter:blur(24px);
  -webkit-backdrop-filter:blur(24px);
  position:relative;
  z-index:100;
}
body.light #nav{background:rgba(245,243,240,0.92);}
.nav-item{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:4px;background:none;border:none;cursor:pointer;
  color:var(--text3);font-size:10px;font-weight:500;letter-spacing:0.3px;
  font-family:'Inter',sans-serif;
  transition:color 0.2s;padding:8px 4px;
  -webkit-tap-highlight-color:transparent;
  text-transform:uppercase;
}
.nav-item svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;transition:transform 0.2s}
.icon{width:16px;height:16px;flex-shrink:0;vertical-align:-3px}
.cat-chip .icon{width:15px;height:15px;color:var(--text3)}
.cat-chip.on .icon{color:var(--gold2)}
.badge-icon{width:13px;height:13px;vertical-align:-2px;margin-right:3px}
.ava svg,.modal-avatar svg,.profile-ava-wrap svg{width:55%;height:55%;color:var(--text3)}
.lb-crown .icon{width:26px;height:26px;color:var(--gold)}
.lb-medal-rank .icon{width:22px;height:22px}
.lb-medal-rank.silver .icon{color:#c0c4cc}
.lb-medal-rank.bronze .icon{color:#c9814a}
.reaction-btn .icon{width:15px;height:15px;vertical-align:-3px;margin-right:2px}
.ca-btn .icon{width:13px;height:13px;vertical-align:-2px;margin-right:2px}
.modal-btn .icon{width:15px;height:15px;vertical-align:-3px;margin-right:4px}
.nav-item.active{color:var(--gold)}
.nav-item.active svg{transform:translateY(-1px)}
.nav-ink{
  position:absolute;bottom:0;left:0;width:20%;height:2px;
  background:var(--gold);border-radius:2px 2px 0 0;
  transition:left 0.3s cubic-bezier(.4,0,.2,1);
}
.page-head{
  padding:20px 20px 0;
  display:flex;align-items:center;justify-content:space-between;
}
.page-head h1{font-size:26px;font-weight:700;letter-spacing:-0.5px;color:var(--text)}
.page-head-sub{font-size:13px;color:var(--text3);margin-top:2px}
.logo-img{width:48px;height:48px;border-radius:12px;object-fit:cover;box-shadow:0 2px 8px rgba(0,0,0,0.1);}
.card{
  background:var(--glass);
  border:0.5px solid var(--border);
  border-radius:var(--radius);
  padding:18px;
  margin:12px 16px 0;
}
.card-gold{
  background:linear-gradient(135deg,rgba(201,168,76,0.08) 0%,rgba(201,168,76,0.03) 100%);
  border-color:var(--border2);
}
.pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;
  background:rgba(201,168,76,0.1);border:0.5px solid rgba(201,168,76,0.25);
  color:var(--gold2);
}
.pill-sm{padding:2px 8px;font-size:10px}
.pill-aura{
  display:inline-flex;align-items:center;gap:7px;
  padding:6px 14px;border-radius:24px;
  font-size:12.5px;font-weight:700;letter-spacing:0.2px;
  background:linear-gradient(135deg,rgba(201,168,76,0.30) 0%,rgba(245,158,11,0.16) 55%,rgba(201,168,76,0.24) 100%);
  border:0.5px solid rgba(245,158,11,0.4);
  color:var(--gold3);
  box-shadow:0 2px 10px rgba(201,168,76,0.2),inset 0 1px 0 rgba(255,255,255,0.07);
}
.pill-aura-badge{font-size:14px;line-height:1}
.pill-aura .bolt-icon{width:13px;height:13px;flex-shrink:0;display:block}
.pill-aura .bolt-icon path{fill:#ff9800}
.pill-aura-pts{color:var(--gold3)}
.ava{
  border-radius:50%;background:linear-gradient(135deg,var(--bg3),var(--bg2));
  border:1.5px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  flex-shrink:0;font-size:1.1em;
}
.input-area{
  width:100%;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:14px 16px;
  color:var(--text);font-family:'Inter',sans-serif;font-size:15px;
  outline:none;resize:none;
  transition:border-color 0.2s;
}
.input-area:focus{border-color:rgba(201,168,76,0.4)}
.input-area::placeholder{color:var(--text3)}
.btn-gold{
  width:100%;padding:15px;border-radius:var(--radius-sm);border:none;
  background:var(--gold);color:#0c0b09;
  font-family:'Inter',sans-serif;font-size:15px;font-weight:700;
  cursor:pointer;letter-spacing:0.2px;
  transition:opacity 0.2s,transform 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.btn-gold:active{transform:scale(0.98);opacity:0.9}
.btn-gold:disabled{opacity:0.4;cursor:not-allowed}
.btn-ghost{
  background:none;border:0.5px solid var(--border2);border-radius:var(--radius-xs);
  color:var(--gold);padding:8px 14px;font-size:13px;font-weight:600;
  font-family:'Inter',sans-serif;cursor:pointer;
  -webkit-tap-highlight-color:transparent;
}
.cat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}
.cat-chip{
  padding:10px 12px;border-radius:var(--radius-sm);font-size:12px;font-weight:500;
  background:var(--bg2);border:0.5px solid var(--border);color:var(--text2);
  cursor:pointer;display:flex;align-items:center;gap:8px;
  transition:all 0.15s;-webkit-tap-highlight-color:transparent;
}
.cat-chip:active{transform:scale(0.97)}
.cat-chip.on{background:rgba(201,168,76,0.1);border-color:rgba(201,168,76,0.35);color:var(--gold2)}
.cat-check{width:16px;height:16px;border-radius:4px;border:1.5px solid currentColor;
  display:flex;align-items:center;justify-content:center;font-size:10px;flex-shrink:0}
.cat-chip.on .cat-check::after{content:'✓'}
.post-card{
  margin:10px 16px 0;
  background:var(--glass);border:0.5px solid var(--border);
  border-radius:var(--radius);padding:16px;
  cursor:pointer;transition:background 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.post-card:active{background:var(--glass2)}
.post-meta{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.post-name{font-size:13px;font-weight:600;color:var(--text);cursor:pointer}
.post-name:hover{color:var(--gold)}
.post-time{font-size:11px;color:var(--text3);margin-left:auto}
.post-body{font-size:14px;line-height:1.6;color:var(--text2);
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:12px}
.post-footer{display:flex;align-items:center;justify-content:space-between;
  padding-top:12px;border-top:0.5px solid var(--border)}
.post-footer-left{display:flex;align-items:center;gap:12px}
.stat-btn{display:flex;align-items:center;gap:5px;color:var(--text3);font-size:12px;
  font-weight:500;background:none;border:none;cursor:pointer;font-family:'Inter',sans-serif;
  -webkit-tap-highlight-color:transparent;padding:0}
.stat-btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}
.read-more{font-size:12px;font-weight:600;color:var(--gold);display:flex;align-items:center;gap:3px}
.lb-hero{
  margin:20px 16px 0;
  background:linear-gradient(135deg,rgba(201,168,76,0.1),rgba(201,168,76,0.04));
  border:0.5px solid var(--border2);border-radius:20px;
  padding:24px 20px;text-align:center;position:relative;overflow:hidden;
}
.lb-hero::before{
  content:'';position:absolute;inset:-40px;
  background:radial-gradient(circle at 50% 0,rgba(201,168,76,0.08),transparent 70%);
}
.lb-crown{font-size:36px;margin-bottom:6px;display:block}
.lb-top-name{font-size:20px;font-weight:700;letter-spacing:-0.3px}
.lb-top-pts{font-size:13px;color:var(--text3);margin-top:4px}
.lb-medals{display:flex;gap:8px;margin-top:20px;justify-content:center}
.lb-medal-card{
  flex:1;background:var(--bg2);border-radius:var(--radius-sm);
  border:0.5px solid var(--border);padding:14px 10px;text-align:center;
}
.lb-medal-rank{font-size:20px;margin-bottom:4px}
.lb-medal-name{font-size:12px;font-weight:600;color:var(--text);margin-bottom:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-medal-pts{font-size:11px;color:var(--text3)}
.lb-list{margin:0 16px}
.lb-row{
  display:flex;align-items:center;gap:12px;padding:14px 0;
  border-bottom:0.5px solid var(--border);
}
.lb-row:last-child{border-bottom:none}
.lb-rank{width:24px;text-align:center;font-size:13px;font-weight:700;color:var(--text3)}
.lb-info{flex:1;min-width:0}
.lb-info-name{font-size:14px;font-weight:600;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.lb-info-name:hover{color:var(--gold)}
.lb-info-aura{font-size:11px;color:var(--text3);margin-top:1px}
.lb-pts{font-size:14px;font-weight:700;color:var(--gold)}
.profile-hero{
  margin:20px 16px 0;
  background:linear-gradient(160deg,var(--bg3),var(--bg2));
  border:0.5px solid var(--border);border-radius:20px;padding:24px;
  text-align:center;position:relative;
}
.profile-ava-wrap{
  width:80px;height:80px;border-radius:50%;margin:0 auto 14px;
  background:linear-gradient(135deg,rgba(201,168,76,0.2),rgba(201,168,76,0.05));
  border:1.5px solid var(--border2);
  display:flex;align-items:center;justify-content:center;font-size:32px;
}
.profile-name{font-size:22px;font-weight:700;letter-spacing:-0.3px}
.profile-pts{font-size:13px;color:var(--text3);margin-top:4px}
.profile-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
  margin-top:20px;border-top:0.5px solid var(--border);padding-top:16px}
.profile-stat{text-align:center}
.profile-stat-num{font-size:20px;font-weight:700;color:var(--gold)}
.profile-stat-lbl{font-size:11px;color:var(--text3);margin-top:2px}
.setting-row{
  display:flex;align-items:center;padding:16px 0;
  border-bottom:0.5px solid var(--border);gap:14px;
}
.setting-row:last-child{border-bottom:none}
.setting-icon{
  width:38px;height:38px;border-radius:10px;
  background:rgba(201,168,76,0.1);border:0.5px solid var(--border2);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.setting-icon svg{width:18px;height:18px;stroke:var(--gold);fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.setting-label{flex:1}
.setting-label-title{font-size:14px;font-weight:600;color:var(--text)}
.setting-label-sub{font-size:12px;color:var(--text3);margin-top:2px}
.toggle{position:relative;width:44px;height:25px;cursor:pointer;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{
  position:absolute;inset:0;border-radius:25px;
  background:var(--bg3);border:0.5px solid var(--border);
  transition:background 0.25s;
}
.toggle input:checked + .toggle-track{background:rgba(201,168,76,0.3);border-color:var(--gold)}
.toggle-thumb{
  position:absolute;width:19px;height:19px;border-radius:50%;
  top:3px;left:3px;
  background:var(--text3);transition:all 0.25s cubic-bezier(.4,0,.2,1);
}
.toggle input:checked ~ .toggle-thumb{left:22px;background:var(--gold)}
.search-wrap{
  display:flex;align-items:center;gap:10px;
  padding:12px 16px;background:var(--glass);
  border:0.5px solid var(--border);border-radius:var(--radius-sm);
  margin:14px 16px 0;
}
.search-wrap svg{width:17px;height:17px;stroke:var(--text3);fill:none;stroke-width:1.8;flex-shrink:0}
.search-wrap input{flex:1;background:none;border:none;outline:none;color:var(--text);
  font-family:'Inter',sans-serif;font-size:14px}
.search-wrap input::placeholder{color:var(--text3)}
.char-count{font-size:11px;color:var(--text3);text-align:right;margin:6px 0 12px}
.skel{
  background:linear-gradient(90deg,var(--bg2) 25%,var(--bg3) 50%,var(--bg2) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite;
  border-radius:var(--radius-xs);
}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
#toast{
  position:fixed;bottom:calc(var(--nav-h) + 80px);left:50%;transform:translateX(-50%) translateY(10px);
  background:var(--gold);color:#0c0b09;padding:10px 20px;border-radius:20px;
  font-size:13px;font-weight:700;opacity:0;pointer-events:none;
  transition:all 0.25s;z-index:999;white-space:nowrap;
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#page-detail{position:relative}
.back-btn{
  display:flex;align-items:center;gap:6px;
  padding:20px 16px 10px;
  color:var(--gold);font-size:14px;font-weight:600;
  background:none;border:none;cursor:pointer;font-family:'Inter',sans-serif;
  -webkit-tap-highlight-color:transparent;
}
.back-btn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2}
/* In light mode the gold-on-cream contrast is too weak for the icon stroke;
   darken it slightly and give the chat room its own explicit override so it
   isn't relying on the ambient --gold var alone. */
body.light .back-btn{color:#8a6d1f}
body.light .back-btn svg{stroke:#8a6d1f}
.comment-item{display:flex;gap:10px;margin-bottom:14px}
.comment-item.reply{margin-left:32px}
.comment-body{flex:1;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:12px}
.comment-name{font-size:12px;font-weight:600;color:var(--gold);margin-bottom:4px;cursor:pointer}
.comment-name:hover{text-decoration:underline}
.comment-text{font-size:13px;line-height:1.55;color:var(--text2)}
.comment-actions{display:flex;gap:12px;margin-top:8px}
.ca-btn{background:none;border:none;cursor:pointer;
  font-size:12px;font-weight:500;color:var(--text3);font-family:'Inter',sans-serif;
  -webkit-tap-highlight-color:transparent;padding:0}
.ca-btn:hover{color:var(--gold)}
/* Fixed comment input bar above nav */
.comment-input-bar{
  position:fixed;bottom:var(--nav-h);left:0;right:0;
  display:flex;align-items:flex-end;gap:8px;
  padding:12px 16px;
  background:rgba(12,11,9,0.95);
  border-top:0.5px solid var(--border);
  backdrop-filter:blur(12px);
  z-index:90;
}
body.light .comment-input-bar{background:rgba(245,243,240,0.95);}
.comment-input-bar textarea{
  flex:1;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-xs);padding:10px 12px;
  color:var(--text);font-family:'Inter',sans-serif;font-size:13px;
  outline:none;resize:none;max-height:100px;min-height:38px;
}
.comment-input-bar textarea:focus{border-color:rgba(201,168,76,0.4)}
.comment-input-bar button{
  width:36px;height:36px;border-radius:50%;
  background:var(--gold);border:none;cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
}
.comment-input-bar button svg{width:16px;height:16px;stroke:#0c0b09;fill:none;stroke-width:2.2}
.media-attach-btn{
  width:36px;height:36px;border-radius:50%;flex-shrink:0;
  background:var(--bg2);border:0.5px solid var(--border);cursor:pointer;
  display:flex;align-items:center;justify-content:center;position:relative;
  -webkit-tap-highlight-color:transparent;
}
.media-attach-btn:active{transform:scale(0.92)}
.media-attach-btn svg{width:16px;height:16px;stroke:var(--text2);fill:none;stroke-width:2}
.media-attach-btn.has-media{border-color:var(--gold)}
.media-attach-btn.has-media svg{stroke:var(--gold)}
.media-preview{
  display:flex;align-items:center;gap:8px;
  background:var(--bg2);border:0.5px solid var(--border);border-radius:var(--radius-xs);
  padding:8px 10px;margin:8px 16px 0;font-size:12px;color:var(--text2);
}
.media-preview img{width:36px;height:36px;border-radius:8px;object-fit:cover;flex-shrink:0}
.media-preview .mp-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.media-preview .mp-remove{background:none;border:none;color:var(--text3);cursor:pointer;font-size:16px;padding:0 4px}
.media-preview .mp-remove:hover{color:var(--gold)}
#comment-media-preview.media-preview{margin:0 0 8px}
.post-media, .comment-media{margin:10px 0;border-radius:var(--radius-sm);overflow:hidden}
.post-media img, .comment-media img{width:100%;display:block;border-radius:var(--radius-sm)}
.post-media video, .comment-media video{width:100%;display:block;border-radius:var(--radius-sm);background:#000}
.post-media audio, .comment-media audio{width:100%;display:block}
.post-media .doc-link, .comment-media .doc-link{
  display:flex;align-items:center;gap:10px;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:12px;color:var(--text);text-decoration:none;font-size:13px;
}
.post-media .doc-link svg{width:20px;height:20px;stroke:var(--gold);fill:none;stroke-width:2;flex-shrink:0}
.post-media img.sticker-media, .comment-media img.sticker-media{width:100px;border-radius:0}
/* ----- Compact voice player ----- */
.voice-player{display:flex;align-items:center;gap:9px;background:var(--bg2);border:0.5px solid var(--border);border-radius:22px;padding:7px 12px;max-width:230px;margin:8px 0}
.voice-player-btn{width:32px;height:32px;border-radius:50%;background:var(--gold);border:none;flex-shrink:0;display:flex;align-items:center;justify-content:center;cursor:pointer;-webkit-tap-highlight-color:transparent}
.voice-player-btn svg{width:14px;height:14px;fill:#0c0b09;stroke:#0c0b09}
.voice-player-btn svg.icon-spinner{fill:none;stroke-width:2.5;stroke-linecap:round;animation:voice-spin 0.8s linear infinite}
@keyframes voice-spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.voice-player-track{flex:1;height:4px;background:var(--border);border-radius:2px;position:relative;cursor:pointer}
.voice-player-progress{position:absolute;left:0;top:0;height:100%;width:0%;background:var(--gold);border-radius:2px;transition:width 0.1s linear}
.voice-player-time{font-size:10.5px;color:var(--text3);flex-shrink:0;min-width:32px;text-align:right;font-variant-numeric:tabular-nums}
/* Inside a chat bubble, the player should blend into the bubble rather than nest a second box */
.msg-bubble .voice-player{background:transparent;border:none;padding:2px 0 0;margin:4px 0 0;max-width:100%;width:188px}
.msg-row.me .voice-player-btn{background:#0c0b09}
.msg-row.me .voice-player-btn svg{fill:var(--gold);stroke:var(--gold)}
.msg-row.me .voice-player-track{background:rgba(12,11,9,0.28)}
.msg-row.me .voice-player-progress{background:#0c0b09}
.msg-row.me .voice-player-time{color:rgba(12,11,9,0.72)}
.msg-row.them .voice-player-track{background:var(--border2)}

.lightbox{
  position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:2000;
  display:none;align-items:center;justify-content:center;
  -webkit-tap-highlight-color:transparent;
}
.lightbox.active{display:flex}
.lightbox img{max-width:94vw;max-height:88vh;object-fit:contain;border-radius:8px}
.lightbox-close{
  position:absolute;top:calc(env(safe-area-inset-top,0) + 16px);right:16px;
  width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,0.12);
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:22px;
  cursor:pointer;
}
/* ----- Voice recording button & UI ----- */
.voice-record-btn{
  width:36px;height:36px;border-radius:50%;flex-shrink:0;
  background:var(--bg2);border:0.5px solid var(--border);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background 0.15s, transform 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.voice-record-btn.recording{background:#e74c3c;border-color:#e74c3c;transform:scale(1.1)}
.voice-record-btn.recording svg{stroke:#fff}
.voice-record-timer{
  position:fixed;bottom:calc(var(--nav-h) + 100px);left:50%;transform:translateX(-50%);
  background:rgba(0,0,0,0.8);color:#fff;padding:8px 20px;border-radius:40px;
  font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;
  display:none;z-index:999;backdrop-filter:blur(8px);
}
.voice-record-timer .cancel-hint{
  font-size:11px;font-weight:400;opacity:0.7;margin-left:12px;
}
.voice-record-timer.active{display:flex;align-items:center;gap:12px}

/* ----- Direct reaction buttons ----- */
.reaction-buttons{
  display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;
}
.reaction-btn{
  display:flex;align-items:center;gap:4px;
  padding:4px 10px;border-radius:20px;background:var(--bg2);
  border:0.5px solid var(--border);cursor:pointer;font-size:13px;
  transition:all 0.15s;font-family:'Inter',sans-serif;color:var(--text2);
}
.reaction-btn.on{background:rgba(201,168,76,0.12);border-color:var(--gold);color:var(--gold)}
.reaction-btn:active{transform:scale(0.92)}
.chat-item{
  display:flex;align-items:center;gap:12px;
  padding:14px 16px;border-bottom:0.5px solid var(--border);
  cursor:pointer;-webkit-tap-highlight-color:transparent;
  transition:background 0.15s;
}
.chat-item:active{background:var(--glass)}
.chat-item-right{flex:1;min-width:0}
.chat-item-top{display:flex;justify-content:space-between;align-items:center}
.chat-item-name{font-size:14px;font-weight:600;color:var(--text)}
.chat-item-time{font-size:11px;color:var(--text3)}
.chat-item-preview{font-size:12px;color:var(--text3);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.unread-badge{
  background:var(--gold);color:#0c0b09;font-size:10px;font-weight:800;
  min-width:18px;height:18px;border-radius:9px;
  display:flex;align-items:center;justify-content:center;padding:0 4px;
  flex-shrink:0;
}
#chat-room{
  position:fixed;inset:0;z-index:200;
  background:var(--bg);
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform 0.3s cubic-bezier(.4,0,.2,1);
}
#chat-room.open{transform:none}
.cr-head{
  display:flex;align-items:center;gap:12px;padding:16px;
  background:rgba(12,11,9,0.95);border-bottom:0.5px solid var(--border);
  flex-shrink:0;
}
.cr-head button{background:none;border:none;cursor:pointer;padding:4px;
  display:flex;align-items:center;justify-content:center;
  -webkit-tap-highlight-color:transparent;
}
.cr-head button svg{width:22px;height:22px;stroke:var(--text);fill:none;stroke-width:2}
body.light .cr-head{background:rgba(245,243,240,0.97)}
body.light .cr-head button svg{stroke:#1a1a1a}
.cr-name{font-size:16px;font-weight:700}
.cr-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.cr-msgs::-webkit-scrollbar{display:none}
.msg-row{display:flex;flex-direction:column;max-width:75%}
.msg-row.me{align-self:flex-end;align-items:flex-end}
.msg-row.them{align-self:flex-start}
.msg-bubble{
  padding:10px 14px;border-radius:18px;font-size:13.5px;line-height:1.45;
  word-break:break-word;
}
.msg-row.me .msg-bubble{
  background:var(--gold);color:#0c0b09;font-weight:500;
  border-bottom-right-radius:4px;
}
.msg-row.them .msg-bubble{
  background:var(--bg3);border:0.5px solid var(--border);color:var(--text);
  border-bottom-left-radius:4px;
}
.msg-time{font-size:10px;color:var(--text3);margin-top:4px;padding:0 4px}
.cr-input{
  display:flex;flex-direction:column;gap:8px;padding:12px 16px;
  border-top:0.5px solid var(--border);
  background:rgba(12,11,9,0.95);flex-shrink:0;
}
.cr-input-row{
  display:flex;align-items:flex-end;gap:8px;
  border-top:0.5px solid var(--border);
  background:rgba(12,11,9,0.95);flex-shrink:0;
}
.cr-input textarea{
  flex:1;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:20px;padding:10px 16px;color:var(--text);
  font-family:'Inter',sans-serif;font-size:14px;outline:none;
  resize:none;min-height:40px;max-height:100px;
}
.cr-input textarea:focus{border-color:rgba(201,168,76,0.4)}
.cr-send{
  width:40px;height:40px;border-radius:50%;
  background:var(--gold);border:none;cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  -webkit-tap-highlight-color:transparent;
}
.cr-send svg{width:17px;height:17px;stroke:#0c0b09;fill:none;stroke-width:2.2}
#auth{
  position:fixed;inset:0;background:var(--bg);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  z-index:9999;gap:16px;
}
.auth-ring{
  width:52px;height:52px;border-radius:50%;
  border:2.5px solid var(--border);border-top-color:var(--gold);
  animation:spin 1s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.auth-label{font-size:15px;font-weight:600;color:var(--gold)}
.section-label{
  font-size:11px;font-weight:700;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--text3);
  padding:18px 16px 8px;
}
.divider{height:0.5px;background:var(--border);margin:0}
.input-label{font-size:12px;font-weight:600;color:var(--text3);margin-bottom:6px;display:block;letter-spacing:0.3px;text-transform:uppercase}
.emoji-picker{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:8px 0 0}
.emoji-opt{
  aspect-ratio:1;background:var(--bg2);border:1.5px solid transparent;
  border-radius:var(--radius-xs);display:flex;align-items:center;justify-content:center;
  font-size:22px;cursor:pointer;transition:all 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.emoji-opt.sel{border-color:var(--gold);background:rgba(201,168,76,0.1)}
.rx-dock{
  position:absolute;bottom:calc(100% + 8px);left:0;
  background:var(--bg2);border:0.5px solid var(--border2);
  border-radius:24px;padding:8px 14px;
  display:flex;gap:12px;z-index:50;
  box-shadow:0 8px 24px rgba(0,0,0,0.4);
  animation:popIn 0.2s cubic-bezier(.175,.885,.32,1.275);
}
@keyframes popIn{0%{transform:scale(0.6) translateY(8px);opacity:0}100%{transform:none;opacity:1}}
.rx-emoji{font-size:22px;cursor:pointer;transition:transform 0.15s;display:inline-block}
.rx-emoji:hover{transform:scale(1.3) translateY(-4px)}
.rx-pill{
  display:inline-flex;align-items:center;gap:4px;
  padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600;
  background:var(--bg2);border:0.5px solid var(--border);color:var(--text2);
  cursor:pointer;transition:all 0.15s;
}
.rx-pill.on{background:rgba(201,168,76,0.12);border-color:var(--border2);color:var(--gold)}
.reaction-trigger{
  background:var(--bg2);border:0.5px solid var(--border);border-radius:20px;
  padding:4px 12px;font-size:12px;color:var(--text3);cursor:pointer;
}
.reaction-trigger:hover{color:var(--gold);border-color:var(--gold);}
.page-head-wrap{
  background:linear-gradient(180deg,rgba(201,168,76,0.05) 0%,transparent 100%);
  padding-bottom:4px;
}
.modal-mask{
  position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,0.7);backdrop-filter:blur(5px);
  z-index:1000;display:flex;align-items:center;justify-content:center;
  visibility:hidden;opacity:0;transition:all 0.2s;
}
.modal-mask.active{visibility:visible;opacity:1;}
.modal-container{
  background:var(--bg);border:1px solid var(--border);border-radius:28px;
  max-width:320px;width:90%;padding:24px;text-align:center;
  position:relative;box-shadow:0 20px 40px rgba(0,0,0,0.4);
}
.modal-close{
  position:absolute;top:12px;right:16px;font-size:22px;cursor:pointer;color:var(--text3);
}
.modal-close:hover{color:var(--gold);}
.modal-avatar{width:80px;height:80px;border-radius:50%;margin:0 auto 12px;background:var(--bg2);display:flex;align-items:center;justify-content:center;font-size:32px;border:2px solid var(--gold);}
.modal-name{font-size:20px;font-weight:700;color:var(--gold);}
.modal-stats{display:flex;justify-content:space-around;margin:16px 0;}
.modal-stat{text-align:center;}
.modal-stat-num{font-size:18px;font-weight:700;color:var(--text);}
.modal-stat-lbl{font-size:11px;color:var(--text3);}
.modal-btn{width:100%;padding:12px;margin-top:8px;border:none;border-radius:40px;font-weight:600;cursor:pointer;}
.modal-btn-primary{background:var(--gold);color:#0c0b09;}
.modal-btn-primary:active{transform:scale(0.97);}
.modal-btn-secondary{background:var(--bg2);border:1px solid var(--border);color:var(--text);}
.modal-btn-secondary:active{background:var(--glass);}
</style>
</head>
<body>
<div id="auth"><div class="auth-ring"></div><span class="auth-label">Connecting…</span></div>

<div id="app" style="display:none;height:100vh;flex-direction:column">
<div id="shell">
  <div id="pages">
    <div class="page active" id="page-vent">
      <div class="page-head-wrap"><div class="page-head" style="padding-top:24px"><div><h1>Share</h1><div class="page-head-sub">Speak your heart, anonymously</div></div><img src="/static/images/vent logo.png" class="logo-img" onerror="this.style.display='none'"></div></div>
      <div class="section-label">Categories</div><div style="padding:0 16px"><div id="cat-grid" class="cat-grid"></div></div>
      <div style="padding:0 16px;margin-top:12px;display:flex;align-items:flex-start;gap:8px"><input type="checkbox" id="vent-explicit-check" style="margin-top:3px;width:16px;height:16px;flex-shrink:0"><label for="vent-explicit-check" style="font-size:12.5px;color:var(--text2);line-height:1.4">This post contains explicit content (may not be suitable for all viewers)</label></div>
      <div style="padding:0 16px;margin-top:14px"><textarea id="vent-txt" class="input-area" rows="5" placeholder="What's on your heart today…" maxlength="5000"></textarea><div class="char-count"><span id="vent-cnt">0</span> / 5000</div></div>
      <div id="vent-media-preview" style="display:none"></div>
      <div style="padding:0 16px;margin-top:14px;display:flex;gap:10px;align-items:center">
        <button type="button" class="media-attach-btn" id="vent-attach-btn" title="Attach media"><svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
        <input type="file" id="vent-file-input" style="display:none" accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.gif">
        <button type="button" class="voice-record-btn" id="vent-voice-btn" title="Voice message"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></button>
        <button class="btn-gold" id="submit-vent" style="flex:1">Post Anonymously</button>
      </div>
    </div>
    <div class="page" id="page-feed">
      <div class="page-head-wrap"><div class="page-head" style="padding-top:24px"><div><h1>Community</h1><div class="page-head-sub">Read, reflect, respond</div></div></div></div>
      <div class="search-wrap"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="22" y2="22"/></svg><input id="search-inp" type="text" placeholder="Search vents…"></div>
      <div id="feed-list"></div><div id="feed-more" style="padding:16px;text-align:center;display:none"><button class="btn-ghost" id="load-more-btn">Load more</button></div>
    </div>
    <div class="page" id="page-detail">
      <button class="back-btn" onclick="gotoFeed()"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>Back</button>
      <div id="detail-post"></div>
      <div class="section-label">Responses</div>
      <div id="detail-comments" style="padding:0 16px 80px"></div>
    </div>
    <div class="page" id="page-leaderboard"><div class="page-head-wrap"><div class="page-head" style="padding-top:24px"><div><h1>Top Voices</h1><div class="page-head-sub">Weekly community leaders</div></div></div></div><div id="lb-content"></div></div>
    <div class="page" id="page-profile"><div id="profile-content"></div></div>
    <div class="page" id="page-edit">
      <button class="back-btn" onclick="go('profile')"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>Profile</button>
      <div style="padding:0 16px"><label class="input-label">Display name</label><input id="ep-name" class="input-area" type="text" placeholder="Your anonymous name" style="height:44px;margin-bottom:16px"><label class="input-label">Bio</label><textarea id="ep-bio" class="input-area" rows="3" placeholder="A short intro…" style="margin-bottom:16px"></textarea><label class="input-label">Avatar</label><div id="ep-emoji" class="emoji-picker" style="margin-bottom:20px"></div><button class="btn-gold" id="save-profile-btn">Save changes</button></div>
    </div>
    <div class="page" id="page-settings">
      <div class="page-head-wrap"><div class="page-head" style="padding-top:24px"><div><h1>Settings</h1><div class="page-head-sub">Manage your preferences</div></div></div></div>
      <div class="card" style="margin-top:14px"><div class="setting-row"><div class="setting-icon"><svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></div><div class="setting-label"><div class="setting-label-title">Notifications</div><div class="setting-label-sub">Replies and interactions</div></div><label class="toggle"><input type="checkbox" id="set-notif"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
      <div class="setting-row"><div class="setting-icon"><svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></div><div class="setting-label"><div class="setting-label-title">Public profile</div><div class="setting-label-sub">Show stats to others</div></div><label class="toggle"><input type="checkbox" id="set-priv"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
      <div class="setting-row"><div class="setting-icon"><svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div><div class="setting-label"><div class="setting-label-title">Light / Dark mode</div><div class="setting-label-sub">Switch theme</div></div><label class="toggle"><input type="checkbox" id="set-theme"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div></div>
      <div style="padding:16px"><button class="btn-gold" id="save-settings-btn">Save settings</button></div>
      <div class="section-label">Account</div>
      <div class="card" style="margin-top:0"><div class="setting-row" style="border:none;cursor:pointer" onclick="go('edit')"><div class="setting-icon"><svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></div><div class="setting-label"><div class="setting-label-title">Edit profile</div><div class="setting-label-sub">Name, bio, avatar</div></div><svg style="width:16px;height:16px;stroke:var(--text3);fill:none;stroke-width:2" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></div></div>
      <div style="padding:16px 16px 0;text-align:center"><div style="font-size:11px;color:var(--text3)">Christian Vent · Built by <a href="https://t.me/YIDIDIYATAMIRUU" style="color:var(--gold);text-decoration:none">@YIDIDIYATAMIRUU</a></div></div>
    </div>
    <div class="page" id="page-chats">
      <div class="page-head-wrap"><div class="page-head" style="padding-top:24px"><div><h1>Messages</h1><div class="page-head-sub" id="chat-unread-label">All caught up</div></div></div></div>
      <div class="divider" style="margin-top:14px"></div><div id="chats-list"></div>
    </div>
  </div>
  <nav id="nav">
    <div class="nav-ink" id="nav-ink"></div>
    <button class="nav-item active" data-page="vent" onclick="go('vent',this)"><svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>Vent</button>
    <button class="nav-item" data-page="feed" onclick="go('feed',this)"><svg viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>Feed</button>
    <button class="nav-item" data-page="chats" onclick="go('chats',this)"><svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Chats</button>
    <button class="nav-item" data-page="leaderboard" onclick="go('leaderboard',this)"><svg viewBox="0 0 24 24"><polyline points="18 20 18 10"/><polyline points="12 20 12 4"/><polyline points="6 20 6 14"/></svg>Rankings</button>
    <button class="nav-item" data-page="settings" onclick="go('settings',this)"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg>Me</button>
  </nav>
</div>
</div>

<!-- FIXED COMMENT INPUT BAR (outside #pages) -->
<div class="comment-input-bar" id="commentBar" style="flex-direction:column;align-items:stretch">
  <div id="comment-media-preview" style="display:none"></div>
  <div style="display:flex;align-items:flex-end;gap:8px">
    <button type="button" class="media-attach-btn" id="comment-attach-btn" title="Attach media"><svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
    <input type="file" id="comment-file-input" style="display:none" accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.gif">
    <textarea id="comment-txt" placeholder="Add a response…" rows="1"></textarea>
    <button type="button" class="voice-record-btn" id="comment-voice-btn" title="Voice message"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></button>
    <button id="send-comment"><svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
  </div>
</div>

<div id="chat-room">
  <div class="cr-head"><button onclick="closeCR()"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg></button><div class="ava" id="cr-ava" style="width:36px;height:36px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="icon"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg></div><div><div class="cr-name" id="cr-name">Chat</div></div></div>
  <div class="cr-msgs" id="cr-msgs"></div>
  <div class="cr-input" style="padding-top:12px;flex-direction:column;gap:6px">
    <div id="chat-media-preview" style="display:none"></div>
    <div style="display:flex;align-items:center;gap:8px">
      <button type="button" class="media-attach-btn" id="chat-attach-btn" title="Attach media"><svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
      <input type="file" id="chat-file-input" style="display:none" accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.gif">
      <textarea id="cr-txt" placeholder="Message…" rows="1" style="flex:1;background:var(--bg2);border:0.5px solid var(--border);border-radius:20px;padding:10px 16px;color:var(--text);font-family:'Inter',sans-serif;font-size:14px;outline:none;resize:none;min-height:40px;max-height:100px;"></textarea>
      <button type="button" class="voice-record-btn" id="chat-voice-btn" title="Voice message"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></button>
      <button class="cr-send" onclick="crSend()"><svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
    </div>
  </div>
</div>

<div id="profileModal" class="modal-mask" onclick="closeProfileModal(event)"><div class="modal-container" onclick="event.stopPropagation()"><span class="modal-close" onclick="closeProfileModal()">&times;</span><div id="modalContent">Loading...</div></div></div>
<div id="toast"></div>
<div id="lightbox" class="lightbox" onclick="closeLightbox(event)">
  <div class="lightbox-close" onclick="closeLightbox(event)">&times;</div>
  <img id="lightbox-img" src="" alt="">
</div>
<div id="voice-timer" class="voice-record-timer"><span id="voice-time">0:00</span><span class="cancel-hint">⬆️ swipe up to cancel</span></div>

<script>
'use strict';
const API = location.origin;
let UID = null, profileCache = null, crPartnerId = null, crPoll = null, currentPostAuthorId = null;
let pendingMedia = null, pendingCommentMedia = null, pendingChatMedia = null;
let feedPage = 1, feedHasMore = true, feedLoading = false, searchQ = '', currentPostId = null;
let chatsCache = [];
const selCats = new Set();
let selEmoji = null;

// Premium line-icon set (SVG) used in place of emoji across the app's UI chrome.
// Expressive/emotional content (reactions, avatar picker) intentionally keeps real emoji.
function ic(paths,opts){
  opts=opts||{};
  const vb=opts.viewBox||'0 0 24 24';
  const fill=opts.fill||'none';
  const sw=opts.strokeWidth||1.8;
  return `<svg viewBox="${vb}" fill="${fill}" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" class="icon">${paths}</svg>`;
}
const ICONS = {
  sparkles: ic('<path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5L12 3z"/><path d="M19 3l.5 1.5L21 5l-1.5.5L19 7l-.5-1.5L17 5l1.5-.5L19 3z"/>'),
  book: ic('<path d="M2 4h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 4h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>'),
  briefcase: ic('<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>'),
  feather: ic('<path d="M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5z"/><path d="M16 8L2 22"/><path d="M17.5 15H9"/>'),
  swords: ic('<path d="M14.5 17.5L3 6V3h3l11.5 11.5"/><path d="M13 19l6-6"/><path d="M16 16l4 4"/><path d="M19 21l2-2"/><path d="M9.5 6.5L21 18v3h-3L6.5 9.5"/>'),
  heart: ic('<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 1 0-7.8 7.8l1 1L12 21l7.8-7.8 1-1a5.5 5.5 0 0 0 0-7.8z"/>'),
  gem: ic('<path d="M6 3h12l4 6-10 12L2 9z"/><path d="M2 9h20"/><path d="M9 3l3 6-3 12"/><path d="M15 3l-3 6 3 12"/>'),
  users: ic('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'),
  dollar: ic('<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
  music: ic('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'),
  home: ic('<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>'),
  megaphone: ic('<path d="M3 11v3a1 1 0 0 0 1 1h2l3.5 5.5a1 1 0 0 0 1.5.2V4.3a1 1 0 0 0-1.5.2L6 10H4a1 1 0 0 0-1 1z"/><path d="M14 6.5v11a5 5 0 0 0 3-4.5v-2a5 5 0 0 0-3-4.5z"/>'),
  pill: ic('<path d="M10.5 20.5L20.5 10.5a4.95 4.95 0 1 0-7-7L3.5 13.5a4.95 4.95 0 1 0 7 7z"/><line x1="8.5" y1="8.5" x2="15.5" y2="15.5"/>'),
  bookmark: ic('<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>'),
  shield: ic('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'),
  crown: ic('<path d="M2 20h20l-2-9-5 4-3-7-3 7-5-4-2 9z"/>'),
  medal: ic('<circle cx="12" cy="15" r="6"/><path d="M9 10L6 2h4l2 4 2-4h4l-3 8"/>'),
  lock: ic('<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
  chat: ic('<path d="M21 11.5a8.38 8.38 0 0 1-4.7 7.6 8.38 8.38 0 0 1-3.8.9 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3h.5a8.48 8.48 0 0 1 8 8v.5z"/>'),
  mail: ic('<rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22 6 12 13 2 6"/>'),
  clock: ic('<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/>'),
  alert: ic('<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
  user: ic('<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>'),
  mic: ic('<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>'),
  paperclip: ic('<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'),
  close: ic('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  reply: ic('<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>'),
  thumbsUp: ic('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>'),
  thumbsDown: ic('<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/>')
};
function avaHtml(v){ return v ? esc(v) : ICONS.user; }

const CATS = [
  ['PrayForMe',ICONS.sparkles,'Pray For Me'],['Bible',ICONS.book,'Bible'],['WorkLife',ICONS.briefcase,'Work & Life'],
  ['SpiritualLife',ICONS.feather,'Spiritual Life'],['ChristianChallenges',ICONS.swords,'Challenges'],
  ['Relationship',ICONS.heart,'Relationship'],['Marriage',ICONS.gem,'Marriage'],['Youth',ICONS.users,'Youth'],
  ['Finance',ICONS.dollar,'Finance'],['WorshipMusic',ICONS.music,'Worship'],['Family',ICONS.home,'Family'],
  ['Testimony',ICONS.megaphone,'Testimony'],['AddictionRecovery',ICONS.pill,'Recovery'],
  ['BibleQuestion',ICONS.book,'Bible Q&A'],['Other',ICONS.bookmark,'Other']
];
const EMOJIS = ['🕊️','✝️','🙏','📖','❤️','🌟','🛡️','⚔️','⛪','🎹','👶','🧑','👴','🌿','🔥'];

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),3000)}
async function api(path,opts={}){
  const r=await fetch(API+path,{headers:{'Content-Type':'application/json'},...opts});
  const d=await r.json();if(!r.ok||!d.success)throw new Error(d.error||'Error');return d;
}

const MAX_MEDIA_BYTES=20*1024*1024;
async function uploadMedia(file, intent){
  if(!file)return null;
  if(file.size>MAX_MEDIA_BYTES)throw new Error('File too large (max 20MB)');
  const fd=new FormData();
  fd.append('file',file);
  fd.append('user_id',UID);
  if(intent) fd.append('intent', intent);
  const r=await fetch(API+'/api/mini-app/upload-media',{method:'POST',body:fd});
  const d=await r.json();
  if(!r.ok||!d.success)throw new Error(d.error||'Upload failed');
  return {media_type:d.media_type,media_id:d.file_id,name:file.name,previewUrl:URL.createObjectURL(file)};
}

function renderMediaPreview(container,media,onRemove){
  if(!media){container.style.display='none';container.innerHTML='';container.classList.remove('media-preview');return;}
  container.classList.add('media-preview');
  container.style.display='flex';
  const isImageLike = media.media_type==='photo'||media.media_type==='sticker'||media.media_type==='gif';
  const isVoice = media.media_type==='voice'||media.media_type==='audio';
  const thumb = isImageLike ? `<img src="${media.previewUrl}">`
    : `<span style="width:36px;height:36px;border-radius:8px;background:var(--bg3);display:flex;align-items:center;justify-content:center;flex-shrink:0">${isVoice?ICONS.mic:ICONS.paperclip}</span>`;
  const label = isVoice ? `Voice message${media.duration?` · ${media.duration}`:''}` : media.name;
  container.innerHTML=`${thumb}<span class="mp-name">${esc(label)}</span><button class="mp-remove" type="button">${ICONS.close}</button>`;
  container.querySelector('.mp-remove').onclick=onRemove;
}

function renderMedia(mediaType,mediaId){
  if(!mediaId||!mediaType||mediaType==='text')return '';
  const src=`/api/mini-app/file/${encodeURIComponent(mediaId)}`;
  if(mediaType==='photo')return `<div class="post-media"><img src="${src}" loading="lazy" onclick="event.stopPropagation();openLightbox('${src}')" style="cursor:zoom-in"></div>`;
  if(mediaType==='gif')return `<div class="post-media"><video src="${src}" autoplay loop muted playsinline></video></div>`;
  if(mediaType==='sticker')return `<div class="post-media"><img class="sticker-media" src="${src}"></div>`;
  if(mediaType==='video')return `<div class="post-media"><video src="${src}" controls playsinline></video></div>`;
  if(mediaType==='voice'||mediaType==='audio')return renderCompactAudioPlayer(src);
  return `<div class="post-media"><a class="doc-link" href="${src}" target="_blank" rel="noopener"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Download attachment</a></div>`;
}

function openLightbox(src){
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('active');
}
function closeLightbox(e){
  if(e) e.stopPropagation();
  document.getElementById('lightbox').classList.remove('active');
  document.getElementById('lightbox-img').src = '';
}

function renderCompactAudioPlayer(src){
  const uid = 'v'+Math.random().toString(36).slice(2,9);
  return `<div class="voice-player">
    <audio class="voice-player-audio" id="${uid}" src="${src}" preload="metadata" playsinline></audio>
    <button type="button" class="voice-player-btn" aria-label="Play voice message">
      <svg class="icon-play" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"/></svg>
      <svg class="icon-pause" viewBox="0 0 24 24" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      <svg class="icon-spinner" viewBox="0 0 24 24" style="display:none"><circle cx="12" cy="12" r="9" opacity="0.25"/><path d="M21 12a9 9 0 0 0-9-9"/></svg>
    </button>
    <div class="voice-player-track"><div class="voice-player-progress"></div></div>
    <span class="voice-player-time">0:00</span>
  </div>`;
}

// ========== VOICE RECORDING (Telegram-style hold-to-record) ==========
let mediaRecorder = null;
let recordedChunks = [];
let recordingTimer = null;
let recordingStartTime = 0;
let currentVoiceTarget = null; // 'vent' | 'comment' | 'chat'
let voiceCancel = false;

function getPreferredVoiceMimeType() {
  const candidates = ['audio/ogg;codecs=opus', 'audio/webm;codecs=opus', 'audio/webm'];
  for (const type of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return '';
}
function setupVoiceButton(btnId, target) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  let pressTimer = null;
  let isPressed = false;
  let startY = 0;

  const startRecording = (e) => {
    e.preventDefault();
    if (mediaRecorder && mediaRecorder.state === 'recording') return;
    voiceCancel = false;
    currentVoiceTarget = target;
    isPressed = true;
    startY = e.type === 'touchstart' ? e.touches[0].clientY : e.clientY;
    // Start recording after a short hold (like Telegram)
    pressTimer = setTimeout(() => {
      if (isPressed) {
        btn.classList.add('recording');
        document.getElementById('voice-timer').classList.add('active');
        navigator.mediaDevices.getUserMedia({ audio: true })
  .then(stream => {
    recordedChunks = [];
    const preferredType = getPreferredVoiceMimeType();
    mediaRecorder = preferredType
      ? new MediaRecorder(stream, { mimeType: preferredType })
      : new MediaRecorder(stream);
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      btn.classList.remove('recording');
      document.getElementById('voice-timer').classList.remove('active');
      clearInterval(recordingTimer);
      const elapsed = Date.now() - recordingStartTime;
      if (!voiceCancel && elapsed >= 400 && recordedChunks.length) {
        const mimeType = mediaRecorder.mimeType || 'audio/webm';
        const ext = mimeType.includes('ogg') ? 'ogg' : 'webm';
        const rawBlob = new Blob(recordedChunks, { type: mimeType });
        const finalizeVoice = (blob) => {
          const file = new File([blob], `voice.${ext}`, { type: mimeType });
          handleVoiceFile(file, target);
        };
        if (mimeType.includes('webm') && window.ysFixWebmDuration) {
          ysFixWebmDuration(rawBlob, elapsed, { logger: false })
            .then(finalizeVoice)
            .catch(() => finalizeVoice(rawBlob));
        } else {
          finalizeVoice(rawBlob);
        }
      } else if (!voiceCancel && elapsed < 400) {
        toast('Recording too short');
      }
      recordedChunks = [];
    };
    mediaRecorder.start();
    recordingStartTime = Date.now();
    recordingTimer = setInterval(updateVoiceTimer, 200);
  })
  .catch(err => { toast('Microphone access denied'); });
      }
    }, 300);
  };

  const stopRecording = (e) => {
    e.preventDefault();
    clearTimeout(pressTimer);
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      // Check if swipe up to cancel (distance > 80px)
      const endY = e.type === 'touchend' ? e.changedTouches[0].clientY : e.clientY;
      if (startY - endY > 80) {
        voiceCancel = true;
        toast('Cancelled');
      }
      mediaRecorder.stop();
    }
    isPressed = false;
  };

  const cancelRecording = (e) => {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      voiceCancel = true;
      mediaRecorder.stop();
    }
    clearTimeout(pressTimer);
    isPressed = false;
  };

  btn.addEventListener('mousedown', startRecording);
  btn.addEventListener('mouseup', stopRecording);
  btn.addEventListener('mouseleave', cancelRecording);
  btn.addEventListener('touchstart', startRecording, { passive: false });
  btn.addEventListener('touchend', stopRecording, { passive: false });
  btn.addEventListener('touchcancel', cancelRecording, { passive: false });
}

function updateVoiceTimer() {
  const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
  const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
  const secs = String(elapsed % 60).padStart(2, '0');
  document.getElementById('voice-time').textContent = `${mins}:${secs}`;
}

async function handleVoiceFile(file, target) {
  try {
    const media = await uploadMedia(file, 'voice');
    if (target === 'vent') {
      pendingMedia = media;
      const preview = document.getElementById('vent-media-preview');
      renderMediaPreview(preview, pendingMedia, () => {
        pendingMedia = null;
        document.getElementById('vent-file-input').value = '';
        document.getElementById('vent-attach-btn').classList.remove('has-media');
        renderMediaPreview(preview, null);
      });
      document.getElementById('vent-attach-btn').classList.add('has-media');
    } else if (target === 'comment') {
      pendingCommentMedia = media;
      const preview = document.getElementById('comment-media-preview');
      renderMediaPreview(preview, pendingCommentMedia, () => {
        pendingCommentMedia = null;
        document.getElementById('comment-file-input').value = '';
        document.getElementById('comment-attach-btn').classList.remove('has-media');
        renderMediaPreview(preview, null);
      });
      document.getElementById('comment-attach-btn').classList.add('has-media');
    } else if (target === 'chat') {
      pendingChatMedia = media;
      const preview = document.getElementById('chat-media-preview');
      renderMediaPreview(preview, pendingChatMedia, () => {
        pendingChatMedia = null;
        document.getElementById('chat-file-input').value = '';
        document.getElementById('chat-attach-btn').classList.remove('has-media');
        renderMediaPreview(preview, null);
      });
      document.getElementById('chat-attach-btn').classList.add('has-media');
    }
  } catch (e) { toast(e.message); }
}

// ========== REACTIONS FOR POSTS (direct buttons) ==========
function renderReactionButtons(itemId, itemType, counts, userReaction) {
  const types = ['like', 'dislike', 'heart'];
  const labels = { like: ICONS.thumbsUp, dislike: ICONS.thumbsDown, heart: ICONS.heart };
  let html = '<div class="reaction-buttons">';
  for (const t of types) {
    const count = counts[t] || 0;
    const active = (userReaction === t) ? 'on' : '';
    html += `<button class="reaction-btn ${active}" data-type="${itemType}" data-id="${itemId}" data-emoji="${t}" onclick="toggleReaction(this, '${itemType}', ${itemId}, '${t}')">${labels[t]} ${count}</button>`;
  }
  html += '</div>';
  return html;
}

async function toggleReaction(btn, itemType, itemId, emoji) {
  const payload = { user_id: UID, type: emoji };
  if (itemType === 'post') payload.post_id = itemId;
  else payload.comment_id = itemId;
  
  const parent = btn.closest('.reaction-buttons');
  const allBtns = parent.querySelectorAll('.reaction-btn');
  const labels = { like: ICONS.thumbsUp, dislike: ICONS.thumbsDown, heart: ICONS.heart };
  
  try {
    const resp = await api('/api/mini-app/react', { method: 'POST', body: JSON.stringify(payload) });
    const counts = resp.reactions.counts;
    const userReaction = resp.reactions.user_reaction;
    
    allBtns.forEach(b => {
      const t = b.dataset.emoji;
      const count = counts[t] || 0;
      b.innerHTML = `${labels[t]} ${count}`;
      if (userReaction === t) b.classList.add('on');
      else b.classList.remove('on');
    });
  } catch (e) {
    toast(e.message);
  }
}

const ink=document.getElementById('nav-ink');
function go(name,btn){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  if(btn){
      btn.classList.add('active');
      const navItems = Array.from(btn.parentElement.querySelectorAll('.nav-item'));
      const index = navItems.indexOf(btn);
      if(index !== -1){
        ink.style.left = (index * 20) + '%';
      }
    }
  if(name==='feed'&&feedPage===1)loadFeed();
  if(name==='leaderboard')loadLB();
  if(name==='profile')loadProfile();
  if(name==='settings')loadSettings();
  if(name==='chats')loadChats();
  if(name==='admin-monitor')loadAdminChats();
  document.getElementById('pages').scrollTop=0;
  // Show/hide fixed comment bar
  const bar=document.getElementById('commentBar');
  if(name==='detail') bar.style.display='flex';
  else bar.style.display='none';
}
function gotoFeed(){go('feed',document.querySelector('[data-page="feed"]'));}

function renderCats(){
  const g=document.getElementById('cat-grid');
  g.innerHTML=CATS.map(([c,icon,l])=>`<div class="cat-chip" data-c="${c}" onclick="toggleCat(this,'${c}')"><div class="cat-check"></div>${icon}<span>${esc(l)}</span></div>`).join('');
}
function toggleCat(el,c){
  if(selCats.has(c)){selCats.delete(c);el.classList.remove('on')}
  else{selCats.add(c);el.classList.add('on')}
}

document.addEventListener('DOMContentLoaded',()=>{
  document.addEventListener('click', function(e){
    const btn = e.target.closest('.voice-player-btn');
    if(btn){
      const audio = btn.closest('.voice-player').querySelector('.voice-player-audio');
      const playIcon = btn.querySelector('.icon-play');
      const pauseIcon = btn.querySelector('.icon-pause');
      const spinnerIcon = btn.querySelector('.icon-spinner');

      // Every tap on a player bumps its "attempt" token. Any pending
      // canplaythrough/canplay/timeout callback from an earlier tap checks this
      // token before doing anything, so an abandoned load can never sneak in
      // and start audio playing "in the background" after the user moved on.
      const cancelAttempt = (a)=>{
        a.dataset.attempt = String((parseInt(a.dataset.attempt || '0', 10)) + 1);
        delete a.dataset.loading;
      };

      // Stop every OTHER player - whether currently playing or still loading -
      // so only one plays at a time and no abandoned load can fire later.
      document.querySelectorAll('.voice-player').forEach(p=>{
        const a = p.querySelector('.voice-player-audio');
        if(a===audio) return;
        if(!a.paused || a.dataset.loading==='1'){
          cancelAttempt(a);
          a.pause();
          p.querySelector('.icon-play').style.display='inline-block';
          p.querySelector('.icon-pause').style.display='none';
          p.querySelector('.icon-spinner').style.display='none';
        }
      });

      if(audio.dataset.loading==='1'){
        // Tapped again while it's still spinning/downloading - treat this as a
        // cancel back to idle, rather than stacking a second load attempt on
        // top of the first (which is what caused duplicate/erratic spinning).
        cancelAttempt(audio);
        spinnerIcon.style.display='none';
        pauseIcon.style.display='none';
        playIcon.style.display='inline-block';
        return;
      }

      if(audio.paused){
        const myAttempt = String((parseInt(audio.dataset.attempt || '0', 10)) + 1);
        audio.dataset.attempt = myAttempt;
        const isCurrent = ()=> audio.dataset.attempt === myAttempt;

        const showPlaying = ()=>{ if(!isCurrent())return; delete audio.dataset.loading; spinnerIcon.style.display='none'; playIcon.style.display='none'; pauseIcon.style.display='inline-block'; };
        const showFailed = ()=>{ if(!isCurrent())return; delete audio.dataset.loading; spinnerIcon.style.display='none'; pauseIcon.style.display='none'; playIcon.style.display='inline-block'; };

        if(audio.readyState >= 3){
          // Already buffered enough to play start-to-finish - go instantly, no spinner needed
          showPlaying();
          audio.play().catch(err=>{ console.error('Playback failed:', err); showFailed(); });
        } else {
          // Show the rotating spinner while the voice note downloads, then start
          // playback the instant it can play through without stalling partway.
          // Mark it as "loading" so a background poll can't tear down the DOM
          // (and abandon the download) before playback actually begins.
          audio.dataset.loading = '1';
          playIcon.style.display='none';
          pauseIcon.style.display='none';
          spinnerIcon.style.display='inline-block';
          const startOnce = ()=>{
            if(!isCurrent()) return; // this attempt was cancelled or superseded - do nothing
            showPlaying();
            audio.play().catch(err=>{ console.error('Playback failed:', err); showFailed(); });
          };
          audio.addEventListener('canplaythrough', startOnce, {once:true});
          // Fallback for browsers/files that never fire canplaythrough reliably
          audio.addEventListener('canplay', ()=>setTimeout(startOnce, 300), {once:true});
          setTimeout(startOnce, 4000); // last-resort so it never spins forever
          audio.preload = 'auto';
          audio.load();
        }
      } else {
        cancelAttempt(audio);
        audio.pause();
        playIcon.style.display='inline-block';
        pauseIcon.style.display='none';
        spinnerIcon.style.display='none';
      }
      return;
    }
    const track = e.target.closest('.voice-player-track');
    if(track){
      const audio = track.closest('.voice-player').querySelector('.voice-player-audio');
      const rect = track.getBoundingClientRect();
      const pct = Math.min(1, Math.max(0, (e.clientX-rect.left)/rect.width));
      if(audio.duration) audio.currentTime = pct*audio.duration;
    }
  });
  document.addEventListener('timeupdate', function(e){
    if(!e.target.classList?.contains('voice-player-audio')) return;
    const player = e.target.closest('.voice-player');
    if(!e.target.duration) return;
    player.querySelector('.voice-player-progress').style.width = (e.target.currentTime/e.target.duration*100)+'%';
    const remaining = e.target.duration - e.target.currentTime;
    const m = Math.floor(remaining/60), s = Math.floor(remaining%60);
    player.querySelector('.voice-player-time').textContent = `${m}:${String(s).padStart(2,'0')}`;
  }, true);
  document.addEventListener('waiting', function(e){
    // Mid-playback buffering stall (e.g. a network hiccup) - show the spinner
    // again instead of silently freezing, so it's clear more is loading.
    if(!e.target.classList?.contains('voice-player-audio')) return;
    e.target.dataset.loading = '1';
    const player = e.target.closest('.voice-player');
    player.querySelector('.icon-play').style.display='none';
    player.querySelector('.icon-pause').style.display='none';
    player.querySelector('.icon-spinner').style.display='inline-block';
  }, true);
  document.addEventListener('playing', function(e){
    if(!e.target.classList?.contains('voice-player-audio')) return;
    delete e.target.dataset.loading;
    const player = e.target.closest('.voice-player');
    player.querySelector('.icon-spinner').style.display='none';
    player.querySelector('.icon-play').style.display='none';
    player.querySelector('.icon-pause').style.display='inline-block';
  }, true);
  document.addEventListener('ended', function(e){
    if(!e.target.classList?.contains('voice-player-audio')) return;
    delete e.target.dataset.loading;
    const player = e.target.closest('.voice-player');
    player.querySelector('.icon-play').style.display='inline-block';
    player.querySelector('.icon-pause').style.display='none';
    player.querySelector('.icon-spinner').style.display='none';
    player.querySelector('.voice-player-progress').style.width='0%';
    e.target.currentTime = 0;
  }, true);
  document.addEventListener('error', function(e){
    if(!e.target.classList?.contains('voice-player-audio')) return;
    delete e.target.dataset.loading;
    const player = e.target.closest('.voice-player');
    player.querySelector('.icon-spinner').style.display='none';
    player.querySelector('.icon-play').style.display='inline-block';
    player.querySelector('.icon-pause').style.display='none';
    player.querySelector('.voice-player-time').textContent = 'Error';
  }, true);

  const txt=document.getElementById('vent-txt');
  if(txt)txt.addEventListener('input',()=>{document.getElementById('vent-cnt').textContent=txt.value.length});
  document.getElementById('submit-vent').addEventListener('click',submitVent);
  document.getElementById('load-more-btn').addEventListener('click',()=>loadFeed(true));
  document.getElementById('send-comment').addEventListener('click',postComment);
  document.getElementById('save-profile-btn').addEventListener('click',saveProfile);
  document.getElementById('save-settings-btn').addEventListener('click',saveSettings);
  let st;document.getElementById('search-inp').addEventListener('input',e=>{
    clearTimeout(st);searchQ=e.target.value.trim();st=setTimeout(()=>{feedPage=1;loadFeed()},500);
  });
  buildEmojiPicker();
  renderCats();
  const themeToggle=document.getElementById('set-theme');
  if(localStorage.getItem('theme')==='light') document.body.classList.add('light');
  if(themeToggle){
    themeToggle.checked=document.body.classList.contains('light');
    themeToggle.addEventListener('change',()=>{
      if(themeToggle.checked) document.body.classList.add('light');
      else document.body.classList.remove('light');
      localStorage.setItem('theme', themeToggle.checked?'light':'dark');
    });
  }
  // Initially hide comment bar
  document.getElementById('commentBar').style.display='none';

  // Vent page media attach
  const ventAttachBtn=document.getElementById('vent-attach-btn');
  const ventFileInput=document.getElementById('vent-file-input');
  const ventMediaPreview=document.getElementById('vent-media-preview');
  ventAttachBtn.addEventListener('click',()=>ventFileInput.click());
  ventFileInput.addEventListener('change',async()=>{
    const file=ventFileInput.files[0];if(!file)return;
    ventAttachBtn.disabled=true;
    try{
      pendingMedia=await uploadMedia(file);
      ventAttachBtn.classList.add('has-media');
      renderMediaPreview(ventMediaPreview,pendingMedia,()=>{
        pendingMedia=null;ventFileInput.value='';ventAttachBtn.classList.remove('has-media');
        renderMediaPreview(ventMediaPreview,null);
      });
    }catch(e){toast(e.message);ventFileInput.value=''}
    finally{ventAttachBtn.disabled=false}
  });

  // Comment bar media attach
  const commentAttachBtn=document.getElementById('comment-attach-btn');
  const commentFileInput=document.getElementById('comment-file-input');
  const commentMediaPreview=document.getElementById('comment-media-preview');
  commentAttachBtn.addEventListener('click',()=>commentFileInput.click());
  commentFileInput.addEventListener('change',async()=>{
    const file=commentFileInput.files[0];if(!file)return;
    commentAttachBtn.disabled=true;
    try{
      pendingCommentMedia=await uploadMedia(file);
      commentAttachBtn.classList.add('has-media');
      renderMediaPreview(commentMediaPreview,pendingCommentMedia,()=>{
        pendingCommentMedia=null;commentFileInput.value='';commentAttachBtn.classList.remove('has-media');
        renderMediaPreview(commentMediaPreview,null);
      });
    }catch(e){toast(e.message);commentFileInput.value=''}
    finally{commentAttachBtn.disabled=false}
  });

  // Chat page media attach
  const chatAttachBtn = document.getElementById('chat-attach-btn');
  const chatFileInput = document.getElementById('chat-file-input');
  const chatMediaPreview = document.getElementById('chat-media-preview');
  if (chatAttachBtn && chatFileInput) {
    chatAttachBtn.addEventListener('click', () => chatFileInput.click());
    chatFileInput.addEventListener('change', async () => {
      const file = chatFileInput.files[0]; if (!file) return;
      chatAttachBtn.disabled = true;
      try {
        pendingChatMedia = await uploadMedia(file);
        chatAttachBtn.classList.add('has-media');
        renderMediaPreview(chatMediaPreview, pendingChatMedia, () => {
          pendingChatMedia = null; chatFileInput.value = ''; chatAttachBtn.classList.remove('has-media');
          renderMediaPreview(chatMediaPreview, null);
        });
      } catch (e) { toast(e.message); chatFileInput.value = ''; }
      finally { chatAttachBtn.disabled = false; }
    });
  }
});

async function submitVent(){
  const txt=document.getElementById('vent-txt').value.trim();
  const cats=[...selCats];
  if(!txt&&!pendingMedia)return toast('Write something first');
  if(!cats.length)return toast('Pick at least one category');
  const btn=document.getElementById('submit-vent');
  btn.disabled=true;btn.textContent='Posting…';
  try{
    const payload={user_id:UID,content:txt,categories:cats,explicit:document.getElementById('vent-explicit-check').checked};
    if(pendingMedia){payload.media_type=pendingMedia.media_type;payload.media_id=pendingMedia.media_id}
    await api('/api/mini-app/submit-vent',{method:'POST',body:JSON.stringify(payload)});
    toast('✅ Shared — awaiting review');
    document.getElementById('vent-txt').value='';
    document.getElementById('vent-cnt').textContent='0';
    document.getElementById('vent-explicit-check').checked=false;
    selCats.clear();document.querySelectorAll('.cat-chip').forEach(c=>c.classList.remove('on'));
    pendingMedia=null;document.getElementById('vent-file-input').value='';
    document.getElementById('vent-attach-btn').classList.remove('has-media');
    renderMediaPreview(document.getElementById('vent-media-preview'),null);
  }catch(e){toast(e.message)}
  finally{btn.disabled=false;btn.textContent='Post Anonymously'}
}

async function loadFeed(append=false){
  if(feedLoading)return;feedLoading=true;
  const list=document.getElementById('feed-list');
  const more=document.getElementById('feed-more');
  if(!append){
    list.innerHTML=skelPosts(3);more.style.display='none';
  } else {
    const loadBtn=document.getElementById('load-more-btn');
    loadBtn.disabled=true;loadBtn.textContent='Loading…';
    list.insertAdjacentHTML('beforeend', `<div id="feed-load-skel">${skelPosts(2)}</div>`);
  }
  try{
    let url=`/api/mini-app/get-posts?page=${feedPage}&user_id=${UID}`;
    if(searchQ)url=`/api/mini-app/search?q=${encodeURIComponent(searchQ)}&page=${feedPage}&user_id=${UID}`;
    const d=await api(url);
    const posts=d.data||[];feedHasMore=d.has_more;
    if(!append){list.innerHTML='';}
    else{const sk=document.getElementById('feed-load-skel');if(sk)sk.remove();}
    if(!posts.length&&!append){list.innerHTML='<div style="text-align:center;padding:40px 20px;color:var(--text3);font-size:14px">Nothing here yet</div>';return}
    posts.forEach(p=>list.insertAdjacentHTML('beforeend',renderPost(p)));
    more.style.display=feedHasMore?'block':'none';
    if(feedHasMore)feedPage++;
  }catch(e){
    const sk=document.getElementById('feed-load-skel');if(sk)sk.remove();
    if(!append)list.innerHTML='<div style="text-align:center;padding:40px;color:var(--text3)">Failed to load</div>'
  }
  finally{
    feedLoading=false;
    const loadBtn=document.getElementById('load-more-btn');
    loadBtn.disabled=false;loadBtn.textContent='Load more';
  }
}

function renderPost(p){
  const cats=(p.categories||[]).map(c=>`<span class="pill pill-sm">${esc(c)}</span>`).join('');
  const unread=p.unread_comments>0?`<span class="pill pill-sm" style="background:rgba(201,168,76,0.2);border-color:var(--gold)">${p.unread_comments} new</span>`:'';
  let reactionsHtml='';
  if(p.reactions&&p.reactions.counts){
    for(let [emoji,count] of Object.entries(p.reactions.counts)){
      if(count>0){
        const activeClass=p.reactions.user_reaction===emoji?'on':'';
        reactionsHtml+=`<span class="rx-pill ${activeClass}" data-type="post" data-id="${p.id}" data-emoji="${emoji}">${esc(emoji)} ${count}</span>`;
      }
    }
  }
  return `<div class="post-card">
    <div class="post-meta"><div class="ava" style="width:34px;height:34px">${avaHtml(p.author?.avatar||p.author?.sex)}</div><div><div class="post-name"${p.author?.is_admin ? '' : ` onclick="event.stopPropagation(); showUserProfile('${p.author?.id}')"`}>${esc(p.author?.name||'Anonymous')} <span style="font-size:12px">${esc(p.author?.aura||'')}</span></div></div><div class="post-time">${esc(p.time_ago||'')}</div></div>
    ${cats?`<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px">${cats}</div>`:''}
    <div class="post-body" onclick="openPost(${p.id})">${esc(p.content)}</div>
    ${p.media_id?`<div onclick="openPost(${p.id})">${renderMedia(p.media_type,p.media_id)}</div>`:''}
    <div onclick="event.stopPropagation();">
      ${renderReactionButtons(p.id, 'post', p.reactions?.counts || {}, p.reactions?.user_reaction)}
    </div>
    <div class="post-footer" onclick="openPost(${p.id})"><div class="post-footer-left"><span class="stat-btn"><svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>${p.comments||0}</span>${unread}</div><span class="read-more">Read →</span></div>
  </div>`;
}

async function openPost(id, reveal){
  currentPostId=id;go('detail',null);
  document.getElementById('detail-post').innerHTML=skelPosts(1);
  document.getElementById('detail-comments').innerHTML=skelComments(3);
  try{
    const revealParam = reveal ? '&reveal=1' : '';
    const d=await api(`/api/mini-app/post/${id}?viewer_id=${UID}${revealParam}`);
    const p=d.data;
    if(p.deleted){
      currentPostAuthorId = null;
      document.getElementById('detail-post').innerHTML=`
        <div class="post-card" style="cursor:default;margin-bottom:0;border-radius:0;margin:0;border-left:none;border-right:none;border-top:none;background:var(--glass2)">
          <div style="font-size:15px;line-height:1.65;color:var(--text3);font-style:italic;padding:16px;display:flex;align-items:center;gap:8px;"><span style="width:18px;height:18px;flex-shrink:0;display:inline-flex">${ICONS.alert}</span> This post has been deleted by the author.</div>
        </div>`;
      const cd=await api(`/api/mini-app/post/${id}/comments?viewer_id=${UID}${revealParam}`);
      renderComments(cd.data||[],null);
      return;
    }
    if(p.content_hidden){
      currentPostAuthorId = p.author_id;
      document.getElementById('detail-post').innerHTML=`
        <div class="post-card" style="cursor:default;margin-bottom:0;border-radius:0;margin:0;border-left:none;border-right:none;border-top:none;background:var(--glass2)">
          <div style="padding:20px;text-align:center">
            <div style="width:32px;height:32px;margin:0 auto 8px;color:var(--gold)">${ICONS.alert}</div>
            <div style="font-size:14px;font-weight:600;color:var(--text);margin-bottom:6px">Explicit Content Warning</div>
            <div style="font-size:13px;color:var(--text3);margin-bottom:14px">${esc(p.content)}</div>
            <button class="btn-gold" onclick="openPost(${id},true)">View Content</button>
          </div>
        </div>`;
      document.getElementById('detail-comments').innerHTML='<div style="text-align:center;padding:20px;color:var(--text3);font-size:13px">Comments are hidden until you view the post.</div>';
      return;
    }
    currentPostAuthorId = p.author_id;
    const cats=(p.categories||[]).map(c=>`<span class="pill pill-sm">${esc(c)}</span>`).join('');
    let reactionsHtml='';
    if(p.reactions&&p.reactions.counts){
      for(let [emoji,count] of Object.entries(p.reactions.counts)){
        if(count>0){
          const activeClass=p.reactions.user_reaction===emoji?'on':'';
          reactionsHtml+=`<span class="rx-pill ${activeClass}" data-type="post" data-id="${p.id}" data-emoji="${emoji}">${esc(emoji)} ${count}</span>`;
        }
      }
    }
    const explicitTag=p.explicit?`<div style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:var(--gold);border:1px solid var(--gold);border-radius:10px;padding:2px 8px;margin-bottom:8px">${ICONS.alert.replace('class="icon"','class="icon badge-icon"')} Explicit</div>`:'';
    document.getElementById('detail-post').innerHTML=`
      <div class="post-card" style="cursor:default;margin-bottom:0;border-radius:0;margin:0;border-left:none;border-right:none;border-top:none;background:var(--glass2)">
        <div class="post-meta"><div class="ava" style="width:38px;height:38px">${avaHtml(p.author?.avatar||p.author?.sex)}</div><div><div class="post-name" style="font-size:14px;cursor:pointer"${p.author?.is_admin ? '' : ` onclick="showUserProfile('${p.author?.id}')"`}>${ICONS.shield.replace('class="icon"','class="icon badge-icon"')} Vent author</div><div style="font-size:11px;color:var(--text3)">${esc(p.time_ago||'')}</div></div></div>
        ${explicitTag}
        ${cats?`<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:12px">${cats}</div>`:''}
        <div style="font-size:15px;line-height:1.65;color:var(--text)">${esc(p.content)}</div>
        ${p.media_id?renderMedia(p.media_type,p.media_id):''}
        <div>
          ${renderReactionButtons(p.id, 'post', p.reactions?.counts || {}, p.reactions?.user_reaction)}
        </div>
      </div>`;
    const cd=await api(`/api/mini-app/post/${id}/comments?viewer_id=${UID}${revealParam}`);
    renderComments(cd.data||[],p.author_id);
    renderComments(cd.data||[],p.author_id);
  }catch(e){document.getElementById('detail-post').innerHTML='<div style="padding:20px;color:var(--text3)">Could not load</div>'}
}

function renderComments(comments,postAuthorId){
  const box=document.getElementById('detail-comments');
  if(!comments.length){box.innerHTML='<div style="text-align:center;padding:30px 20px;color:var(--text3);font-size:14px">No responses yet — be the first!</div>';return}
  const map={};comments.forEach(c=>map[c.id]={...c,children:[]});
  const roots=[];comments.forEach(c=>c.parent_id&&map[c.parent_id]?map[c.parent_id].children.push(map[c.id]):roots.push(map[c.id]));
  const rr=(c,dep)=>{
    const isAuthor=String(c.author_id)===String(postAuthorId);
    const nameBadge=isAuthor?ICONS.shield.replace('class="icon"','class="icon badge-icon"'):'';
    const name=isAuthor?'Vent author':(c.author?.name||'Anonymous');
    const mine=String(c.author_id)===String(UID);
    let reactionsHtml='';
    if(c.reactions&&c.reactions.counts){
      for(let [emoji,count] of Object.entries(c.reactions.counts)){
        if(count>0){
          const activeClass=c.reactions.user_reaction===emoji?'on':'';
          reactionsHtml+=`<span class="rx-pill ${activeClass}" data-type="comment" data-id="${c.id}" data-emoji="${emoji}">${esc(emoji)} ${count}</span>`;
        }
      }
    }
    return `<div class="comment-item${dep>0?' reply':''}"><div class="ava" style="width:28px;height:28px;font-size:13px">${avaHtml(c.author?.sex)}</div><div class="comment-body"><div class="comment-name"${c.author?.is_admin ? '' : ` onclick="showUserProfile('${c.author_id}')"`}>${nameBadge}${esc(name)} <span style="font-size:10px;color:var(--text3)">${esc(c.time_ago||'')}</span></div><div class="comment-text">${esc(c.content)}</div>${c.media_id?renderMedia(c.media_type,c.media_id):''}
      ${renderReactionButtons(c.id, 'comment', c.reactions?.counts || {}, c.reactions?.user_reaction)}
      <div class="comment-actions"><button class="ca-btn" onclick="replyTo(${c.id})">${ICONS.reply} Reply</button>${mine?`<button class="ca-btn" onclick="delComment(${c.id})">Delete</button>`:''}</div></div></div>${c.children.map(ch=>rr(ch,dep+1)).join('')}`;
  };
  box.innerHTML=roots.map(c=>rr(c,0)).join('');
}

async function submitReaction(targetType,targetId,emoji,uiElement){
  try{
    const payload={user_id:UID,type:emoji};
    if(targetType==='post') payload.post_id=parseInt(targetId);
    else payload.comment_id=parseInt(targetId);
    const resp=await api('/api/mini-app/react',{method:'POST',body:JSON.stringify(payload)});
    if(resp.success){
      const container=uiElement.closest('.reactions-container');
      if(container){
        let html='';
        for(let [em,cnt] of Object.entries(resp.reactions.counts)){
          if(cnt>0){
            const activeClass=resp.reactions.user_reaction===em?'on':'';
            html+=`<span class="rx-pill ${activeClass}" data-type="${targetType}" data-id="${targetId}" data-emoji="${em}">${esc(em)} ${cnt}</span>`;
          }
        }
        const triggerBtn=container.querySelector('.reaction-trigger');
        container.innerHTML=html;
        if(triggerBtn) container.appendChild(triggerBtn);
      }
    }
  }catch(e){toast(e.message);}
}
function showReactionDock(anchor,targetType,targetId){
  const existing=document.querySelector('.rx-dock');
  if(existing) existing.remove();
  const dock=document.createElement('div');dock.className='rx-dock';
  const emojis=['🙏','❤️','🔥','😢','😡','👎'];
  emojis.forEach(e=>{
    const sp=document.createElement('span');sp.className='rx-emoji';sp.textContent=e;
    sp.onclick=async (ev)=>{
      ev.stopPropagation();dock.remove();
      await submitReaction(targetType,targetId,e,anchor);
    };
    dock.appendChild(sp);
  });
  anchor.parentNode.style.position='relative';
  anchor.parentNode.appendChild(dock);
  setTimeout(()=>{const remover=()=>{if(dock.parentNode)dock.remove(); document.removeEventListener('click',remover);}; document.addEventListener('click',remover);},50);
}

let replyToId=0;
function replyTo(id){replyToId=id;const t=document.getElementById('comment-txt');t.placeholder='Replying…';t.focus()}
async function postComment(){
  const txt=document.getElementById('comment-txt').value.trim();
  if((!txt&&!pendingCommentMedia)||!currentPostId)return;
  const btn=document.getElementById('send-comment');btn.disabled=true;
  try{
    const payload={user_id:UID,content:txt,parent_comment_id:replyToId};
    if(pendingCommentMedia){payload.media_type=pendingCommentMedia.media_type;payload.media_id=pendingCommentMedia.media_id}
    await api(`/api/mini-app/post/${currentPostId}/comment`,{method:'POST',body:JSON.stringify(payload)});
    document.getElementById('comment-txt').value='';replyToId=0;toast('Posted');
    pendingCommentMedia=null;document.getElementById('comment-file-input').value='';
    document.getElementById('comment-attach-btn').classList.remove('has-media');
    renderMediaPreview(document.getElementById('comment-media-preview'),null);
    const cd=await api(`/api/mini-app/post/${currentPostId}/comments?viewer_id=${UID}`);
    renderComments(cd.data||[],currentPostAuthorId);
  }catch(e){toast(e.message)}finally{btn.disabled=false}
}
async function delComment(id){
  if(!confirm('Delete this response?'))return;
  try{await api(`/api/mini-app/comment/${id}`,{method:'DELETE',body:JSON.stringify({user_id:UID})});
    toast('Deleted');const cd=await api(`/api/mini-app/post/${currentPostId}/comments?viewer_id=${UID}`);
    renderComments(cd.data||[],currentPostAuthorId);}catch(e){toast(e.message)}
}

async function loadLB(){
  const box=document.getElementById('lb-content');box.innerHTML=skelLB();
  try{
    const d=await api('/api/mini-app/leaderboard');
    const users=d.data||[];
    if(!users.length){box.innerHTML='<div style="text-align:center;padding:40px;color:var(--text3)">No data yet</div>';return}
    const [g,s,b,...rest]=users;
    let html='';
    if(g){const crownHtml=g.weekly_badge?esc(g.weekly_badge):ICONS.crown;html+=`<div class="lb-hero"><span class="lb-crown">${crownHtml}</span><div class="lb-top-name">${esc(g.name)}</div><div class="lb-top-pts">${esc(g.aura)} ${g.points} pts</div><div class="lb-medals">${s?`<div class="lb-medal-card"><div class="lb-medal-rank silver">${ICONS.medal}</div><div class="lb-medal-name">${esc(s.name)}</div><div class="lb-medal-pts">${s.points} pts</div></div>`:''}${b?`<div class="lb-medal-card"><div class="lb-medal-rank bronze">${ICONS.medal}</div><div class="lb-medal-name">${esc(b.name)}</div><div class="lb-medal-pts">${b.points} pts</div></div>`:''}</div></div>`}
    if(rest.length){
      html+='<div class="section-label">More contributors</div><div class="lb-list card">';
      rest.forEach((u,i)=>{html+=`<div class="lb-row"><div class="lb-rank">${i+4}</div><div class="ava" style="width:36px;height:36px">${avaHtml(u.avatar||u.sex)}</div><div class="lb-info"><div class="lb-info-name" onclick="showUserProfile('${u.id}')">${esc(u.weekly_badge||'')} ${esc(u.name)}</div><div class="lb-info-aura">${esc(u.aura)}</div></div><div class="lb-pts">${u.points}</div></div>`});
      html+='</div>';
    }
    box.innerHTML=html;
  }catch(e){box.innerHTML='<div style="text-align:center;padding:40px;color:var(--text3)">Failed to load</div>'}
}

async function loadProfile(){
  if(!UID)return;
  const box=document.getElementById('profile-content');box.innerHTML=skelProfile();
  try{
    const d=await api(`/api/mini-app/profile/${UID}?viewer_id=${UID}`);
    profileCache=d.data;const p=d.data;
    const postsR=await api(`/api/mini-app/get-posts?user_id=${UID}&page=1`);
    const myPosts=(postsR.data||[]).filter(x=>x.author?.is_me);
    box.innerHTML=`
      <div class="profile-hero"><div style="position:absolute;top:16px;right:16px"><button class="btn-ghost" onclick="setupEdit()" style="font-size:12px;padding:6px 12px">Edit</button></div>
      <div class="profile-ava-wrap">${avaHtml(p.avatar||p.sex)}</div>
      <div class="profile-name">${esc(p.weekly_badge||'')} ${esc(p.name)}</div>
      <div style="margin-top:6px"><span class="pill-aura"><span class="pill-aura-badge">${esc(p.aura)}</span><svg class="bolt-icon" viewBox="0 0 24 24"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg><span class="pill-aura-pts">${p.rating} pts</span></span></div>
      <div class="profile-stats"><div class="profile-stat"><div class="profile-stat-num">${p.stats?.posts||0}</div><div class="profile-stat-lbl">Vents</div></div><div class="profile-stat"><div class="profile-stat-num">${p.stats?.followers||0}</div><div class="profile-stat-lbl">Followers</div></div><div class="profile-stat"><div class="profile-stat-num">${p.stats?.comments||0}</div><div class="profile-stat-lbl">Replies</div></div></div></div>
      ${myPosts.length?`<div class="section-label">My recent vents</div><div style="padding:0 16px">${myPosts.slice(0,3).map(p=>`<div class="post-card" onclick="openPost(${p.id})" style="margin:0 0 10px"><div class="post-body" style="-webkit-line-clamp:2">${esc(p.content)}</div><div style="font-size:11px;color:var(--text3);margin-top:6px">${esc(p.time_ago)}</div></div>`).join('')}</div>`:''}
    `;
  }catch(e){box.innerHTML='<div style="padding:40px;text-align:center;color:var(--text3)">Could not load profile</div>'}
}

function setupEdit(){
  const p=profileCache;if(!p)return;
  document.getElementById('ep-name').value=p.name||'';
  document.getElementById('ep-bio').value=p.bio||'';
  selEmoji=p.avatar||null;
  buildEmojiPicker();
  go('edit',null);
}
function buildEmojiPicker(){
  const g=document.getElementById('ep-emoji');if(!g)return;
  g.innerHTML=EMOJIS.map(e=>`<div class="emoji-opt${selEmoji===e?' sel':''}" onclick="pickEmoji(this,'${e}')">${e}</div>`).join('');
}
function pickEmoji(el,e){ selEmoji=e; document.querySelectorAll('.emoji-opt').forEach(x=>x.classList.remove('sel')); el.classList.add('sel'); }
async function saveProfile(){
  const name=document.getElementById('ep-name').value.trim();
  if(!name)return toast('Name required');
  const btn=document.getElementById('save-profile-btn');btn.disabled=true;
  try{
    await api(`/api/mini-app/profile/${UID}`,{method:'PUT',body:JSON.stringify({name,bio:document.getElementById('ep-bio').value.trim(),avatar:selEmoji})});
    toast('Profile updated');go('profile',document.querySelector('[data-page="settings"]'));
  }catch(e){toast(e.message)}finally{btn.disabled=false}
}

async function loadSettings(){
  try{const d=await api(`/api/mini-app/settings/${UID}`);
    document.getElementById('set-notif').checked=d.data.notifications;
    document.getElementById('set-priv').checked=d.data.privacy_public;
  }catch(e){}
}
async function saveSettings(){
  const btn=document.getElementById('save-settings-btn');btn.disabled=true;
  try{
    await api(`/api/mini-app/settings/${UID}`,{method:'POST',body:JSON.stringify({notifications:document.getElementById('set-notif').checked,privacy_public:document.getElementById('set-priv').checked})});
    toast('Saved');
  }catch(e){toast(e.message)}finally{btn.disabled=false}
}

let isAdminUser = false;
let adminMonitorPoll = null;
let adminViewingPair = null;

async function checkAdminStatus(){
  try{
    const d = await api(`/api/mini-app/profile/${UID}?viewer_id=${UID}`);
    isAdminUser = !!d.data.is_admin;
    if(!isAdminUser) return;

    // Insert before nav-ink shifts break — recompute ink width for 6 items
    document.getElementById('nav').insertAdjacentHTML('beforeend',
      `<button class="nav-item" data-page="admin-monitor" onclick="go('admin-monitor',this)">
        <svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>Monitor
      </button>`);
    document.querySelectorAll('.nav-item').forEach(b=>{ b.style.fontSize='9px'; });
    document.getElementById('nav-ink').style.width='16.66%';

    document.getElementById('pages').insertAdjacentHTML('beforeend', `
      <div class="page" id="page-admin-monitor">
        <div class="page-head-wrap"><div class="page-head" style="padding-top:24px">
          <div><h1>Chat Monitor</h1><div class="page-head-sub">Admin oversight — live</div></div>
        </div></div>
        <div class="search-wrap">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="22" y2="22"/></svg>
          <input id="admin-search-inp" type="text" placeholder="Search by name or user ID…">
        </div>
        <div id="admin-chats-list"></div>
      </div>`);

    let st;
    document.getElementById('admin-search-inp').addEventListener('input', e=>{
      clearTimeout(st);
      st = setTimeout(()=>loadAdminChats(e.target.value.trim()), 400);
    });
  }catch(e){console.error('checkAdminStatus failed:', e);}
}

async function loadAdminChats(search=''){
  const list = document.getElementById('admin-chats-list');
  try{
    const q = search ? `&search=${encodeURIComponent(search)}` : '';
    const d = await api(`/api/mini-app/admin/chats?admin_id=${UID}&page=1${q}`);
    const convos = d.data || [];
    if(!convos.length){
      list.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text3)">No conversations found</div>';
      return;
    }
    list.innerHTML = convos.map(c => `
      <div class="chat-item" onclick="openAdminTranscript('${c.user_a}','${c.user_b}','${esc(c.name_a)}','${esc(c.name_b)}')">
        <div class="ava" style="width:44px;height:44px;font-size:14px">${esc(c.avatar_a)}${esc(c.avatar_b)}</div>
        <div class="chat-item-right">
          <div class="chat-item-top">
            <span class="chat-item-name">${esc(c.name_a)} ↔ ${esc(c.name_b)}</span>
            <span class="chat-item-time">${c.msg_count} msgs</span>
          </div>
          <div class="chat-item-preview">${esc(c.last_content || ('[' + (c.last_media_type || 'media') + ']'))}</div>
        </div>
      </div>`).join('');
  }catch(e){
    list.innerHTML = '<div style="padding:20px;color:var(--text3)">Failed to load</div>';
  }
}

function openAdminTranscript(userA, userB, nameA, nameB){
  adminViewingPair = [userA, userB];
  document.getElementById('cr-name').textContent = `🔴 ${nameA} ↔ ${nameB}`;
  document.getElementById('cr-ava').innerHTML = ICONS.shield;
  document.getElementById('chat-room').classList.add('open');
  document.querySelector('.cr-input').style.display = 'none'; // admins observe, don't send
  fetchAdminTranscript(true);
  clearInterval(adminMonitorPoll);
  adminMonitorPoll = setInterval(fetchAdminTranscript, 4000);
}

async function fetchAdminTranscript(scroll=false){
  if(!adminViewingPair) return;
  const [a, b] = adminViewingPair;
  try{
    const box = document.getElementById('cr-msgs');
    // Same fix as fetchCRMsgs: don't tear down the DOM while a voice note is
    // actively playing OR still downloading (spinner phase) - the innerHTML
    // replace recreates every <audio> element from scratch, which both kills
    // playback and abandons an in-flight download before it can ever start.
    const isBusyVoice = Array.from(box.querySelectorAll('.voice-player-audio')).some(el=>!el.paused || el.dataset.loading==='1');
    if(isBusyVoice) return;
    const d = await api(`/api/mini-app/admin/chats/${a}/${b}?admin_id=${UID}&limit=100`);
    const wasBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 80;
    box.innerHTML = (d.data || []).map(m => `
      <div class="msg-row ${String(m.sender_id)===String(a) ? 'them' : 'me'}">
        <div class="msg-bubble">${esc(m.content||'')}${m.media_id ? renderMedia(m.media_type, m.media_id) : ''}</div>
        <div class="msg-time">${esc(m.time_display||'')}</div>
      </div>`).join('');
    if(scroll || wasBottom) box.scrollTop = box.scrollHeight;
  }catch(e){}
}

async function loadChats(){
  const list=document.getElementById('chats-list');list.innerHTML=skelChats();
  try{
    const d=await api(`/api/mini-app/chats?user_id=${UID}`);
    const chats=d.data||[];
    const unread=chats.reduce((a,c)=>a+(c.unread_count||0),0);
    document.getElementById('chat-unread-label').textContent=unread?`${unread} unread message${unread>1?'s':''}`:'All caught up';
    if(!chats.length){list.innerHTML='<div style="text-align:center;padding:40px;color:var(--text3);font-size:14px">No messages yet</div>';return}
    chatsCache=chats;
    list.innerHTML=chats.map(c=>`<div class="chat-item" onclick="openCR('${c.partner_id}')"><div class="ava" style="width:44px;height:44px;font-size:18px">${avaHtml(c.partner_avatar||c.partner_sex)}</div><div class="chat-item-right"><div class="chat-item-top"><span class="chat-item-name">${esc(c.partner_name||'Anonymous')}</span><span class="chat-item-time">${esc(c.time_ago||'')}</span></div><div style="display:flex;align-items:center"><div class="chat-item-preview">${c.is_mine?'You: ':''}${esc(c.last_message||'')}</div>${c.unread_count>0?`<span class="unread-badge" style="margin-left:8px">${c.unread_count}</span>`:''}</div></div></div>`).join('');
  }catch(e){list.innerHTML='<div style="padding:20px;color:var(--text3)">Failed to load</div>'}
}

function openCR(pid,name,ava){
  crPartnerId=pid;
  if(name===undefined){
    const c=chatsCache.find(x=>String(x.partner_id)===String(pid));
    name=c?(c.partner_name||'Anonymous'):'Chat';
    ava=c?(c.partner_avatar||c.partner_sex):null;
  }
  document.getElementById('cr-name').textContent=name;
  document.getElementById('cr-ava').innerHTML=avaHtml(ava);
  document.getElementById('chat-room').classList.add('open');
  document.getElementById('cr-txt').value='';
  fetchCRMsgs(true);
  clearInterval(crPoll);crPoll=setInterval(fetchCRMsgs,3000);
}
function closeCR(){
  document.getElementById('chat-room').classList.remove('open');
  clearInterval(crPoll);crPartnerId=null;loadChats();
}
async function fetchCRMsgs(scroll=false){
  if(!crPartnerId)return;
  try{
    const box=document.getElementById('cr-msgs');
    // Don't tear down the message list while a voice note is actively playing
    // OR still downloading (spinner phase) - the innerHTML replace below
    // recreates every <audio> element from scratch, which both stops audio
    // the instant a poll tick lands and abandons a download mid-flight.
    // Skip this refresh cycle; the next poll picks up new messages once it's done.
    const isBusyVoice = Array.from(box.querySelectorAll('.voice-player-audio')).some(a=>!a.paused || a.dataset.loading==='1');
    if(isBusyVoice) return;
    const d=await api(`/api/mini-app/chats/${crPartnerId}?user_id=${UID}`);
    const wasBottom=box.scrollHeight-box.scrollTop<=box.clientHeight+80;
    box.innerHTML=(d.data||[]).map(m=>`<div class="msg-row ${m.is_mine?'me':'them'}"><div class="msg-bubble">${esc(m.content)}${m.media_id?renderMedia(m.media_type,m.media_id):''}</div><div class="msg-time">${esc(m.timestamp||'')}</div></div>`).join('');
    if(scroll||wasBottom)box.scrollTop=box.scrollHeight;
  }catch(e){}
}
async function crSend(){
  const txt=document.getElementById('cr-txt').value.trim();
  if((!txt&&!pendingChatMedia)||!crPartnerId)return;
  
  const payload = {sender_id:UID, receiver_id:crPartnerId, content:txt};
  if(pendingChatMedia) {
    payload.media_type = pendingChatMedia.media_type;
    payload.media_id = pendingChatMedia.media_id;
  }
  
  document.getElementById('cr-txt').value='';
  try{
    await api('/api/mini-app/chats/send',{method:'POST',body:JSON.stringify(payload)});
    if(pendingChatMedia) {
      pendingChatMedia = null;
      document.getElementById('chat-file-input').value = '';
      document.getElementById('chat-attach-btn').classList.remove('has-media');
      renderMediaPreview(document.getElementById('chat-media-preview'), null);
    }
    fetchCRMsgs(true);
  }catch(e){toast(e.message)}
}

// Chat request functions (requires backend endpoints)
// Expected endpoints:
// GET  /api/mini-app/chat-request/status?user_id=XXX&target_id=YYY → { status: 'none'|'pending'|'accepted' }
// POST /api/mini-app/chat-request/send → { success: true }
async function getChatRequestStatus(targetId){
  try{
    const res=await api(`/api/mini-app/chat-request/status?user_id=${UID}&target_id=${targetId}`);
    return res.status;
  }catch(e){return 'none';}
}
async function sendChatRequest(targetId){
  try{
    await api('/api/mini-app/chat-request/send',{method:'POST',body:JSON.stringify({sender_id:UID,receiver_id:targetId})});
    toast('✅ Chat request sent! The user will be notified.');
    return true;
  }catch(e){toast(e.message); return false;}
}

async function showUserProfile(userId){
  if(!userId) return;
  if(String(userId)===String(UID)){ go('profile'); return; }
  const modal=document.getElementById('profileModal');
  const contentDiv=document.getElementById('modalContent');
  modal.classList.add('active');
  contentDiv.innerHTML='<div class="skel" style="height:150px;"></div>';
  
  const isPostAuthor = (currentPostAuthorId && String(userId) === String(currentPostAuthorId));
  
  try{
    const data = await api(`/api/mini-app/profile/${userId}?viewer_id=${UID}`);
    const u = data.data;
    const requestStatus = await getChatRequestStatus(userId);
    let buttonHtml = '';
    if(requestStatus==='accepted'){
      buttonHtml = `<button class="modal-btn modal-btn-primary" id="chatActionBtn">${ICONS.chat} Open Chat</button>`;
    }else if(requestStatus==='pending'){
      buttonHtml = `<button class="modal-btn modal-btn-secondary" disabled style="opacity:0.6">${ICONS.clock} Request Pending</button>`;
    }else{
      buttonHtml = `<button class="modal-btn modal-btn-primary" id="chatActionBtn">${ICONS.mail} Request to Chat</button>`;
    }
    
    let nameDisplay = u.name;
    let nameBadge = '';
    if(isPostAuthor){
      nameDisplay = 'Vent author';
      nameBadge = ICONS.shield.replace('class="icon"','class="icon badge-icon"');
      contentDiv.innerHTML = `
        <div class="modal-avatar">${avaHtml(u.avatar||u.sex)}</div>
        <div class="modal-name">${nameBadge}${esc(nameDisplay)}</div>
        ${buttonHtml}
      `;
    } else {
      contentDiv.innerHTML = `
        <div class="modal-avatar">${avaHtml(u.avatar||u.sex)}</div>
        <div class="modal-name">${esc(u.name)}</div>
        <div class="modal-stats"><div class="modal-stat"><div class="modal-stat-num">${u.stats?.posts||0}</div><div class="modal-stat-lbl">Vents</div></div><div class="modal-stat"><div class="modal-stat-num">${u.stats?.comments||0}</div><div class="modal-stat-lbl">Replies</div></div><div class="modal-stat"><div class="modal-stat-num">${u.stats?.followers||0}</div><div class="modal-stat-lbl">Followers</div></div></div>
        ${buttonHtml}
      `;
    }
    
    const btn = document.getElementById('chatActionBtn');
    if(btn && requestStatus!=='pending'){
      btn.onclick = async function(){
        if(requestStatus==='accepted'){
          closeProfileModal();
          openCR(userId, u.name, u.avatar||u.sex);
        }else{
          const sent = await sendChatRequest(userId);
          if(sent){
            closeProfileModal();
            toast('Request sent! You can chat once they accept.');
          }
        }
      };
    }
  }catch(e){ contentDiv.innerHTML='<div style="color:var(--text3)">Failed to load profile</div>'; }
}
function closeProfileModal(e){
  const modal=document.getElementById('profileModal');
  if(e && e.target !== modal) return;
  modal.classList.remove('active');
}

function skelPosts(n){return Array(n).fill(`<div class="post-card" style="cursor:default"><div style="display:flex;gap:10px;margin-bottom:12px"><div class="skel" style="width:34px;height:34px;border-radius:50%"></div><div style="flex:1"><div class="skel" style="height:12px;width:60%;margin-bottom:6px"></div><div class="skel" style="height:10px;width:30%"></div></div></div><div class="skel" style="height:13px;margin-bottom:6px"></div><div class="skel" style="height:13px;width:80%;margin-bottom:6px"></div><div class="skel" style="height:13px;width:60%"></div></div>`).join('')}
function skelLB(){return `<div style="margin:20px 16px 0"><div class="skel" style="height:180px;border-radius:20px;margin-bottom:12px"></div><div class="skel" style="height:14px;margin-bottom:8px"></div><div class="skel" style="height:14px;width:70%"></div></div>`}
function skelProfile(){return `<div style="margin:20px 16px 0"><div class="skel" style="height:200px;border-radius:20px"></div></div>`}
function skelComments(n){
  return Array(n).fill(`
    <div class="comment-item">
      <div class="skel" style="width:28px;height:28px;border-radius:50%;flex-shrink:0"></div>
      <div class="comment-body" style="background:var(--bg2)">
        <div class="skel" style="height:10px;width:40%;margin-bottom:8px"></div>
        <div class="skel" style="height:12px;margin-bottom:4px"></div>
        <div class="skel" style="height:12px;width:70%"></div>
      </div>
    </div>
  `).join('');
}
function skelChats(){return Array(4).fill(`<div style="display:flex;gap:12px;padding:14px 16px;border-bottom:0.5px solid var(--border)"><div class="skel" style="width:44px;height:44px;border-radius:50%;flex-shrink:0"></div><div style="flex:1"><div class="skel" style="height:13px;width:50%;margin-bottom:6px"></div><div class="skel" style="height:11px;width:80%"></div></div></div>`).join('')}

async function init(){
  const tg=window.Telegram?.WebApp;
  if(tg){try{tg.expand();tg.ready()}catch(e){}}
  const user=tg?.initDataUnsafe?.user;
  if(user?.id){UID=String(user.id)}
  if(!UID){
    const t=new URLSearchParams(location.search).get('token');
    if(t){try{const r=await fetch(API+'/api/verify-token/'+t);const d=await r.json();if(d.success)UID=String(d.user_id)}catch(e){}}
  }
  document.getElementById('auth').style.display='none';
  document.getElementById('app').style.display='flex';
  if(UID){loadFeed(); checkAdminStatus();}
  else{document.getElementById('feed-list').innerHTML='<div style="text-align:center;padding:60px 20px;color:var(--text3)"><div style="width:32px;height:32px;margin:0 auto 12px;color:var(--text3)">'+ICONS.lock+'</div><div style="font-size:16px;font-weight:600;color:var(--text);margin-bottom:6px">Sign in required</div><div style="font-size:13px">Open via the Telegram bot to access Christian Vent</div></div>';}

  // Setup voice buttons after DOM ready
  setupVoiceButton('vent-voice-btn', 'vent');
  setupVoiceButton('comment-voice-btn', 'comment');
  setupVoiceButton('chat-voice-btn', 'chat');
}
init();
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
        media_type = data.get('media_type') or 'text'
        media_id = data.get('media_id')

        explicit = bool(data.get('explicit', False))

        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
        
        if not content and not media_id:
            return jsonify({'success': False, 'error': 'Content cannot be empty'}), 400
            
        if not categories:
            return jsonify({'success': False, 'error': 'At least one category is required'}), 400
        
        # Check if user exists
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        if not media_id:
            media_type = 'text'

        # Insert the post
        post_row = db_execute(
            "INSERT INTO posts (content, author_id, media_type, media_id, approved, explicit) VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING post_id",
            (content, user_id, media_type, media_id, explicit),
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
            logger.info(f"Mini App Multi-Cat Post submitted: ID {post_id} by {user_id}")
            
            # Notify admin immediately
            notify_admin_of_new_post_sync(post_id)
            
            return jsonify({
                'success': True,
                'message': 'Your vent has been submitted for admin approval!',
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
        
        logger.info(f"Mini App Post awaiting approval from {author_name}: {post_preview}")
        
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_ID,
            "text": f"New post awaiting approval from {author_name}:\n\n{post_preview}",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"approve_post_{post_id}"},
                        {"text": "Reject", "callback_data": f"reject_post_{post_id}"}
                    ]
                ]
            }
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Error in sync admin notification: {e}")

def _telegram_media_method(media_type):
    """Map our internal media_type to (telegram_api_method, field_name)."""
    return {
        'photo': ('sendPhoto', 'photo'),
        'video': ('sendVideo', 'video'),
        'voice': ('sendVoice', 'voice'),
        'audio': ('sendAudio', 'audio'),
        'document': ('sendDocument', 'document'),
        'gif': ('sendAnimation', 'animation'),
        'sticker': ('sendSticker', 'sticker'),
    }.get(media_type, ('sendDocument', 'document'))


def send_telegram_message_sync(chat_id, text, parse_mode='HTML', reply_markup=None):
    """Send a plain text message synchronously via requests (no context.bot needed)."""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f"send_telegram_message_sync failed: {e}")
        return None


def send_telegram_media_sync(chat_id, media_type, media_id, caption=None, parse_mode='HTML', reply_markup=None):
    """
    Send a real media message (photo/voice/video/document/gif/sticker) synchronously,
    using a file_id already stored on Telegram. Falls back to a text message if the
    media type is missing/unsupported, or if the media send itself fails.
    """
    if not media_id or not media_type or media_type == 'text':
        if caption:
            return send_telegram_message_sync(chat_id, caption, parse_mode=parse_mode, reply_markup=reply_markup)
        return None

    method, field = _telegram_media_method(media_type)
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    payload = {"chat_id": chat_id, field: media_id}

    # sendSticker does not accept a caption param at all — send it as a follow-up message instead
    if caption and media_type != 'sticker':
        payload["caption"] = caption[:1024]  # Telegram's caption hard limit
        if parse_mode:
            payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = requests.post(url, json=payload, timeout=15)
        result = resp.json()
        if result.get('ok'):
            if caption and media_type == 'sticker':
                send_telegram_message_sync(chat_id, caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return result
        logger.error(f"send_telegram_media_sync failed ({method}): {result}")
    except Exception as e:
        logger.error(f"send_telegram_media_sync request error: {e}")

    # Media send failed entirely — still let the person know something arrived
    if caption:
        return send_telegram_message_sync(chat_id, caption, parse_mode=parse_mode, reply_markup=reply_markup)
    return None


def notify_user_of_private_message_sync(sender_id, receiver_id, message_content, media_type='text', media_id=None):
    """Sync replacement for notify_user_of_private_message — actually delivers the media file."""
    try:
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (receiver_id, sender_id)
        )
        if is_blocked:
            return

        receiver = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (receiver_id,))
        if not receiver or not receiver.get('notifications_enabled'):
            return

        sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
        sender_name = get_display_name(sender)
        safe_sender_name = html.escape(sender_name)

        preview_content = (message_content or "")[:200]
        if message_content and len(message_content) > 200:
            preview_content += '...'
        safe_preview = html.escape(preview_content) if preview_content else ""

        keyboard = {
            "inline_keyboard": [[
                {"text": "Reply", "callback_data": f"reply_msg_{sender_id}"},
                {"text": "Block", "callback_data": f"block_user_{sender_id}"}
            ]]
        }

        header = f"<b>New Private Message</b>\n\nFrom: <b>{safe_sender_name}</b>\n\n"
        footer = "\n\n<i>Use /inbox to view all messages</i>"

        if media_id and media_type and media_type != 'text':
            caption = header + safe_preview + footer
            result = send_telegram_media_sync(
                chat_id=receiver_id, media_type=media_type, media_id=media_id,
                caption=caption, parse_mode='HTML', reply_markup=keyboard
            )
            if result and result.get('ok'):
                return
            # if media send failed outright, fall through to plain text below

        fallback_body = safe_preview if safe_preview else "<i>[attachment]</i>"
        notification_text = header + fallback_body + footer
        send_telegram_message_sync(receiver_id, notification_text, parse_mode='HTML', reply_markup=keyboard)

    except Exception as e:
        logger.error(f"notify_user_of_private_message_sync failed: {e}")


def notify_vent_author_of_comment_sync(post_id, commenter_id, comment_id=None, comment_content=None, media_type='text', media_id=None):
    """Sync replacement for notify_vent_author_of_comment, for use from Flask routes."""
    try:
        post = db_fetch_one("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        author_id = post['author_id']
        if str(author_id) == str(commenter_id):
            return

        author = db_fetch_one("SELECT user_id, notifications_enabled FROM users WHERE user_id = %s", (author_id,))
        if not author or not author.get('notifications_enabled'):
            return

        commenter = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (commenter_id,))
        commenter_name = get_display_name(commenter)

        post_preview = post['content'][:50] + '...' if post['content'] and len(post['content']) > 50 else (post['content'] or "")
        safe_commenter = html.escape(commenter_name)
        safe_post_preview = html.escape(post_preview)

        media_label = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🩹 Sticker', 'photo': '🖼 Photo'}.get(media_type)
        safe_comment = html.escape((comment_content or '')[:500]) if comment_content else (media_label or "")

        lines = ["<b>New comment on your vent!</b>", "", f"<b>{safe_commenter}</b> wrote:"]
        if safe_comment:
            lines.append(f"“{safe_comment}”")
        lines += ["", f"<b>Your vent:</b> {safe_post_preview}", "", f"<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>"]
        notification_text = "\n".join(lines)

        reply_markup = None
        if comment_id:
            reply_markup = {"inline_keyboard": [[
                {"text": "↩ Reply", "callback_data": f"reply_{post_id}_{comment_id}"}
            ]]}

        if media_id and media_type and media_type != 'text':
            result = send_telegram_media_sync(author_id, media_type, media_id, caption=notification_text, parse_mode='HTML', reply_markup=reply_markup)
            if result and result.get('ok'):
                return

        send_telegram_message_sync(author_id, notification_text, parse_mode='HTML', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"notify_vent_author_of_comment_sync failed: {e}")


def notify_user_of_reply_sync(post_id, parent_comment_id, replier_id, new_comment_id=None, comment_content=None, media_type='text', media_id=None):
    """Sync replacement for notify_user_of_reply, for use from Flask routes."""
    try:
        parent_comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (parent_comment_id,))
        if not parent_comment:
            return

        original_author = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (parent_comment['author_id'],))
        if not original_author or not original_author.get('notifications_enabled'):
            return
        if str(original_author['user_id']) == str(replier_id):
            return  # don't notify yourself

        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return

        if str(replier_id) == str(post['author_id']):
            safe_replier_name = "Vent author"
        else:
            replier = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (replier_id,))
            safe_replier_name = html.escape(get_display_name(replier))

        post_preview = post['content'][:50] + '...' if post['content'] and len(post['content']) > 50 else (post['content'] or "")
        safe_post_preview = html.escape(post_preview)
        safe_parent_preview = html.escape((parent_comment['content'] or '[media]')[:100])

        media_label = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🩹 Sticker', 'photo': '🖼 Photo'}.get(media_type)
        safe_comment = html.escape((comment_content or '')[:500]) if comment_content else (media_label or "")

        lines = [f"{safe_replier_name} replied to your comment:", f"<i>{safe_parent_preview}</i>", ""]
        if safe_comment:
            lines += ["<b>Their reply:</b>", f"“{safe_comment}”", ""]
        lines.append(f"Post: {safe_post_preview}")
        lines.append(f"\n<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>")
        notification_text = "\n".join(lines)

        reply_markup = None
        if new_comment_id:
            reply_markup = {"inline_keyboard": [[
                {"text": "↩ Reply", "callback_data": f"replytoreply_{post_id}_{parent_comment_id}_{new_comment_id}"}
            ]]}

        if media_id and media_type and media_type != 'text':
            result = send_telegram_media_sync(original_author['user_id'], media_type, media_id, caption=notification_text, parse_mode='HTML', reply_markup=reply_markup)
            if result and result.get('ok'):
                return

        send_telegram_message_sync(original_author['user_id'], notification_text, parse_mode='HTML', reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"notify_user_of_reply_sync failed: {e}")

def update_channel_post_comment_count_sync(post_id):
    """Sync version of update_channel_post_comment_count for the mini app"""
    try:
        post = db_fetch_one("SELECT channel_message_id, explicit FROM posts WHERE post_id = %s", (post_id,))
        if not post or not post['channel_message_id']:
            return
            
        total_comments = count_all_comments(post_id)
        
        buttons = []
        if post.get('explicit'):
            buttons.append({"text": "View Post", "url": f"https://t.me/{BOT_USERNAME}?start=viewpost_{post_id}"})
        buttons.append({"text": f"Add/View Comments ({total_comments})", "url": f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}"})

        url = f"https://api.telegram.org/bot{TOKEN}/editMessageReplyMarkup"
        payload = {
            "chat_id": CHANNEL_ID,
            "message_id": post['channel_message_id'],
            "reply_markup": {
                "inline_keyboard": [buttons]
            }
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Error in sync channel comment update: {e}")

MINI_APP_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB, matches Telegram's bot-API upload cap

def _detect_mini_app_media_type(filename, mimetype):
    """Map an uploaded file's name/mimetype to (our media_type, telegram send method, telegram field name)."""
    ext = os.path.splitext(filename or '')[1].lower()
    mt = (mimetype or '').lower()

    if ext == '.gif' or mt == 'image/gif':
        return 'gif', 'sendAnimation', 'animation'
    if ext == '.webp':
        return 'sticker', 'sendSticker', 'sticker'
    if mt.startswith('image/') or ext in {'.jpg', '.jpeg', '.png'}:
        return 'photo', 'sendPhoto', 'photo'
    if mt.startswith('video/') or ext in {'.mp4', '.mov', '.mkv'}:
        return 'video', 'sendVideo', 'video'
    if ext in {'.ogg', '.oga'} or mt in {'audio/ogg', 'audio/oga'}:
        return 'voice', 'sendVoice', 'voice'
    if mt.startswith('audio/'):
        return 'audio', 'sendAudio', 'audio'
    return 'document', 'sendDocument', 'document'

@flask_app.route('/api/mini-app/upload-media', methods=['POST'])
def mini_app_upload_media():
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400

        upload = request.files['file']
        if not upload or not upload.filename:
            return jsonify({'success': False, 'error': 'No file selected'}), 400

        upload.stream.seek(0, os.SEEK_END)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size == 0:
            return jsonify({'success': False, 'error': 'Empty file'}), 400
        if size > MINI_APP_MAX_UPLOAD_BYTES:
            return jsonify({'success': False, 'error': 'File too large (max 20MB)'}), 400

        intent = (request.form.get('intent') or '').lower()

        if intent == 'voice':
            # Force voice regardless of detected mimetype (webm/opus from MediaRecorder etc.)
            media_type, tg_method, tg_field = 'voice', 'sendVoice', 'voice'
        else:
            media_type, tg_method, tg_field = _detect_mini_app_media_type(upload.filename, upload.mimetype)

        storage_chat_id = ADMIN_ID or CHANNEL_ID
        if not storage_chat_id:
            return jsonify({'success': False, 'error': 'Media storage is not configured'}), 500

        def _send(method, field):
            upload.stream.seek(0)
            files = {field: (upload.filename, upload.stream, upload.mimetype or 'application/octet-stream')}
            data = {'chat_id': storage_chat_id, 'disable_notification': True}
            resp = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=30)
            return resp.json()

        result = _send(tg_method, tg_field)

        # If a forced voice-note send fails (codec Telegram won't accept as voice),
        # retry as a regular audio file before finally falling back to a document.
        if not result.get('ok') and intent == 'voice':
            result = _send('sendAudio', 'audio')
            media_type = 'audio'

        if not result.get('ok') and tg_method != 'sendDocument':
            result = _send('sendDocument', 'document')
            media_type = 'document'

        if not result.get('ok'):
            logger.error(f"Telegram media upload failed: {result}")
            return jsonify({'success': False, 'error': 'Failed to upload media to Telegram'}), 502

        msg = result['result']
        file_id = None
        if media_type == 'photo' and msg.get('photo'):
            file_id = msg['photo'][-1]['file_id']
        elif media_type == 'video' and msg.get('video'):
            file_id = msg['video']['file_id']
        elif media_type == 'voice' and msg.get('voice'):
            file_id = msg['voice']['file_id']
        elif media_type == 'audio' and msg.get('audio'):
            file_id = msg['audio']['file_id']
        elif media_type == 'sticker' and msg.get('sticker'):
            file_id = msg['sticker']['file_id']
        elif media_type == 'gif' and msg.get('animation'):
            file_id = msg['animation']['file_id']
        elif msg.get('document'):
            file_id = msg['document']['file_id']
            media_type = 'document'

        if not file_id:
            logger.error(f"Could not extract file_id from Telegram response: {result}")
            return jsonify({'success': False, 'error': 'Could not read uploaded file'}), 502

        logger.info(f"Mini App media uploaded: {media_type} -> {file_id}")
        return jsonify({'success': True, 'file_id': file_id, 'media_type': media_type})

    except Exception as e:
        logger.error(f"Error in mini-app media upload: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/file/<path:file_id>', methods=['GET'])
def mini_app_file_proxy(file_id):
    """Resolves a Telegram file_id to its CDN URL and redirects there, so the
    bot token never needs to be exposed to the frontend."""
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TOKEN}/getFile", params={'file_id': file_id}, timeout=10)
        result = resp.json()
        if not result.get('ok'):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        file_path = result['result']['file_path']
        return redirect(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}")
    except Exception as e:
        logger.error(f"Error proxying file {file_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
                p.media_id,
                p.explicit,
                u.user_id as author_id,
                u.sex as author_sex,
                u.avatar_emoji as author_avatar,
                u.anonymous_name as author_name,
                u.is_admin as author_is_admin,
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
            WHERE p.approved = TRUE AND p.deleted = FALSE
            GROUP BY p.post_id, u.user_id, u.sex, u.avatar_emoji, u.anonymous_name, u.is_admin
            ORDER BY p.timestamp DESC
            LIMIT %s OFFSET %s
        ''', (user_id, per_page, offset))
        
        # Batch load reactions for posts
        post_ids = [p['post_id'] for p in posts]
        reactions_map = {}
        user_reactions_map = {}
        
        if post_ids:
            counts_res = db_fetch_all("""
                SELECT post_id, type, COUNT(*) as cnt
                FROM reactions
                WHERE post_id IN %s AND post_id IS NOT NULL
                GROUP BY post_id, type
            """, (tuple(post_ids),))
            
            for row in (counts_res or []):
                pid = row['post_id']
                rtype = row['type']
                rcnt = row['cnt']
                if pid not in reactions_map:
                    reactions_map[pid] = {}
                reactions_map[pid][rtype] = rcnt
                
            if user_id:
                user_res = db_fetch_all("""
                    SELECT post_id, type
                    FROM reactions
                    WHERE post_id IN %s AND user_id = %s AND post_id IS NOT NULL
                """, (tuple(post_ids), str(user_id)))
                
                for row in (user_res or []):
                    pid = row['post_id']
                    rtype = row['type']
                    user_reactions_map[pid] = rtype

        formatted_posts = []
        viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(user_id),)) if user_id else None
        is_admin_viewer = bool(viewer_row and viewer_row.get('is_admin'))
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
            aura_sticker = "" if post['author_is_admin'] else format_aura(rating)
            
            category_list = post['categories'].split(',') if post['categories'] else ['Other']
            
            is_owner = str(post['author_id']) == str(user_id)
            is_explicit = bool(post.get('explicit'))
            hide_content = is_explicit and not is_owner and not is_admin_viewer
            if hide_content:
                content_preview = "This post contains explicit content that may not be suitable for all viewers."
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'full_content': post['content'] if not hide_content else content_preview,
                'categories': category_list,
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'unread_comments': post['unread_comments'],
                'explicit': is_explicit,
                'content_hidden': hide_content,
                'author': {
                    'name': 'Anonymous',
                    'sex': post['author_sex'] or '👤',
                    'avatar': post['author_avatar'] or "",
                    'aura': aura_sticker,
                    'is_me': str(post['author_id']) == str(user_id),
                    'is_admin': post['author_is_admin']
                },
                'has_media': post['media_type'] != 'text' and not hide_content,
                'media_type': None if hide_content else post['media_type'],
                'media_id': None if hide_content else post['media_id'],
                'reactions': {
                    'counts': reactions_map.get(post['post_id'], {}),
                    'user_reaction': user_reactions_map.get(post['post_id'], None)
                }
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
                p.post_id, p.vent_number, p.content, p.timestamp, p.comment_count, p.media_type, p.media_id, p.deleted, p.explicit,
                u.user_id as author_id, u.sex as author_sex, u.avatar_emoji as author_avatar, u.anonymous_name as author_name,
                u.is_admin as author_is_admin,
                STRING_AGG(pc.category_code, ', ') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.post_id = %s AND p.approved = TRUE
            GROUP BY p.post_id, p.deleted, u.user_id, u.sex, u.avatar_emoji, u.anonymous_name, u.is_admin
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
        
        # Get viewer_id
        viewer_id = request.args.get('viewer_id')
        reveal_requested = request.args.get('reveal') == '1'
        
        viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(viewer_id),)) if viewer_id else None
        is_privileged_viewer = bool(viewer_id) and (
            str(viewer_id) == str(post['author_id']) or bool(viewer_row and viewer_row.get('is_admin'))
        )
        is_explicit = bool(post.get('explicit'))
        show_content = is_privileged_viewer or reveal_requested or not is_explicit
        
        # Fetch post reactions counts
        counts_res = db_fetch_all("""
            SELECT type, COUNT(*) as cnt
            FROM reactions
            WHERE post_id = %s AND post_id IS NOT NULL
            GROUP BY type
        """, (post_id,))
        counts = {row['type']: row['cnt'] for row in (counts_res or [])}
        
        user_reaction = None
        if viewer_id:
            user_res = db_fetch_one("""
                SELECT type FROM reactions
                WHERE post_id = %s AND user_id = %s AND post_id IS NOT NULL
            """, (post_id, str(viewer_id)))
            user_reaction = user_res['type'] if user_res else None

        if post.get('deleted'):
            formatted_post = {
                'id': post['post_id'],
                'content': "This content has been deleted by the author.",
                'categories': category_list,
                'vent_number': post.get('vent_number'),
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'author_id': post['author_id'],
                'deleted': True,
                'explicit': is_explicit,
                'content_hidden': False,
                'author': {
                    'id': post['author_id'],
                    'name': 'Anonymous',
                    'sex': post['author_sex'] or '👤',
                    'avatar': post['author_avatar'] or "",
                    'aura': "" if post['author_is_admin'] else format_aura(rating),
                    'is_admin': post['author_is_admin']
                },
                'reactions': {
                    'counts': {},
                    'user_reaction': None
                }
            }
            return jsonify({'success': True, 'data': formatted_post})

        formatted_post = {
            'id': post['post_id'],
            'content': post['content'] if show_content else "This post contains explicit content that may not be suitable for all viewers.",
            'categories': category_list,
            'vent_number': post.get('vent_number'),
            'time_ago': time_ago,
            'comments': post['comment_count'] or 0,
            'author_id': post['author_id'],
            'media_type': post['media_type'] if show_content else None,
            'media_id': post['media_id'] if show_content else None,
            'explicit': is_explicit,
            'content_hidden': is_explicit and not show_content,
            'author': {
                'id': post['author_id'],
                'name': 'Anonymous',
                'sex': post['author_sex'] or '👤',
                'avatar': post['author_avatar'] or "",
                'aura': "" if post['author_is_admin'] else format_aura(rating),
                'is_admin': post['author_is_admin']
            },
            'reactions': {
                'counts': counts if show_content else {},
                'user_reaction': user_reaction if show_content else None
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
        # Get viewer_id
        viewer_id = request.args.get('viewer_id')
        reveal_requested = request.args.get('reveal') == '1'

        post_gate = db_fetch_one("SELECT author_id, explicit FROM posts WHERE post_id = %s", (post_id,))
        if post_gate and post_gate.get('explicit'):
            viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(viewer_id),)) if viewer_id else None
            is_privileged_viewer = bool(viewer_id) and (
                str(viewer_id) == str(post_gate['author_id']) or bool(viewer_row and viewer_row.get('is_admin'))
            )
            if not is_privileged_viewer and not reveal_requested:
                return jsonify({'success': True, 'data': [], 'content_hidden': True})

        comments = db_fetch_all('''
            SELECT 
                c.comment_id,
                c.parent_comment_id,
                c.content,
                c.type as media_type,
                c.file_id as media_id,
                c.timestamp as time_ago,
                u.user_id as author_id,
                u.sex as author_sex,
                u.avatar_emoji as author_avatar,
                u.anonymous_name as author_name,
                u.is_admin as author_is_admin
            FROM comments c
            JOIN users u ON c.author_id = u.user_id
            WHERE c.post_id = %s
            ORDER BY c.timestamp ASC
        ''', (post_id,))

        # Batch load reactions for comments
        comment_ids = [c['comment_id'] for c in comments]
        comment_reactions_map = {}
        comment_user_reactions_map = {}
        
        if comment_ids:
            counts_res = db_fetch_all("""
                SELECT comment_id, type, COUNT(*) as cnt
                FROM reactions
                WHERE comment_id IN %s AND comment_id IS NOT NULL
                GROUP BY comment_id, type
            """, (tuple(comment_ids),))
            
            for row in (counts_res or []):
                cid = row['comment_id']
                rtype = row['type']
                rcnt = row['cnt']
                if cid not in comment_reactions_map:
                    comment_reactions_map[cid] = {}
                comment_reactions_map[cid][rtype] = rcnt
                
            if viewer_id:
                user_res = db_fetch_all("""
                    SELECT comment_id, type
                    FROM reactions
                    WHERE comment_id IN %s AND user_id = %s AND comment_id IS NOT NULL
                """, (tuple(comment_ids), str(viewer_id)))
                
                for row in (user_res or []):
                    cid = row['comment_id']
                    rtype = row['type']
                    comment_user_reactions_map[cid] = rtype
        post_author = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
        post_author_id = post_author['author_id'] if post_author else None
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
                'media_type': c['media_type'],
                'media_id': c['media_id'],
                'time_ago': calc_time,
                'author_id': c['author_id'],
                'author': {
                    'id': c['author_id'],
                    'name': c['author_name'] or 'Anonymous',
                    'sex': c['author_sex'] or '👤',
                    'avatar': c['author_avatar'] or "",
                    'aura': "" if c['author_is_admin'] else format_aura(rating),
                    'is_admin': c['author_is_admin'],
                    'is_vent_author': str(c['author_id']) == str(post_author_id) if post_author_id else False
                },
                'reactions': {
                    'counts': comment_reactions_map.get(c['comment_id'], {}),
                    'user_reaction': comment_user_reactions_map.get(c['comment_id'], None)
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
        media_type = data.get('media_type') or 'text'
        file_id = data.get('media_id') or data.get('file_id')

        if not user_id:
            return jsonify({'success': False, 'error': 'Not authenticated'}), 401
        if not content and not file_id:
            return jsonify({'success': False, 'error': 'Empty response'}), 400
        if not file_id:
            media_type = 'text'

        new_comment_row = db_execute(
            "INSERT INTO comments (post_id, author_id, content, parent_comment_id, type, file_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING comment_id",
            (post_id, user_id, content, parent_comment_id, media_type, file_id),
            fetchone=True
        )
        new_comment_id = new_comment_row['comment_id'] if new_comment_row else None
        db_execute(
            "UPDATE posts SET comment_count = COALESCE(comment_count, 0) + 1 WHERE post_id = %s",
            (post_id,)
        )

        update_channel_post_comment_count_sync(post_id)
        calculate_user_rating.cache_clear()

        # Notify the right person: parent-comment author for a reply, otherwise the vent author
        if parent_comment_id and parent_comment_id != 0:
            notify_user_of_reply_sync(
                post_id, parent_comment_id, user_id, new_comment_id,
                comment_content=content, media_type=media_type, media_id=file_id
            )
        else:
            notify_vent_author_of_comment_sync(
                post_id, user_id, new_comment_id,
                comment_content=content, media_type=media_type, media_id=file_id
            )

        return jsonify({'success': True, 'message': 'Reply posted successfully!'})
    except Exception as e:
        logger.error(f"Failed to post native comment: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/react', methods=['POST'])
def mini_app_toggle_reaction():
    try:
        data = request.json or {}
        user_id = str(data.get('user_id', ''))
        post_id = data.get('post_id')
        comment_id = data.get('comment_id')
        reaction_type = data.get('type') # e.g. ""
        
        if not user_id or not reaction_type:
            return jsonify({'success': False, 'error': 'Missing parameters'}), 400
            
        if post_id is None and comment_id is None:
            return jsonify({'success': False, 'error': 'Must provide post_id or comment_id'}), 400
            
        # Toggle logic
        if post_id is not None:
            existing = db_fetch_one(
                "SELECT type FROM reactions WHERE post_id = %s AND user_id = %s",
                (post_id, user_id)
            )
            if existing:
                if existing['type'] == reaction_type:
                    db_execute("DELETE FROM reactions WHERE post_id = %s AND user_id = %s", (post_id, user_id))
                    action = 'removed'
                else:
                    db_execute("UPDATE reactions SET type = %s WHERE post_id = %s AND user_id = %s", (reaction_type, post_id, user_id))
                    action = 'updated'
            else:
                db_execute("INSERT INTO reactions (post_id, user_id, type) VALUES (%s, %s, %s)", (post_id, user_id, reaction_type))
                action = 'added'
                
            # Get updated counts
            counts_res = db_fetch_all(
                "SELECT type, COUNT(*) as cnt FROM reactions WHERE post_id = %s GROUP BY type",
                (post_id,)
            )
            counts = {row['type']: row['cnt'] for row in (counts_res or [])}
            
            # Fetch current reaction
            cur_res = db_fetch_one("SELECT type FROM reactions WHERE post_id = %s AND user_id = %s", (post_id, user_id))
            user_reaction = cur_res['type'] if cur_res else None
            
        else:
            existing = db_fetch_one(
                "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                (comment_id, user_id)
            )
            if existing:
                if existing['type'] == reaction_type:
                    db_execute("DELETE FROM reactions WHERE comment_id = %s AND user_id = %s", (comment_id, user_id))
                    action = 'removed'
                else:
                    db_execute("UPDATE reactions SET type = %s WHERE comment_id = %s AND user_id = %s", (reaction_type, comment_id, user_id))
                    action = 'updated'
            else:
                db_execute("INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)", (comment_id, user_id, reaction_type))
                action = 'added'
                
            # Get updated counts
            counts_res = db_fetch_all(
                "SELECT type, COUNT(*) as cnt FROM reactions WHERE comment_id = %s GROUP BY type",
                (comment_id,)
            )
            counts = {row['type']: row['cnt'] for row in (counts_res or [])}
            
            # Fetch current reaction
            cur_res = db_fetch_one("SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s", (comment_id, user_id))
            user_reaction = cur_res['type'] if cur_res else None
            
        # Clear rating caches since aura changes
        calculate_user_rating.cache_clear()
        format_aura.cache_clear()
        
        return jsonify({
            'success': True,
            'action': action,
            'reactions': {
                'counts': counts,
                'user_reaction': user_reaction
            }
        })
        
    except Exception as e:
        logger.error(f"Error toggle reaction: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/chats', methods=['GET'])
def mini_app_get_chats():
    try:
        user_id = request.args.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Missing user_id'}), 400
            
        # DISTINCT ON query to get the latest message per partner, omitting aura points as requested
        rows = db_fetch_all("""
            WITH last_messages AS (
                SELECT DISTINCT ON (partner_id)
                    CASE 
                        WHEN sender_id = %s THEN receiver_id 
                        ELSE sender_id 
                    END AS partner_id,
                    content,
                    timestamp,
                    is_read,
                    sender_id
                FROM private_messages
                WHERE sender_id = %s OR receiver_id = %s
                ORDER BY partner_id, timestamp DESC
            )
            SELECT 
                lm.partner_id,
                lm.content,
                lm.timestamp,
                lm.is_read,
                lm.sender_id,
                u.anonymous_name as partner_name,
                u.sex as partner_sex,
                u.avatar_emoji as partner_avatar,
                u.is_admin as partner_is_admin,
                COALESCE((
                    SELECT COUNT(*) 
                    FROM private_messages 
                    WHERE sender_id = lm.partner_id 
                      AND receiver_id = %s 
                      AND is_read = FALSE
                ), 0) as unread_count
            FROM last_messages lm
            JOIN users u ON lm.partner_id = u.user_id
            ORDER BY lm.timestamp DESC
        """, (user_id, user_id, user_id, user_id))
        
        chats = []
        for r in (rows or []):
            if isinstance(r['timestamp'], str):
                msg_time = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                msg_time = r['timestamp']
            
            now = datetime.now()
            diff = now - msg_time
            if diff.days > 0:
                time_str = f"{diff.days}d ago"
            elif diff.seconds > 3600:
                time_str = f"{diff.seconds // 3600}h ago"
            elif diff.seconds > 60:
                time_str = f"{diff.seconds // 60}m ago"
            else:
                time_str = "Just now"
                
            chats.append({
                'partner_id': r['partner_id'],
                'partner_name': r['partner_name'] or 'Anonymous',
                'partner_sex': r['partner_sex'] or '👤',
                'partner_avatar': r['partner_avatar'] or '',
                'partner_is_admin': r['partner_is_admin'],
                'last_message': r['content'],
                'time_ago': time_str,
                'is_mine': str(r['sender_id']) == str(user_id),
                'unread_count': r['unread_count']
            })
            
        return jsonify({'success': True, 'data': chats})
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def format_ethiopian_time(dt):
    """Format datetime into Western (EAT) + Ethiopian Amharic time format."""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
        except:
            return dt
            
    # If server is in UTC, adjust to Ethiopia time (UTC+3)
    now_local = datetime.now()
    now_utc = datetime.utcnow()
    if abs((now_local - now_utc).total_seconds()) < 60:
        dt = dt + timedelta(hours=3)
        
    western_str = dt.strftime('%I:%M %p')
    
    H = dt.hour
    M = dt.minute
    
    if H >= 6 and H < 12:
        period = "ጠዋት"
    elif H >= 12 and H < 16:
        period = "ከሰዓት"
    elif H >= 16 and H < 18:
        period = "ምሽት"
    elif H >= 18:
        period = "ማታ"
    else:
        period = "ሌሊት"
        
    eth_hour = (H - 6) if H >= 6 else (H + 6)
    if eth_hour == 0:
        eth_hour = 12
    elif eth_hour > 12:
        eth_hour -= 12
        
    eth_str = f"{eth_hour}:{M:02d} {period}"
    return f"{western_str} ({eth_str})"

@flask_app.route('/api/mini-app/chats/<partner_id>', methods=['GET'])
def mini_app_get_messages(partner_id):
    try:
        user_id = request.args.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'Missing user_id'}), 400
            
        # Mark incoming messages from this partner as read
        db_execute("""
            UPDATE private_messages 
            SET is_read = TRUE 
            WHERE sender_id = %s AND receiver_id = %s AND is_read = FALSE
        """, (partner_id, user_id))
        
        # Get messages history
        rows = db_fetch_all("""
            SELECT message_id, sender_id, receiver_id, content, timestamp, is_read, media_type, media_id
            FROM private_messages
            WHERE (sender_id = %s AND receiver_id = %s)
               OR (sender_id = %s AND receiver_id = %s)
            ORDER BY timestamp ASC
        """, (user_id, partner_id, partner_id, user_id))
        
        messages = []
        for r in (rows or []):
            if isinstance(r['timestamp'], str):
                msg_time = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                msg_time = r['timestamp']
            
            messages.append({
                'id': r['message_id'],
                'sender_id': r['sender_id'],
                'receiver_id': r['receiver_id'],
                'content': r['content'],
                'media_type': r.get('media_type', 'text'),
                'media_id': r.get('media_id'),
                'timestamp': format_ethiopian_time(msg_time),
                'is_read': r['is_read'],
                'is_mine': str(r['sender_id']) == str(user_id)
            })
            
        return jsonify({'success': True, 'data': messages})
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/chats/send', methods=['POST'])
def mini_app_send_message():
    try:
        data = request.json or {}
        sender_id = str(data.get('sender_id', ''))
        receiver_id = str(data.get('receiver_id', ''))
        content = data.get('content', '').strip()
        media_type = data.get('media_type', 'text')
        media_id = data.get('media_id')

        if not sender_id or not receiver_id:
            return jsonify({'success': False, 'error': 'Missing sender/receiver'}), 400
        if not content and not media_id:
            return jsonify({'success': False, 'error': 'Empty message'}), 400

        block_check = db_fetch_one(
            "SELECT 1 FROM blocks WHERE (blocker_id = %s AND blocked_id = %s)",
            (receiver_id, sender_id)
        )
        if block_check:
            return jsonify({'success': False, 'error': 'You are blocked by this user.'}), 403

        res = db_execute("""
            INSERT INTO private_messages (sender_id, receiver_id, content, media_type, media_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING message_id, timestamp
        """, (sender_id, receiver_id, content, media_type, media_id), fetchone=True)

        # Deliver a real notification — including the actual media file if present
        notify_user_of_private_message_sync(
            sender_id=sender_id,
            receiver_id=receiver_id,
            message_content=content,
            media_type=media_type,
            media_id=media_id
        )

        msg_time = res['timestamp'] if (res and 'timestamp' in res) else datetime.now()
        return jsonify({
            'success': True,
            'data': {
                'id': res['message_id'] if res else None,
                'sender_id': sender_id,
                'receiver_id': receiver_id,
                'content': content,
                'timestamp': format_ethiopian_time(msg_time),
                'is_read': False,
                'is_mine': True
            }
        })
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
        
@flask_app.route('/api/mini-app/chat-request/status', methods=['GET'])
def mini_app_chat_request_status():
    """Check the status of a chat request between two users."""
    user_id = request.args.get('user_id')
    target_id = request.args.get('target_id')
    
    if not user_id or not target_id:
        return jsonify({'success': False, 'error': 'Missing user_id or target_id'}), 400
    
    row = db_fetch_one("""
        SELECT status FROM chat_requests
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
        ORDER BY timestamp DESC LIMIT 1
    """, (user_id, target_id, target_id, user_id))
    
    if not row:
        return jsonify({'success': True, 'status': 'none'})
    
    return jsonify({'success': True, 'status': row['status']})


@flask_app.route('/api/mini-app/chat-request/send', methods=['POST'])
def mini_app_send_chat_request():
    """Send a chat request from one user to another."""
    data = request.get_json()
    sender_id = str(data.get('sender_id', ''))
    receiver_id = str(data.get('receiver_id', ''))
    
    if not sender_id or not receiver_id:
        return jsonify({'success': False, 'error': 'Missing sender_id or receiver_id'}), 400
    
    if sender_id == receiver_id:
        return jsonify({'success': False, 'error': 'Cannot request chat with yourself'}), 400
    
    # Check if a request already exists
    existing = db_fetch_one("""
        SELECT status FROM chat_requests
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
    """, (sender_id, receiver_id, receiver_id, sender_id))
    
    if existing:
        if existing['status'] == 'accepted':
            return jsonify({'success': True, 'status': 'accepted', 'message': 'Chat already accepted'})
        elif existing['status'] == 'pending':
            return jsonify({'success': False, 'error': 'Chat request already pending'}), 409
    
    # Insert new pending request
    db_execute("""
        INSERT INTO chat_requests (sender_id, receiver_id, status)
        VALUES (%s, %s, 'pending')
    """, (sender_id, receiver_id))
    
    # --- Send Telegram notification to the receiver (using requests, no async needed) ---
    try:
        import requests
        sender = db_fetch_one("SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (sender_id,))
        sender_name = sender['anonymous_name'] if sender else 'Anonymous'
        sender_icon = sender['avatar_emoji'] if (sender and sender['avatar_emoji']) else '👤'
        
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": int(receiver_id),
            "text": f"*New Chat Request*\n\n{sender_icon} *{sender_name}* wants to chat with you.",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "Accept", "callback_data": f"acceptchat_{sender_id}"},
                        {"text": "Decline", "callback_data": f"declinechat_{sender_id}"}
                    ],
                    [
                        {"text": "View Profile", "url": f"https://t.me/{BOT_USERNAME}?start=profileid_{sender_id}"}
                    ]
                ]
            },
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to send chat request notification: {e}")
    
    return jsonify({'success': True, 'status': 'pending', 'message': 'Chat request sent'})
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
                u.weekly_badge,
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
                'id': str(user['user_id']),
                'rank': idx,
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'avatar': user['avatar_emoji'] or "",
                'points': user['total'],
                'aura': format_aura(user['total']),
                'weekly_badge': user['weekly_badge'] or ""
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
        
        # Check viewer for privacy
        viewer_id = request.args.get('viewer_id')
        viewer = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (viewer_id,)) if viewer_id else None
        is_viewer_admin = viewer['is_admin'] if viewer else False
        is_owner = str(user_id) == str(viewer_id)
        
        followers = db_fetch_one(
            "SELECT COUNT(*) as count FROM followers WHERE followed_id = %s",
            (user_id,)
        )
        follower_count = followers['count'] if followers else 0
        
        aura_display = "" if user.get('is_admin') else format_aura(rating)
        rating_display = rating
        
        # Apply privacy
        if not is_viewer_admin and not is_owner:
            if user.get('hide_aura'):
                aura_display = "Hidden"
                rating_display = "Hidden"
            if user.get('hide_follower_count'):
                follower_count = "Hidden"

        posts = db_fetch_one(
            "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE",
            (user_id,)
        )
        
        comments = db_fetch_one(
            "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
            (user_id,)
        )
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'avatar': user['avatar_emoji'] or "",
                'weekly_badge': user['weekly_badge'] or "",
                'rating': rating_display,
                'aura': aura_display,
                'is_admin': bool(user.get('is_admin')),


                'stats': {
                    'followers': follower_count,
                    'posts': posts['count'] if posts else 0,
                    'comments': comments['count'] if comments else 0
                }
            }
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app profile: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _require_admin(user_id):
    user = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(user_id),))
    return bool(user and user.get('is_admin'))


@flask_app.route('/api/mini-app/admin/chats', methods=['GET'])
def mini_app_admin_chats():
    admin_id = request.args.get('admin_id')
    if not admin_id or not _require_admin(admin_id):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    page = int(request.args.get('page', 1))
    search = (request.args.get('search') or '').strip() or None
    per_page = 20
    offset = (page - 1) * per_page

    convos = get_admin_conversations(limit=per_page, offset=offset, search=search)
    total = get_admin_conversations_count(search=search)

    data = []
    for c in convos:
        last_ts = c['last_ts']
        data.append({
            'user_a': c['user_a'], 'user_b': c['user_b'],
            'name_a': c['name_a'] or 'Anonymous', 'name_b': c['name_b'] or 'Anonymous',
            'avatar_a': c.get('avatar_a') or c.get('sex_a') or '👤',
            'avatar_b': c.get('avatar_b') or c.get('sex_b') or '👤',
            'msg_count': c['msg_count'],
            'last_content': c['last_content'],
            'last_media_type': c.get('last_media_type'),
            'last_sender_id': c['last_sender_id'],
            'last_ts': last_ts.isoformat() if hasattr(last_ts, 'isoformat') else str(last_ts)
        })

    return jsonify({'success': True, 'data': data, 'page': page, 'has_more': len(convos) == per_page, 'total': total})


@flask_app.route('/api/mini-app/admin/chats/<user_a>/<user_b>', methods=['GET'])
def mini_app_admin_chat_transcript(user_a, user_b):
    admin_id = request.args.get('admin_id')
    if not admin_id or not _require_admin(admin_id):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    limit = int(request.args.get('limit', 100))
    msgs = get_admin_conversation_transcript(user_a, user_b, limit=limit)

    data = []
    for m in msgs:
        ts = m['timestamp']
        data.append({
            'id': m['message_id'],
            'sender_id': m['sender_id'],
            'receiver_id': m['receiver_id'],
            'content': m['content'],
            'media_type': m.get('media_type', 'text'),
            'media_id': m.get('media_id'),
            'time_display': format_ethiopian_time(ts)
        })

    return jsonify({'success': True, 'data': data})

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
                p.explicit,
                u.anonymous_name as author_name,
                u.sex as author_sex,
                STRING_AGG(pc.category_code, ',') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = FALSE
            GROUP BY p.post_id, u.anonymous_name, u.sex, p.content, p.timestamp, p.media_type, p.explicit
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
        user_id = request.args.get('user_id')
        
        sql = '''
            SELECT p.post_id, p.content, p.timestamp, p.comment_count, p.explicit, p.media_type, p.media_id,
                   u.user_id as author_id, u.sex as author_sex, u.avatar_emoji as author_avatar, u.anonymous_name as author_name,
                   STRING_AGG(DISTINCT pc.category_code, ',') as categories
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = TRUE AND p.deleted = FALSE
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
        viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(user_id),)) if user_id else None
        is_admin_viewer = bool(viewer_row and viewer_row.get('is_admin'))
        for post in posts:
            rating = calculate_user_rating(post['author_id'])
            is_owner = str(post['author_id']) == str(user_id)
            is_explicit = bool(post.get('explicit'))
            hide_content = is_explicit and not is_owner and not is_admin_viewer
            content_preview = post['content'][:300] + '...' if len(post['content']) > 300 else post['content']
            if hide_content:
                content_preview = "This post contains explicit content that may not be suitable for all viewers."
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'categories': post['categories'].split(',') if post['categories'] else [],
                'comments': post['comment_count'] or 0,
                'explicit': is_explicit,
                'content_hidden': hide_content,
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
