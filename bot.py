import jwt
import requests
import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
    TypeHandler, ApplicationHandlerStop
)
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode
from telegram.error import BadRequest
import threading
from waitress import serve as waitress_serve
from flask import Flask, jsonify, request, redirect, send_from_directory, Response
# NOTE: `time` used to be imported twice (datetime.time, then the `time` module), so the
# module silently shadowed the class and `time(0, 0, tzinfo=...)` raised TypeError.
# datetime.time is now imported as dt_time; `time` is always the stdlib module.
from datetime import datetime, timedelta, timezone, time as dt_time
import time
import asyncio
import html
from types import SimpleNamespace, MappingProxyType
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import json
import re
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9
    ZoneInfo = None

# moved logger setup to top
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
EXPLICIT_WARNING_HTML = (
    "<pre>"
    "        ⚠️ ማስጠንቀቂያ ⚠️\n"
    "\n"
    " የዚህ post ይዘት ለሁሉም አባላት ተገቢ አይደለም።\n"
    " በራስዎ ሃላፊነት ይህንን ፖስት ማንበብ ከፈለጉ፣\n"
    "ከታች ያለውን \"View Post\" የሚለውን ይጫኑ።"
    "</pre>"
)
# Per-vent "show my sex" question. Plain text on purpose: no emoji decoration.
SHOW_SEX_QUESTION = "Show your sex under the vent number on this post?"
SHOW_SEX_YES_LABEL = "Yes"
SHOW_SEX_NO_LABEL = "No"
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
                    -- DEPRECATED (kept only so existing rows/backups still load cleanly):
                    -- awaiting_name, waiting_for_post, waiting_for_comment, selected_category,
                    -- comment_post_id, comment_idx, reply_idx, nested_idx,
                    -- waiting_for_private_message, private_message_target, awaiting_bio.
                    -- The bot no longer reads or writes any of these - all per-user
                    -- conversational state now lives in context.user_data (see
                    -- get_state/set_state/reset_state). Safe to drop in a later migration
                    -- once you've confirmed nothing external still reads them, e.g.:
                    --   ALTER TABLE users DROP COLUMN awaiting_name, DROP COLUMN waiting_for_post,
                    --     DROP COLUMN waiting_for_comment, DROP COLUMN selected_category,
                    --     DROP COLUMN comment_post_id, DROP COLUMN comment_idx, DROP COLUMN reply_idx,
                    --     DROP COLUMN nested_idx, DROP COLUMN waiting_for_private_message,
                    --     DROP COLUMN private_message_target, DROP COLUMN awaiting_bio;
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
                # ONE information_schema query for every column probe below (previously one
                # round trip per column). Membership is checked in Python against this snapshot;
                # each probed column is independent, so no probe depends on an earlier ALTER.
                c.execute("""
                    SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name IN ('users', 'posts', 'comments', 'reactions',
                                         'private_messages', 'blocks')
                """)
                existing_columns = {(r['table_name'], r['column_name']) if isinstance(r, dict) else (r[0], r[1])
                                    for r in c.fetchall()}

                # Check for 'bio' column in users
                if ('users', 'bio') not in existing_columns:
                    logger.info("Adding missing column: bio to users table")
                    c.execute("ALTER TABLE users ADD COLUMN bio TEXT DEFAULT 'No bio set.'")

                # Check for 'awaiting_bio' column in users
                if ('users', 'awaiting_bio') not in existing_columns:
                    logger.info("Adding missing column: awaiting_bio to users table")
                    c.execute("ALTER TABLE users ADD COLUMN awaiting_bio BOOLEAN DEFAULT FALSE")

                # Check for 'avatar_emoji' column in users
                if ('users', 'avatar_emoji') not in existing_columns:
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
                    if ('users', col_name) not in existing_columns:
                        logger.info(f"Adding missing column: {col_name} to users table")
                        c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")

                # Add timestamp to reactions
                if ('reactions', 'timestamp') not in existing_columns:
                    c.execute("ALTER TABLE reactions ADD COLUMN timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    logger.info("Added timestamp column to reactions table")

                # Add post_id to reactions table and update indexes
                if ('reactions', 'post_id') not in existing_columns:
                    c.execute("ALTER TABLE reactions ALTER COLUMN comment_id DROP NOT NULL")
                    c.execute("ALTER TABLE reactions ADD COLUMN post_id INTEGER REFERENCES posts(post_id) DEFAULT NULL")
                    logger.info("Added post_id column to reactions table")
                    
                    c.execute("ALTER TABLE reactions DROP CONSTRAINT IF EXISTS reactions_comment_id_user_id_key")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reactions_post_user ON reactions (post_id, user_id) WHERE post_id IS NOT NULL")
                    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reactions_comment_user ON reactions (comment_id, user_id) WHERE comment_id IS NOT NULL")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_reactions_lookup ON reactions (post_id, comment_id, type)")

                # Indexes for the columns comment/rating lookups filter on - these were
                # missing, so every comments-page load and every rating calculation was
                # doing sequential scans on posts/comments/blocks/followers as they grow.
                c.execute("CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments (post_id, timestamp)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_comments_author_id ON comments (author_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_comments_parent_id ON comments (parent_comment_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_posts_author_id ON posts (author_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_reactions_comment_id ON reactions (comment_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_blocks_blocked_id ON blocks (blocked_id)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_followers_followed_id ON followers (followed_id)")

                # These were previously created only inside the one-time reactions.post_id
                # migration above, so they'd silently never exist on a DB where that
                # migration had already run before this fix (e.g. restored from a backup
                # taken after the migration, or the migration re-ordered). Moved out here
                # so they're always ensured, like every other index in this block.
                c.execute("CREATE INDEX IF NOT EXISTS idx_pm_lookup ON private_messages (sender_id, receiver_id, timestamp DESC)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_pm_unread ON private_messages (receiver_id, is_read)")
                # Covers the reverse direction of get_admin_conversation_transcript's /
                # get_admin_conversations' LATERAL "last message between A and B" lookup
                # (idx_pm_lookup only covers sender->receiver order efficiently).
                c.execute("CREATE INDEX IF NOT EXISTS idx_pm_receiver_sender ON private_messages (receiver_id, sender_id, timestamp DESC)")
                # get_admin_conversations groups by LEAST(sender_id, receiver_id) /
                # GREATEST(sender_id, receiver_id) to find each unique conversation pair.
                # That expression can't use a plain btree on (sender_id, receiver_id), so
                # without this the whole private_messages table gets hash-aggregated on
                # every admin conversations list/search. A matching expression index lets
                # Postgres do an index-only scan for both the grouping and the MAX(timestamp)
                # ordering instead.
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_pm_conversation_pair
                    ON private_messages (LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id), timestamp DESC)
                """)

                # Private messages media columns
                if ('private_messages', 'media_type') not in existing_columns:
                    logger.info("Adding missing media columns to private_messages table")
                    c.execute("ALTER TABLE private_messages ADD COLUMN media_type TEXT DEFAULT 'text'")
                    c.execute("ALTER TABLE private_messages ADD COLUMN media_id TEXT")

                # Private messages edit/delete columns - lets a sender edit or
                # (soft-)delete a message they sent, in both the mini app and the bot.
                # Soft-delete (is_deleted flag) is used instead of a hard DELETE so the
                # other side's thread keeps a "Message deleted" placeholder instead of
                # a confusing gap.
                if ('private_messages', 'is_edited') not in existing_columns:
                    logger.info("Adding edit/delete columns to private_messages table")
                    c.execute("ALTER TABLE private_messages ADD COLUMN is_edited BOOLEAN DEFAULT FALSE")
                    c.execute("ALTER TABLE private_messages ADD COLUMN edited_at TIMESTAMP")
                    c.execute("ALTER TABLE private_messages ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE")
                    c.execute("ALTER TABLE private_messages ADD COLUMN deleted_at TIMESTAMP")

                # notif_message_id: the message_id of the live "New Private Message"
                # notification the bot delivered to the receiver's chat. Storing it lets
                # us natively edit/delete that real Telegram message later (via the
                # Bot API) instead of only updating our own DB copy.
                if ('private_messages', 'notif_message_id') not in existing_columns:
                    logger.info("Adding notif_message_id column to private_messages table")
                    c.execute("ALTER TABLE private_messages ADD COLUMN notif_message_id INTEGER")

                # reply_to_id: the private message this one quotes (Telegram-style reply, no nesting).
                if ('private_messages', 'reply_to_id') not in existing_columns:
                    logger.info("Adding reply_to_id column to private_messages table")
                    c.execute("ALTER TABLE private_messages ADD COLUMN reply_to_id INTEGER")

                # Editing/deleting private messages now uses Telegram's native
                # edit/delete on the real notification message, so a soft-deleted
                # message no longer needs (or gets) a "Message deleted" placeholder —
                # it's just gone. Purge any old soft-deleted rows left over from
                # before this change so nothing embarrassing lingers.
                c.execute("DELETE FROM private_messages WHERE is_deleted = TRUE")

                # Add timestamp to blocks
                if ('blocks', 'timestamp') not in existing_columns:
                    c.execute("ALTER TABLE blocks ADD COLUMN timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                    logger.info("Added timestamp column to blocks table")

                # Add weekly_badge to users
                if ('users', 'weekly_badge') not in existing_columns:
                    c.execute("ALTER TABLE users ADD COLUMN weekly_badge TEXT DEFAULT NULL")
                    logger.info("Added weekly_badge column to users table")


                # ---------------- Database Schema Migration ----------------
                # Check if thread_from_post_id column exists, if not add it
                if ('posts', 'thread_from_post_id') not in existing_columns:
                    logger.info("Adding missing column: thread_from_post_id to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN thread_from_post_id BIGINT DEFAULT NULL")

                # Check if vent_number column exists, if not add it
                if ('posts', 'vent_number') not in existing_columns:
                    logger.info("Adding missing column: vent_number to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN vent_number INTEGER DEFAULT NULL")
                
                # Check for 'rejection_reason' column in posts
                if ('posts', 'rejection_reason') not in existing_columns:
                    logger.info("Adding missing column: rejection_reason to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN rejection_reason TEXT DEFAULT NULL")

                # Check for 'search_vector' column in posts
                if ('posts', 'search_vector') not in existing_columns:
                    logger.info("Adding search_vector to posts table")
                    c.execute("SAVEPOINT sp_search_vector")
                    try:
                        c.execute("""
                            ALTER TABLE posts ADD COLUMN search_vector tsvector
                            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
                        """)
                        c.execute("CREATE INDEX idx_posts_search ON posts USING GIN(search_vector)")
                        c.execute("RELEASE SAVEPOINT sp_search_vector")
                    except Exception as e:
                        # Roll back just this step so the rest of init_db's transaction survives.
                        c.execute("ROLLBACK TO SAVEPOINT sp_search_vector")
                        logger.error(f"Failed to add search_vector (maybe not Postgres?): {e}")
                
                # ---------------- Database Multi-Category Migration ----------------
                # 1. Add selected_categories to users table
                if ('users', 'selected_categories') not in existing_columns:
                    logger.info("Adding missing column: selected_categories to users table")
                    c.execute("ALTER TABLE users ADD COLUMN selected_categories TEXT DEFAULT NULL")

                # 2. Check if posts still has 'category' column
                has_category_column = ('posts', 'category') in existing_columns

                if has_category_column:
                    # Create junction table
                    c.execute('''
                        CREATE TABLE IF NOT EXISTS post_categories (
                            post_id INTEGER REFERENCES posts(post_id) ON DELETE CASCADE,
                            category_code TEXT,
                            PRIMARY KEY (post_id, category_code)
                        )
                    ''')
                    # added category migration
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

                # User reports store the reported user's Telegram ID in target_id, and
                # Telegram IDs can exceed the 32-bit INTEGER range, so widen the column.
                c.execute("""
                    SELECT data_type FROM information_schema.columns
                    WHERE table_name = 'reports' AND column_name = 'target_id'
                """)
                _tid_row = c.fetchone()
                _tid_type = (_tid_row['data_type'] if isinstance(_tid_row, dict) else _tid_row[0]) if _tid_row else None
                if _tid_type == 'integer':
                    logger.info("Widening reports.target_id to BIGINT")
                    c.execute("ALTER TABLE reports ALTER COLUMN target_id TYPE BIGINT")

                # ---------------- warning_count column migration ----------------
                if ('users', 'warning_count') not in existing_columns:
                    logger.info("Adding missing column: warning_count to users table")
                    c.execute("ALTER TABLE users ADD COLUMN warning_count INTEGER DEFAULT 0")

                # ---------------- moderation (ban / warn) migration ----------------
                for col_name, col_type in [
                    ('is_banned', 'BOOLEAN DEFAULT FALSE'),
                    ('ban_reason', 'TEXT DEFAULT NULL'),
                    ('banned_at', 'TIMESTAMP DEFAULT NULL'),
                    ('banned_by', 'TEXT DEFAULT NULL'),
                ]:
                    if ('users', col_name) not in existing_columns:
                        logger.info(f"Adding missing column: {col_name} to users table")
                        c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")

                c.execute('''
                    CREATE TABLE IF NOT EXISTS user_warnings (
                        warning_id SERIAL PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        admin_id TEXT,
                        reason TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                c.execute("CREATE INDEX IF NOT EXISTS idx_user_warnings_user ON user_warnings (user_id, created_at DESC)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users (is_banned) WHERE is_banned = TRUE")

                # Check for 'thread_context_post_id' column in users
                if ('users', 'thread_context_post_id') not in existing_columns:
                    logger.info("Adding missing column: thread_context_post_id to users table")
                    c.execute("ALTER TABLE users ADD COLUMN thread_context_post_id BIGINT DEFAULT NULL")

                # Added telegram_message_id to comments for cross-page threading
                if ('comments', 'telegram_message_id') not in existing_columns:
                    logger.info("Adding telegram_message_id column to comments table")
                    c.execute("ALTER TABLE comments ADD COLUMN telegram_message_id BIGINT DEFAULT NULL")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_telegram_message_id ON comments(telegram_message_id)")

                # Check for 'deleted' column in posts
                if ('posts', 'deleted') not in existing_columns:
                    logger.info("Adding missing column: deleted to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN deleted BOOLEAN DEFAULT FALSE")

                # Check for 'explicit' column in posts
                if ('posts', 'explicit') not in existing_columns:
                    logger.info("Adding missing column: explicit to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN explicit BOOLEAN DEFAULT FALSE")

                # Check for 'revealed_sex' column in posts. Holds the sex emoji the author
                # chose to show under the vent number for THIS post (a snapshot taken at
                # submission time), or NULL when the author kept it hidden. NULL for every
                # existing post, so nothing already published changes.
                if ('posts', 'revealed_sex') not in existing_columns:
                    logger.info("Adding missing column: revealed_sex to posts table")
                    c.execute("ALTER TABLE posts ADD COLUMN revealed_sex TEXT DEFAULT NULL")

                # Partial index that lets the mini-app feed (approved, not deleted, newest first)
                # read one page straight off the index instead of sorting every post.
                c.execute("""
                    CREATE INDEX IF NOT EXISTS idx_posts_feed
                    ON posts (timestamp DESC) WHERE approved = TRUE AND deleted = FALSE
                """)

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
    """Give every approved post that has no vent_number the next free number, oldest
    first (continuing after the current maximum, exactly like the old per-row loop).
    Startup cost when nothing needs numbering: one cheap EXISTS query."""
    try:
        needs_numbers = db_fetch_one(
            "SELECT EXISTS(SELECT 1 FROM posts WHERE approved = TRUE AND vent_number IS NULL) AS needs"
        )
        if not needs_numbers or not needs_numbers['needs']:
            return

        # One set-based UPDATE instead of 2-3 queries per post. The scalar subquery is
        # evaluated once against the pre-update snapshot, so numbers continue after MAX.
        updated = db_execute("""
            UPDATE posts
            SET vent_number = t.rn + COALESCE((SELECT MAX(vent_number) FROM posts WHERE approved = TRUE), 0)
            FROM (
                SELECT post_id, ROW_NUMBER() OVER (ORDER BY timestamp ASC, post_id ASC) AS rn
                FROM posts
                WHERE approved = TRUE AND vent_number IS NULL
            ) t
            WHERE posts.post_id = t.post_id AND posts.vent_number IS NULL
        """)
        logger.info(f"Assigned vent numbers to existing approved posts (update ok={updated})")

    except Exception as e:
        logger.error(f"Error assigning vent numbers: {e}")

async def fix_vent_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix vent numbers"""
    user_id = str(update.effective_user.id)
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    
    if not user or not user['is_admin']:
        await update.message.reply_text("You don't have permission to use this command.")
        return
    
    await update.message.reply_text("Reassigning vent numbers to all approved posts...")
    
    try:
        # Reset all vent numbers first
        (await db_execute_async("UPDATE posts SET vent_number = NULL WHERE approved = TRUE"))
        
        # Get all approved posts in chronological order
        posts = (await db_fetch_all_async(
            "SELECT post_id FROM posts WHERE approved = TRUE ORDER BY timestamp ASC"
        ))
        
        count = 0
        for idx, post in enumerate(posts, start=1):
            (await db_execute_async(
                "UPDATE posts SET vent_number = %s WHERE post_id = %s",
                (idx, post['post_id'])
            ))
            count += 1
        
        await update.message.reply_text(f"Successfully assigned vent numbers to {count} posts.")
        
    except Exception as e:
        logger.error(f"Error in fix_vent_numbers: {e}")
        await update.message.reply_text(f"Error: {str(e)}")

async def fix_missing_sex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix missing sex emoji for users with avatars"""
    user_id = str(update.effective_user.id)
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user.get('is_admin'):
        await update.message.reply_text("Admin only.")
        return

    # Fix users where sex is NULL or empty but avatar_emoji exists
    rows_fixed = (await db_execute_async("""
        UPDATE users 
        SET sex = '👤' 
        WHERE (sex IS NULL OR sex = '') 
        AND avatar_emoji IS NOT NULL
    """))
    _invalidate_user_cache()
    
    await update.message.reply_text(f"Fixed missing sex for {rows_fixed} users.")


async def reset_weekly_badges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger weekly badge awarding."""
    user_id = str(update.effective_user.id)
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    
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

# SPEED: these helpers used to *sleep* on purpose (0.3-2s each, several per screen) so loading
# "animations" could be seen. They are now instant; signatures are unchanged so every caller
# still works. The typing indicator is fire-and-forget (it is cosmetic, never worth waiting on).
async def _send_typing(context, chat_id):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

async def typing_animation(context, chat_id, duration=1):
    """Show typing indicator (does not wait; `duration` kept for compatibility)."""
    try:
        asyncio.create_task(_send_typing(context, chat_id))
    except Exception:
        pass

async def animated_loading(loading_msg, text="Processing", steps=3):
    """Formerly animated dots with sleeps; now a no-op (the loading message is shown as-is
    and replaced by the real content as soon as it's ready)."""
    return None

async def replace_with_success(loading_msg, success_text):
    """Replace loading message with success message (no artificial pause)"""
    try:
        return await loading_msg.edit_text(f"{success_text}")
    except Exception:
        return loading_msg

async def replace_with_error(loading_msg, error_text):
    """Replace loading message with error message (no artificial pause)"""
    try:
        await loading_msg.edit_text(f"{error_text}")
        return loading_msg
    except Exception:
        return loading_msg
# Database helper functions
# -------------------- PostgreSQL Connection Pool --------------------
from psycopg2 import pool

# Create a global connection pool (reuses DB connections instead of reconnecting every time)
try:
    db_pool = pool.ThreadedConnectionPool(
        # ThreadedConnectionPool, not SimpleConnectionPool: getconn/putconn are now
        # called concurrently from multiple threads (Flask's own thread, plus every
        # asyncio.to_thread worker), and SimpleConnectionPool isn't safe for that.
        2, 20,  # min 2, max 20 connections - raised from 10 now that DB calls run
                # concurrently instead of one-at-a-time on the event loop
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=5
    )
    logging.info("Database connection pool created successfully")
except Exception as e:
    logging.error(f"Failed to create database pool: {e}")
    db_pool = None


# ThreadedConnectionPool.getconn() RAISES "connection pool exhausted" the instant all 20
# connections are busy instead of waiting. With handlers now running concurrently, a burst would
# turn into errors, so callers first take a slot from this semaphore (same size as the pool) and
# simply queue for a moment instead.
_DB_MAX_CONNECTIONS = 20
_db_slots = threading.BoundedSemaphore(_DB_MAX_CONNECTIONS)


def _acquire_db_slot():
    if not _db_slots.acquire(timeout=30):
        raise RuntimeError("Timed out waiting for a free database connection")


def db_execute(query, params=(), fetch=False, fetchone=False):
    """Execute a SQL query using the global connection pool. Raises on error."""
    conn = None
    _acquire_db_slot()
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
        _db_slots.release()


def db_fetch_one(query, params=()):
    return db_execute(query, params, fetchone=True)

def db_fetch_all(query, params=()):
    return db_execute(query, params, fetch=True)


# -------------------- Async DB wrappers (Telegram handlers only) --------------------
# psycopg2 is synchronous, so calling db_execute/db_fetch_one/db_fetch_all directly
# from an `async def` handler blocks the whole event loop for every other chat
# until the query returns. asyncio.to_thread() runs the same sync call in the
# default thread pool instead, freeing the loop to keep handling other updates.
#
# Flask routes should keep calling the sync db_execute/db_fetch_one/db_fetch_all
# directly - each Flask request already runs in its own worker thread, so there's
# no event loop being blocked and wrapping them here would just add overhead.
async def db_execute_async(query, params=(), fetch=False, fetchone=False):
    """Async version of db_execute for use inside Telegram bot handlers (async def ...)."""
    try:
        return await asyncio.to_thread(db_execute, query, params, fetch, fetchone)
    except Exception as e:
        # db_execute already logs and re-raises; this log adds the async call-site context.
        logging.error(f"Async database error (db_execute_async): {e}")
        raise


async def db_fetch_one_async(query, params=()):
    """Async version of db_fetch_one for use inside Telegram bot handlers."""
    return await db_execute_async(query, params, fetchone=True)


async def db_fetch_all_async(query, params=()):
    """Async version of db_fetch_all for use inside Telegram bot handlers."""
    return await db_execute_async(query, params, fetch=True)


# ==================== SHARED PERF / CONSISTENCY HELPERS ====================

def _clamp_int(raw, lo, hi, default):
    """Parse `raw` (usually a query-string value) as an int and clamp it to [lo, hi].
    Missing / non-numeric input falls back to `default` (which is clamped too)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _parse_positive_int(raw):
    """Optional id-style query parameter: a positive int32, or None if absent/invalid.
    (Not _clamp_int: a missing before_id must stay None, not get clamped up to 1.)"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 2**31 - 1 else None


def _escape_like(term):
    """Escape LIKE/ILIKE wildcards so user input is matched literally.
    Use together with `ESCAPE '\\'` in the SQL."""
    return (term or '').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


# ---- user row cache (only the columns hot paths actually need) ----
_USER_CACHE_COLUMNS = (
    "user_id, is_admin, notifications_enabled, anonymous_name, sex, avatar_emoji, bio, "
    "weekly_badge, hide_aura, hide_bio, hide_follower_count, hide_role, is_banned, ban_reason"
)
# lru_cache cannot evict a single key, and a plain cache_clear() races with a reader that
# fetched the old row just before an UPDATE committed (it would re-store stale data right
# after the clear). Putting a generation number in the cache key fixes both: invalidation
# bumps the generation, so every entry (and any in-flight stale read) becomes unreachable.
_user_cache_gen = 0
_user_cache_lock = threading.Lock()


@lru_cache(maxsize=4096)
def _user_row_cached(user_id, gen):
    row = db_fetch_one(f"SELECT {_USER_CACHE_COLUMNS} FROM users WHERE user_id = %s", (user_id,))
    if row is None:
        # lru_cache never caches exceptions, so "user not found" is not remembered and a
        # user created a moment later is picked up on the very next call.
        raise LookupError(user_id)
    return MappingProxyType(dict(row))  # read-only: callers share this object


def get_user_cached(user_id):
    """Small cached replacement for `SELECT * FROM users WHERE user_id = ...` on hot paths.
    Returns a read-only mapping with the columns in _USER_CACHE_COLUMNS, or None if the
    user does not exist. BLOCKING (may hit the DB): from async code use
    `await asyncio.to_thread(get_user_cached, uid)`."""
    try:
        return _user_row_cached(str(user_id), _user_cache_gen)
    except LookupError:
        return None


@lru_cache(maxsize=2048)
def _user_display_name_cached(user_id, gen):
    row = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (user_id,))
    if row is None:
        raise LookupError(user_id)
    return row['anonymous_name'] or 'Anon'


def get_user_display_name(user_id, default='Anon'):
    """Cached anonymous_name lookup (used by the 8-second admin live monitor tick)."""
    try:
        return _user_display_name_cached(str(user_id), _user_cache_gen)
    except LookupError:
        return default


def _invalidate_user_cache(user_id=None):
    """Drop cached user rows / display names. Call AFTER the UPDATE has committed.
    `user_id` is accepted for readability at call sites; invalidation is global."""
    global _user_cache_gen
    with _user_cache_lock:
        _user_cache_gen += 1
    _user_row_cached.cache_clear()
    _user_display_name_cached.cache_clear()


_USER_COLUMN_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


def db_update_user(user_id, **fields):
    """UPDATE users SET <col>=<val>, ... WHERE user_id = <user_id>, then invalidate the
    user caches. The single place user-row writes should go through. Column names are
    identifiers (never user input) and are validated; values are always parameters."""
    if not fields:
        return False
    for col in fields:
        if not _USER_COLUMN_RE.match(col):
            raise ValueError(f"Invalid users column name: {col!r}")
    assignments = ", ".join(f"{col} = %s" for col in fields)
    result = db_execute(
        f"UPDATE users SET {assignments} WHERE user_id = %s",
        tuple(fields.values()) + (str(user_id),)
    )
    _invalidate_user_cache(user_id)
    return result


async def db_update_user_async(user_id, **fields):
    """Async wrapper of db_update_user for Telegram handlers."""
    return await asyncio.to_thread(db_update_user, user_id, **fields)


# ---- keep-alive HTTP session for Telegram Bot API calls made from Flask/worker threads ----
# requests.post() opens a brand-new TLS connection every call (~100-300ms of handshake). One
# shared Session with a connection pool reuses them.
_tg_session = requests.Session()
_tg_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=20)
_tg_session.mount("https://", _tg_adapter)


# ---- fire-and-forget Telegram HTTP notifications ----
_NOTIFY_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tg-notify")


def _fire_and_forget(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) on the notification pool and return immediately.
    Errors are logged, never raised into the request thread."""
    def _log_outcome(fut):
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            logger.error(f"Background notification failed: {exc}")
    try:
        _NOTIFY_EXECUTOR.submit(fn, *args, **kwargs).add_done_callback(_log_outcome)
    except RuntimeError as e:  # pool already shut down (interpreter exiting)
        logger.error(f"Could not queue background notification: {e}")


# ---- main-menu JWT cache ----
_MENU_JWT_TTL_SECONDS = 25 * 24 * 3600   # token itself is valid for 30 days
_menu_jwt_cache = {}
_menu_jwt_lock = threading.Lock()


def _get_menu_jwt(user_id):
    uid = str(user_id)
    now = time.time()
    with _menu_jwt_lock:
        hit = _menu_jwt_cache.get(uid)
        if hit and hit[1] > now:
            return hit[0]
    token = jwt.encode(
        {'user_id': uid, 'exp': datetime.now(timezone.utc) + timedelta(days=30)},
        TOKEN,
        algorithm='HS256'
    )
    with _menu_jwt_lock:
        if len(_menu_jwt_cache) > 50000:  # keep memory bounded: drop expired entries
            for k in [k for k, v in _menu_jwt_cache.items() if v[1] <= now]:
                _menu_jwt_cache.pop(k, None)
        _menu_jwt_cache[uid] = (token, now + _MENU_JWT_TTL_SECONDS)
    return token


def _clear_menu_jwt_cache(user_id=None):
    """Forget cached mini-app tokens (one user, or all). Call on logout / token rebuild."""
    with _menu_jwt_lock:
        if user_id is None:
            _menu_jwt_cache.clear()
        else:
            _menu_jwt_cache.pop(str(user_id), None)


# ---- reaction weights + ONE scoring query shared by every aura/leaderboard path ----
# Emoji are written as escapes on purpose: the previous dict literal lost its emoji
# somewhere along the way and collapsed into duplicate '' keys.
REACTION_WEIGHTS = {
    'like': 1,                 # legacy text types
    'dislike': -2,
    'heart': 2,
    '\U0001F44D': 1,           # 👍
    '\U0001F44E': -2,          # 👎
    '\U0001F621': -2,          # 😡
    '\u2764\ufe0f': 2,         # ❤️
    '\u2764': 2,               # ❤ (no variation selector)
    '\U0001F64F': 2,           # 🙏
    '\U0001F525': 2,           # 🔥
    '\U0001F622': 1,           # 😢
}
DEFAULT_REACTION_WEIGHT = 1    # any type not listed above (same default as before)

WEEKLY_BADGES = ["\U0001F947", "\U0001F948", "\U0001F949"]  # 🥇 🥈 🥉


def _reaction_weight_case_sql(type_col):
    """SQL CASE expression mapping reactions.type -> weight, built from REACTION_WEIGHTS.
    Keys are code constants; anything that could break out of a SQL literal is rejected."""
    whens = []
    for key, weight in REACTION_WEIGHTS.items():
        if "'" in key or '%' in key or '\\' in key:
            raise ValueError(f"Unsafe reaction key for SQL: {key!r}")
        whens.append(f"WHEN '{key}' THEN {int(weight)}")
    return f"(CASE {type_col} {' '.join(whens)} ELSE {int(DEFAULT_REACTION_WEIGHT)} END)"


# Requires a preceding CTE:  score_authors(author_id TEXT)  - the set of users to score.
# Each component is aggregated once over that set (no per-user correlated subqueries) and
# the result `author_scores(author_id, score)` matches calculate_user_rating() exactly:
#   approved posts*10 + comments*2 + weighted reactions on their comments AND posts
#   + followers*2 - blocks received*10
_SCORE_CTES_SQL = f"""
    a_posts AS (
        SELECT p.author_id, COUNT(*) AS n
        FROM posts p JOIN score_authors sa ON sa.author_id = p.author_id
        WHERE p.approved = TRUE
        GROUP BY p.author_id
    ),
    a_comments AS (
        SELECT c.author_id, COUNT(*) AS n
        FROM comments c JOIN score_authors sa ON sa.author_id = c.author_id
        GROUP BY c.author_id
    ),
    a_comment_rx AS (
        SELECT c.author_id, SUM({_reaction_weight_case_sql('r.type')}) AS pts
        FROM comments c
        JOIN score_authors sa ON sa.author_id = c.author_id
        JOIN reactions r ON r.comment_id = c.comment_id
        GROUP BY c.author_id
    ),
    a_post_rx AS (
        SELECT p.author_id, SUM({_reaction_weight_case_sql('r.type')}) AS pts
        FROM posts p
        JOIN score_authors sa ON sa.author_id = p.author_id
        JOIN reactions r ON r.post_id = p.post_id
        GROUP BY p.author_id
    ),
    a_blocks AS (
        SELECT b.blocked_id AS author_id, COUNT(*) AS n
        FROM blocks b JOIN score_authors sa ON sa.author_id = b.blocked_id
        GROUP BY b.blocked_id
    ),
    a_followers AS (
        SELECT f.followed_id AS author_id, COUNT(*) AS n
        FROM followers f JOIN score_authors sa ON sa.author_id = f.followed_id
        GROUP BY f.followed_id
    ),
    author_scores AS (
        SELECT sa.author_id,
               (COALESCE(ap.n, 0) * 10 + COALESCE(ac.n, 0) * 2
                + COALESCE(acr.pts, 0) + COALESCE(apr.pts, 0)
                + COALESCE(af.n, 0) * 2 - COALESCE(ab.n, 0) * 10) AS score
        FROM score_authors sa
        LEFT JOIN a_posts ap ON ap.author_id = sa.author_id
        LEFT JOIN a_comments ac ON ac.author_id = sa.author_id
        LEFT JOIN a_comment_rx acr ON acr.author_id = sa.author_id
        LEFT JOIN a_post_rx apr ON apr.author_id = sa.author_id
        LEFT JOIN a_blocks ab ON ab.author_id = sa.author_id
        LEFT JOIN a_followers af ON af.author_id = sa.author_id
    )
"""


# ---- leaderboard cache (60s, busted whenever anything that moves a score changes) ----
_LEADERBOARD_TTL_SECONDS = 60
_leaderboard_gen = 0
_leaderboard_lock = threading.Lock()


def _leaderboard_cache_bust():
    """Invalidate the cached leaderboard. Cheap and safe to call from any thread."""
    global _leaderboard_gen
    with _leaderboard_lock:
        _leaderboard_gen += 1
    _leaderboard_rows_cached.cache_clear()


@lru_cache(maxsize=16)
def _leaderboard_rows_cached(top_n, gen, bucket):
    rows = db_fetch_all(f"""
        WITH score_authors AS (
            SELECT user_id AS author_id FROM users WHERE is_admin = FALSE
        ),
        {_SCORE_CTES_SQL}
        SELECT u.user_id, u.anonymous_name, u.sex, u.avatar_emoji, u.weekly_badge, s.score AS total
        FROM author_scores s
        JOIN users u ON u.user_id = s.author_id
        ORDER BY s.score DESC, u.user_id
        LIMIT %s
    """, (top_n,))
    return tuple(dict(r) for r in (rows or []))


def get_leaderboard_rows(top_n=10):
    """Top `top_n` non-admin users by aura score, cached for ~60s per top_n.
    Returns fresh dict copies (safe for callers to mutate). BLOCKING on a cache miss."""
    top_n = _clamp_int(top_n, 1, 100, 10)
    bucket = int(time.time() // _LEADERBOARD_TTL_SECONDS)
    return [dict(r) for r in _leaderboard_rows_cached(top_n, _leaderboard_gen, bucket)]

# ---- channel text for a post its author deleted ----
# Telegram has no text alignment, so "centered" is done the only reliable way: the notice block is
# monospace (<code>, which is also tap-to-copy) and each line is left-padded to the block's width.
# The first line is the widest, so nothing is ever leading-space-trimmed. Hashtags/links stay
# outside <code> (they'd lose their link/hashtag behaviour inside it) and are centered
# approximately with em-spaces. Tweak DELETED_POST_* below if it looks off on your clients.
DELETED_POST_ICON = "\u26A0\uFE0F"                       # ⚠️  yellow warning sign
DELETED_POST_NOTICE = ("This content has been", "deleted by the author.")
_EM_SPACE = "\u2003"


def _mono_cells(text):
    """Approximate width of `text` in a monospace font (the warning sign is 2 cells wide)."""
    return len(text.replace(DELETED_POST_ICON, "##"))


def _center_mono(text, width):
    if not text:
        return text
    return " " * max(0, (width - _mono_cells(text)) // 2) + text


def _center_prop(visible_text, width_cells):
    """Leading em-spaces that roughly center proportional-font text under a monospace block."""
    pad_em = (width_cells * 0.6 - len(visible_text) * 0.52) / 2
    return _EM_SPACE * max(0, round(pad_em))


def build_deleted_channel_text(vent_display, hashtags):
    """HTML for the channel message of a deleted post: warning sign first, whole notice
    centered and tap-to-copy (single <code> block), then hashtags and footer links centered."""
    first = f"{DELETED_POST_ICON} {DELETED_POST_NOTICE[0]}"
    lines = [first, DELETED_POST_NOTICE[1], "", vent_display, "\u2501" * 15]
    width = max(_mono_cells(first), max(_mono_cells(l) for l in lines))
    block = "\n".join(_center_mono(l, width) if i else l for i, l in enumerate(lines))
    if _mono_cells(first) < width:
        block = _center_mono(first, width) + block[len(first):]
    tag_pad = _center_prop(hashtags, width)
    link_pad = _center_prop("Telegram | Bot", width)
    return (
        f"<code>{html.escape(block)}</code>\n\n"
        f"{tag_pad}{html.escape(hashtags)}\n"
        f"{link_pad}<a href='https://t.me/christianvent'>Telegram</a> | <a href='https://t.me/{BOT_USERNAME}'>Bot</a>"
    )


def get_admin_conversations(limit=20, offset=0, search=None):
    """List distinct conversation pairs, most recently active first."""
    where_extra = ""
    params = []
    if search:
        where_extra = ("WHERE ua.anonymous_name ILIKE %s ESCAPE '\\' OR ub.anonymous_name ILIKE %s ESCAPE '\\' "
                       "OR p.user_a = %s OR p.user_b = %s")
        like = f"%{_escape_like(search)}%"  # wildcards in the term are matched literally
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
        where_extra = ("WHERE ua.anonymous_name ILIKE %s ESCAPE '\\' OR ub.anonymous_name ILIKE %s ESCAPE '\\' "
                       "OR p.user_a = %s OR p.user_b = %s")
        like = f"%{_escape_like(search)}%"  # wildcards in the term are matched literally
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


def get_admin_conversation_transcript(user_a, user_b, limit=50, offset=0):
    """`limit` messages between two users, returned oldest-first. offset=0 is the newest
    window; offset=limit is the window before that, and so on (offset counts from newest)."""
    return db_fetch_all("""
        SELECT * FROM (
            SELECT pm.*, u.anonymous_name as sender_name
            FROM private_messages pm
            JOIN users u ON pm.sender_id = u.user_id
            WHERE (pm.sender_id = %s AND pm.receiver_id = %s)
               OR (pm.sender_id = %s AND pm.receiver_id = %s)
            ORDER BY pm.timestamp DESC, pm.message_id DESC
            LIMIT %s OFFSET %s
        ) sub
        ORDER BY timestamp ASC, message_id ASC
    """, (user_a, user_b, user_b, user_a, limit, offset))


def get_admin_conversation_message_count(user_a, user_b):
    """Total number of messages ever exchanged between this pair, so the admin
    monitor can tell whether there's older history left to load."""
    row = db_fetch_one("""
        SELECT COUNT(*) as cnt
        FROM private_messages
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
    """, (user_a, user_b, user_b, user_a))
    return row['cnt'] if row else 0


# -------------------- Unified conversational state (context.user_data only) --------------------
# Single source of truth for "what input is this user's next message going to?".
# This replaces the old waiting_for_post / waiting_for_comment / awaiting_name /
# waiting_for_private_message / awaiting_bio / awaiting_rejection_reason columns on
# `users`, which duplicated what context.user_data already tracked (e.g. comment_post_id)
# and could drift out of sync - a message handler that updated one and forgot the other
# left a user stuck in a flow they could no longer get out of. All per-user "temporary
# input mode" state now lives exclusively in context.user_data.
STATE_IDLE = 'idle'
STATE_AWAITING_POST = 'awaiting_post'
STATE_AWAITING_COMMENT = 'awaiting_comment'
STATE_AWAITING_PRIVATE_MESSAGE = 'awaiting_private_message'
STATE_AWAITING_NAME = 'awaiting_name'
STATE_AWAITING_BIO = 'awaiting_bio'
STATE_AWAITING_REJECTION_REASON = 'awaiting_rejection_reason'
STATE_REPORTING = 'reporting'
# state entered when a user is editing the content of an already-published
# (approved) post from the "edit_published_<post_id>" flow in view_post/button_handler.
STATE_AWAITING_EDIT_CONTENT = 'awaiting_edit_content'
# State entered when a user tapped "Edit" on a private message they sent
# (see the edit_sent_msg_<id> callback and the block in handle_message).
STATE_AWAITING_PM_EDIT = 'awaiting_pm_edit'


def get_state(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the user's current conversational state. Defaults to idle."""
    return context.user_data.get('state', STATE_IDLE)


def set_state(context: ContextTypes.DEFAULT_TYPE, state: str, **data):
    """Enter a new state, optionally stashing the data that state needs
    (e.g. set_state(context, STATE_AWAITING_COMMENT, comment_post_id=post_id)).
    Data keys land directly in context.user_data so existing lookups like
    context.user_data['comment_post_id'] keep working unchanged."""
    context.user_data['state'] = state
    context.user_data.update(data)


def reset_state(context: ContextTypes.DEFAULT_TYPE):
    """Return the user to idle and clear all state-scoped data. Call this whenever
    a flow finishes, errors out, or is cancelled - the single place to keep in sync
    instead of one DB UPDATE plus a hand-picked list of context keys."""
    context.user_data['state'] = STATE_IDLE
    for key in ('comment_post_id', 'comment_idx', 'reply_idx', 'nested_idx',
                'selected_category', 'selected_categories', 'private_message_target',
                'thread_context_post_id', 'thread_from_post_id', 'rejecting_post',
                'reporting', 'pending_explicit_check', 'pending_sex_check', 'editing_comment', 'editing_post',
                'pending_post', 'broadcasting', 'broadcast_step', 'broadcast_type',
                'editing_categories_for_pending', 'pending_comment_edit',
                'editing_published_post', 'editing_pm_id'):
        context.user_data.pop(key, None)


def clear_edit_published_state(context: ContextTypes.DEFAULT_TYPE):
    """Clear the state used while a user is editing the content of an already
    -published post (see the `edit_published_<post_id>` callback in button_handler
    and the STATE_AWAITING_EDIT_CONTENT handling in handle_message).

    This is the single place that knows how to unwind that flow, mirroring the
    role reset_state() plays for the rest of the bot's input flows.
    """
    context.user_data.pop('editing_published_post', None)
    if context.user_data.get('state') == STATE_AWAITING_EDIT_CONTENT:
        context.user_data['state'] = STATE_IDLE


async def reset_user_waiting_states(user_id: str, chat_id: int = None, context: ContextTypes.DEFAULT_TYPE = None):
    """Reset all waiting states for a user and optionally restore main menu.
    State now lives only in context.user_data (see reset_state above) - the old
    per-flow columns on `users` are no longer read or written by the bot handlers."""
    if context:
        reset_state(context)

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
    fixed_count = (await asyncio.to_thread(fix_orphaned_comments_for_post, post_id))
    
    # Recalculate total count
    new_count = (await asyncio.to_thread(count_all_comments, post_id))
    
    # Update DB column
    (await db_execute_async("UPDATE posts SET comment_count = %s WHERE post_id = %s", (new_count, post_id)))
    
    # Update channel button
    await update_channel_post_comment_count(context, post_id)
    
    return fixed_count

async def recount_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to fix orphans and update comment counts for all posts"""
    user_id = str(update.effective_user.id)
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to use this command.")
        return
        
    status_msg = await update.message.reply_text("Scanning all posts and fixing comment counts...")
    
    try:
        # Get all approved posts
        posts = (await db_fetch_all_async("SELECT post_id FROM posts WHERE approved = TRUE"))
        
        posts_scanned = len(posts)
        posts_fixed = 0
        orphans_adopted = 0
        
        for post in posts:
            post_id = post['post_id']
            
            # Adopt orphans for this post
            fixed = (await asyncio.to_thread(fix_orphaned_comments_for_post, post_id))
            if fixed > 0:
                orphans_adopted += fixed
                
            # Recalculate count
            actual_count = (await asyncio.to_thread(count_all_comments, post_id))
            
            # Get current DB count
            db_post = (await db_fetch_one_async("SELECT comment_count FROM posts WHERE post_id = %s", (post_id,)))
            current_db_count = db_post['comment_count'] if db_post else 0
            
            if actual_count != current_db_count or fixed > 0:
                # Update DB
                (await db_execute_async("UPDATE posts SET comment_count = %s WHERE post_id = %s", (actual_count, post_id)))
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
    ("Story Time", "StoryTime"),
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
            
            // Show a temporary loading state
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
    """Verify JWT token"""
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
        # Secure JWT (valid for 30 days), cached per user for 25 days so the menu isn't
        # re-signed on every single message. See _get_menu_jwt / _clear_menu_jwt_cache.
        token = _get_menu_jwt(user_id)
        
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
    # Weighted scoring (single query, shared with the batch/leaderboard/rank paths via
    # _SCORE_CTES_SQL so every screen agrees):
    #   Approved posts +10 | Comments +2 | Reactions per REACTION_WEIGHTS (on comments and
    #   posts) | Followers +2 | Blocks received -10
    row = db_fetch_one(
        f"WITH score_authors AS (SELECT %s::text AS author_id), {_SCORE_CTES_SQL} "
        "SELECT score FROM author_scores",
        (str(user_id),)
    )
    return int(row['score']) if row and row['score'] is not None else 0

def get_user_ratings_batch(user_ids):
    """Same scoring as calculate_user_rating(), for many users in ONE query (it used to be
    5 GROUP-BY queries). Returns {user_id: score} keyed by the ids passed in."""
    originals = {}
    for uid in user_ids:
        if uid is not None:
            originals[str(uid)] = uid
    if not originals:
        return {}
    ratings = {orig: 0 for orig in originals.values()}
    rows = db_fetch_all(
        f"WITH score_authors AS (SELECT UNNEST(%s::text[]) AS author_id), {_SCORE_CTES_SQL} "
        "SELECT author_id, score FROM author_scores",
        (list(originals.keys()),)
    )
    for row in rows or []:
        orig = originals.get(str(row['author_id']))
        if orig is not None:
            ratings[orig] = int(row['score'] or 0)
    return ratings

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



def _persist_weekly_badges(top_users, today):
    """All DB work for award_weekly_badges in one worker-thread call: clear old badges,
    then write history + current badges for every winner with one round trip each
    (execute_values) inside a single transaction. Returns [(user_id, name, points, badge)]."""
    winners = []
    rows = []
    for idx, user_data in enumerate(top_users[:len(WEEKLY_BADGES)]):
        rank = idx + 1
        badge = WEEKLY_BADGES[idx]
        winners.append((user_data['user_id'], user_data['weekly_points'], badge))
        rows.append((user_data['user_id'], today, rank, user_data['weekly_points'], badge))

    conn = None
    _acquire_db_slot()
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            # Clear previous badges first (same order of operations as before).
            cur.execute("UPDATE users SET weekly_badge = NULL")
            if rows:
                execute_values(cur, """
                    INSERT INTO weekly_rankings (user_id, week_start, rank, points_earned, badge_emoji)
                    VALUES %s
                    ON CONFLICT (user_id, week_start) DO UPDATE
                    SET rank = EXCLUDED.rank, points_earned = EXCLUDED.points_earned,
                        badge_emoji = EXCLUDED.badge_emoji
                """, rows)
                execute_values(cur, """
                    UPDATE users AS u SET weekly_badge = v.badge
                    FROM (VALUES %s) AS v(user_id, badge)
                    WHERE u.user_id = v.user_id
                """, [(w[0], w[2]) for w in winners])
                cur.execute(
                    "SELECT user_id, anonymous_name FROM users WHERE user_id = ANY(%s)",
                    ([w[0] for w in winners],)
                )
                names = {r['user_id']: r['anonymous_name'] for r in cur.fetchall()}
            else:
                names = {}
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            db_pool.putconn(conn)
        _db_slots.release()

    _invalidate_user_cache()   # weekly_badge changed for many users
    _leaderboard_cache_bust()  # leaderboard rows carry weekly_badge
    return [(uid, names.get(uid) or "Contributor", points, badge) for uid, points, badge in winners]


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

        top_users = await asyncio.to_thread(calculate_top_weekly_contributors)
        today = datetime.now(timezone.utc).date()

        # NOTE: previously the old badges were cleared BEFORE calculating winners; they are
        # now cleared together with the new ones in a single transaction, so a crash in
        # between can no longer leave the community with no badges at all.
        winners = await asyncio.to_thread(_persist_weekly_badges, top_users or [], today)
        if not winners:
            logger.info("No users earned points this week.")
            summary['success'] = True
            return summary

        winners_info = []
        for user_id, name, points, badge_emoji in winners:
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
    """Create an aura emoji from weighted contribution points (see calculate_user_rating).

    The weekly top-3 badges are separate: WEEKLY_BADGES = 🥇 🥈 🥉 (users.weekly_badge)."""
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

def normalize_revealed_sex(value):
    """Only 👨 / 👩 are ever shown next to a vent number. Anything else (None, '', the
    unset 👤 placeholder, junk) means 'show nothing'."""
    return value if value in ('👨', '👩') else None

def vent_header_html(vent_display: str, revealed_sex=None) -> str:
    """HTML for the top line of a channel post: the copyable vent number, with the
    author's chosen sex emoji on its own line right under it (outside <code>, so it
    isn't swept up when someone copies the number). No emoji -> identical to the old output."""
    header = f"<code>{vent_display}</code>"
    sex = normalize_revealed_sex(revealed_sex)
    if sex:
        header += f"\n{sex}"
    return header

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
    """1-based leaderboard rank of a (non-admin) user, or None. One query: the shared score
    CTE plus RANK() over it - no more loading every user into Python to find one position."""
    row = db_fetch_one(f"""
        WITH score_authors AS (
            SELECT user_id AS author_id FROM users WHERE is_admin = FALSE
        ),
        {_SCORE_CTES_SQL},
        ranked AS (
            SELECT author_id, RANK() OVER (ORDER BY score DESC) AS rnk FROM author_scores
        )
        SELECT rnk FROM ranked WHERE author_id = %s
    """, (str(user_id),))
    return int(row['rnk']) if row else None

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
        post = (await db_fetch_one_async("SELECT channel_message_id, comment_count, explicit FROM posts WHERE post_id = %s", (post_id,)))
        if not post or not post['channel_message_id']:
            return
        
        # Count all comments for this post
        total_comments = (await asyncio.to_thread(count_all_comments, post_id))
        
        # Update the database with the new count
        (await db_execute_async("UPDATE posts SET comment_count = %s WHERE post_id = %s", (total_comments, post_id)))
        
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
    
    # Top 10 users with weighted aura: one shared CTE query, cached for ~60s
    # (see get_leaderboard_rows / _leaderboard_cache_bust), run off the event loop.
    top_users = await asyncio.to_thread(get_leaderboard_rows, 10)

    
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
    user_rank = await asyncio.to_thread(get_user_rank, user_id)
    
    if user_rank:
        user_data = await db_fetch_one_async("SELECT anonymous_name, sex, is_admin FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = await asyncio.to_thread(calculate_user_rating, user_id)
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
        user = (await db_fetch_one_async("SELECT notifications_enabled, privacy_public, is_admin FROM users WHERE user_id = %s", (user_id,)))
        
        if not user:
            if update.message:
                await update.message.reply_text("Please use /start first to initialize your profile.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("Please use /start first to initialize your profile.")
            return
        
        notifications_status = "ON" if user['notifications_enabled'] else "OFF"
        privacy_status = "Public" if user['privacy_public'] else "Private"

        pending_requests_row = (await db_fetch_one_async(
            "SELECT COUNT(*) as cnt FROM chat_requests WHERE receiver_id = %s AND status = 'pending'",
            (user_id,)
        ))
        pending_requests = pending_requests_row['cnt'] if pending_requests_row else 0
        requests_label = f"Chat Requests ({pending_requests})" if pending_requests else "Chat Requests"
        
        keyboard = [
            [
                InlineKeyboardButton(f"Notifications: {notifications_status}", callback_data='toggle_notifications'),
                InlineKeyboardButton(f"Privacy: {privacy_status}", callback_data='toggle_privacy')
            ],
            [
                InlineKeyboardButton("Privacy Controls", callback_data='privacy_settings'),
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
    
    user = (await db_fetch_one_async("""
        SELECT hide_aura, hide_bio, hide_follower_count, hide_role 
        FROM users WHERE user_id = %s
    """, (user_id,)))
    
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

async def send_post_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, post_content: str, category: str, media_type: str = 'text', media_id: str = None, thread_from_post_id: int = None, explicit: bool = False, revealed_sex: str = None):
    revealed_sex = normalize_revealed_sex(revealed_sex)
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
        thread_post = (await db_fetch_one_async("SELECT content, channel_message_id FROM posts WHERE post_id = %s", (thread_from_post_id,)))
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
    if revealed_sex:
        explicit_tag += "*Your sex will be shown under the vent number*\n\n"
    
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
        'revealed_sex': revealed_sex,
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
async def send_telegram_media_async(context: ContextTypes.DEFAULT_TYPE, chat_id, media_type, media_id, caption=None, parse_mode=None, reply_markup=None):
    """
    Send an actual media message (photo/voice/audio/video/document/gif/sticker) via the
    bot, using a file_id already stored on Telegram. Falls back to a plain text message
    (using `caption` as the text) if the media type is missing/unsupported, or if the
    media send itself fails. Returns the sent Message object, or None.
    """
    if not media_id or not media_type or media_type == 'text':
        if caption:
            return await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=parse_mode, reply_markup=reply_markup)
        return None

    # Telegram's caption hard limit
    safe_caption = caption[:1024] if caption else None

    try:
        if media_type == 'photo':
            return await context.bot.send_photo(chat_id=chat_id, photo=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'voice':
            return await context.bot.send_voice(chat_id=chat_id, voice=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'audio':
            return await context.bot.send_audio(chat_id=chat_id, audio=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'video':
            return await context.bot.send_video(chat_id=chat_id, video=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'document':
            return await context.bot.send_document(chat_id=chat_id, document=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'gif':
            return await context.bot.send_animation(chat_id=chat_id, animation=media_id, caption=safe_caption, parse_mode=parse_mode, reply_markup=reply_markup)
        elif media_type == 'sticker':
            # sendSticker doesn't accept a caption at all — send the sticker, then
            # follow up with the notification text (and keyboard) as its own message.
            msg = await context.bot.send_sticker(chat_id=chat_id, sticker=media_id)
            if caption:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return msg
        else:
            if caption:
                return await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=parse_mode, reply_markup=reply_markup)
            return None
    except Exception as e:
        logger.error(f"send_telegram_media_async failed ({media_type}): {e}")
        # Media send failed entirely — still let the person know something arrived
        if caption:
            return await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=parse_mode, reply_markup=reply_markup)
        return None


async def notify_vent_author_of_comment(context: ContextTypes.DEFAULT_TYPE, post_id: int, commenter_id: str, comment_id: int = None, comment_content: str = None, comment_type: str = 'text', media_id: str = None):
    """Notify the post author when a new top‑level comment is added."""
    try:
        post = (await db_fetch_one_async("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,)))
        if not post:
            return
        
        author_id = post['author_id']
        if author_id == commenter_id:
            return
        
        author = (await db_fetch_one_async("SELECT user_id, notifications_enabled FROM users WHERE user_id = %s", (author_id,)))
        if not author or not author['notifications_enabled']:
            return
        
        commenter = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (commenter_id,)))
        commenter_name = get_display_name(commenter)
        
        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
        
        # Use HTML parsing – no need to escape markdown special characters
        import html
        safe_commenter_name = html.escape(commenter_name)
        safe_post_preview = html.escape(post_preview)

        # Show the actual comment text so it's visible in the notification itself
        media_labels = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🏷 Sticker', 'photo': '🖼 Photo'}
        if comment_content:
            safe_comment_text = html.escape(truncate_for_telegram(comment_content, COMMENT_TEXT_CONTENT_LIMIT))
        else:
            safe_comment_text = media_labels.get(comment_type, '')

        comment_block = f"<blockquote>{safe_comment_text}</blockquote>\n" if safe_comment_text else ""

        notification_text = (
            f"💬 <b>New comment on your vent</b>\n\n"
            f"<b>{safe_commenter_name}</b> wrote:\n"
            f"{comment_block}"
            f"<i>Your vent: {safe_post_preview}</i>\n\n"
            f"<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>"
        )

        reply_markup = None
        if comment_id:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Reply", callback_data=f"reply_{post_id}_{comment_id}")]
            ])

        # If the comment has media, send the actual file with the notification text as
        # its caption; falls back to a text-only message if there's no media or the send fails.
        if media_id and comment_type and comment_type != 'text':
            await send_telegram_media_async(
                context, author_id, comment_type, media_id,
                caption=notification_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
            return

        await context.bot.send_message(
            chat_id=author_id,
            text=notification_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error notifying vent author: {e}")
async def notify_user_of_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, comment_id: int, replier_id: str, new_comment_id: int = None, comment_content: str = None, comment_type: str = 'text', media_id: str = None):
    try:
        comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (comment_id,)))
        if not comment:
            return
        
        original_author = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (comment['author_id'],)))
        if not original_author or not original_author['notifications_enabled']:
            return
        
        post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
        if not post:
            return
            
        # === FIX: Vent author anonymization in reply notification ===
        if str(replier_id) == str(post['author_id']):
            replier_display = "Vent author"
            safe_replier_name = replier_display
        else:
            replier = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (replier_id,)))
            replier_name = get_display_name(replier)
            safe_replier_name = escape_markdown(replier_name, version=2)
        
        post_preview = post['content'][:50] + '...' if len(post['content']) > 50 else post['content']
        
        safe_post_preview = escape_markdown(post_preview, version=2)
        safe_parent_preview = escape_markdown(comment['content'][:100], version=2)

        # Show the actual reply text, not just the comment it replied to
        media_labels = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🏷 Sticker', 'photo': '🖼 Photo'}
        if comment_content:
            safe_reply_text = escape_markdown(truncate_for_telegram(comment_content, COMMENT_TEXT_CONTENT_LIMIT), version=2)
        else:
            safe_reply_text = escape_markdown(media_labels.get(comment_type, ''), version=2)

        reply_block = f">{safe_reply_text}\n\n" if safe_reply_text else ""

        notification_text = (
            f"{safe_replier_name} replied to your comment\\:\n\n"
            f"{reply_block}"
            f"_Replying to:_ {safe_parent_preview}\n\n"
            f"Post\\: {safe_post_preview}\n\n"
            f"[View conversation](https://t.me/{BOT_USERNAME}?start=comments_{post_id})"
        )

        reply_markup = None
        if new_comment_id:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩ Reply", callback_data=f"replytoreply_{post_id}_{comment_id}_{new_comment_id}")]
            ])

        # If the reply has media, send the actual file with the notification text as its
        # caption; falls back to a text-only message if there's no media or the send fails.
        if media_id and comment_type and comment_type != 'text':
            await send_telegram_media_async(
                context, original_author['user_id'], comment_type, media_id,
                caption=notification_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup
            )
            return

        await context.bot.send_message(
            chat_id=original_author['user_id'],
            text=notification_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error sending reply notification: {e}")

async def notify_post_author_of_thread_reply(context: ContextTypes.DEFAULT_TYPE, post_id: int, parent_comment_id: int, replier_id: str, comment_content: str = None, comment_type: str = 'text', media_id: str = None):
    """Tell the vent author about replies other people leave under comments on their vent."""
    try:
        import html
        post = await db_fetch_one_async("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        author_id = str(post['author_id'])
        if author_id == str(replier_id):
            return  # the author wrote this reply themselves

        parent = await db_fetch_one_async("SELECT author_id, content FROM comments WHERE comment_id = %s", (parent_comment_id,))
        if not parent:
            return
        if str(parent['author_id']) == author_id:
            return  # already notified by notify_user_of_reply

        author = await db_fetch_one_async("SELECT user_id, notifications_enabled FROM users WHERE user_id = %s", (author_id,))
        if not author or not author['notifications_enabled']:
            return

        replier = await db_fetch_one_async("SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (replier_id,))
        replier_name = get_display_name(replier)
        parent_author = await db_fetch_one_async("SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (parent['author_id'],))
        parent_name = get_display_name(parent_author)

        post_preview = (post['content'][:60] + '...') if post['content'] and len(post['content']) > 60 else (post['content'] or "")
        media_labels = {'voice': '[Voice message]', 'gif': '[GIF]', 'sticker': '[Sticker]', 'photo': '[Photo]'}
        body = truncate_for_telegram(comment_content, COMMENT_TEXT_CONTENT_LIMIT) if comment_content else media_labels.get(comment_type, '')

        lines = [f"<b>{html.escape(replier_name)}</b> replied to {html.escape(parent_name)} on your vent:", ""]
        if body:
            lines.append(f"<blockquote>{html.escape(body)}</blockquote>")
        lines.append(f"They were replying to: {html.escape((parent['content'] or '[media]')[:100])}")
        lines.append(f"Your vent: {html.escape(post_preview)}")
        lines.append(f"\n<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>")
        text = "\n".join(lines)

        if media_id and comment_type and comment_type != 'text':
            await send_telegram_media_async(context, author_id, comment_type, media_id, caption=text, parse_mode=ParseMode.HTML)
            return
        await context.bot.send_message(chat_id=author_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error notifying vent author of thread reply: {e}")

async def notify_admin_of_new_post(context: ContextTypes.DEFAULT_TYPE, post_id: int):
    if not ADMIN_ID:
        return
    
    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
    if not post:
        return
    
    author = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (post['author_id'],)))
    author_name = get_display_name(author)
    
    media_type = post.get('media_type') or 'text'
    media_id = post.get('media_id')
    content_text = post['content'] or ''
    
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
        ],
        [InlineKeyboardButton("🛡 Moderate author", callback_data=f"mod_post_{post_id}")],
    ])
    
    explicit_line = "Marked as explicit\n\n" if post.get('explicit') else ""
    media_label = {'voice': '[Voice message — no caption]', 'audio': '[Audio — no caption]', 'photo': '[Photo — no caption]'}.get(media_type, '')

    try:
        if media_id and media_type != 'text':
            # Media notifications: send the actual file so the admin can review it,
            # with a caption capped to Telegram's 1024-char caption limit.
            caption_body = content_text[:900] + ('...' if len(content_text) > 900 else '') if content_text else media_label
            caption = f"New post awaiting approval from {author_name}:\n\n{explicit_line}{caption_body}"[:1024]
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=media_id, caption=caption, reply_markup=keyboard)
            elif media_type == 'voice':
                await context.bot.send_voice(chat_id=ADMIN_ID, voice=media_id, caption=caption, reply_markup=keyboard)
            elif media_type == 'audio':
                await context.bot.send_audio(chat_id=ADMIN_ID, audio=media_id, caption=caption, reply_markup=keyboard)
            else:
                post_preview = content_text[:4000] + ('...' if len(content_text) > 4000 else '')
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"New post awaiting approval from {author_name}:\n\n{explicit_line}{post_preview}",
                    reply_markup=keyboard
                )
        else:
            post_preview = content_text[:4000] + ('...' if len(content_text) > 4000 else '')
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
        is_blocked = (await db_fetch_one_async(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (receiver_id, sender_id)
        ))
        if is_blocked:
            return

        receiver = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (receiver_id,)))
        if not receiver or not receiver['notifications_enabled']:
            return

        sender = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (sender_id,)))
        sender_name = get_display_name(sender)
        safe_sender_name = escape_markdown(sender_name, version=2)

        media_type, media_id = 'text', None
        if message_id:
            media_row = (await db_fetch_one_async(
                "SELECT media_type, media_id FROM private_messages WHERE message_id = %s",
                (message_id,)
            ))
            if media_row:
                media_type = media_row.get('media_type') or 'text'
                media_id = media_row.get('media_id')

        full_content = truncate_for_telegram(message_content or "", PM_TEXT_CONTENT_LIMIT)
        safe_preview_content = escape_markdown(full_content, version=2) if full_content else ""

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Reply", callback_data=f"reply_msg_{sender_id}"),
                InlineKeyboardButton("Block", callback_data=f"block_user_{sender_id}")
            ]
        ])

        header_lines = ["*New Private Message*", "", "From: " + safe_sender_name, ""]
        header = "\n".join(header_lines)

        sent_msg = None

        if media_id and media_type != 'text':
            caption_content = truncate_for_telegram(message_content or "", PM_CAPTION_CONTENT_LIMIT)
            safe_caption_content = escape_markdown(caption_content, version=2) if caption_content else ""
            caption_lines = [header, safe_caption_content, "", "_Use /inbox to view all messages_"]
            caption = "\n".join(caption_lines)
            if len(caption) > 1024:
                caption = truncate_for_telegram(caption, 1024)
            try:
                if media_type == 'photo':
                    sent_msg = await context.bot.send_photo(chat_id=receiver_id, photo=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'voice':
                    sent_msg = await context.bot.send_voice(chat_id=receiver_id, voice=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'audio':
                    sent_msg = await context.bot.send_audio(chat_id=receiver_id, audio=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'video':
                    sent_msg = await context.bot.send_video(chat_id=receiver_id, video=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'document':
                    sent_msg = await context.bot.send_document(chat_id=receiver_id, document=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                elif media_type == 'gif':
                    sent_msg = await context.bot.send_animation(chat_id=receiver_id, animation=media_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard)
                else:
                    raise ValueError("Unhandled media_type: " + str(media_type))
            except Exception as media_err:
                logger.error("Failed to deliver media private message, falling back to text notice: " + str(media_err))

        if not sent_msg:
            fallback_body = safe_preview_content if safe_preview_content else "_\\\\[attachment\\\\]_"
            notification_lines = [header, fallback_body, "", "_Use /inbox to view all messages_"]
            notification_text = "\n".join(notification_lines)
            sent_msg = await context.bot.send_message(
                chat_id=receiver_id,
                text=notification_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard
            )

        # Remember the live notification's message_id so a later edit/delete of
        # this private message can be applied natively to the real Telegram message.
        if sent_msg and message_id:
            (await db_execute_async(
                "UPDATE private_messages SET notif_message_id = %s WHERE message_id = %s",
                (sent_msg.message_id, message_id)
            ))
    except Exception as e:
        logger.error("Error sending private message notification: " + str(e))


async def edit_native_pm_notification(context: ContextTypes.DEFAULT_TYPE, receiver_id: str, notif_message_id: int,
                                       sender_id: str, new_content: str, media_type: str = 'text', media_id: str = None):
    """Apply an edit to the live notification message already delivered to the
    receiver, using Telegram's native edit-message call — so the receiver sees
    the change in place (with Telegram's own "edited" tag) instead of only our
    DB copy changing underneath them."""
    try:
        sender = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (sender_id,)))
        sender_name = get_display_name(sender) if sender else "Someone"
        safe_sender_name = escape_markdown(sender_name, version=2)

        header = f"*New Private Message*\n\nFrom: {safe_sender_name}\n"
        footer = "\n_Use /inbox to view all messages_"

        if media_id and media_type and media_type != 'text':
            caption_preview = truncate_for_telegram(new_content or "", PM_CAPTION_CONTENT_LIMIT)
            safe_caption_preview = escape_markdown(caption_preview, version=2) if caption_preview else ""
            caption = f"{header}\n{safe_caption_preview}{footer}"
            if len(caption) > 1024:
                caption = truncate_for_telegram(caption, 1024)
            await context.bot.edit_message_caption(
                chat_id=receiver_id,
                message_id=notif_message_id,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            text_preview = truncate_for_telegram(new_content or "", PM_TEXT_CONTENT_LIMIT)
            safe_preview = escape_markdown(text_preview, version=2) if text_preview else ""
            body = safe_preview if safe_preview else "_\\[attachment\\]_"
            text = f"{header}\n{body}{footer}"
            await context.bot.edit_message_text(
                chat_id=receiver_id,
                message_id=notif_message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        # Not fatal — e.g. the receiver already deleted the notification, blocked
        # the bot, or Telegram's edit rules rejected it. The DB copy is still updated.
        logger.warning(f"edit_native_pm_notification failed (receiver={receiver_id}, msg={notif_message_id}): {e}")


async def delete_native_pm_notification(context: ContextTypes.DEFAULT_TYPE, chat_id: str, notif_message_id: int):
    """Natively delete a previously-delivered notification message via the Bot
    API, so a deleted private message leaves no placeholder behind."""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=notif_message_id)
    except Exception as e:
        logger.warning(f"delete_native_pm_notification failed (chat={chat_id}, msg={notif_message_id}): {e}")




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
    
    top_users = (await asyncio.to_thread(calculate_top_weekly_contributors))
    if not top_users:
        await query.message.reply_text("No users earned points in the last 7 days.")
        return

    winners_info = []
    badges = WEEKLY_BADGES
    for idx, user_data in enumerate(top_users):
        u = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (user_data['user_id'],)))
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
            f"• Winners announced: {'\u2705' if summary['announcement_sent'] else '\u274C'}\n"
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
    
    last_date, winners = (await asyncio.to_thread(get_last_week_winners))
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
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
        time=dt_time(0, 0, tzinfo=timezone.utc),
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']: return
    
    top_users = (await asyncio.to_thread(calculate_top_weekly_contributors))
    if not top_users:
        await update.message.reply_text("No users earned points in the last 7 days.")
        return
    winners_info = []
    badges = WEEKLY_BADGES
    for idx, user_data in enumerate(top_users):
        u = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (user_data['user_id'],)))
        name = u['anonymous_name'] if u else "Anonymous"
        winners_info.append(f"{badges[idx]} {name} – {user_data['weekly_points']} pts")
    text = "*Weekly Points (Last 7 days)*\n\n" + "\n".join(winners_info) + "\n\n_Admin only – no announcement sent._"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def force_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
        return
    
    # Get statistics for display
    pending_posts = (await db_fetch_one_async("SELECT COUNT(*) as count FROM posts WHERE approved = FALSE"))
    pending_count = pending_posts['count'] if pending_posts else 0
    
    total_users = (await db_fetch_one_async("SELECT COUNT(*) as count FROM users"))
    users_count = total_users['count'] if total_users else 0
    
    active_today = (await db_fetch_one_async('''
        SELECT COUNT(DISTINCT user_id) as count 
        FROM (
            SELECT author_id as user_id FROM posts WHERE DATE(timestamp) = CURRENT_DATE
            UNION 
            SELECT author_id as user_id FROM comments WHERE DATE(timestamp) = CURRENT_DATE
        ) AS active_users
    '''))
    active_count = active_today['count'] if active_today else 0
    
    keyboard = [
        [InlineKeyboardButton(f"Pending Posts ({pending_count})", callback_data='admin_pending')],
        [InlineKeyboardButton(f"Users: {users_count}", callback_data='admin_users')],
        [InlineKeyboardButton("Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("Send Broadcast", callback_data='admin_broadcast')],
        [InlineKeyboardButton("Weekly Tools", callback_data='admin_weekly_tools')],
        [InlineKeyboardButton("Pending Reports", callback_data='admin_reports')],
        [InlineKeyboardButton("🛡 Moderation", callback_data='mod_menu')],
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
    # No query.answer() here - the message edit below already dismisses the loading spinner
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
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
    # Same as above - the edit_message_text below handles dismissing the spinner
    
    user_id = str(query.from_user.id)
    
    # Verify admin permissions
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        if is_callback:
            await update.callback_query.answer("You don't have permission to access this.", show_alert=True)
        else:
            await update.message.reply_text("You don't have permission to access this.")
        return
    
    # Get user count for confirmation
    total_users = (await db_fetch_one_async("SELECT COUNT(*) as count FROM users"))
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
    all_users = (await db_fetch_all_async("SELECT user_id FROM users WHERE user_id != %s", (user_id,)))
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to access this.", show_alert=True)
        return
    
    # Get user statistics for targeting
    total_users = (await db_fetch_one_async("SELECT COUNT(*) as count FROM users"))
    active_users = (await db_fetch_one_async('''
        SELECT COUNT(DISTINCT user_id) as count 
        FROM (
            SELECT author_id as user_id FROM posts WHERE DATE(timestamp) >= CURRENT_DATE - INTERVAL '7 days'
            UNION 
            SELECT author_id as user_id FROM comments WHERE DATE(timestamp) >= CURRENT_DATE - INTERVAL '7 days'
        ) AS active_users
    '''))
    
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
ADMIN_PENDING_PAGE_SIZE = 5

async def show_pending_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    user_id = str(update.effective_user.id)
    
    # Verify admin permissions
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
        return

    if page < 1:
        page = 1
    per_page = ADMIN_PENDING_PAGE_SIZE

    total_row = (await db_fetch_one_async("SELECT COUNT(*) as cnt FROM posts WHERE approved = FALSE"))
    total = total_row['cnt'] if total_row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    # Get pending posts for this page (simplified - no JOIN with pending_notifications)
    posts = (await db_fetch_all_async("""
        SELECT p.post_id, p.content, u.anonymous_name, p.media_type, p.media_id, p.explicit,
               STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        JOIN users u ON p.author_id = u.user_id
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.approved = FALSE
        GROUP BY p.post_id, u.anonymous_name, p.media_type, p.media_id, p.content, p.timestamp, p.explicit
        ORDER BY p.timestamp DESC
        LIMIT %s OFFSET %s
    """, (per_page, offset)))
    
    if not posts:
        if update.callback_query:
            await update.callback_query.message.reply_text("No pending posts!")
        else:
            await update.message.reply_text("No pending posts!")
        return
    
    # Which posts the admin has already checked off for bulk delete, so the
    # "Select" button re-renders in the right state across pages/reopens.
    bulk_selected = context.user_data.setdefault('bulk_delete_ids', set())

    # Send each pending post to admin (one page's worth at a time)
    for post in posts:
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
            ],
            [
                InlineKeyboardButton(
                    "☑ Selected" if post['post_id'] in bulk_selected else "☐ Select for bulk delete",
                    callback_data=f"toggle_bulkdel_{post['post_id']}"
                )
            ],
            [InlineKeyboardButton("🛡 Moderate author", callback_data=f"mod_post_{post['post_id']}")],
        ])
        
        content_text = post['content'] or ''
        # Captions are capped harder than plain messages (Telegram limits captions to 1024
        # chars) - use a shorter preview for media posts so the caption never silently fails.
        max_preview = 2000 if post['media_type'] == 'text' else 800
        preview = content_text[:max_preview] + ('...' if len(content_text) > max_preview else '')
        if not preview and post['media_type'] != 'text':
            preview = f"[{post['media_type']} — no caption]"
        safe_preview = html.escape(preview)
        safe_name = html.escape(post['anonymous_name'] or "Anonymous")
        safe_cats = html.escape(post['categories'] or 'Other')
        explicit_line = "<b>Marked as explicit</b>\n\n" if post.get('explicit') else ""
        
        text = f"<b>Pending Post</b> [{safe_cats}]\n\n{explicit_line}{safe_preview}\n\n<b>{safe_name}</b>"
        if post['media_type'] != 'text':
            text = text[:1024]
        
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

    # Pagination footer so admins can reach posts beyond the first page
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"admin_pending_page_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"admin_pending_page_{page+1}"))

    # Bulk-delete row: either the posts checked off across any page, or every
    # pending post regardless of page/selection. Both go through a confirm step.
    bulk_row = [
        InlineKeyboardButton(f"🗑 Delete Selected ({len(bulk_selected)})", callback_data="bulkdel_sel_confirm"),
        InlineKeyboardButton(f"🗑 Delete ALL ({total})", callback_data="bulkdel_all_confirm")
    ]
    nav_markup = InlineKeyboardMarkup([nav_row, bulk_row, [InlineKeyboardButton("Admin Panel", callback_data='admin_panel')]])

    footer_text = f"Showing {len(posts)} of {total} pending post(s) — page {page}/{total_pages}"
    if update.callback_query:
        await update.callback_query.message.reply_text(footer_text, reply_markup=nav_markup)
    else:
        await update.message.reply_text(footer_text, reply_markup=nav_markup)


async def toggle_bulk_delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Check/uncheck a single pending post for the next bulk-delete run. Selection
    is kept in user_data so it survives paging through the pending-posts list."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to do this.", show_alert=True)
        return

    selected = context.user_data.setdefault('bulk_delete_ids', set())
    if post_id in selected:
        selected.discard(post_id)
        now_selected = False
    else:
        selected.add(post_id)
        now_selected = True

    # Refresh just the "Select" button on this post's own message.
    try:
        new_rows = list(query.message.reply_markup.inline_keyboard)
        for row in new_rows:
            for i, btn in enumerate(row):
                if btn.callback_data == f"toggle_bulkdel_{post_id}":
                    row[i] = InlineKeyboardButton(
                        "☑ Selected" if now_selected else "☐ Select for bulk delete",
                        callback_data=f"toggle_bulkdel_{post_id}"
                    )
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
    except Exception as e:
        logger.error(f"Error updating bulk-delete toggle for post {post_id}: {e}")

    await query.answer("Selected for deletion" if now_selected else "Removed from selection")


async def confirm_bulk_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Show a Yes/Cancel confirmation before an irreversible bulk delete.
    mode is 'selected' (only checked-off posts) or 'all' (every pending post)."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to do this.", show_alert=True)
        return

    if mode == 'selected':
        count = len(context.user_data.get('bulk_delete_ids', set()))
        if count == 0:
            await query.answer("No posts selected.", show_alert=True)
            return
        text = f"⚠️ Delete {count} selected pending post(s)?\n\nThis cannot be undone."
        yes_callback = "bulkdel_sel_execute"
    else:
        total_row = (await db_fetch_one_async("SELECT COUNT(*) as cnt FROM posts WHERE approved = FALSE"))
        count = total_row['cnt'] if total_row else 0
        if count == 0:
            await query.answer("No pending posts to delete.", show_alert=True)
            return
        text = f"⚠️ Delete ALL {count} pending post(s)?\n\nThis cannot be undone."
        yes_callback = "bulkdel_all_execute"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=yes_callback),
         InlineKeyboardButton("Cancel", callback_data="bulkdel_cancel")]
    ])
    await query.edit_message_text(text, reply_markup=kb)


async def execute_bulk_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Actually delete the posts after confirmation. Authors are not individually
    notified here (unlike single Reject) to avoid a burst of messages when many
    posts go at once — only the admin sees the result."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to do this.", show_alert=True)
        return

    try:
        if mode == 'selected':
            ids = list(context.user_data.get('bulk_delete_ids', set()))
            if not ids:
                await query.answer("No posts selected.", show_alert=True)
                return
            rows = (await db_execute_async(
                "DELETE FROM posts WHERE post_id = ANY(%s) AND approved = FALSE RETURNING post_id",
                (ids,), fetch=True
            )) or []
        else:
            rows = (await db_execute_async(
                "DELETE FROM posts WHERE approved = FALSE RETURNING post_id",
                (), fetch=True
            )) or []

        deleted_count = len(rows)
        context.user_data['bulk_delete_ids'] = set()
        logger.info(f"Admin {user_id} bulk-deleted {deleted_count} pending post(s) (mode={mode})")

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back to Pending Posts", callback_data="admin_pending")]])
        await query.edit_message_text(f"✅ Deleted {deleted_count} pending post(s).", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error in execute_bulk_delete (mode={mode}): {e}")
        await query.edit_message_text("Error deleting posts. Please try again.")


async def toggle_post_explicit(update: Update, context: ContextTypes.DEFAULT_TYPE, post_id: int):
    """Admin flags or unflags a post as explicit — works for posts still pending
    review as well as posts already published to the channel (in which case the
    live channel message content and keyboard are updated too)."""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        await query.answer("You don't have permission to do this.", show_alert=True)
        return

    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
    if not post:
        await query.answer("Post not found.", show_alert=True)
        return

    new_explicit = not post.get('explicit')
    (await db_execute_async("UPDATE posts SET explicit = %s WHERE post_id = %s", (new_explicit, post_id)))

    # If already live in the channel, update the channel message content + keyboard too
    if post.get('approved') and post.get('channel_message_id'):
        try:
            cats_row = (await db_fetch_all_async("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,)))
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
                f"{vent_header_html(vent_display, post.get('revealed_sex'))}\n\n"
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        try:
            await query.answer("You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("You don't have permission to do this.")
        return
    
    # Get the post
    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
    if not post:
        try:
            await query.answer("Post not found.", show_alert=True)
        except:
            await query.edit_message_text("Post not found.")
        return
    
    try:
        # Get the next vent number FIRST
        max_vent = (await db_fetch_one_async("SELECT MAX(vent_number) as max_num FROM posts WHERE approved = TRUE"))
        next_vent_number = (max_vent['max_num'] or 0) + 1
        
        # Get categories for this post
        cats_row = (await db_fetch_all_async("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,)))
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
            original_post = (await db_fetch_one_async(
                "SELECT channel_message_id FROM posts WHERE post_id = %s", 
                (post['thread_from_post_id'],)
            ))
            if original_post and original_post['channel_message_id']:
                reply_to_message_id = original_post['channel_message_id']
        
        # Send post to channel based on media type
        if post.get('explicit'):
            body_html = EXPLICIT_WARNING_HTML
        else:
            body_html = html.escape(post['content'])
        safe_hashtags = html.escape(hashtags)
        channel_text = (
            f"{vent_header_html(vent_display, post.get('revealed_sex'))}\n\n"
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
        success = (await db_execute_async(
            "UPDATE posts SET approved = TRUE, admin_approved_by = %s, channel_message_id = %s, vent_number = %s WHERE post_id = %s",
            (user_id, msg.message_id, next_vent_number, post_id)
        ))
        
        # Clear Aura Cache for real-time accuracy
        calculate_user_rating.cache_clear()
        _leaderboard_cache_bust()
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
        # Update the admin's original message to remove the Approve/Reject buttons
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
    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
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

        # Rejected posts are deleted outright rather than archived; if a status-based
        # soft-rejection flow is ever needed, add a `status` column instead of reusing this.
        success = (await db_execute_async("DELETE FROM posts WHERE post_id = %s", (post_id,)))
        
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
            
        # Return to admin panel right away (used to wait 1s)
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        try:
            await query.answer("You don't have permission to do this.", show_alert=True)
        except:
            await query.edit_message_text("You don't have permission to do this.")
        return
    
    # Get the post
    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
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
    user = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
    if not user:
        anon = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        success = (await db_execute_async(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        ))
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
                set_state(context, STATE_AWAITING_COMMENT, comment_post_id=post_id, comment_idx=None)

                post = await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,))
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

                user_data = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (target_user_id,)))
                if not user_data:
                    await update.message.reply_text("User not found.")
                    return

                followers = (await db_fetch_all_async("SELECT * FROM followers WHERE followed_id = %s", (user_data['user_id'],)))
                rating = (await asyncio.to_thread(calculate_user_rating, user_data['user_id']))
                current_user_id = user_id

                # Determine if this is a vent author context (viewing from a post)
                is_vent_author = False
                if post_id:
                    post_info = (await db_fetch_one_async("SELECT author_id FROM posts WHERE post_id = %s", (post_id,)))
                    if post_info and str(post_info['author_id']) == str(target_user_id) and str(target_user_id) != str(current_user_id):
                        is_vent_author = True

                # Build buttons
                btn = []
                if user_data['user_id'] != current_user_id:
                    # Check chat request status
                    accepted_request = (await db_fetch_one_async(
                        "SELECT status FROM chat_requests WHERE "
                        "((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)) AND status = 'accepted'",
                        (current_user_id, user_data['user_id'], user_data['user_id'], current_user_id)
                    ))

                    chat_btn_text = "Chat" if accepted_request else "Request to Chat"
                    chat_btn_callback = f'message_{user_data["user_id"]}' if accepted_request else f'chatrequest_{user_data["user_id"]}'

                    # For vent authors, only show chat and block/unblock (no follow/unfollow)
                    if is_vent_author:
                        btn.append([InlineKeyboardButton(chat_btn_text, callback_data=chat_btn_callback)])
                        # Check block status
                        is_blocked = (await db_fetch_one_async("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (current_user_id, user_data['user_id'])))
                        if is_blocked:
                            btn.append([InlineKeyboardButton("Unblock User", callback_data=f'unblock_user_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Block User", callback_data=f'block_user_{user_data["user_id"]}')])
                    else:
                        # Normal profile: show follow/unfollow, chat, block
                        is_following = (await db_fetch_one_async(
                            "SELECT * FROM followers WHERE follower_id = %s AND followed_id = %s",
                            (current_user_id, user_data['user_id'])
                        ))
                        if is_following:
                            btn.append([InlineKeyboardButton("Unfollow", callback_data=f'unfollow_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Follow", callback_data=f'follow_{user_data["user_id"]}')])

                        btn.append([InlineKeyboardButton(chat_btn_text, callback_data=chat_btn_callback)])

                        is_blocked = (await db_fetch_one_async("SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (current_user_id, user_data['user_id'])))
                        if is_blocked:
                            btn.append([InlineKeyboardButton("Unblock User", callback_data=f'unblock_user_{user_data["user_id"]}')])
                        else:
                            btn.append([InlineKeyboardButton("Block User", callback_data=f'block_user_{user_data["user_id"]}')])

                    btn.append([InlineKeyboardButton("Report User", callback_data=f'report_user_{user_data["user_id"]}')])

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
                    viewer_data = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (current_user_id,)))
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
                            following_row = (await db_fetch_one_async(
                                "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (target_user_id,)
                            ))
                            following_count = str(following_row['count'] if following_row else 0)

                        hide_role = user_data.get('hide_role')
                    else:
                        rating_str = str(rating)
                        level_str = str(level)
                        is_target_admin = user_data.get('is_admin', False)
                        aura_str = "" if is_target_admin else format_aura(rating)
                        follower_count = str(len(followers))
                        following_row = (await db_fetch_one_async(
                            "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (target_user_id,)
                        ))
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
    unread_count_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND is_read = FALSE",
        (user_id,)
    ))
    unread_count = unread_count_row['count'] if unread_count_row else 0

    # Pagination settings — one row per conversation partner
    per_page = 7
    offset = (page - 1) * per_page

    # Group messages by sender so each row represents one person, not one message
    conversations = (await db_fetch_all_async('''
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
    ''', (user_id, per_page, offset)))

    total_conv_row = (await db_fetch_one_async(
        "SELECT COUNT(DISTINCT sender_id) as count FROM private_messages WHERE receiver_id = %s",
        (user_id,)
    ))
    total_conversations = total_conv_row['count'] if total_conv_row else 0
    total_pages = max(1, (total_conversations + per_page - 1) // per_page)

    if not conversations:
        # No messages - clean empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No messages")

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

    # One round trip (off the event loop); the total rides along as a window count. An empty
    # page has no row to carry it, but then we either fall back a page or show "no requests".
    requests = await db_fetch_all_async(
        """
        SELECT cr.sender_id, cr.timestamp, u.anonymous_name, u.sex, u.avatar_emoji, u.weekly_badge,
               COUNT(*) OVER () AS total_count
        FROM chat_requests cr
        JOIN users u ON u.user_id = cr.sender_id
        WHERE cr.receiver_id = %s AND cr.status = 'pending'
        ORDER BY cr.timestamp DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, per_page, offset)
    )
    total = int(requests[0]['total_count']) if requests else 0
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

    sender = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (sender_id,)))
    sender_name = get_display_name(sender) if sender else "Unknown User"

    per_page = 6
    offset = (page - 1) * per_page

    messages = (await db_fetch_all_async('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s AND pm.sender_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, sender_id, per_page, offset)))

    total_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s AND sender_id = %s",
        (user_id, sender_id)
    ))
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

    is_blocked = (await db_fetch_one_async(
        "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
        (user_id, sender_id)
    ))

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

        preview = "Message deleted" if msg.get('is_deleted') else (msg['content'] or '[attachment]')
        if len(preview) > 26:
            preview = preview[:23] + '...'
        clean_preview = preview.replace('*', '').replace('_', '').replace('`', '').strip()
        if msg.get('is_edited') and not msg.get('is_deleted'):
            clean_preview += " (edited)"

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

    message = (await db_fetch_one_async('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex, u.user_id as sender_id
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.message_id = %s AND pm.receiver_id = %s
    ''', (message_id, user_id)))

    if not message:
        try:
            await query.message.edit_text(
                "Message not found or you don't have permission to view it.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await query.message.reply_text("Message not found.")
        return

    (await db_execute_async("UPDATE private_messages SET is_read = TRUE WHERE message_id = %s", (message_id,)))

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
    if message.get('is_deleted'):
        media_type = 'text'
        media_id = None

    if message.get('is_deleted'):
        body_text = "_This message was deleted\\._"
    else:
        body_text = escape_markdown(message['content'], version=2) if message['content'] else ""
        if message.get('is_edited'):
            body_text += "\n\n_\\(edited\\)_"
    text_lines = [
        "*Message from " + escape_markdown(message['sender_name'], version=2) + "*",
        "_" + escape_markdown(time_ago, version=2) + "_",
        "",
        body_text
    ]
    text = "\n".join(text_lines)

    is_blocked = (await db_fetch_one_async(
        "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
        (user_id, message['sender_id'])
    ))
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
    message = (await db_fetch_one_async('''
        SELECT pm.content, u.anonymous_name as sender_name
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.message_id = %s AND pm.receiver_id = %s
    ''', (message_id, user_id)))

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
    """Delete a received message — instant and clean, straight back to the
    conversation, with no "Deleting..." / "Message deleted" interstitial."""
    query = update.callback_query

    user_id = str(query.from_user.id)

    msg = (await db_fetch_one_async(
        "SELECT notif_message_id FROM private_messages WHERE message_id = %s AND receiver_id = %s",
        (message_id, user_id)
    ))

    # Delete the message
    success = (await db_execute_async(
        "DELETE FROM private_messages WHERE message_id = %s AND receiver_id = %s",
        (message_id, user_id)
    ))

    if success:
        # Also remove the original notification bubble from this chat, if it's
        # still there, so no trace of the message is left behind.
        if msg and msg.get('notif_message_id'):
            await delete_native_pm_notification(context, user_id, msg['notif_message_id'])
        await query.answer("Message deleted")
        await show_conversation(update, context, sender_id, from_page, list_page)
    else:
        await query.answer("Error deleting message", show_alert=True)


async def edit_sent_message_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Ask the sender for replacement text for a private message they sent."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    msg = (await db_fetch_one_async(
        "SELECT sender_id, content, is_deleted FROM private_messages WHERE message_id = %s",
        (message_id,)
    ))

    if not msg or str(msg['sender_id']) != str(user_id):
        await query.answer("That message is no longer available to edit.", show_alert=True)
        return
    if msg.get('is_deleted'):
        await query.answer("That message was deleted, so it can't be edited.", show_alert=True)
        return

    set_state(context, STATE_AWAITING_PM_EDIT, editing_pm_id=message_id)

    current = msg.get('content') or '[attachment]'
    await query.message.reply_text(
        f"Current message:\n_{escape_markdown(current, version=2)}_\n\nSend the new text, or tap Cancel\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Cancel", callback_data="cancel_edit_sent_msg")
        ]])
    )


async def delete_sent_message_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Confirm before a sender deletes a private message they sent."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    msg = (await db_fetch_one_async(
        "SELECT sender_id, is_deleted FROM private_messages WHERE message_id = %s",
        (message_id,)
    ))

    if not msg or str(msg['sender_id']) != str(user_id):
        await query.answer("That message is no longer available.", show_alert=True)
        return
    if msg.get('is_deleted'):
        await query.answer("Already deleted.", show_alert=True)
        return

    await query.message.reply_text(
        "Delete this message? It'll be removed from their chat too — no trace left behind.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Delete", callback_data=f"confirm_delete_sent_msg_{message_id}"),
            InlineKeyboardButton("Keep", callback_data=f"cancel_delete_sent_msg_{message_id}")
        ]])
    )


async def delete_sent_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Delete a private message the current user sent — a real, clean delete.
    Removes the live notification from the receiver's chat natively and drops
    the row entirely, so nothing embarrassing (a "Message deleted" placeholder)
    is left for the other person to see."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    msg = (await db_fetch_one_async(
        "SELECT sender_id, receiver_id, is_deleted, notif_message_id FROM private_messages WHERE message_id = %s",
        (message_id,)
    ))

    if not msg or str(msg['sender_id']) != str(user_id):
        await query.answer("That message is no longer available.", show_alert=True)
        return
    if msg.get('is_deleted'):
        await query.message.edit_text("Already deleted.")
        return

    # Best-effort: remove the live notification from the receiver's chat first.
    if msg.get('notif_message_id'):
        await delete_native_pm_notification(context, msg['receiver_id'], msg['notif_message_id'])

    (await db_execute_async("DELETE FROM private_messages WHERE message_id = %s", (message_id,)))
    await query.message.edit_text("Message deleted.")


async def mark_all_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark all messages as read"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)

    # Mark all as read
    (await db_execute_async(
        "UPDATE private_messages SET is_read = TRUE WHERE receiver_id = %s",
        (user_id,)
    ))

    await query.answer("All messages marked as read")
    await show_inbox(update, context, 1)  # Refresh inbox
async def show_messages(update: Update, context: ContextTypes.DEFAULT_TYPE, page=1):
    user_id = str(update.effective_user.id)
    
    # Mark messages as read when viewing
    (await db_execute_async(
        "UPDATE private_messages SET is_read = TRUE WHERE receiver_id = %s",
        (user_id,)
    ))
    
    # Get messages with pagination
    per_page = 5
    offset = (page - 1) * per_page
    
    messages = (await db_fetch_all_async('''
        SELECT pm.*, u.anonymous_name as sender_name, u.sex as sender_sex
        FROM private_messages pm
        JOIN users u ON pm.sender_id = u.user_id
        WHERE pm.receiver_id = %s
        ORDER BY pm.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset)))
    
    total_messages_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM private_messages WHERE receiver_id = %s",
        (user_id,)
    ))
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
    post = (await db_fetch_one_async("""
        SELECT p.*, STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.post_id = %s
        GROUP BY p.post_id
    """, (post_id,)))
    if not post:
        if hasattr(update, 'message') and update.message:
            viewer_id = str(update.effective_user.id) if update.effective_user else None
            await update.message.reply_text("Post not found.", reply_markup=get_main_menu(viewer_id) if viewer_id else None)
        return

    viewer_id = str(update.effective_user.id) if update.effective_user else None
    viewer_row = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (viewer_id,))) if viewer_id else None
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
        comment_count = (await asyncio.to_thread(count_all_comments, post_id))
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

    # Author-chosen sex emoji goes on its own line right under the vent number.
    # Never shown on a deleted post.
    sex_line = ""
    if not post.get('deleted') and normalize_revealed_sex(post.get('revealed_sex')):
        sex_line = f"{normalize_revealed_sex(post.get('revealed_sex'))}\n"

    header_text = (
        f"*{escaped_vent}*\n"
        f"{sex_line}"
        f"{explicit_tag}"
        f"{escaped_categories}\n\n"
        f"{escaped_text}"
    )

    comment_count = (await asyncio.to_thread(count_all_comments, post_id))
    header_kb = [
        [InlineKeyboardButton(f"View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton("Write Comment", callback_data=f"writecomment_{post_id}")]
    ]
    if not post.get('deleted'):
        header_kb.append([InlineKeyboardButton("Report Post", callback_data=f"report_post_{post_id}")])

    media_type = post.get('media_type') or 'text'
    media_id = post.get('media_id')
    # Deleted posts show the "content has been deleted" placeholder text only —
    # don't resend the original media alongside it.
    has_media = bool(media_id) and media_type != 'text' and not post.get('deleted')

    if target_message:
        header_kb_markup = InlineKeyboardMarkup(header_kb)

        if has_media:
            # Telegram captions are capped at 1024 chars, unlike message text.
            caption_text = header_text
            if len(caption_text) > 1024:
                caption_text = caption_text[:1021] + '...'

            media_kwargs = {
                'caption': caption_text,
                'parse_mode': ParseMode.MARKDOWN_V2,
                'reply_markup': header_kb_markup
            }

            try:
                if media_type == 'photo':
                    await target_message.reply_photo(photo=media_id, **media_kwargs)
                elif media_type == 'voice':
                    await target_message.reply_voice(voice=media_id, **media_kwargs)
                elif media_type == 'audio':
                    await target_message.reply_audio(audio=media_id, **media_kwargs)
                elif media_type == 'gif':
                    await target_message.reply_animation(animation=media_id, **media_kwargs)
                elif media_type == 'video':
                    await target_message.reply_video(video=media_id, **media_kwargs)
                elif media_type == 'sticker':
                    # Stickers don't reliably support captions across clients, so send the
                    # sticker first, then the header text (with the keyboard) as a follow-up.
                    try:
                        await target_message.reply_sticker(sticker=media_id)
                    except (BadRequest, TypeError) as e:
                        logger.warning(f"Failed to send sticker for post {post_id}: {e}")
                    await target_message.reply_text(
                        header_text,
                        reply_markup=header_kb_markup,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                else:
                    # Unrecognized media type — fall back to text-only header
                    await target_message.reply_text(
                        header_text,
                        reply_markup=header_kb_markup,
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
            except Exception as e:
                logger.error(f"Error sending post media in show_comments_menu for post {post_id}: {e}")
                # Fall back to a text-only header if the media send fails for any reason
                await target_message.reply_text(
                    header_text,
                    reply_markup=header_kb_markup,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
        else:
            await target_message.reply_text(
                header_text,
                reply_markup=header_kb_markup,
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

def truncate_for_telegram(text, max_len):
    """Truncate raw text to fit a Telegram length limit, keeping as much of the
    original message as possible (instead of a short fixed preview) and adding
    an ellipsis only when truncation actually happens."""
    if not text:
        return text or ""
    if len(text) <= max_len:
        return text
    return text[:max(0, max_len - 1)].rstrip() + "…"

# Telegram hard limits: plain messages <=4096 chars, media captions <=1024 chars.
# Leave headroom for the header/footer text wrapped around the message content
# in each notification below, so the combined message never gets rejected or
# silently cut off by Telegram itself.
PM_TEXT_CONTENT_LIMIT = 3500
PM_CAPTION_CONTENT_LIMIT = 850
COMMENT_TEXT_CONTENT_LIMIT = 3500

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
    comment_type = comment.get('type') or 'text'
    file_id = comment.get('file_id')
    content = comment.get('content') or ""
    
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
            user_reaction = (await db_fetch_one_async(
                "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                (comment_id, user_id)
            ))
        user_reaction_type = user_reaction['type'] if user_reaction else None
        
        likes_row = (await db_fetch_one_async(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type NOT IN ('dislike', '👎', '😡')",
            (comment_id,)
        ))
        likes = likes_row['cnt'] if likes_row else 0
        
        dislikes_row = (await db_fetch_one_async(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type IN ('dislike', '👎', '😡')",
            (comment_id,)
        ))
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
    if comment.get('author_id') == user_id:
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

    escaped_content = escape_markdown_v2(content) if content else ""
    message_text = f"{escaped_content}\n\n{author_text}" if escaped_content else author_text
    plain_text = f"{content}\n\n" if content else ""
    valid_reply_id = reply_to_message_id if isinstance(reply_to_message_id, int) and reply_to_message_id > 0 else None

    async def _send_media(target_reply_id, use_markdown=True):
        p_mode = ParseMode.MARKDOWN_V2 if use_markdown else None
        caption = message_text if use_markdown else f"{plain_text}{author_text}"
        if caption and len(caption) > 1024:
            caption = caption[:1020] + '...'
        
        if comment_type == 'text' or not file_id:
            text_to_send = message_text if use_markdown else f"{plain_text}{author_text}"
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text_to_send,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id,
                disable_web_page_preview=True
            )
        elif comment_type == 'photo':
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'voice':
            return await context.bot.send_voice(
                chat_id=chat_id,
                voice=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'audio':
            return await context.bot.send_audio(
                chat_id=chat_id,
                audio=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'video':
            return await context.bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'document':
            return await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'gif':
            return await context.bot.send_animation(
                chat_id=chat_id,
                animation=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id
            )
        elif comment_type == 'sticker':
            st_msg = await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=file_id,
                reply_to_message_id=target_reply_id
            )
            # The text message carries the buttons and is what replies thread under, so it (not
            # the bare sticker) is the message whose id gets stored and returned.
            text_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text if use_markdown else author_text,
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=st_msg.message_id,
                disable_web_page_preview=True
            )
            return text_msg
        else:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=message_text if use_markdown else f"{plain_text}{author_text}",
                reply_markup=kb,
                parse_mode=p_mode,
                reply_to_message_id=target_reply_id,
                disable_web_page_preview=True
            )

    msg = None
    try:
        # Step 1: Send with markdown and threading
        msg = await _send_media(valid_reply_id, use_markdown=True)
    except Exception as e1:
        logger.warning(f"Initial send failed for comment {comment_id} ({comment_type}): {e1}")
        
        # Step 2: Retry standalone if reply threading caused the error
        if valid_reply_id and "reply" in str(e1).lower():
            try:
                msg = await _send_media(None, use_markdown=True)
            except Exception as e2:
                logger.warning(f"Standalone send with markdown failed for comment {comment_id}: {e2}")
        
        # Step 3: If still failed (e.g. MarkdownV2 entity parse error), retry without markdown
        if not msg:
            try:
                msg = await _send_media(valid_reply_id, use_markdown=False)
            except Exception as e3:
                logger.warning(f"Send without markdown failed for comment {comment_id}: {e3}")
                try:
                    msg = await _send_media(None, use_markdown=False)
                except Exception as e4:
                    logger.error(f"All media send attempts failed for comment {comment_id}: {e4}")

    # Fallback to plain text only if the media file itself failed completely (e.g. invalid file_id)
    if not msg:
        try:
            fallback_text = f"[Media: {comment_type}] {content}\n\n{author_text}".strip()
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=fallback_text,
                reply_markup=kb,
                disable_web_page_preview=True
            )
        except Exception as e_final:
            logger.error(f"Absolute final fallback failed for comment {comment_id}: {e_final}")

    if msg:
        await db_execute_async(
            "UPDATE comments SET telegram_message_id = %s WHERE comment_id = %s",
            (msg.message_id, comment_id)
        )
        return msg.message_id

    return None

def _fetch_comments_page_data(post_id, per_page, offset, user_id):
    """Synchronous - all blocking psycopg2 calls for one comments-page render,
    meant to be run off the event loop via asyncio.to_thread(). Returns plain
    dicts/lists only, no DB objects, so it's safe to hand back to async code."""
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    if not post:
        return {'post': None, 'comments': [], 'total_comments': 0,
                'reaction_data': {}, 'parent_msg_ids': {}, 'ratings_map': {}}

    comments = db_fetch_all("""
        SELECT c.*, u.sex AS user_sex, u.avatar_emoji, u.anonymous_name, u.is_admin, COALESCE(u.hide_aura, FALSE) AS hide_aura
        FROM comments c
        LEFT JOIN users u ON c.author_id = u.user_id
        WHERE c.post_id = %s
        ORDER BY c.timestamp ASC
        LIMIT %s OFFSET %s
    """, (post_id, per_page, offset))
    for c in comments:
        c['sex'] = c.pop('user_sex', '👤') or '👤'

    total_comments = count_all_comments(post_id)

    reaction_data = {}
    parent_msg_ids = {}
    ratings_map = {}
    viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(user_id),)) if user_id else None
    is_viewer_admin = bool(viewer_row and viewer_row.get('is_admin'))

    comment_ids = [c['comment_id'] for c in comments]
    if comment_ids:
        counts = db_fetch_all("""
            SELECT comment_id,
                   CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END as rgroup,
                   COUNT(*) as cnt
            FROM reactions WHERE comment_id IN %s GROUP BY comment_id, CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END
        """, (tuple(comment_ids),))
        for row in counts:
            cid = row['comment_id']
            reaction_data.setdefault(cid, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            if row['rgroup'] == 'like': reaction_data[cid]['likes'] = row['cnt']
            else: reaction_data[cid]['dislikes'] = row['cnt']

        u_reacts = db_fetch_all("SELECT comment_id, type FROM reactions WHERE comment_id IN %s AND user_id = %s", (tuple(comment_ids), user_id))
        for row in u_reacts:
            cid = row['comment_id']
            reaction_data.setdefault(cid, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            reaction_data[cid]['user_reaction'] = row['type']

        parent_ids = [c['parent_comment_id'] for c in comments if c.get('parent_comment_id', 0) != 0]
        if parent_ids:
            p_rows = db_fetch_all("SELECT comment_id, telegram_message_id FROM comments WHERE comment_id IN %s", (tuple(parent_ids),))
            for row in p_rows: parent_msg_ids[row['comment_id']] = row['telegram_message_id']

        ratings_map = get_user_ratings_batch([c['author_id'] for c in comments])

        # Clear "unread" only up to the newest comment on this page (GREATEST: never move the
        # marker backwards when the user pages through older comments).
        if user_id and comments and comments[-1].get('timestamp'):
            try:
                db_execute("""
                    INSERT INTO post_views (user_id, post_id, last_viewed)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, post_id)
                    DO UPDATE SET last_viewed = GREATEST(post_views.last_viewed, EXCLUDED.last_viewed)
                """, (str(user_id), post_id, comments[-1]['timestamp']))
            except Exception as pv_err:
                logger.warning(f"Could not update post_views for {user_id}/{post_id}: {pv_err}")

    return {
        'post': post, 'comments': comments, 'total_comments': total_comments,
        'reaction_data': reaction_data, 'parent_msg_ids': parent_msg_ids, 'ratings_map': ratings_map,
        'is_viewer_admin': is_viewer_admin,
    }

async def show_comments_page(update, context, post_id, page=1, reply_pages=None):
    if update.effective_chat is None:
        logger.error("Cannot determine chat from update: %s", update)
        return
    chat_id = update.effective_chat.id

    # Show typing animation
    await typing_animation(context, chat_id, 0.5)

    user_id = str(update.effective_user.id)
    per_page = 10
    offset = (page - 1) * per_page

    # All of the DB work for this page - post, comments, reactions, threading,
    # and per-author ratings - runs together in a worker thread via to_thread.
    # Previously these ran as blocking psycopg2 calls directly on the asyncio
    # event loop, so every comment-page load froze ALL users' button presses
    # for the duration of the DB round trips (and calculate_user_rating() was
    # being called once per comment, i.e. up to 5 extra blocking queries per
    # comment shown). See _fetch_comments_page_data / get_user_ratings_batch.
    page_data = await asyncio.to_thread(_fetch_comments_page_data, post_id, per_page, offset, user_id)

    post = page_data['post']
    if not post:
        await context.bot.send_message(chat_id, "Post not found.")
        return

    # Keep the real author_id even for deleted posts so the vent author is still
    # shown as "Vent author" (not their real nickname) when commenting under their
    # own deleted post — blanking this out here previously deanonymized them.
    post_author_id = post['author_id']
    comments = page_data['comments']
    total_comments = page_data['total_comments']
    reaction_data = page_data['reaction_data']
    parent_msg_ids = page_data['parent_msg_ids']
    ratings_map = page_data['ratings_map']
    is_viewer_admin = page_data.get('is_viewer_admin', False)
    total_pages = (total_comments + per_page - 1) // per_page

    if not comments and page == 1:
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

    context._user_id = user_id
    msg_ids = {}

    for comment in comments:
        comment_id = comment['comment_id']
        parent_id = comment.get('parent_comment_id', 0)
        
        # User cached or joined data
        rating = ratings_map.get(comment['author_id'], 0)
        is_author = str(comment['author_id']) == str(post_author_id)
        
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{comment['author_id']}_{post_id}"
        show_aura = not comment.get('hide_aura') or str(user_id) == str(comment['author_id']) or is_viewer_admin
        aura_text = f"_Aura_ ⚡ {escape_markdown(str(rating), version=2)} pts" if (not comment.get('is_admin') and show_aura) else ""
        
        if is_author:
            # Vent author: show sex emoji + clickable "Vent author" (no custom avatar, no aura)
            sex_emoji = comment.get('sex') or '👤'
            author_text = f"{sex_emoji} [_{escape_markdown('Vent author', version=2)}_]({profile_link})"
        else:
            # Normal user: show full display (sex + custom avatar + name + aura)
            sex_emoji = comment.get('sex') or '👤'
            avatar_emoji = comment.get('avatar_emoji')
            if sex_emoji in ('👨', '👩'):
                author_avatar = f"{sex_emoji} {avatar_emoji}" if avatar_emoji else sex_emoji
            else:
                author_avatar = avatar_emoji if avatar_emoji else '👤'
            author_label = f"[_{escape_markdown(comment['anonymous_name'] or 'Anonymous', version=2)}_]({profile_link})"
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
async def send_reply_message(context, chat_id, reply, post_author_id, post_id, reply_to_message_id, pre_fetched_data=None, rating_override=None, show_aura=None):
    """Send a single reply message with proper formatting using pre-fetched user data if available"""
    # Use joined data if available, else fetch
    is_admin = reply.get('is_admin')
    if is_admin is None: # Not pre-fetched
        reply_user = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (reply['author_id'],)))
        is_admin = reply_user.get('is_admin', False) if reply_user else False
        display_sex = get_display_sex(reply_user) if reply_user else '👤'
        display_name = get_display_name(reply_user) if reply_user else 'Anonymous'
        avatar_emoji = reply_user.get('avatar_emoji') if reply_user else None
        hide_aura = reply_user.get('hide_aura', False) if reply_user else False
    else:
        display_sex = reply.get('sex') or '👤'
        display_name = reply.get('anonymous_name') or 'Anonymous'
        avatar_emoji = reply.get('avatar_emoji')
        hide_aura = reply.get('hide_aura', False)

    # rating_override lets callers pass a pre-batched rating (see show_more_replies)
    # instead of triggering a fresh 5-query calculate_user_rating() call per reply.
    rating_reply = rating_override if rating_override is not None else (await asyncio.to_thread(calculate_user_rating, reply['author_id']))
    reply_profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply['author_id']}_{post_id}"
    if show_aura is None:
        show_aura = not hide_aura
    aura_text = f"_Aura_ ⚡ {escape_markdown(str(rating_reply), version=2)} pts" if (not is_admin and show_aura) else ""
    
    # Check if reply author is the vent author
    if str(reply['author_id']) == str(post_author_id):
        # Vent author reply: clickable "Vent author" with sex emoji
        sex_emoji = display_sex or '👤'
        reply_author_text = f"{sex_emoji} [_{escape_markdown('Vent author', version=2)}_]({reply_profile_link})"
    else:
        # Normal user
        author_sex = display_sex or '👤'
        author_label = f"[_{escape_markdown(display_name, version=2)}_]({reply_profile_link})"
        if author_sex in ('👨', '👩'):
            author_avatar = f"{author_sex} {avatar_emoji}" if avatar_emoji else author_sex
        else:
            author_avatar = avatar_emoji if avatar_emoji else '👤'
        reply_author_text = f"{author_avatar} {author_label} {aura_text}".strip()

    # Pass pre-fetched reaction data if available (e.g. from show_more_replies)
    # Pass the full reply dict (already done, but ensured)
    return await send_comment_message(context, chat_id, reply, reply_author_text, reply_to_message_id, pre_fetched_data=pre_fetched_data)

def _fetch_more_replies_data(comment_id, post_id, replies_per_page, offset, user_id):
    """Synchronous - all blocking DB calls for one 'show more replies' page,
    meant to run off the event loop via asyncio.to_thread()."""
    post = db_fetch_one("SELECT author_id FROM posts WHERE post_id = %s", (post_id,))
    post_author_id = post['author_id'] if post else None

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

    replies = db_fetch_all("""
        WITH RECURSIVE comment_tree AS (
            SELECT * FROM comments WHERE parent_comment_id = %s
            UNION ALL
            SELECT c.* FROM comments c
            JOIN comment_tree ct ON c.parent_comment_id = ct.comment_id
        )
        SELECT ct.*, u.sex AS user_sex, u.anonymous_name, u.is_admin, u.avatar_emoji, COALESCE(u.hide_aura, FALSE) AS hide_aura
        FROM comment_tree ct
        LEFT JOIN users u ON ct.author_id = u.user_id
        ORDER BY ct.timestamp ASC LIMIT %s OFFSET %s
    """, (comment_id, replies_per_page, offset))
    for r in replies:
        r['sex'] = r.pop('user_sex', '👤') or '👤'

    reply_ids = [r['comment_id'] for r in replies]
    reaction_data = {}
    parent_msg_ids = {}
    ratings_map = {}
    viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(user_id),)) if user_id else None
    is_viewer_admin = bool(viewer_row and viewer_row.get('is_admin'))

    if reply_ids:
        counts = db_fetch_all("""
            SELECT comment_id,
                   CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END as rgroup,
                   COUNT(*) as cnt
            FROM reactions WHERE comment_id IN %s GROUP BY comment_id, CASE WHEN type IN ('dislike', '👎', '😡') THEN 'dislike' ELSE 'like' END
        """, (tuple(reply_ids),))
        for row in counts:
            cid = row['comment_id']
            reaction_data.setdefault(cid, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            if row['rgroup'] == 'like': reaction_data[cid]['likes'] = row['cnt']
            else: reaction_data[cid]['dislikes'] = row['cnt']

        u_reacts = db_fetch_all("SELECT comment_id, type FROM reactions WHERE comment_id IN %s AND user_id = %s", (tuple(reply_ids), user_id))
        for row in u_reacts:
            cid = row['comment_id']
            reaction_data.setdefault(cid, {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            reaction_data[cid]['user_reaction'] = row['type']

        p_ids = [r['parent_comment_id'] for r in replies]
        if p_ids:
            p_rows = db_fetch_all("SELECT comment_id, telegram_message_id FROM comments WHERE comment_id IN %s", (tuple(p_ids),))
            for row in p_rows: parent_msg_ids[row['comment_id']] = row['telegram_message_id']

        ratings_map = get_user_ratings_batch([r['author_id'] for r in replies])

    return {
        'post_author_id': post_author_id, 'total_replies': total_replies, 'replies': replies,
        'reaction_data': reaction_data, 'parent_msg_ids': parent_msg_ids, 'ratings_map': ratings_map,
        'is_viewer_admin': is_viewer_admin,
    }

async def show_more_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, comment_id: int, page: int):
    """Show additional replies for a comment (paginated)"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    
    # Get the comment to find its post and telegram_message_id
    comment = (await db_fetch_one_async("SELECT post_id, telegram_message_id FROM comments WHERE comment_id = %s", (comment_id,)))
    if not comment:
        await query.answer("Comment not found", show_alert=True)
        return
    
    post_id = comment['post_id']
    base_reply_to_id = comment.get('telegram_message_id')
    replies_per_page = 5
    offset = 3 + (page - 1) * replies_per_page
    user_id = str(update.effective_user.id)

    try:
        page_data = await asyncio.to_thread(_fetch_more_replies_data, comment_id, post_id, replies_per_page, offset, user_id)
    except Exception as e:
        logger.error(f"Error fetching more replies for comment {comment_id}: {e}")
        await query.answer("Error loading replies", show_alert=True)
        return

    post_author_id = page_data['post_author_id']
    total_replies = page_data['total_replies']
    total_pages = (total_replies - 3 + replies_per_page - 1) // replies_per_page
    replies = page_data['replies']
    reaction_data = page_data['reaction_data']
    parent_msg_ids = page_data['parent_msg_ids']
    ratings_map = page_data['ratings_map']
    is_viewer_admin = page_data.get('is_viewer_admin', False)

    # Delete the "Show more replies" button
    try: await query.message.delete()
    except: pass
    
    msg_ids = {comment_id: base_reply_to_id}

    for reply in replies:
        reply_msg_id = None  # defined before the try so the except fallback can never hit UnboundLocalError
        try:
            pid = reply.get('parent_comment_id')
            target_msg_id = msg_ids.get(pid) or parent_msg_ids.get(pid) or base_reply_to_id
            
            pref = reaction_data.get(reply['comment_id'], {'likes': 0, 'dislikes': 0, 'user_reaction': None})
            reply_rating = ratings_map.get(reply['author_id'], 0)
            show_aura = not reply.get('hide_aura') or str(user_id) == str(reply['author_id']) or is_viewer_admin
            reply_msg_id = await send_reply_message(context, chat_id, reply, post_author_id, post_id, target_msg_id, pre_fetched_data=pref, rating_override=reply_rating, show_aura=show_aura)
            
            if reply_msg_id:
                msg_ids[reply['comment_id']] = reply_msg_id
        except Exception as e:
            logger.error(f"Error sending reply {reply.get('comment_id')}: {e}")
            
            if reply_msg_id:
                msg_ids[reply['comment_id']] = reply_msg_id
    
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
    user = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
    if not user:
        return
    
    display_name = get_display_name(user)
    display_sex = get_display_sex(user)
    rating = (await asyncio.to_thread(calculate_user_rating, user_id))
    
    weekly_badge = user.get('weekly_badge')
    if weekly_badge:
        display_name = f"{weekly_badge} {display_name}"

    
    
    followers = (await db_fetch_all_async(
        "SELECT * FROM followers WHERE followed_id = %s",
        (user_id,)
    ))
    
    bio = user.get('bio', 'No bio set.')
    level = (rating // 10) + 1
    follower_count = len(followers)

    # Fetch following count (users this person follows)
    following_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM followers WHERE follower_id = %s", (user_id,)
    ))
    following_count = following_row['count'] if following_row else 0
    
    # Profile action grid
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

# Builds the paginated "Thread to Previous Post" picker content
def build_thread_pick_content(user_id, page=1):
    """Build the text + keyboard for one page of the 'Thread to Previous Post'
    picker. Previously this only ever showed the 6 most recent posts with no
    way to reach anything older; now it's paginated the same way My Previous
    Posts is."""
    per_page = 6
    page = max(page, 1)
    offset = (page - 1) * per_page

    recent_posts = db_fetch_all(
        "SELECT post_id, content, vent_number FROM posts "
        "WHERE author_id = %s AND approved = TRUE AND deleted = FALSE "
        "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    )
    total_row = db_fetch_one(
        "SELECT COUNT(*) as cnt FROM posts WHERE author_id = %s AND approved = TRUE AND deleted = FALSE",
        (user_id,)
    )
    total_posts = total_row['cnt'] if total_row else 0
    total_pages = max((total_posts + per_page - 1) // per_page, 1)

    if not recent_posts:
        return None, None, total_pages

    thread_kb = []
    for p in recent_posts:
        label = p['content'][:40] + ('…' if len(p['content']) > 40 else '')
        num = p.get('vent_number')
        prefix = f"Vent-{num:03d}: " if num else ""
        thread_kb.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"thread_pick_{p['post_id']}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀ Newer", callback_data=f"threadpg_{page - 1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Older ▶", callback_data=f"threadpg_{page + 1}"))
    if nav_row:
        thread_kb.append(nav_row)

    thread_kb.append([InlineKeyboardButton("No Thread (Standalone)", callback_data="thread_pick_none")])
    thread_kb.append([InlineKeyboardButton("Back to Preview", callback_data="thread_pick_cancel")])

    text = "*Thread to Previous Post*\n\nPick one of your recent posts to continue as a thread, or keep this post standalone:"
    if total_pages > 1:
        text += f"\n\nPage {page}/{total_pages}"

    return text, InlineKeyboardMarkup(thread_kb), total_pages

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
    posts = (await db_fetch_all_async(
        "SELECT * FROM posts WHERE author_id = %s AND approved = TRUE AND deleted = FALSE ORDER BY timestamp DESC LIMIT %s OFFSET %s",
        (user_id, per_page, offset)
    ))
    
    total_posts_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM posts WHERE author_id = %s AND approved = TRUE AND deleted = FALSE",
        (user_id,)
    ))
    total_posts = total_posts_row['count'] if total_posts_row else 0
    total_pages = (total_posts + per_page - 1) // per_page
    
    if not posts:
        # Show empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No posts found")
        
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
        
        # Comment count is already denormalized onto posts.comment_count and kept in
        # sync by update_channel_post_comment_count() whenever a comment is added/removed -
        # calling count_all_comments() here was a redundant extra query per post
        # (an N+1: one COUNT(*) query per row instead of using the column already fetched).
        comment_count = post['comment_count'] or 0
        
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

# Function to view a specific post
# Function to view a specific post in detail
# Function to show menu for My Content
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

# Function to show a single post with action buttons
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
    post = (await db_fetch_one_async("""
        SELECT p.*, STRING_AGG(pc.category_code, ', ') as categories
        FROM posts p
        LEFT JOIN post_categories pc ON p.post_id = pc.post_id
        WHERE p.post_id = %s
        GROUP BY p.post_id
    """, (post_id,)))
    
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
    comment_count = (await asyncio.to_thread(count_all_comments, post_id))
    
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
    # (We've already verified above that post['author_id'] == user_id, so every
    # button below - including Edit Post - is implicitly author-only.)
    keyboard = [
        [InlineKeyboardButton("View Comments", callback_data=f"viewcomments_{post_id}_1")],
        [InlineKeyboardButton("Continue Thread", callback_data=f"continue_post_{post_id}")],
    ]

    # Let the author edit their post's content once it has been approved
    # and published to the channel.
    if post.get('approved'):
        keyboard.append(
            [InlineKeyboardButton("Edit Post", callback_data=f"edit_published_{post_id}")]
        )

    keyboard.append([
        InlineKeyboardButton("Delete Post", callback_data=f"delete_post_{post_id}_{from_page}"),
        InlineKeyboardButton("Back to List", callback_data=f"my_posts_{from_page}")
    ])
    keyboard.append([
        InlineKeyboardButton("Back to My Content", callback_data='my_content_menu'),
        InlineKeyboardButton("Main Menu", callback_data='menu')
    ])
    
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
# Function to show user's comments
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
    comments = (await db_fetch_all_async('''
        SELECT c.*, p.content as post_content, p.post_id
        FROM comments c
        JOIN posts p ON c.post_id = p.post_id
        WHERE c.author_id = %s
        ORDER BY c.timestamp DESC
        LIMIT %s OFFSET %s
    ''', (user_id, per_page, offset)))
    
    total_comments_row = (await db_fetch_one_async(
        "SELECT COUNT(*) as count FROM comments WHERE author_id = %s",
        (user_id,)
    ))
    total_comments = total_comments_row['count'] if total_comments_row else 0
    total_pages = (total_comments + per_page - 1) // per_page
    
    if not comments:
        # Show empty state
        if loading_msg:
            await replace_with_success(loading_msg, "No comments found")
        
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
    """Fetch paginated pending reports with reporter name (newest first)."""
    return db_fetch_all(
        """SELECT r.*, u.anonymous_name as reporter_name
           FROM reports r
           LEFT JOIN users u ON r.reporter_id = u.user_id
           WHERE r.status = 'pending'
           ORDER BY r.created_at DESC, r.report_id DESC
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
    elif target_type == 'user':
        row = db_fetch_one("SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (str(target_id),))
        if row:
            return f"User: {get_display_name(row)} (ID {target_id})", str(target_id)
    return None, None


def _report_type_label(target_type: str) -> str:
    return {'post': 'Post', 'comment': 'Comment', 'user': 'User'}.get(target_type, 'Content')


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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,)))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    per_page = 8
    offset = (page - 1) * per_page
    convos = (await asyncio.to_thread(get_admin_conversations, limit=per_page, offset=offset))
    total = (await asyncio.to_thread(get_admin_conversations_count))
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


TRANSCRIPT_PAGE_SIZE = 40


def _format_transcript_text(user_a, user_b, live=False, page=1, total_pages=1):
    page = max(1, int(page or 1))
    msgs = get_admin_conversation_transcript(
        user_a, user_b, limit=TRANSCRIPT_PAGE_SIZE, offset=(page - 1) * TRANSCRIPT_PAGE_SIZE
    )
    # Names come from an LRU (invalidated by db_update_user): the live monitor calls this
    # every 8 seconds and used to run two user lookups on every tick.
    name_a = get_user_display_name(user_a)
    name_b = get_user_display_name(user_b)

    header = "*LIVE*" if live else "*Transcript*"
    lines = [f"{header}: {escape_markdown(name_a, version=2)} ↔ {escape_markdown(name_b, version=2)}\n"]
    if live:
        lines.append("_auto\\-refreshing every 8s_\n")
    elif total_pages > 1:
        lines.append(f"_Page {page}/{total_pages} \\(page 1 = newest\\)_\n")
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


def _transcript_view_data(user_a, user_b, page):
    """Sync helper (run via to_thread): total page count + the rendered text for one page."""
    total = get_admin_conversation_message_count(user_a, user_b)
    total_pages = max(1, (total + TRANSCRIPT_PAGE_SIZE - 1) // TRANSCRIPT_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    return page, total_pages, _format_transcript_text(user_a, user_b, live=False, page=page, total_pages=total_pages)


async def show_admin_chat_transcript(update, context, user_a, user_b, page=1, live=False):
    query = update.callback_query
    admin_id = str(update.effective_user.id)
    user = await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    total_pages = 1
    if live:
        text = await asyncio.to_thread(_format_transcript_text, user_a, user_b, True)
        page = 1
    else:
        # `page` is real now: page 1 = newest 40 messages, higher pages walk back in time.
        page, total_pages, text = await asyncio.to_thread(_transcript_view_data, user_a, user_b, page)
    live_label = "Stop Live" if live else "Go Live"
    live_cb = f"admin_chat_stoplive_{user_a}_{user_b}" if live else f"admin_chat_golive_{user_a}_{user_b}"
    kb = [
        [InlineKeyboardButton("Refresh", callback_data=f"admin_chat_view_{user_a}_{user_b}_{page}"),
         InlineKeyboardButton(live_label, callback_data=live_cb)]
    ]
    if not live and total_pages > 1:
        nav = []
        if page < total_pages:
            nav.append(InlineKeyboardButton("◀ Older", callback_data=f"admin_chat_view_{user_a}_{user_b}_{page + 1}"))
        if page > 1:
            nav.append(InlineKeyboardButton("Newer ▶", callback_data=f"admin_chat_view_{user_a}_{user_b}_{page - 1}"))
        kb.append(nav)
    kb.append([InlineKeyboardButton("Chat List", callback_data='admin_chats_1')])
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
    text = await asyncio.to_thread(_format_transcript_text, d['user_a'], d['user_b'], True)
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (admin_id,)))
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

    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        if query:
            await query.answer("No permission.", show_alert=True)
        return

    per_page = 5
    offset = (page - 1) * per_page
    reports = (await asyncio.to_thread(get_pending_reports, offset=offset, limit=per_page))

    total_row = (await db_fetch_one_async("SELECT COUNT(*) as cnt FROM reports WHERE status = 'pending'"))
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
        preview, _ = (await asyncio.to_thread(get_report_content_preview, rep['target_type'], rep['target_id']))
        preview = (preview or '[deleted]')[:60]
        type_label = _report_type_label(rep['target_type'])
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
        keyboard.append([InlineKeyboardButton("🛡 Moderate author", callback_data=f"mod_rep_{rep['report_id']}")])

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
        reporter = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (reporter_id,)))
        reporter_name = reporter['anonymous_name'] if reporter else 'Anonymous'
        type_label = _report_type_label(target_type)
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
        post = (await db_fetch_one_async("SELECT content, author_id FROM posts WHERE post_id = %s", (post_id,)))
        comment_author = (await db_fetch_one_async("SELECT user_id, anonymous_name FROM users WHERE user_id = %s", (comment['author_id'],)))
        
        # Don't notify yourself
        if str(reactor_id) == str(comment['author_id']):
            return

        # Anonymization: If the person reacting is the post author
        if post and str(reactor_id) == str(post['author_id']):
            reactor_display = "Vent author"
        else:
            reactor = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (reactor_id,)))
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
    # to show the dark-toast loading animations.
    
    user_id = str(query.from_user.id)
    
    # Log the callback data for debugging
    logger.info(f"Callback data received: {query.data} from user {user_id}")
    
    try:
        # ... rest of your code
        # Handle noop callback (do nothing for separator buttons)
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
                    explicit=pending_post.get('explicit', False),
                    revealed_sex=pending_post.get('revealed_sex')
                )
                return

            # Store selected categories and enter the awaiting-post state.
            # thread_from_post_id (if any) was already set in context.user_data
            # directly when the "continue this post" button was tapped.
            set_state(context, STATE_AWAITING_POST, selected_categories=','.join(selected))

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
            current = (await db_fetch_one_async("SELECT notifications_enabled FROM users WHERE user_id = %s", (user_id,)))
            if current:
                new_value = not current['notifications_enabled']
                await db_update_user_async(user_id, notifications_enabled=new_value)
            await show_settings(update, context)
        
        elif query.data == 'toggle_privacy':
            current = (await db_fetch_one_async("SELECT privacy_public FROM users WHERE user_id = %s", (user_id,)))
            if current:
                new_value = not current['privacy_public']
                await db_update_user_async(user_id, privacy_public=new_value)
            await show_settings(update, context)

        elif query.data == 'privacy_settings':
            await show_privacy_settings(update, context)

        elif query.data.startswith('toggle_hide_'):
            metric = query.data.replace('toggle_hide_', '')
            col = f"hide_{metric}"
            
            # Simple toggle logic
            current = (await db_fetch_one_async(f"SELECT {col} FROM users WHERE user_id = %s", (user_id,)))
            if current:
                new_val = not current[col]
                await db_update_user_async(user_id, **{col: new_val})
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
            set_state(context, STATE_AWAITING_NAME)
            await query.message.reply_text(
                "Please type your new anonymous name:\n\nTap Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

        elif query.data == 'edit_bio':
            await query.answer("Opening Bio Editor...", show_alert=False)
            set_state(context, STATE_AWAITING_BIO)
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
            
            await db_update_user_async(user_id, sex=sex)
            await query.message.reply_text("Sex updated!")
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data.startswith(('follow_', 'unfollow_')):
            await query.answer("Updating Follow...", show_alert=False)
            target_uid = query.data.split('_', 1)[1]
            if query.data.startswith('follow_'):
                try:
                    (await db_execute_async(
                        "INSERT INTO followers (follower_id, followed_id) VALUES (%s, %s)",
                        (user_id, target_uid)
                    ))
                    calculate_user_rating.cache_clear()  # followers add aura points
                    _leaderboard_cache_bust()
                    # Notify the followed user if they have notifications enabled
                    followed_user = (await db_fetch_one_async(
                        "SELECT notifications_enabled FROM users WHERE user_id = %s", (target_uid,)
                    ))
                    if followed_user and followed_user['notifications_enabled']:
                        follower_data = (await db_fetch_one_async(
                            "SELECT anonymous_name, avatar_emoji FROM users WHERE user_id = %s", (user_id,)
                        ))
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
                (await db_execute_async(
                    "DELETE FROM followers WHERE follower_id = %s AND followed_id = %s",
                    (user_id, target_uid)
                ))
            calculate_user_rating.cache_clear()
            _leaderboard_cache_bust()
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
            rows = (await db_fetch_all_async(
                "SELECT u.user_id, u.anonymous_name, u.avatar_emoji FROM followers f "
                "JOIN users u ON f.follower_id = u.user_id "
                "WHERE f.followed_id = %s ORDER BY u.anonymous_name LIMIT %s OFFSET %s",
                (user_id, per_page, offset)
            ))
            total_row = (await db_fetch_one_async(
                "SELECT COUNT(*) as cnt FROM followers WHERE followed_id = %s", (user_id,)
            ))
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
            rows = (await db_fetch_all_async(
                "SELECT u.user_id, u.anonymous_name, u.avatar_emoji FROM followers f "
                "JOIN users u ON f.followed_id = u.user_id "
                "WHERE f.follower_id = %s ORDER BY u.anonymous_name LIMIT %s OFFSET %s",
                (user_id, per_page, offset)
            ))
            total_row = (await db_fetch_one_async(
                "SELECT COUNT(*) as cnt FROM followers WHERE follower_id = %s", (user_id,)
            ))
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
                set_state(context, STATE_AWAITING_COMMENT, comment_post_id=post_id, comment_idx=None)

                await query.message.reply_text(
                    "Type your comment, or send a voice message, GIF, or sticker.\n\nTap Cancel to return to the menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML
                )
                return
        # Like/Dislike reaction handling
        elif query.data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
            try:
                parts = query.data.split('_')
                comment_id = int(parts[1])
                reaction_type = 'like' if parts[0] in ('likecomment', 'likereply') else 'dislike'

                # Check if user already has a reaction on this comment
                existing_reaction = (await db_fetch_one_async(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                ))

                if existing_reaction:
                    is_existing_like = existing_reaction['type'] not in ('dislike', '👎', '😡')
                    is_new_like = reaction_type == 'like'
                    if is_existing_like == is_new_like:
                        # User is clicking the same reaction group - remove it (toggle off)
                        (await db_execute_async(
                            "DELETE FROM reactions WHERE comment_id = %s AND user_id = %s",
                            (comment_id, user_id)
                        ))
                    else:
                        # User is changing reaction group - update it
                        (await db_execute_async(
                            "UPDATE reactions SET type = %s WHERE comment_id = %s AND user_id = %s",
                            (reaction_type, comment_id, user_id)
                        ))
                else:
                    # User is adding a new reaction
                    (await db_execute_async(
                        "INSERT INTO reactions (comment_id, user_id, type) VALUES (%s, %s, %s)",
                        (comment_id, user_id, reaction_type)
                    ))
                
                # Clear Aura Cache
                calculate_user_rating.cache_clear()
                _leaderboard_cache_bust()
                format_aura.cache_clear()

                # Get updated counts
                likes_row = (await db_fetch_one_async(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type NOT IN ('dislike', '👎', '😡')",
                    (comment_id,)
                ))
                likes = likes_row['cnt'] if likes_row else 0
                
                dislikes_row = (await db_fetch_one_async(
                    "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = %s AND type IN ('dislike', '👎', '😡')",
                    (comment_id,)
                ))
                dislikes = dislikes_row['cnt'] if dislikes_row else 0

                comment = (await db_fetch_one_async(
                    "SELECT post_id, parent_comment_id, author_id, type, content FROM comments WHERE comment_id = %s",
                    (comment_id,)
                ))
                if not comment:
                    await query.answer("Comment not found", show_alert=True)
                    return

                post_id = comment['post_id']
                parent_comment_id = comment['parent_comment_id']

                # Get user's current reaction after update
                user_reaction = (await db_fetch_one_async(
                    "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
                    (comment_id, user_id)
                ))

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

        # Handle edit comment
        elif query.data.startswith("edit_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (comment_id,)))
            
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

        # Handle delete comment
        elif query.data.startswith("delete_comment_"):
            comment_id = int(query.data.split('_')[2])
            comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (comment_id,)))
            
            if comment and comment['author_id'] == user_id:
                # Get post_id before deleting for updating comment count
                post_id = comment['post_id']
                
                # Orphan Adoption: Become top-level first
                (await db_execute_async("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (comment_id,)))
                
                # Delete the comment and its reactions
                (await db_execute_async("DELETE FROM reactions WHERE comment_id = %s", (comment_id,)))
                (await db_execute_async("DELETE FROM comments WHERE comment_id = %s", (comment_id,)))
                calculate_user_rating.cache_clear()
                _leaderboard_cache_bust()
                
                await query.answer("Comment deleted")
                await query.message.delete()
                
                # Update comment count with orphan check
                await adopt_orphaned_replies(context, post_id)
            else:
                await query.answer("You can only delete your own comments", show_alert=True)

        # Handle delete post
        elif query.data.startswith("delete_post_"):
            try:
                parts = query.data.split('_')
                post_id = int(parts[2])
                
                # Get the page number (default to 1 if not provided)
                from_page = 1
                if len(parts) > 3:
                    from_page = int(parts[3])
                
                post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
                
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
                
                post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
                
                if post and post['author_id'] == user_id:
                    if post['channel_message_id']:
                        try:
                            if post.get('vent_number'):
                                vent_display = f"Vent - {post['vent_number']:03d}"
                            else:
                                vent_display = "Vent"

                            cats_row = (await db_fetch_all_async("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,)))
                            categories = [row['category_code'] for row in cats_row]
                            hashtags = ' '.join([f"#{cat}" for cat in categories]) if categories else "#Other"

                            channel_text = build_deleted_channel_text(vent_display, hashtags)

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
                    
                    (await db_execute_async("UPDATE posts SET deleted = TRUE WHERE post_id = %s", (post_id,)))
                    
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

        # Let an author edit the content of a post that's already been
        # approved and published to the channel.
        elif query.data.startswith("edit_published_"):
            try:
                post_id = int(query.data.split('_')[2])
                post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))

                # Permission check: only the author can edit their own post
                if not post or post['author_id'] != user_id:
                    await query.answer("You can only edit your own posts", show_alert=True)
                    return

                # Content edits via this flow only apply once a post is live
                if not post.get('approved'):
                    await query.answer("Only approved, published posts can be edited this way", show_alert=True)
                    return

                # Enter the "editing a published post" state and remember which post
                set_state(context, STATE_AWAITING_EDIT_CONTENT, editing_published_post=post_id)

                # Message 1: ONLY the copyable content, wrapped in <pre> with no
                # other text - so the user can select/copy just the content
                # without dragging in any instruction text.
                content_escaped = html.escape(post['content'])
                await query.message.reply_text(
                    f"<pre>{content_escaped}</pre>",
                    parse_mode=ParseMode.HTML
                )

                # Message 2: the instructions, sent separately on purpose.
                await query.message.reply_text(
                    "Edit your post \u2013 send the entire corrected text as a new message. "
                    "Tap Cancel to abort.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Cancel", callback_data='cancel_input')]
                    ])
                )
                return
            except (IndexError, ValueError) as e:
                logger.error(f"Error starting published post edit: {e}")
                await query.answer("Error processing request", show_alert=True)

        
        elif query.data.startswith('chatrequest_'):
            target_id = query.data.split('_')[1]
            if target_id == user_id:
                await query.answer("You cannot chat with yourself.", show_alert=True)
                return

            # Check for existing request
            existing = (await db_fetch_one_async(
                "SELECT status, timestamp FROM chat_requests WHERE sender_id = %s AND receiver_id = %s",
                (user_id, target_id)
            ))
            
            if existing:
                if existing['status'] == 'accepted':
                    await query.answer("Request already accepted!", show_alert=False)
                    set_state(context, STATE_AWAITING_PRIVATE_MESSAGE, private_message_target=target_id)
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
                (await db_execute_async(
                    "UPDATE chat_requests SET timestamp = CURRENT_TIMESTAMP WHERE sender_id = %s AND receiver_id = %s",
                    (user_id, target_id)
                ))
                await query.answer("🔔 Reminder sent!", show_alert=False)

                sender_data = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
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
                (await db_execute_async(
                    "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'pending')",
                    (user_id, target_id)
                ))
                await query.answer("Chat request sent!", show_alert=False)
                
                # Notify receiver
                sender_data = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
                sender_name = get_display_name(sender_data)
                
                receiver_text = (
                    f"*New Chat Request\\!*\n"
                    f"_{escape_markdown(sender_name, version=2)}_ wants to chat with you\\.\n\n"
                    f"_You can find this anytime under Settings ➜ Chat Requests\\._"
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
            (await db_execute_async(
                "UPDATE chat_requests SET status = 'accepted' WHERE sender_id = %s AND receiver_id = %s",
                (sender_id, user_id)
            ))
            # Mutual chat permission
            (await db_execute_async(
                "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'accepted') ON CONFLICT DO NOTHING",
                (user_id, sender_id)
            ))
            
            await query.answer("✅ Request accepted!", show_alert=False)
            await query.message.edit_text("✅ *You accepted the chat request\\!*", parse_mode=ParseMode.MARKDOWN_V2)
            
            receiver_data = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
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
            (await db_execute_async("DELETE FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (sender_id, user_id)))
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
                    (await db_execute_async(
                        "UPDATE chat_requests SET status = 'accepted' WHERE sender_id = %s AND receiver_id = %s",
                        (sender_id, user_id)
                    ))
                    # Mutual chat permission, mirroring the acceptchat_ flow
                    (await db_execute_async(
                        "INSERT INTO chat_requests (sender_id, receiver_id, status) VALUES (%s, %s, 'accepted') ON CONFLICT DO NOTHING",
                        (user_id, sender_id)
                    ))
                    await query.answer("✅ Request accepted!", show_alert=False)

                    receiver_data = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (user_id,)))
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
                    (await db_execute_async(
                        "DELETE FROM chat_requests WHERE sender_id = %s AND receiver_id = %s",
                        (sender_id, user_id)
                    ))
                    await query.answer("❌ Request rejected.", show_alert=False)

                # Refresh the list in place so the user can keep working through it
                await show_chat_requests(update, context, page=page)
            except Exception as e:
                logger.error(f"Error in reqaccept/reqreject handler: {e}")
                await query.answer("Error processing request. Please try again.", show_alert=True)

        elif query.data.startswith('message_'):
            target_id = query.data.split('_')[1]
            check = (await db_fetch_one_async("SELECT status FROM chat_requests WHERE sender_id = %s AND receiver_id = %s", (user_id, target_id)))
            
            if not check or check['status'] != 'accepted':
                await query.answer("You must send a chat request first!", show_alert=True)
                return

            await query.answer("Opening Chat...", show_alert=False)
            set_state(context, STATE_AWAITING_PRIVATE_MESSAGE, private_message_target=target_id)
            await query.message.reply_text("*Please type your private message:*\n\nTap Cancel to return to menu.", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)
        
        elif query.data.startswith('reply_msg_'):
            # Existing reply logic (requires accepted chat as well)
            target_id = query.data[len('reply_msg_'):]
            if not target_id or not target_id.isdigit():
                await query.answer("Invalid ID", show_alert=True)
                return
                
            check = (await db_fetch_one_async("""
                SELECT 1 FROM chat_requests 
                WHERE (sender_id = %s AND receiver_id = %s AND status = 'accepted')
                   OR (sender_id = %s AND receiver_id = %s AND status = 'accepted')
            """, (user_id, target_id, target_id, user_id)))
            pm_check = (await db_fetch_one_async("""
                SELECT 1 FROM private_messages 
                WHERE (sender_id = %s AND receiver_id = %s)
                   OR (sender_id = %s AND receiver_id = %s)
            """, (user_id, target_id, target_id, user_id)))
            
            if not check and not pm_check:
                await query.answer("No active chat permission.", show_alert=True)
                return

            set_state(context, STATE_AWAITING_PRIVATE_MESSAGE, private_message_target=target_id)
            target_user = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,)))
            await query.message.reply_text(f"*Replying to {target_user['anonymous_name']}*\n\nPlease send your text,voice or picturemessage:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_menu)

        elif query.data.startswith("reply_"):
            parts = query.data.split("_")
            if len(parts) == 3:
                post_id = int(parts[1])
                comment_id = int(parts[2])
                set_state(context, STATE_AWAITING_COMMENT, comment_post_id=post_id, comment_idx=comment_id)

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
                set_state(context, STATE_AWAITING_COMMENT, comment_post_id=post_id, comment_idx=comment_id)

                await query.message.reply_text(
                    "Please type your reply or send a voice message, GIF, or sticker:\n\nTap Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML
                )
        # Handle Previous Posts pagination
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

        # Handle Previous Posts button
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
        
        elif query.data.startswith('view_comment_'):
            try:
                comment_id = int(query.data.split('_')[2])
                comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (comment_id,)))
                
                if comment and comment['author_id'] == user_id:
                    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (comment['post_id'],)))
                    
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

        # Handle continue post (threading) - renamed from elaborate
        elif query.data.startswith("continue_post_"):
            post_id = int(query.data.split('_')[2])
            post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))
            
            if post and post['author_id'] == user_id:
                context.user_data['thread_from_post_id'] = post_id
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
            # Only users who have actually set a sex get the extra question; for
            # everyone else the flow is exactly what it was before.
            sex_row = (await db_fetch_one_async("SELECT sex FROM users WHERE user_id = %s", (user_id,)))
            user_sex = normalize_revealed_sex(sex_row['sex'] if sex_row else None)
            if user_sex:
                context.user_data['pending_sex_check'] = {
                    'content': pending['content'],
                    'category': pending['category'],
                    'media_type': pending.get('media_type', 'text'),
                    'media_id': pending.get('media_id'),
                    'thread_from_post_id': pending.get('thread_from_post_id'),
                    'explicit': explicit_flag,
                    'sex': user_sex,
                }
                sex_kb = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(SHOW_SEX_NO_LABEL, callback_data='post_sex_no'),
                        InlineKeyboardButton(SHOW_SEX_YES_LABEL, callback_data='post_sex_yes')
                    ]
                ])
                await query.message.reply_text(SHOW_SEX_QUESTION, reply_markup=sex_kb)
                return

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

        elif query.data in ('post_sex_yes', 'post_sex_no'):
            pending = context.user_data.get('pending_sex_check')
            if not pending:
                await query.answer("Post data not found. Please start over.", show_alert=True)
                return
            await query.answer()
            del context.user_data['pending_sex_check']

            # Remove the Yes/No buttons from the question message
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

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
                explicit=pending.get('explicit', False),
                revealed_sex=pending['sex'] if query.data == 'post_sex_yes' else None
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

            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            text, reply_markup, _ = (await asyncio.to_thread(build_thread_pick_content, user_id, page=1))
            if not reply_markup:
                await query.message.reply_text(
                    "You don't have any previous posts yet to thread from.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Back to Preview", callback_data='thread_pick_cancel')]
                    ])
                )
                return

            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            return

        elif query.data.startswith('threadpg_'):
            try:
                page = int(query.data[len('threadpg_'):])
            except ValueError:
                page = 1
            await query.answer()
            text, reply_markup, _ = (await asyncio.to_thread(build_thread_pick_content, user_id, page=page))
            if not reply_markup:
                # Page emptied out (e.g. a post got deleted between clicks) - fall back to page 1
                text, reply_markup, _ = (await asyncio.to_thread(build_thread_pick_content, user_id, page=1))
            if reply_markup:
                try:
                    await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                except BadRequest:
                    pass
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
                explicit=pending_post.get('explicit', False),
                revealed_sex=pending_post.get('revealed_sex')
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
                owned_post = (await db_fetch_one_async(
                    "SELECT post_id FROM posts WHERE post_id = %s AND author_id = %s AND approved = TRUE AND deleted = FALSE",
                    (candidate_id, user_id)
                ))
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
                explicit=pending_post.get('explicit', False),
                revealed_sex=pending_post.get('revealed_sex')
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
                revealed_sex = normalize_revealed_sex(pending_post.get('revealed_sex'))
                
                # Insert post (without 'category' column which was dropped)
                if thread_from_post_id:
                    post_row = (await db_execute_async(
                        "INSERT INTO posts (content, author_id, media_type, media_id, thread_from_post_id, explicit, revealed_sex) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id, thread_from_post_id, explicit_flag, revealed_sex),
                        fetchone=True
                    ))
                else:
                    post_row = (await db_execute_async(
                        "INSERT INTO posts (content, author_id, media_type, media_id, explicit, revealed_sex) VALUES (%s, %s, %s, %s, %s, %s) RETURNING post_id",
                        (post_content, user_id, media_type, media_id, explicit_flag, revealed_sex),
                        fetchone=True
                    ))
                
                if post_row:
                    post_id = post_row['post_id']
                    
                    # Insert categories into junction table
                    category_list = category.split(',') if category else []
                    for cat_code in category_list:
                        (await db_execute_async(
                            "INSERT INTO post_categories (post_id, category_code) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (post_id, cat_code.strip())
                        ))
                
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
            await show_pending_posts(update, context, page=1)

        elif query.data.startswith('admin_pending_page_'):
            try:
                page = int(query.data.split('_')[-1])
            except (IndexError, ValueError):
                page = 1
            await show_pending_posts(update, context, page=page)
            
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

        # Bulk delete of pending posts: per-post checkbox, then delete-selected or delete-all
        elif query.data.startswith('toggle_bulkdel_'):
            try:
                post_id = int(query.data.split('_')[-1])
                await toggle_bulk_delete_select(update, context, post_id)
            except ValueError:
                await query.answer("Invalid post ID", show_alert=True)
            except Exception as e:
                logger.error(f"Error in toggle_bulk_delete_select handler: {e}")
                await query.answer("Error toggling selection", show_alert=True)

        elif query.data == 'bulkdel_sel_confirm':
            await confirm_bulk_delete(update, context, 'selected')

        elif query.data == 'bulkdel_all_confirm':
            await confirm_bulk_delete(update, context, 'all')

        elif query.data == 'bulkdel_sel_execute':
            await execute_bulk_delete(update, context, 'selected')

        elif query.data == 'bulkdel_all_execute':
            await execute_bulk_delete(update, context, 'all')

        elif query.data == 'bulkdel_cancel':
            await show_pending_posts(update, context, page=1)

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

        elif query.data.startswith('edit_sent_msg_'):
            try:
                pm_id = int(query.data[len('edit_sent_msg_'):])
                await edit_sent_message_prompt(update, context, pm_id)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing edit_sent_msg: {e}")
                await query.answer("Error", show_alert=True)

        elif query.data.startswith('cancel_edit_sent_msg'):
            reset_state(context)
            await query.answer("Cancelled")
            try:
                await query.message.delete()
            except:
                pass

        elif query.data.startswith('confirm_delete_sent_msg_'):
            try:
                pm_id = int(query.data[len('confirm_delete_sent_msg_'):])
                await delete_sent_message(update, context, pm_id)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing confirm_delete_sent_msg: {e}")
                await query.answer("Error", show_alert=True)

        elif query.data.startswith('cancel_delete_sent_msg'):
            await query.answer("Kept")
            try:
                await query.message.delete()
            except:
                pass

        elif query.data.startswith('delete_sent_msg_'):
            try:
                pm_id = int(query.data[len('delete_sent_msg_'):])
                await delete_sent_message_prompt(update, context, pm_id)
            except (IndexError, ValueError) as e:
                logger.error(f"Error parsing delete_sent_msg: {e}")
                await query.answer("Error", show_alert=True)

        elif query.data == 'refresh_mini_app':
            await query.answer("Refreshing...")
            await mini_app_command(update, context)
        elif query.data == 'select_avatar':
            await show_avatar_selection(update, context, page=0)

        elif query.data.startswith('avatar_page_'):
            page = int(query.data.split('_')[2])
            await show_avatar_selection(update, context, page=page)

        elif query.data == 'noop':
            await query.answer()

        elif query.data.startswith('set_avatar_'):
            emoji = query.data.split('_', 2)[2]
            await db_update_user_async(user_id, avatar_emoji=emoji)
            await query.answer(f"Avatar set to {emoji}!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'clear_avatar':
            await db_update_user_async(user_id, avatar_emoji=None)
            await query.answer("Avatar removed!", show_alert=True)
            await send_updated_profile(user_id, query.message.chat.id, context)
            
        elif query.data == 'list_blocked':
            await query.answer("Loading blocked users...", show_alert=False)
            blocked = (await db_fetch_all_async(
                """SELECT u.user_id, u.anonymous_name, u.sex 
                FROM blocks b JOIN users u ON b.blocked_id = u.user_id 
                WHERE b.blocker_id = %s""",
                (user_id,)
            ))
            
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
            (await db_execute_async("DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s", (user_id, target_id)))
            
            # Clear Aura Cache for real-time accuracy
            calculate_user_rating.cache_clear()
            _leaderboard_cache_bust()
            format_aura.cache_clear()
            
            await query.answer("User unblocked!", show_alert=False)
            
            # Refresh view (either profiles or list)
            if "Blocked Users" in query.message.text:
                # If we are in the list, refresh the list
                blocked = (await db_fetch_all_async(
                    "SELECT u.user_id, u.anonymous_name, u.sex FROM blocks b JOIN users u ON b.blocked_id = u.user_id WHERE b.blocker_id = %s",
                    (user_id,)
                ))
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
            target_user = (await db_fetch_one_async("SELECT * FROM users WHERE user_id = %s", (target_id,)))
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
                (await db_execute_async(
                    "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)",
                    (user_id, target_id)
                ))
                
                # Clear Aura Cache for real-time accuracy
                calculate_user_rating.cache_clear()
                _leaderboard_cache_bust()
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
                post = (await db_fetch_one_async("SELECT post_id FROM posts WHERE post_id = %s", (post_id,)))
                if not post:
                    await query.answer("Post not found.", show_alert=True)
                    return
                # Show confirmation
                context.user_data['pending_report'] = {'type': 'post', 'id': post_id}
                keyboard = [
                    [InlineKeyboardButton("Yes, Report", callback_data=f"confirm_report_post_{post_id}")],
                    [InlineKeyboardButton("No, Cancel", callback_data="cancel_report")]
                ]
                await query.message.reply_text(
                    "⚠️ <b>Are you sure you want to report this post?</b>",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in report_post handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('report_comment_'):
            try:
                comment_id = int(query.data.split('_')[2])
                comment = (await db_fetch_one_async("SELECT comment_id FROM comments WHERE comment_id = %s", (comment_id,)))
                if not comment:
                    await query.answer("Comment not found.", show_alert=True)
                    return
                context.user_data['pending_report'] = {'type': 'comment', 'id': comment_id}
                keyboard = [
                    [InlineKeyboardButton("Yes, Report", callback_data=f"confirm_report_comment_{comment_id}")],
                    [InlineKeyboardButton("No, Cancel", callback_data="cancel_report")]
                ]
                await query.message.reply_text(
                    "⚠️ <b>Are you sure you want to report this comment?</b>",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in report_comment handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('report_cuser_') or query.data.startswith('report_user_'):
            try:
                if query.data.startswith('report_cuser_'):
                    # Resolved on the server so the commenter's identity is never put in the button
                    c_id = int(query.data[len('report_cuser_'):])
                    crow = (await db_fetch_one_async("SELECT author_id FROM comments WHERE comment_id = %s", (c_id,)))
                    target_uid = str(crow['author_id']) if crow else None
                else:
                    target_uid = query.data[len('report_user_'):]
                if not target_uid or not target_uid.isdigit():
                    await query.answer("User not found.", show_alert=True)
                    return
                if target_uid == str(user_id):
                    await query.answer("You can't report yourself.", show_alert=True)
                    return
                target_row = (await db_fetch_one_async("SELECT user_id, is_admin FROM users WHERE user_id = %s", (target_uid,)))
                if not target_row:
                    await query.answer("User not found.", show_alert=True)
                    return
                if target_row['is_admin']:
                    await query.answer("This user can't be reported.", show_alert=True)
                    return
                context.user_data['pending_report'] = {'type': 'user', 'id': int(target_uid)}
                keyboard = [
                    [InlineKeyboardButton("Yes, Report User", callback_data="confirm_report_user")],
                    [InlineKeyboardButton("No, Cancel", callback_data="cancel_report")]
                ]
                await query.message.reply_text(
                    "⚠️ <b>Are you sure you want to report this user?</b>\n\n"
                    "An admin will review the report and may warn or ban the user. "
                    "False reports can lead to action against your own account.",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in report_user handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data == 'confirm_report_user':
            try:
                pending = context.user_data.get('pending_report')
                if not pending or pending.get('type') != 'user':
                    await query.answer("This report expired. Tap Report User again.", show_alert=True)
                    return
                context.user_data['reporting'] = {'type': 'user', 'id': pending['id'], 'timestamp': time.time()}
                context.user_data.pop('pending_report', None)
                await query.message.reply_text(
                    "*Report User*\n\nPlease type a short reason for reporting this user (max 200 characters).\n\nTap Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in confirm_report_user handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('confirm_report_post_'):
            try:
                post_id = int(query.data.split('_')[3])  # confirm_report_post_<post_id>
                context.user_data['reporting'] = {'type': 'post', 'id': post_id, 'timestamp': time.time()}
                await query.message.reply_text(
                    "*Report Post*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in confirm_report_post handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data.startswith('confirm_report_comment_'):
            try:
                comment_id = int(query.data.split('_')[3])
                context.user_data['reporting'] = {'type': 'comment', 'id': comment_id, 'timestamp': time.time()}
                await query.message.reply_text(
                    "*Report Comment*\n\nPlease type a short reason for reporting this content (max 200 characters).\n\nTap Cancel to go back.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in confirm_report_comment handler: {e}", exc_info=True)
                await query.answer("Error processing request", show_alert=True)

        elif query.data == 'cancel_report':
            try:
                context.user_data.pop('pending_report', None)
                context.user_data.pop('reporting', None)
                await query.message.edit_text("Report cancelled.")
                await query.answer()
                return
            except Exception as e:
                logger.error(f"Error in cancel_report handler: {e}", exc_info=True)
                await query.answer("Report cancelled.")

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
                report = (await db_fetch_one_async("SELECT * FROM reports WHERE report_id = %s", (report_id,)))
                if not report:
                    await query.answer("Report not found.", show_alert=True)
                    return
                preview, author_id = (await asyncio.to_thread(get_report_content_preview, report['target_type'], report['target_id']))
                type_label = _report_type_label(report['target_type'])
                preview_text = html.escape(preview or '[Content deleted]')
                safe_reason = html.escape(report['reason'])
                reporter = (await db_fetch_one_async("SELECT anonymous_name FROM users WHERE user_id = %s", (report['reporter_id'],)))
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
                (await asyncio.to_thread(resolve_report, report_id, user_id, 'dismissed', None))
                await query.answer("Report dismissed.", show_alert=False)
                await show_admin_reports(update, context, page=1)
            except Exception as e:
                logger.error(f"Error in report_dismiss handler: {e}")
                await query.answer("Error dismissing report", show_alert=True)

        elif query.data.startswith('report_delete_'):
            try:
                report_id = int(query.data.split('_')[2])
                report = (await db_fetch_one_async("SELECT * FROM reports WHERE report_id = %s", (report_id,)))
                if not report:
                    await query.answer("Report not found.", show_alert=True)
                    return
        
                target_type = report['target_type']
                target_id = report['target_id']
                author_id = None
        
                if target_type == 'post':
                    # ---------- DELETE POST ----------
                    post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (target_id,)))
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
                    (await db_execute_async("DELETE FROM reactions WHERE comment_id IN (SELECT comment_id FROM comments WHERE post_id = %s)", (target_id,)))
                    (await db_execute_async("DELETE FROM comments WHERE post_id = %s", (target_id,)))
                    (await db_execute_async("DELETE FROM post_categories WHERE post_id = %s", (target_id,)))
                    # 3. Delete the post itself, verify it's gone
                    deleted = (await db_execute_async("DELETE FROM posts WHERE post_id = %s RETURNING post_id", (target_id,), fetchone=True))
                    calculate_user_rating.cache_clear()
                    _leaderboard_cache_bust()
                    if not deleted:
                        raise Exception("Post deletion from database failed (no rows returned)")
        
                    author_id = post.get('author_id')
                    logger.info(f"Post {target_id} deleted by admin {user_id}")
        
                elif target_type == 'comment':
                    # ---------- DELETE COMMENT ----------
                    comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (target_id,)))
                    if not comment:
                        await query.answer("Comment already deleted.", show_alert=True)
                        return
        
                    post_id = comment['post_id']
                    # 1. Re‑parent child comments to top level
                    (await db_execute_async("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (target_id,)))
                    # 2. Delete reactions and the comment itself
                    (await db_execute_async("DELETE FROM reactions WHERE comment_id = %s", (target_id,)))
                    deleted = (await db_execute_async("DELETE FROM comments WHERE comment_id = %s RETURNING comment_id", (target_id,), fetchone=True))
                    calculate_user_rating.cache_clear()
                    _leaderboard_cache_bust()
                    if not deleted:
                        raise Exception("Comment deletion from database failed (no rows returned)")
        
                    # 3. Update comment count and channel button
                    await adopt_orphaned_replies(context, post_id)
        
                    author_id = comment.get('author_id')
                    logger.info(f"Comment {target_id} deleted by admin {user_id}")
        
                else:
                    await query.answer("This is a user report, so there is no content to delete. Use Warn User or Moderate author.", show_alert=True)
                    return
        
                # ---------- AFTER DELETION: update report, clear caches, notify author ----------
                (await asyncio.to_thread(resolve_report, report_id, user_id, 'action_taken', 'deleted'))
        
                # Clear aura caches (important for leaderboard updates)
                calculate_user_rating.cache_clear()
                _leaderboard_cache_bust()
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
                report = (await db_fetch_one_async("SELECT * FROM reports WHERE report_id = %s", (report_id,)))
                if not report:
                    await query.answer("Report not found.", show_alert=True)
                    return
                _, author_id = (await asyncio.to_thread(get_report_content_preview, report['target_type'], report['target_id']))
                (await asyncio.to_thread(resolve_report, report_id, user_id, 'action_taken', 'warned'))
                if author_id:
                    ok, _msg = await mod_warn_user(
                        context, author_id, user_id,
                        f"Reported content (report #{report_id}). Please follow the community guidelines."
                    )
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
    user = (await db_fetch_one_async("SELECT is_admin FROM users WHERE user_id = %s", (user_id,)))
    if not user or not user['is_admin']:
        if update.message:
            await update.message.reply_text("You don't have permission to access this.")
        elif update.callback_query:
            await update.callback_query.message.reply_text("You don't have permission to access this.")
        return
    
    stats = (await db_fetch_one_async('''
        SELECT 
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM posts WHERE approved = TRUE) as approved_posts,
            (SELECT COUNT(*) FROM posts WHERE approved = FALSE) as pending_posts,
            (SELECT COUNT(*) FROM comments) as total_comments,
            (SELECT COUNT(*) FROM private_messages) as total_messages
    '''))
    
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
    # Cached, narrow accessor instead of a full-row SELECT on every inbound message
    # (invalidated by db_update_user).
    user = await asyncio.to_thread(get_user_cached, user_id)
    

    # Handle cancel command or main menu buttons while in an input state
    main_menu_buttons = ["Share", "Chat Requests", "Profile", "Posts", "Top", "Settings", "Open App", "❌ Cancel", "/cancel"]
    
    if text in main_menu_buttons or text.lower() in ("cancel", "❌ cancel"):
        # UNCONDITIONALLY reset all waiting states when a menu button is pressed
        # We pass None for chat_id to reset quietly, as we'll send the specific menu next
        await reset_user_waiting_states(user_id, None, context)
        
        # Early exit for explicit cancellation
        if text in ["❌ Cancel", "/cancel"] or text.lower() in ("cancel", "❌ cancel"):
            await update.message.reply_text(
                "Input cancelled.",
                reply_markup=get_main_menu(user_id)
            )
            return
        
        # For other main menu buttons (e.g. "Share"), we fall through 
        # so the handlers below can process the command with a clean state.

    # Handle rejection reason capture from admin
    if context.user_data.get('awaiting_rejection_reason'):
        # Guard intentionally kept: reset_state() does not clear this flag.
        if text in main_menu_buttons: return
        post_id = context.user_data.get('rejecting_post')
        if post_id:
            logger.info(f"Admin {user_id} providing reason for post {post_id}")
            await finalize_rejection(update, context, post_id, reason=text)
            return

    # Handle report reason capture from user
    # IMPORTANT: comment flow always wins. If the user is mid-comment
    # (state is STATE_AWAITING_COMMENT) a lingering 'reporting' state
    # must NOT hijack their message — fall through and let the
    # awaiting-comment branch further down handle it instead.
    if context.user_data.get('reporting') and get_state(context) != STATE_AWAITING_COMMENT:
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

            report_id = (await asyncio.to_thread(create_report, user_id, target_type, target_id, reason))

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

    # Handle comment editing

    if 'editing_comment' in context.user_data:
        comment_id = context.user_data['editing_comment']
        comment = (await db_fetch_one_async("SELECT * FROM comments WHERE comment_id = %s", (comment_id,)))
        
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
            (await db_execute_async(
                "UPDATE comments SET content = %s WHERE comment_id = %s",
                (cleaned_text, comment_id)
            ))
            
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


    if 'editing_post' in context.user_data and context.user_data['editing_post']:
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
                explicit=pending_post.get('explicit', False),
                revealed_sex=pending_post.get('revealed_sex')
            )
            return
        else:
            del context.user_data['editing_post']
            await update.message.reply_text(
                "No pending post found. Please start over.",
                reply_markup=get_main_menu(user_id)
            )


            return

    # Handle editing of an already-published (approved) post's content
    # (see the edit_published_<post_id> callback in button_handler)
    if get_state(context) == STATE_AWAITING_EDIT_CONTENT and context.user_data.get('editing_published_post'):
        if text in main_menu_buttons: return

        post_id = context.user_data['editing_published_post']
        post = (await db_fetch_one_async("SELECT * FROM posts WHERE post_id = %s", (post_id,)))

        # Re-verify the post still exists and still belongs to this user
        if not post or post['author_id'] != user_id:
            clear_edit_published_state(context)
            await update.message.reply_text(
                "Error updating post. Please try again.",
                reply_markup=get_main_menu(user_id)
            )
            return

        # Guard against users accidentally pasting our own "copy the text below"
        # instruction along with the content they meant to edit.
        cleaned_text, was_cleaned = sanitize_pasted_edit(text)
        if was_cleaned:
            await update.message.reply_text(
                "Looks like our copy instructions got pasted in too — I've trimmed those out."
            )

        # Update the post's content in the database first, so the edit is
        # never lost even if the channel message update below fails.
        (await db_execute_async(
            "UPDATE posts SET content = %s WHERE post_id = %s",
            (cleaned_text, post_id)
        ))

        # If the post is live in the channel, keep the published message in sync.
        # Text posts get their message text updated; media posts only have their
        # caption updated - the underlying media file is left untouched.
        channel_update_ok = True
        if post.get('channel_message_id'):
            try:
                # Same vent-number formatting as approve_post
                vent_display = f"Vent - {post['vent_number']:03d}" if post.get('vent_number') else f"Post #{post_id}"

                # Same categories/hashtags construction as approve_post
                cats_row = (await db_fetch_all_async("SELECT category_code FROM post_categories WHERE post_id = %s", (post_id,)))
                categories = [row['category_code'] for row in cats_row]
                hashtags = ' '.join([f"#{cat}" for cat in categories]) if categories else "#Other"
                safe_hashtags = html.escape(hashtags)

                # Explicit posts keep their content hidden behind "View Post",
                # exactly as they're shown when first approved
                if post.get('explicit'):
                    body_html = EXPLICIT_WARNING_HTML
                else:
                    body_html = html.escape(cleaned_text)

                # Same channel text construction as in approve_post, using the new content
                channel_text = (
                    f"{vent_header_html(vent_display, post.get('revealed_sex'))}\n\n"
                    f"{body_html}\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{safe_hashtags}\n"
                    f"<a href='https://t.me/christianvent'>Telegram</a> | <a href='https://t.me/{BOT_USERNAME}'>Bot</a>"
                )

                channel_keyboard = build_channel_post_keyboard(
                    post_id, post.get('comment_count') or 0, post.get('explicit', False)
                )

                if post.get('media_type', 'text') == 'text':
                    await context.bot.edit_message_text(
                        chat_id=CHANNEL_ID,
                        message_id=post['channel_message_id'],
                        text=channel_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=channel_keyboard,
                        disable_web_page_preview=True
                    )
                else:
                    # Media post: only the caption changes, the media itself stays unchanged
                    await context.bot.edit_message_caption(
                        chat_id=CHANNEL_ID,
                        message_id=post['channel_message_id'],
                        caption=channel_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=channel_keyboard
                    )
            except Exception as e:
                # DB is already updated above; log and let the user know the
                # channel copy may briefly lag behind rather than losing their edit.
                logger.error(f"Error updating channel message for edited post {post_id}: {e}")
                channel_update_ok = False

        # Clear the editing state now that the update attempt is complete
        clear_edit_published_state(context)

        if channel_update_ok:
            await update.message.reply_text(
                "Your post has been updated successfully!",
                reply_markup=get_main_menu(user_id)
            )
        else:
            await update.message.reply_text(
                "Your post was updated, but the published channel message couldn't be "
                "refreshed automatically. Please contact an admin if it doesn't update shortly.",
                reply_markup=get_main_menu(user_id)
            )
        return

    # If user doesn't exist, create them
    # only create user if not exists
    if not user:
        anon = create_anonymous_name(user_id)
        is_admin = str(user_id) == str(ADMIN_ID)
        await db_execute_async(
            "INSERT INTO users (user_id, anonymous_name, sex, is_admin) VALUES (%s, %s, %s, %s)",
            (user_id, anon, '👤', is_admin)
        )
        user = await asyncio.to_thread(get_user_cached, user_id)

    # Check if we have a thread_from_post_id for continuation
    thread_from_post_id = context.user_data.get('thread_from_post_id')

    state = get_state(context)

    if state == STATE_AWAITING_POST:
        category = context.user_data.get('selected_categories')

        if not category:
            await update.message.reply_text("No categories selected. Please start over.", reply_markup=get_main_menu(user_id))
            reset_state(context)
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

            
            # Reset flow state for BOTH text and media posts before handing off
            # to the explicit-content confirmation (a callback-driven step, so no
            # further text state should be "waiting" for the interim).
            reset_state(context)

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
                    InlineKeyboardButton("የለውም", callback_data='post_explicit_no'),
                    InlineKeyboardButton("አዎ", callback_data='post_explicit_yes')
                ]
            ])
            await update.message.reply_text(
                "⚠️ ይህ ፖስት ወሲባዊ ወይም ለሁሉም እድሜ የማይሆን ይዘት አለው?",
                reply_markup=explicit_kb
            )
            return
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            await update.message.reply_text(
                "Error processing your media. Please try again.",
                reply_markup=get_main_menu(user_id)

            )
            # Reset state on error
            reset_state(context)
            return

    elif state == STATE_AWAITING_COMMENT:
        post_id = context.user_data.get('comment_post_id')
    
        parent_comment_id = 0
        if context.user_data.get('comment_idx'):
            try:
                parent_comment_id = int(context.user_data['comment_idx'])
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
        new_comment_row = await db_execute_async(
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
        _leaderboard_cache_bust()
        format_aura.cache_clear()

    
        # Reset state
        reset_state(context)
    
        await update.message.reply_text("Your comment has been posted!", reply_markup=get_main_menu(user_id))

        # Update comment count in background
        asyncio.create_task(update_channel_post_comment_count(context, post_id))
        
        # Notify vent author if this is a top‑level comment
        if parent_comment_id == 0:
            await notify_vent_author_of_comment(context, post_id, user_id, new_comment_id, comment_content=content, comment_type=comment_type, media_id=file_id)
        
        # Notify parent comment author if this is a reply
        if parent_comment_id != 0:
            await notify_user_of_reply(context, post_id, parent_comment_id, user_id, new_comment_id, comment_content=content, comment_type=comment_type, media_id=file_id)
            await notify_post_author_of_thread_reply(context, post_id, parent_comment_id, user_id, comment_content=content, comment_type=comment_type, media_id=file_id)
        return

    elif state == STATE_AWAITING_PM_EDIT:
        pm_id = context.user_data.get('editing_pm_id')
        new_content = (text or "").strip()

        if not pm_id:
            reset_state(context)
            await update.message.reply_text("Nothing to edit. Please try again.", reply_markup=get_main_menu(user_id))
            return

        if not new_content:
            await update.message.reply_text("Message can't be empty. Send the new text, or type Cancel.")
            return

        msg = (await db_fetch_one_async(
            "SELECT sender_id, receiver_id, is_deleted, media_type, media_id, notif_message_id "
            "FROM private_messages WHERE message_id = %s",
            (pm_id,)
        ))
        reset_state(context)

        if not msg or str(msg['sender_id']) != str(user_id):
            await update.message.reply_text("That message is no longer available to edit.", reply_markup=get_main_menu(user_id))
            return
        if msg.get('is_deleted'):
            await update.message.reply_text("That message was deleted, so it can't be edited.", reply_markup=get_main_menu(user_id))
            return

        (await db_execute_async(
            "UPDATE private_messages SET content = %s, is_edited = TRUE, edited_at = CURRENT_TIMESTAMP WHERE message_id = %s",
            (new_content, pm_id)
        ))

        # Reflect the edit live in the receiver's chat using Telegram's native
        # edit, instead of the change only existing in our own DB copy.
        if msg.get('notif_message_id'):
            await edit_native_pm_notification(
                context,
                receiver_id=msg['receiver_id'],
                notif_message_id=msg['notif_message_id'],
                sender_id=user_id,
                new_content=new_content,
                media_type=msg.get('media_type'),
                media_id=msg.get('media_id')
            )

        await update.message.reply_text("Message updated.", reply_markup=get_main_menu(user_id))
        return

    elif state == STATE_AWAITING_PRIVATE_MESSAGE:
        target_id = context.user_data.get('private_message_target')
        
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
        is_blocked = await db_fetch_one_async(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (target_id, user_id)
        )
        
        if is_blocked:
            await update.message.reply_text(
                "You cannot send messages to this user. They have blocked you.",
                reply_markup=get_main_menu(user_id)
            )
            reset_state(context)
            return
        
        # Save message
        message_row = await db_execute_async(
            "INSERT INTO private_messages (sender_id, receiver_id, content, media_type, media_id) VALUES (%s, %s, %s, %s, %s) RETURNING message_id",
            (user_id, target_id, message_content, media_type, media_id),
            fetchone=True
        )
        
        # Reset state
        reset_state(context)
        
        # Notify receiver
        await notify_user_of_private_message(context, user_id, target_id, message_content, message_row['message_id'] if message_row else None)
        
        await update.message.reply_text(
            "Your message has been sent!",
            reply_markup=get_main_menu(user_id)
        )

        if message_row:
            sent_id = message_row['message_id']
            await update.message.reply_text(
                "Changed your mind?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Edit", callback_data=f"edit_sent_msg_{sent_id}"),
                    InlineKeyboardButton("Delete", callback_data=f"delete_sent_msg_{sent_id}")
                ]])
            )

        return

    if state == STATE_AWAITING_NAME:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            await db_update_user_async(user_id, anonymous_name=new_name)
            reset_state(context)
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
        
    if state == STATE_AWAITING_BIO:
        if not text:
            await update.message.reply_text("Bio must be text. Please try again.")
            return
            
        if len(text) > 200:
             await update.message.reply_text("Bio is too long (max 200 chars). Please shorten it.")
             return
             
        await db_update_user_async(user_id, bio=text)
        reset_state(context)
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

    user = (await db_fetch_one_async(
        "SELECT waiting_for_private_message, private_message_target FROM users WHERE user_id = %s",
        (user_id,)
    ))

    if not user or not user["waiting_for_private_message"]:
        return  # Not replying to a private message

    receiver_id = user["private_message_target"]

    # Prevent sending message to self
    if receiver_id == user_id:
        await update.message.reply_text("You cannot message yourself.")
        return

    # Save message
    msg = (await db_execute_async(
        """
        INSERT INTO private_messages (sender_id, receiver_id, content)
        VALUES (%s, %s, %s)
        RETURNING message_id
        """,
        (user_id, receiver_id, text),
        fetchone=True
    ))

    # Reset reply state
    (await db_execute_async(
        """
        UPDATE users
        SET waiting_for_private_message = FALSE,
            private_message_target = NULL
        WHERE user_id = %s
        """,
        (user_id,)
    ))

    # Notify receiver
    await notify_user_of_private_message(
        context,
        sender_id=user_id,
        receiver_id=receiver_id,
        message_content=text,
        message_id=msg["message_id"]
    )

    await update.message.reply_text("Message sent!")

    if msg:
        sent_id = msg["message_id"]
        await update.message.reply_text(
            "Changed your mind?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Edit", callback_data=f"edit_sent_msg_{sent_id}"),
                InlineKeyboardButton("Delete", callback_data=f"delete_sent_msg_{sent_id}")
            ]])
        )

async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True) 

from telegram import BotCommand 

async def set_bot_commands(app):
    # asyncio.to_thread() uses the loop's default executor, which is only min(32, cpu+4) threads
    # (as low as 5 on a small Render instance) - too few now that every DB call goes through it.
    try:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=_DB_MAX_CONNECTIONS + 12, thread_name_prefix="bot-worker")
        )
    except Exception as e:
        logger.error(f"Could not enlarge the default executor: {e}")
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

# ==================== MODERATION: BAN / WARN / UNBAN ====================
MAX_WARNINGS_BEFORE_BAN = 3        # auto-ban at this many warnings (0 = never auto-ban)
UNBAN_RESETS_WARNINGS = True       # unbanned users start with a clean slate
MOD_REASON_MAX_LEN = 300
MOD_PENDING_TIMEOUT_SECONDS = 300  # how long the bot waits for a typed reason
_MOD_MENU_BUTTONS = {"Share", "Chat Requests", "Profile", "Posts", "Top", "Settings",
                     "Open App", "❌ Cancel", "/cancel"}


def _mod_clean_reason(text):
    return (text or "").strip()[:MOD_REASON_MAX_LEN]


def _mod_fmt_dt(value):
    try:
        return value.strftime('%b %d, %Y')
    except Exception:
        return str(value or '')


async def mod_is_admin(user_id) -> bool:
    row = await asyncio.to_thread(get_user_cached, str(user_id))
    return bool(row and row.get('is_admin'))


async def mod_get_target(target_id):
    return await db_fetch_one_async(
        "SELECT user_id, anonymous_name, avatar_emoji, is_admin, is_banned, ban_reason, "
        "banned_at, warning_count FROM users WHERE user_id = %s",
        (str(target_id),)
    )


async def mod_ban_user(context, target_id, admin_id, reason=None):
    """Returns (ok: bool, html_message: str)."""
    target_id = str(target_id)
    if target_id == str(admin_id):
        return False, "You can't ban yourself."
    target = await mod_get_target(target_id)
    if not target:
        return False, "User not found."
    if target['is_admin']:
        return False, "Admins can't be banned."
    if target['is_banned']:
        return False, "That user is already banned."

    reason = _mod_clean_reason(reason) or None
    await db_execute_async(
        "UPDATE users SET is_banned = TRUE, ban_reason = %s, banned_at = CURRENT_TIMESTAMP, "
        "banned_by = %s WHERE user_id = %s",
        (reason, str(admin_id), target_id)
    )
    _invalidate_user_cache(target_id)
    logger.info(f"Admin {admin_id} banned user {target_id} (reason: {reason})")
    try:
        await db_execute_async(
            "UPDATE reports SET status = 'action_taken', reviewed_by = %s, reviewed_at = NOW(), "
            "action_taken = 'banned' WHERE target_type = 'user' AND target_id = %s AND status = 'pending'",
            (str(admin_id), int(target_id))
        )
    except Exception as e:
        logger.warning(f"Could not auto-resolve user reports for {target_id}: {e}")

    notice = "🚫 <b>You have been banned from this bot.</b>"
    if reason:
        notice += f"\n\n<b>Reason:</b> {html.escape(reason)}"
    try:
        await context.bot.send_message(chat_id=target_id, text=notice, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Could not notify banned user {target_id}: {e}")

    name = html.escape(get_display_name(target))
    msg = f"🚫 Banned <b>{name}</b> (<code>{target_id}</code>)."
    if reason:
        msg += f"\nReason: {html.escape(reason)}"
    return True, msg


async def mod_unban_user(context, target_id, admin_id):
    target_id = str(target_id)
    target = await mod_get_target(target_id)
    if not target:
        return False, "User not found."
    if not target['is_banned']:
        return False, "That user isn't banned."

    warn_reset = ", warning_count = 0" if UNBAN_RESETS_WARNINGS else ""
    await db_execute_async(
        "UPDATE users SET is_banned = FALSE, ban_reason = NULL, banned_at = NULL, "
        f"banned_by = NULL{warn_reset} WHERE user_id = %s",
        (target_id,)
    )
    _invalidate_user_cache(target_id)
    logger.info(f"Admin {admin_id} unbanned user {target_id}")

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="✅ <b>Your ban has been lifted.</b> You can use the bot again. "
                 "Please follow the community guidelines.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.warning(f"Could not notify unbanned user {target_id}: {e}")

    name = html.escape(get_display_name(target))
    return True, f"✅ Unbanned <b>{name}</b> (<code>{target_id}</code>)."


async def mod_warn_user(context, target_id, admin_id, reason):
    target_id = str(target_id)
    reason = _mod_clean_reason(reason)
    if not reason:
        return False, "A reason is required for a warning."
    if target_id == str(admin_id):
        return False, "You can't warn yourself."
    target = await mod_get_target(target_id)
    if not target:
        return False, "User not found."
    if target['is_admin']:
        return False, "Admins can't be warned."
    if target['is_banned']:
        return False, "That user is already banned."

    await db_execute_async(
        "INSERT INTO user_warnings (user_id, admin_id, reason) VALUES (%s, %s, %s)",
        (target_id, str(admin_id), reason)
    )
    row = await db_execute_async(
        "UPDATE users SET warning_count = COALESCE(warning_count, 0) + 1 "
        "WHERE user_id = %s RETURNING warning_count",
        (target_id,), fetchone=True
    )
    _invalidate_user_cache(target_id)
    count = int(row['warning_count']) if row else 1
    logger.info(f"Admin {admin_id} warned user {target_id} ({count}): {reason}")

    limit_txt = f"{count}/{MAX_WARNINGS_BEFORE_BAN}" if MAX_WARNINGS_BEFORE_BAN else str(count)
    notice = f"⚠️ <b>Warning from admin</b>\n\n<b>Reason:</b> {html.escape(reason)}\n\nWarnings: {limit_txt}"
    if MAX_WARNINGS_BEFORE_BAN:
        notice += f"\nReaching {MAX_WARNINGS_BEFORE_BAN} warnings results in a ban."
    try:
        await context.bot.send_message(chat_id=target_id, text=notice, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Could not notify warned user {target_id}: {e}")

    name = html.escape(get_display_name(target))
    msg = f"⚠️ Warned <b>{name}</b> (<code>{target_id}</code>), warnings: {limit_txt}.\nReason: {html.escape(reason)}"

    if MAX_WARNINGS_BEFORE_BAN and count >= MAX_WARNINGS_BEFORE_BAN:
        ok, ban_msg = await mod_ban_user(
            context, target_id, admin_id,
            f"Reached {MAX_WARNINGS_BEFORE_BAN} warnings (last: {reason})"
        )
        if ok:
            msg += "\n\n🚫 <b>Auto-banned</b> for reaching the warning limit."
    return True, msg


# ---------- screens (each returns (text, InlineKeyboardMarkup)) ----------

def mod_menu_content():
    text = (
        "<b>🛡 Moderation</b>\n\n"
        "<b>Commands</b>\n"
        "/ban &lt;user_id&gt; [reason]\n"
        "/warn &lt;user_id&gt; &lt;reason&gt;\n"
        "/unban &lt;user_id&gt;\n"
        "/warnings &lt;user_id&gt;\n"
        "/user &lt;user_id&gt;\n"
        "/banned\n\n"
        "You can also tap <b>🛡 Moderate author</b> under a pending post or a report."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Banned users", callback_data="mod_list_1")],
        [InlineKeyboardButton("« Admin Panel", callback_data="admin_panel")]
    ])
    return text, kb


async def mod_banned_content(page=1):
    per_page = 8
    page = max(1, page)
    offset = (page - 1) * per_page
    rows = await db_fetch_all_async(
        """SELECT user_id, anonymous_name, avatar_emoji, ban_reason,
                  COUNT(*) OVER () AS total_count
           FROM users WHERE is_banned = TRUE
           ORDER BY banned_at DESC NULLS LAST LIMIT %s OFFSET %s""",
        (per_page, offset)
    )
    back = [InlineKeyboardButton("« Moderation", callback_data="mod_menu")]
    if not rows:
        return "<b>🚫 Banned users</b>\n\nNo banned users.", InlineKeyboardMarkup([back])

    total = int(rows[0]['total_count'])
    total_pages = max(1, (total + per_page - 1) // per_page)
    lines = [f"<b>🚫 Banned users</b> ({total}), page {page}/{total_pages}\n"]
    kb = []
    for r in rows:
        name = html.escape(get_display_name(r))
        reason = html.escape((r.get('ban_reason') or 'no reason')[:60])
        lines.append(f"• <b>{name}</b> (<code>{r['user_id']}</code>): {reason}")
        kb.append([InlineKeyboardButton(f"{get_display_name(r)}"[:40], callback_data=f"mod_user_{r['user_id']}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"mod_list_{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"mod_list_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(back)
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def mod_user_panel(target_id):
    t = await mod_get_target(target_id)
    back = [InlineKeyboardButton("« Moderation", callback_data="mod_menu")]
    if not t:
        return "User not found.", InlineKeyboardMarkup([back])

    name = html.escape(get_display_name(t))
    warns = int(t['warning_count'] or 0)
    warns_txt = f"{warns}/{MAX_WARNINGS_BEFORE_BAN}" if MAX_WARNINGS_BEFORE_BAN else str(warns)
    if t['is_admin']:
        status = "🛡 Administrator"
    elif t['is_banned']:
        status = "🚫 Banned"
    else:
        status = "✅ Active"

    lines = [f"<b>{name}</b>", f"ID: <code>{t['user_id']}</code>", f"Status: {status}", f"Warnings: {warns_txt}"]
    if t['is_banned']:
        lines.append(f"Ban reason: {html.escape(t['ban_reason'] or 'none given')}")
        if t.get('banned_at'):
            lines.append(f"Banned on: {_mod_fmt_dt(t['banned_at'])}")

    uid = t['user_id']
    kb = []
    if not t['is_admin']:
        second = (InlineKeyboardButton("✅ Unban", callback_data=f"mod_unban_{uid}") if t['is_banned']
                  else InlineKeyboardButton("🚫 Ban", callback_data=f"mod_ban_{uid}"))
        row = []
        if not t['is_banned']:
            row.append(InlineKeyboardButton("⚠️ Warn", callback_data=f"mod_warn_{uid}"))
        row.append(second)
        kb.append(row)
        kb.append([InlineKeyboardButton("📜 Warning history", callback_data=f"mod_hist_{uid}")])
    kb.append(back)
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def mod_history_content(target_id):
    rows = await db_fetch_all_async(
        "SELECT reason, created_at FROM user_warnings WHERE user_id = %s "
        "ORDER BY created_at DESC LIMIT 10",
        (str(target_id),)
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"mod_user_{target_id}")]])
    if not rows:
        return f"<b>Warning history</b> (<code>{target_id}</code>)\n\nNo warnings on record.", kb
    lines = [f"<b>Warning history</b> (<code>{target_id}</code>), latest 10\n"]
    for r in rows:
        lines.append(f"• {_mod_fmt_dt(r['created_at'])}: {html.escape(r['reason'])}")
    return "\n".join(lines), kb


async def _mod_show(query, text, kb):
    """Edit the current message; if it can't be edited (e.g. a photo post), send a new one."""
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ---------- inline-button handler (callback data starts with "mod_") ----------

async def moderation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = str(query.from_user.id)
    if not await mod_is_admin(admin_id):
        await query.answer("You don't have permission to do this.", show_alert=True)
        return
    await query.answer()
    data = query.data

    try:
        if data == 'mod_menu':
            text, kb = mod_menu_content()
            await _mod_show(query, text, kb)

        elif data.startswith('mod_list_'):
            page = int(data.split('_')[2]) if data.split('_')[2].isdigit() else 1
            text, kb = await mod_banned_content(page)
            await _mod_show(query, text, kb)

        elif data.startswith('mod_user_'):
            text, kb = await mod_user_panel(data[len('mod_user_'):])
            await _mod_show(query, text, kb)

        elif data.startswith('mod_hist_'):
            text, kb = await mod_history_content(data[len('mod_hist_'):])
            await _mod_show(query, text, kb)

        elif data.startswith('mod_rep_'):
            rep = await db_fetch_one_async(
                "SELECT target_type, target_id FROM reports WHERE report_id = %s",
                (int(data[len('mod_rep_'):]),)
            )
            author_id = None
            if rep:
                _, author_id = await asyncio.to_thread(get_report_content_preview, rep['target_type'], rep['target_id'])
            if not author_id:
                await query.message.reply_text("The reported content no longer exists, so its author can't be resolved.")
                return
            text, kb = await mod_user_panel(author_id)
            await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith('mod_post_'):
            row = await db_fetch_one_async(
                "SELECT author_id FROM posts WHERE post_id = %s", (int(data[len('mod_post_'):]),)
            )
            if not row:
                await query.message.reply_text("That post no longer exists.")
                return
            text, kb = await mod_user_panel(row['author_id'])
            await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith('mod_warn_') or data.startswith('mod_ban_'):
            is_warn = data.startswith('mod_warn_')
            target = data.split('_', 2)[2]
            context.user_data['mod_pending'] = {
                'action': 'warn' if is_warn else 'ban',
                'target': target,
                'ts': time.time()
            }
            rows = []
            if not is_warn:
                rows.append([InlineKeyboardButton("Ban without a reason", callback_data=f"mod_skip_{target}")])
            rows.append([InlineKeyboardButton("Cancel", callback_data=f"mod_user_{target}")])
            prompt = ("Type the <b>reason for the warning</b> and send it as a message."
                      if is_warn else
                      "Type the <b>reason for the ban</b> (the user will see it), or tap the button to skip.")
            await query.message.reply_text(
                f"{prompt}\n\nUser: <code>{target}</code>",
                reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
            )

        elif data.startswith('mod_skip_'):
            target = data[len('mod_skip_'):]
            context.user_data.pop('mod_pending', None)
            ok, msg = await mod_ban_user(context, target, admin_id, None)
            await query.message.reply_text(
                msg, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View user", callback_data=f"mod_user_{target}")]])
            )

        elif data.startswith('mod_unban_'):
            target = data[len('mod_unban_'):]
            ok, msg = await mod_unban_user(context, target, admin_id)
            text, kb = await mod_user_panel(target)
            await _mod_show(query, (msg + "\n\n" + text) if ok else (f"⚠️ {msg}\n\n" + text), kb)

    except Exception as e:
        logger.error(f"moderation_callback error ({data}): {e}", exc_info=True)
        try:
            await query.message.reply_text("Something went wrong. Please try again.")
        except Exception:
            pass


# ---------- typed reason capture (runs before the normal message handler) ----------

async def mod_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get('mod_pending')
    if not pending or not update.message or update.message.text is None:
        return  # not ours: fall through to the normal handlers

    text = update.message.text.strip()
    admin_id = str(update.effective_user.id)

    if time.time() - pending.get('ts', 0) > MOD_PENDING_TIMEOUT_SECONDS:
        context.user_data.pop('mod_pending', None)
        return
    if text in _MOD_MENU_BUTTONS or text.lower() in ("cancel", "❌ cancel"):
        context.user_data.pop('mod_pending', None)
        return  # let the normal flow show "Input cancelled" / open the menu item
    if not await mod_is_admin(admin_id):
        context.user_data.pop('mod_pending', None)
        return

    context.user_data.pop('mod_pending', None)
    if pending['action'] == 'warn':
        ok, msg = await mod_warn_user(context, pending['target'], admin_id, text)
    else:
        ok, msg = await mod_ban_user(context, pending['target'], admin_id, text)
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View user", callback_data=f"mod_user_{pending['target']}")]])
    )
    raise ApplicationHandlerStop  # don't let the normal message handler see this text


# ---------- slash commands ----------

async def _mod_command_guard(update: Update):
    if not await mod_is_admin(update.effective_user.id):
        await update.message.reply_text("You don't have permission to use this command.")
        return False
    return True


def _mod_parse_args(context):
    args = context.args or []
    if not args or not args[0].isdigit():
        return None, ""
    return args[0], " ".join(args[1:]).strip()


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    target, reason = _mod_parse_args(context)
    if not target:
        await update.message.reply_text("Usage: /ban <user_id> [reason]")
        return
    ok, msg = await mod_ban_user(context, target, str(update.effective_user.id), reason)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    target, _ = _mod_parse_args(context)
    if not target:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    ok, msg = await mod_unban_user(context, target, str(update.effective_user.id))
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    target, reason = _mod_parse_args(context)
    if not target or not reason:
        await update.message.reply_text("Usage: /warn <user_id> <reason>")
        return
    ok, msg = await mod_warn_user(context, target, str(update.effective_user.id), reason)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def warnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    target, _ = _mod_parse_args(context)
    if not target:
        await update.message.reply_text("Usage: /warnings <user_id>")
        return
    text, kb = await mod_history_content(target)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    target, _ = _mod_parse_args(context)
    if not target:
        await update.message.reply_text("Usage: /user <user_id>")
        return
    text, kb = await mod_user_panel(target)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    text, kb = await mod_banned_content(1)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def mod_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _mod_command_guard(update):
        return
    text, kb = mod_menu_content()
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ---------- enforcement ----------

async def ban_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler. Banned non-admins get a notice and nothing else runs."""
    user = update.effective_user
    if not user:
        return
    row = await asyncio.to_thread(get_user_cached, str(user.id))
    if not row or not row.get('is_banned') or row.get('is_admin'):
        return

    reason = row.get('ban_reason')
    text = "🚫 You are banned from this bot."
    if reason:
        text += f"\nReason: {reason}"
    try:
        if update.callback_query:
            await update.callback_query.answer("🚫 You are banned from this bot.", show_alert=True)
        elif update.message:
            await update.message.reply_text(text)
    except Exception:
        pass
    raise ApplicationHandlerStop


@flask_app.before_request
def _mini_app_ban_gate():
    """Blocks banned users from every mini-app API call (reads and writes)."""
    path = request.path
    if not path.startswith('/api/mini-app/') or path.startswith('/api/mini-app/file/'):
        return None
    try:
        uid = request.args.get('user_id') or request.args.get('viewer_id') or request.args.get('admin_id')
        if not uid and request.is_json:
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                uid = body.get('user_id') or body.get('sender_id') or body.get('admin_id')
        if not uid and request.form:
            uid = request.form.get('user_id')
        if not uid and request.method in ('PUT', 'POST'):
            uid = (request.view_args or {}).get('user_id')  # /profile/<id>, /settings/<id> writes
        if not uid:
            return None
        row = get_user_cached(str(uid))
        if row and row.get('is_banned') and not row.get('is_admin'):
            msg = "🚫 Your account has been banned."
            if row.get('ban_reason'):
                msg += f" Reason: {row['ban_reason']}"
            return jsonify({'success': False, 'error': msg, 'banned': True}), 403
    except Exception as e:
        logger.error(f"mini-app ban gate error: {e}")  # fail open: never take the app down
    return None


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
    # concurrent_updates: by default PTB handles updates ONE AT A TIME, so a single slow handler
    # (e.g. a page that sends 10 comment messages) makes every other user wait. With it on, updates
    # run concurrently. Trade-off: two updates from the SAME user in the same instant can now
    # interleave (state lives in context.user_data), which the old strict ordering prevented.
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(set_bot_commands).build()
    
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
    
    # ---- moderation ----
    app.add_handler(TypeHandler(Update, ban_gate), group=-2)                                   # blocks banned users first
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mod_text_input), group=-1)  # typed ban/warn reasons
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("warnings", warnings_command))
    app.add_handler(CommandHandler("user", user_command))
    app.add_handler(CommandHandler("banned", banned_command))
    app.add_handler(CommandHandler("mod", mod_command))
    app.add_handler(CallbackQueryHandler(moderation_callback, pattern=r'^mod_'))                # BEFORE button_handler

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_message_text))
    
    app.add_error_handler(error_handler)
    
    
    
    # Start Flask server in a separate thread for Render
    port = int(os.environ.get('PORT', 5000))
    threading.Thread(
        target=lambda: waitress_serve(flask_app, host='0.0.0.0', port=port, threads=8),
        # waitress replaces Werkzeug's dev server: it's a real production WSGI
        # server (proper thread pool, not "not intended for production use"
        # like flask_app.run()), while still running in-process alongside the
        # bot's polling loop - no webhook migration needed. threads=8 lets up
        # to 8 mini-app API requests be handled at once; raise it if the app
        # grows and Render gives it more CPU/connections to work with.
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
                time=dt_time(0, 0, tzinfo=timezone.utc),
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
    """Complete Mini App - returns the mini app UI."""
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
  --border2:rgba(var(--gold-rgb),0.2);
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
  --border2:rgba(var(--gold-rgb),0.3);
  --text:#1a1a1a;
  --text2:#4a4a4a;
  --text3:#6b6b6b;
}
html,body{height:100%;overflow:hidden}
body{
  font-family:'Inter',sans-serif;
  background:var(--bg);
  color:var(--text);
  font-size:16px;
  line-height:1.5;
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
  color:var(--text3);font-size:11px;font-weight:600;letter-spacing:0.3px;
  font-family:'Inter',sans-serif;
  transition:color 0.2s;padding:8px 4px;
  -webkit-tap-highlight-color:transparent;
  text-transform:uppercase;
}
.nav-item svg{width:23px;height:23px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;transition:transform 0.2s}
.icon{width:16px;height:16px;flex-shrink:0;vertical-align:-3px}
.cat-chip .icon{width:15px;height:15px;color:var(--text3)}
.cat-chip.on .icon{color:var(--gold2)}
.badge-icon{width:13px;height:13px;vertical-align:-2px;margin-right:3px}
.ava svg,.modal-avatar svg,.profile-ava-wrap svg{width:55%;height:55%;color:var(--gold2)}
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
.page-head-sub{font-size:14px;color:var(--text3);margin-top:2px}
.logo-img{width:48px;height:48px;border-radius:12px;object-fit:cover;box-shadow:0 2px 8px rgba(0,0,0,0.1);}
.card{
  background:var(--glass);
  border:0.5px solid var(--border);
  border-radius:var(--radius);
  padding:18px;
  margin:12px 16px 0;
}
.card-gold{
  background:linear-gradient(135deg,rgba(var(--gold-rgb),0.08) 0%,rgba(var(--gold-rgb),0.03) 100%);
  border-color:var(--border2);
}
.pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600;
  background:rgba(var(--gold-rgb),0.1);border:0.5px solid rgba(var(--gold-rgb),0.25);
  color:var(--gold2);
}
.pill-sm{padding:2px 8px;font-size:11px}
.pill-aura{
  display:inline-flex;align-items:center;gap:7px;
  padding:6px 14px;border-radius:24px;
  font-size:12.5px;font-weight:700;letter-spacing:0.2px;
  background:linear-gradient(135deg,rgba(var(--gold-rgb),0.30) 0%,rgba(245,158,11,0.16) 55%,rgba(var(--gold-rgb),0.24) 100%);
  border:0.5px solid rgba(245,158,11,0.4);
  color:var(--gold3);
  box-shadow:0 2px 10px rgba(var(--gold-rgb),0.2),inset 0 1px 0 rgba(255,255,255,0.07);
}
.pill-aura-badge{font-size:14px;line-height:1}
.pill-aura .bolt-icon{width:13px;height:13px;flex-shrink:0;display:block}
.pill-aura .bolt-icon path{fill:#ff9800}
.pill-aura-pts{color:var(--gold3)}
.ava{
  border-radius:50%;
  background:linear-gradient(145deg,rgba(var(--gold-rgb),0.22),var(--bg3) 55%,var(--bg2));
  border:1.5px solid rgba(var(--gold-rgb),0.55);
  box-shadow:0 2px 10px rgba(var(--gold-rgb),0.2),inset 0 1px 1px rgba(255,255,255,0.08);
  display:flex;align-items:center;justify-content:center;
  flex-shrink:0;font-size:1.1em;
}
.input-area{
  width:100%;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:14px 16px;
  color:var(--text);font-family:'Inter',sans-serif;font-size:16px;
  outline:none;resize:none;
  transition:border-color 0.2s;
}
.input-area:focus{border-color:rgba(var(--gold-rgb),0.4)}
.input-area::placeholder{color:var(--text3)}
.btn-gold{
  width:100%;padding:16px;border-radius:var(--radius-sm);border:none;
  background:var(--gold);color:#0c0b09;
  font-family:'Inter',sans-serif;font-size:16px;font-weight:700;
  cursor:pointer;letter-spacing:0.2px;
  box-shadow:0 4px 14px rgba(var(--gold-rgb),0.3);
  transition:opacity 0.2s,transform 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.btn-gold:active{transform:scale(0.98);opacity:0.9}
.btn-gold:disabled{opacity:0.4;cursor:not-allowed;box-shadow:none}
.btn-ghost{
  background:none;border:1.5px solid var(--border2);border-radius:var(--radius-xs);
  color:var(--gold);padding:10px 16px;font-size:14px;font-weight:700;
  font-family:'Inter',sans-serif;cursor:pointer;
  -webkit-tap-highlight-color:transparent;
}
.cat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}
.cat-chip{
  padding:12px 14px;border-radius:var(--radius-sm);font-size:13.5px;font-weight:600;
  background:var(--bg2);border:1px solid var(--border);color:var(--text2);
  cursor:pointer;display:flex;align-items:center;gap:8px;
  transition:all 0.15s;-webkit-tap-highlight-color:transparent;
}
.cat-chip:active{transform:scale(0.97)}
.cat-chip.on{background:rgba(var(--gold-rgb),0.12);border-color:var(--gold);color:var(--gold2)}
.cat-check{width:16px;height:16px;
  display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0;
  color:transparent;transition:color 0.15s}
.cat-chip.on .cat-check{color:var(--gold2)}
.cat-chip.on .cat-check::after{content:'✓';font-weight:700}
.post-card{
  margin:10px 16px 0;
  background:var(--glass);border:0.5px solid var(--border);
  border-radius:var(--radius);padding:16px;
  cursor:pointer;transition:background 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.post-card:active{background:var(--glass2)}
.post-meta{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.post-name{font-size:14px;font-weight:600;color:var(--text);cursor:pointer}
.post-name:hover{color:var(--gold)}
.post-time{font-size:12px;color:var(--text3);margin-left:auto}
.post-body{font-size:16px;line-height:1.6;color:var(--text2);
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:12px}
.post-footer{display:flex;align-items:center;justify-content:space-between;
  padding-top:12px;border-top:0.5px solid var(--border)}
.post-footer-left{display:flex;align-items:center;gap:12px}
.stat-btn{display:flex;align-items:center;gap:5px;color:var(--text3);font-size:13px;
  font-weight:600;background:none;border:none;cursor:pointer;font-family:'Inter',sans-serif;
  -webkit-tap-highlight-color:transparent;padding:0}
.stat-btn svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.8}
.read-more{font-size:13px;font-weight:700;color:var(--gold);display:flex;align-items:center;gap:3px}
.lb-hero{
  margin:20px 16px 0;
  background:linear-gradient(135deg,rgba(var(--gold-rgb),0.1),rgba(var(--gold-rgb),0.04));
  border:0.5px solid var(--border2);border-radius:20px;
  padding:24px 20px;text-align:center;position:relative;overflow:hidden;
}
.lb-hero::before{
  content:'';position:absolute;inset:-40px;
  background:radial-gradient(circle at 50% 0,rgba(var(--gold-rgb),0.08),transparent 70%);
}
.lb-crown{font-size:36px;margin-bottom:6px;display:block}
.lb-top-name{font-size:20px;font-weight:700;letter-spacing:-0.3px}
.lb-top-pts{font-size:14px;color:var(--text3);margin-top:4px}
.lb-medals{display:flex;gap:8px;margin-top:20px;justify-content:center}
.lb-medal-card{
  flex:1;background:var(--bg2);border-radius:var(--radius-sm);
  border:0.5px solid var(--border);padding:14px 10px;text-align:center;
}
.lb-medal-rank{font-size:20px;margin-bottom:4px}
.lb-medal-name{font-size:13px;font-weight:600;color:var(--text);margin-bottom:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lb-medal-pts{font-size:12px;color:var(--text3)}
.lb-list{margin:0 16px}
.lb-row{
  display:flex;align-items:center;gap:12px;padding:14px 0;
  border-bottom:0.5px solid var(--border);
}
.lb-row:last-child{border-bottom:none}
.lb-rank{width:24px;text-align:center;font-size:14px;font-weight:700;color:var(--text3)}
.lb-info{flex:1;min-width:0}
.lb-info-name{font-size:15px;font-weight:600;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.lb-info-name:hover{color:var(--gold)}
.lb-info-aura{font-size:12px;color:var(--text3);margin-top:1px}
.lb-pts{font-size:15px;font-weight:700;color:var(--gold)}
.profile-hero{
  margin:20px 16px 0;
  background:linear-gradient(160deg,var(--bg3),var(--bg2));
  border:0.5px solid var(--border);border-radius:20px;padding:24px;
  text-align:center;position:relative;
}
.profile-ava-wrap{
  width:80px;height:80px;border-radius:50%;margin:0 auto 14px;
  background:linear-gradient(145deg,rgba(var(--gold-rgb),0.3),rgba(var(--gold-rgb),0.05) 60%,var(--bg2));
  border:2px solid rgba(var(--gold-rgb),0.6);
  box-shadow:0 0 0 4px rgba(var(--gold-rgb),0.08),0 6px 20px rgba(var(--gold-rgb),0.25),inset 0 1px 1px rgba(255,255,255,0.1);
  display:flex;align-items:center;justify-content:center;font-size:32px;
}
.profile-name{font-size:22px;font-weight:700;letter-spacing:-0.3px}
.profile-pts{font-size:14px;color:var(--text3);margin-top:4px}
.profile-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
  margin-top:20px;border-top:0.5px solid var(--border);padding-top:16px}
.profile-stat{text-align:center}
.profile-stat-num{font-size:21px;font-weight:700;color:var(--gold)}
.profile-stat-lbl{font-size:12px;color:var(--text3);margin-top:2px}
.setting-row{
  display:flex;align-items:center;padding:16px 0;
  border-bottom:0.5px solid var(--border);gap:14px;
}
.setting-row:last-child{border-bottom:none}
.setting-icon{
  width:40px;height:40px;border-radius:10px;
  background:rgba(var(--gold-rgb),0.12);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.setting-icon svg{width:19px;height:19px;stroke:var(--gold);fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.setting-label{flex:1}
.setting-label-title{font-size:15px;font-weight:600;color:var(--text)}
.setting-label-sub{font-size:13px;color:var(--text3);margin-top:2px}
.toggle{position:relative;width:46px;height:26px;cursor:pointer;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{
  position:absolute;inset:0;border-radius:25px;
  background:var(--bg3);border:0.5px solid var(--border);
  transition:background 0.25s;
}
.toggle input:checked + .toggle-track{background:rgba(var(--gold-rgb),0.3);border-color:var(--gold)}
.toggle-thumb{
  position:absolute;width:20px;height:20px;border-radius:50%;
  top:3px;left:3px;
  background:var(--text3);transition:all 0.25s cubic-bezier(.4,0,.2,1);
}
.toggle input:checked ~ .toggle-thumb{left:23px;background:var(--gold)}
/* ===== Me tab: profile, edit profile, avatars (flat, one accent) ===== */
.av-svg{display:block;width:100%;height:100%}
body svg.av-svg{color:var(--gold2)}
body.light svg.av-svg{color:var(--gold-dark,#8a6d1f)}
.ava svg.av-svg,.modal-avatar svg.av-svg,.profile-ava-wrap svg.av-svg{width:62%;height:62%}
.me-head{padding:22px 20px 0}
.me-head h1{font-size:26px;font-weight:700;letter-spacing:-.5px;color:var(--text)}
.me-hero{margin:22px 20px 0}
.me-top{display:flex;align-items:center;gap:16px}
.me-ava{
  position:relative;width:76px;height:76px;border-radius:50%;flex-shrink:0;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  background:var(--bg2);border:1px solid var(--border);-webkit-tap-highlight-color:transparent;
}
.me-ava svg.av-svg{width:56%;height:56%}
.me-id{min-width:0}
.me-name{font-size:22px;font-weight:700;letter-spacing:-.3px;line-height:1.2;word-break:break-word}
.me-meta{display:flex;align-items:center;flex-wrap:wrap;gap:4px 14px;margin-top:6px;font-size:14px;color:var(--text2)}
.me-meta:empty{display:none}
.me-meta span{display:inline-flex;align-items:center;gap:6px}
.me-meta .icon{width:14px;height:14px}
.me-bio{
  margin-top:18px;max-width:34em;font-size:15px;line-height:1.6;color:var(--text2);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;
}
.me-bio.empty{color:var(--text3);cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.me-stats{display:flex;gap:30px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}
.me-stat-num{font-size:20px;font-weight:700;letter-spacing:-.2px;color:var(--text);font-variant-numeric:tabular-nums}
.me-stat-lbl{font-size:13px;color:var(--text3)}
.me-prog{margin-top:18px}
.me-prog-top{font-size:13px;color:var(--text3);margin-bottom:7px}
.me-prog-bar{height:3px;border-radius:3px;background:var(--border);overflow:hidden}
.me-prog-fill{height:100%;background:var(--gold);transition:width .5s ease}
.me-edit-btn{
  margin-top:20px;height:44px;padding:0 20px;border-radius:10px;cursor:pointer;
  display:inline-flex;align-items:center;gap:8px;
  background:none;border:1px solid var(--border);color:var(--text);
  font-family:'Inter',sans-serif;font-size:14px;font-weight:600;-webkit-tap-highlight-color:transparent;
}
.me-edit-btn:active{background:var(--glass2)}
.me-edit-btn svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.me-label{padding:34px 20px 6px;font-size:17px;font-weight:700;letter-spacing:-.2px;color:var(--text)}
.me-group{margin:0 20px;border-top:1px solid var(--border)}
.me-row{display:flex;align-items:center;gap:14px;padding:15px 0;border-bottom:1px solid var(--border)}
.me-row.stack{flex-direction:column;align-items:stretch;gap:12px}
.me-row-head{display:flex;align-items:center;gap:14px}
.me-row.tap{cursor:pointer;-webkit-tap-highlight-color:transparent}
.me-row.tap:active{background:var(--glass2)}
.me-ico{width:22px;height:22px;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.me-ico svg{width:20px;height:20px;stroke:var(--text2);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.me-text{flex:1;min-width:0}
.me-title{font-size:15px;font-weight:600;color:var(--text)}
.me-sub{font-size:13px;color:var(--text3);margin-top:2px;line-height:1.4}
.me-chev{width:16px;height:16px;stroke:var(--text3);fill:none;stroke-width:2;flex-shrink:0}
.me-note{padding:12px 20px 0;font-size:13px;line-height:1.5;color:var(--text3)}
.me-foot{padding:34px 20px 8px;font-size:12px;color:var(--text3)}
.me-foot a{color:var(--text2)}
.seg{display:flex;gap:4px}
.seg button{
  flex:1;display:flex;align-items:center;justify-content:center;gap:7px;padding:10px 8px;border-radius:10px;
  background:none;border:1px solid var(--border);color:var(--text3);
  font-family:'Inter',sans-serif;font-size:14px;font-weight:600;cursor:pointer;-webkit-tap-highlight-color:transparent;
}
.seg button.on{border-color:var(--gold);color:var(--text)}
.seg svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.swatches{display:flex;gap:14px}
.sw{
  --c:#c9a84c;width:30px;height:30px;border-radius:50%;padding:0;cursor:pointer;background:var(--c);
  border:2px solid var(--bg);outline:2px solid transparent;display:flex;align-items:center;justify-content:center;
  color:#0c0b09;-webkit-tap-highlight-color:transparent;
}
.sw svg{width:13px;height:13px;opacity:0}
.sw.on{outline-color:var(--text)}
.sw.on svg{opacity:1}
.modal-bio{font-size:14px;line-height:1.5;color:var(--text2);margin:2px 8px 14px;word-break:break-word}
.modal-role{display:inline-flex;align-items:center;gap:5px;margin:0 0 10px;font-size:13px;font-weight:600;color:var(--text2)}
.modal-role .icon{width:13px;height:13px}

/* edit profile */
.ed-head{padding:2px 20px 0}
.ed-head h1{font-size:26px;font-weight:700;letter-spacing:-.5px;color:var(--text)}
.ed-preview{margin:18px 20px 0;padding-bottom:18px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--border)}
.ed-prev-ava{width:64px;height:64px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:var(--bg2);border:1px solid var(--border)}
.ed-prev-ava svg.av-svg{width:56%;height:56%}
.ed-prev-txt{min-width:0;flex:1}
.ed-prev-name{font-size:18px;font-weight:700;letter-spacing:-.2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ed-prev-bio{font-size:14px;line-height:1.45;color:var(--text2);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.ed-prev-bio.empty{color:var(--text3)}
.ed-card{margin:22px 20px 0}
.ed-label{display:flex;justify-content:space-between;align-items:baseline;margin:0 0 10px;font-size:15px;font-weight:600;color:var(--text)}
.ed-count{font-size:13px;font-weight:400;color:var(--text3);font-variant-numeric:tabular-nums}
.ed-link{background:none;border:none;padding:0;cursor:pointer;font-family:'Inter',sans-serif;font-size:13px;font-weight:600;color:var(--text2);text-decoration:underline;text-underline-offset:3px}
.ed-input{
  width:100%;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:13px 14px;
  color:var(--text);font-family:'Inter',sans-serif;font-size:16px;outline:none;
}
.ed-input:focus{border-color:var(--gold)}
.ed-input::placeholder{color:var(--text3)}
.ed-hint{margin-top:10px;font-size:13px;line-height:1.5;color:var(--text3)}
.ed-actions{padding:24px 20px 24px;display:flex;flex-direction:column;gap:6px}
.ed-cancel{background:none;border:none;padding:12px;cursor:pointer;font-family:'Inter',sans-serif;font-size:14px;font-weight:600;color:var(--text3)}
.av-tabs{display:flex;gap:20px;overflow-x:auto;margin:0 -20px 14px;padding:0 20px;border-bottom:1px solid var(--border);scrollbar-width:none}
.av-tabs::-webkit-scrollbar{display:none}
.av-tab{
  flex:0 0 auto;padding:8px 0 10px;margin-bottom:-1px;background:none;border:none;border-bottom:2px solid transparent;
  color:var(--text3);font-family:'Inter',sans-serif;font-size:14px;font-weight:600;cursor:pointer;-webkit-tap-highlight-color:transparent;
}
.av-tab.on{color:var(--text);border-bottom-color:var(--gold)}
.av-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.av-tile{
  aspect-ratio:1;padding:0;border-radius:12px;cursor:pointer;background:var(--bg2);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent;
}
.av-tile>svg.av-svg{width:58%;height:58%}
.av-tile.sel{border-color:var(--gold);background:var(--bg3);box-shadow:inset 0 0 0 1px var(--gold)}
.av-check{display:none}
.search-wrap{
  display:flex;align-items:center;gap:10px;
  padding:13px 16px;background:var(--glass);
  border:0.5px solid var(--border);border-radius:var(--radius-sm);
  margin:14px 16px 0;
}
.search-wrap svg{width:18px;height:18px;stroke:var(--text3);fill:none;stroke-width:1.8;flex-shrink:0}
.search-wrap input{flex:1;background:none;border:none;outline:none;color:var(--text);
  font-family:'Inter',sans-serif;font-size:16px}
.search-wrap input::placeholder{color:var(--text3)}
.char-count{font-size:12px;color:var(--text3);text-align:right;margin:6px 0 12px}
.vent-label{margin:2px 0 10px}
.vent-num{font-size:12px;font-weight:600;color:var(--text3);letter-spacing:0.3px}
.vent-sex{font-size:16px;line-height:1.25;margin-top:2px}
.skel{
  background:linear-gradient(90deg,var(--bg2) 25%,var(--bg3) 50%,var(--bg2) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite;
  border-radius:var(--radius-xs);
}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
#toast{
  position:fixed;bottom:calc(var(--nav-h) + 80px);left:50%;transform:translateX(-50%) translateY(10px);
  background:var(--gold);color:#0c0b09;padding:11px 22px;border-radius:20px;
  font-size:14px;font-weight:700;opacity:0;pointer-events:none;
  transition:all 0.25s;z-index:999;white-space:nowrap;
}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#page-detail{position:relative}
.back-btn{
  display:flex;align-items:center;gap:6px;
  padding:20px 16px 10px;
  color:var(--gold);font-size:15px;font-weight:700;
  background:none;border:none;cursor:pointer;font-family:'Inter',sans-serif;
  -webkit-tap-highlight-color:transparent;
}
.back-btn svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:2.2}
/* In light mode the gold-on-cream contrast is too weak for the icon stroke;
   darken it slightly and give the chat room its own explicit override so it
   isn't relying on the ambient --gold var alone. */
body.light .back-btn{color:var(--gold-dark,#8a6d1f)}
body.light .back-btn svg{stroke:var(--gold-dark,#8a6d1f)}
.comment-item{display:flex;gap:10px;margin-bottom:14px}
.comment-item.reply{margin-left:32px}
.comment-body{flex:1;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:12px}
.comment-name{font-size:13px;font-weight:600;color:var(--gold);margin-bottom:4px;cursor:pointer}
.comment-name:hover{text-decoration:underline}
.comment-text{font-size:15px;line-height:1.55;color:var(--text2)}
.comment-actions{display:flex;gap:14px;margin-top:8px}
.ca-btn{background:none;border:none;cursor:pointer;
  font-size:13px;font-weight:600;color:var(--text3);font-family:'Inter',sans-serif;
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
  border-radius:var(--radius-xs);padding:11px 12px;
  color:var(--text);font-family:'Inter',sans-serif;font-size:16px;
  outline:none;resize:none;max-height:100px;min-height:42px;
}
.comment-input-bar textarea:focus{border-color:rgba(var(--gold-rgb),0.4)}
.comment-input-bar button{
  width:40px;height:40px;border-radius:50%;
  background:var(--gold);border:none;cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 8px rgba(var(--gold-rgb),0.35);
}
.comment-input-bar button svg{width:17px;height:17px;stroke:#0c0b09;fill:none;stroke-width:2.2}
.media-attach-btn{
  width:40px;height:40px;border-radius:50%;flex-shrink:0;
  background:var(--bg2);border:1px solid var(--border);cursor:pointer;
  display:flex;align-items:center;justify-content:center;position:relative;
  -webkit-tap-highlight-color:transparent;
}
.media-attach-btn:active{transform:scale(0.92)}
.media-attach-btn svg{width:17px;height:17px;stroke:var(--text2);fill:none;stroke-width:2}
.media-attach-btn.has-media{border-color:var(--gold)}
.media-attach-btn.has-media svg{stroke:var(--gold)}
.media-preview{
  display:flex;align-items:center;gap:8px;
  background:var(--bg2);border:0.5px solid var(--border);border-radius:var(--radius-xs);
  padding:8px 10px;margin:8px 16px 0;font-size:13px;color:var(--text2);
}
.media-preview img{width:36px;height:36px;border-radius:8px;object-fit:cover;flex-shrink:0}
.media-preview .mp-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.media-preview .mp-remove{background:none;border:none;color:var(--text3);cursor:pointer;font-size:17px;padding:0 4px}
.media-preview .mp-remove:hover{color:var(--gold)}
#comment-media-preview.media-preview{margin:0 0 8px}
.post-media, .comment-media{margin:10px 0;border-radius:var(--radius-sm);overflow:hidden}
.post-media img, .comment-media img{width:100%;display:block;border-radius:var(--radius-sm)}
.post-media video, .comment-media video{width:100%;display:block;border-radius:var(--radius-sm);background:#000}
.post-media audio, .comment-media audio{width:100%;display:block}
.post-media .doc-link, .comment-media .doc-link{
  display:flex;align-items:center;gap:10px;background:var(--bg2);border:0.5px solid var(--border);
  border-radius:var(--radius-sm);padding:12px;color:var(--text);text-decoration:none;font-size:14px;
}
.post-media .doc-link svg{width:20px;height:20px;stroke:var(--gold);fill:none;stroke-width:2;flex-shrink:0}
.post-media img.sticker-media, .comment-media img.sticker-media{width:100px;border-radius:0}
/* ----- Compact voice player ----- */
.voice-player{display:flex;align-items:center;gap:9px;background:var(--bg2);border:0.5px solid var(--border);border-radius:22px;padding:7px 12px;max-width:230px;margin:8px 0}
.voice-player-btn{width:34px;height:34px;border-radius:50%;background:var(--gold);border:none;flex-shrink:0;display:flex;align-items:center;justify-content:center;cursor:pointer;-webkit-tap-highlight-color:transparent}
.voice-player-btn svg{width:15px;height:15px;fill:#0c0b09;stroke:#0c0b09}
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
/* ----- Voice recording (Telegram-style) ----- */
.voice-record-btn{
  width:40px;height:40px;border-radius:50%;flex-shrink:0;cursor:pointer;
  background:none!important;border:none!important;box-shadow:none!important;
  display:flex;align-items:center;justify-content:center;
  touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;
  -webkit-tap-highlight-color:transparent;
}
.voice-record-btn svg,.comment-input-bar .voice-record-btn svg{width:24px;height:24px;fill:none;stroke:var(--text3);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.vr-bar{position:absolute;left:0;right:0;top:0;bottom:0;z-index:5;display:flex;align-items:center;gap:10px;
  background:var(--bg);color:var(--text);border-radius:21px;animation:vrIn .18s ease-out;user-select:none;-webkit-user-select:none}
.vr-dot{width:12px;height:12px;border-radius:50%;background:#f44336;flex-shrink:0;animation:vrBlink 1s ease-in-out infinite}
.vr-time{font-size:17px;font-variant-numeric:tabular-nums;min-width:58px}
.vr-slide{flex:1;display:flex;align-items:center;justify-content:center;gap:4px;color:var(--text3);font-size:15px;white-space:nowrap;padding-right:56px}
.vr-slide svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;animation:vrNudge 1.2s ease-in-out infinite}
.vr-textbtn{background:none;border:none;color:var(--tg-theme-link-color,#3390ec);font-size:16px;font-weight:500;padding:8px;cursor:pointer;margin-left:auto;margin-right:56px;font-family:inherit}
.vr-iconbtn{width:36px;height:36px;border-radius:50%;border:none;background:none;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;padding:0}
.vr-iconbtn svg{width:22px;height:22px;fill:none;stroke:var(--text3);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.vr-iconbtn.play{background:var(--tg-theme-button-color,#3390ec)}
.vr-iconbtn.play svg{stroke:#fff;fill:#fff;width:16px;height:16px}
.vr-track{flex:1;height:4px;border-radius:2px;background:var(--border);overflow:hidden;margin-right:56px}
.vr-track i{display:block;height:100%;width:0;background:var(--tg-theme-button-color,#3390ec)}
.vr-bin{margin:0 auto;animation:vrBin .38s ease-in forwards}
.vr-bin svg{width:26px;height:26px;fill:none;stroke:#f44336;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.vr-halo,.vr-orb,.vr-lock{position:fixed;border-radius:50%;pointer-events:none}
.vr-halo{width:64px;height:64px;margin:-32px 0 0 -32px;background:var(--tg-theme-button-color,#3390ec);opacity:.25;z-index:1000;transform:scale(1);transition:transform .09s linear}
.vr-orb{width:64px;height:64px;margin:-32px 0 0 -32px;background:var(--tg-theme-button-color,#3390ec);z-index:1002;
  display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.3);
  transition:width .2s,height .2s,margin .2s,transform .12s;animation:vrPop .16s ease-out}
.vr-orb svg{width:28px;height:28px;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.vr-orb.sm{width:44px;height:44px;margin:-22px 0 0 -22px;pointer-events:auto;cursor:pointer}
.vr-orb.sm svg{width:20px;height:20px}
.vr-lock{width:44px;height:96px;margin:-48px 0 0 -22px;border-radius:22px;background:var(--bg2);border:0.5px solid var(--border);
  box-shadow:0 2px 12px rgba(0,0,0,.35);z-index:1001;display:flex;flex-direction:column;align-items:center;justify-content:space-around;animation:vrPop .2s ease-out}
.vr-lock svg{width:18px;height:18px;fill:none;stroke:var(--text2);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.vr-lock.stop{height:44px;margin:-22px 0 0 -22px;border-radius:50%;pointer-events:auto;cursor:pointer;justify-content:center}
.vr-lock.stop svg{fill:#f44336;stroke:#f44336;width:16px;height:16px}
.vr-tip{position:fixed;z-index:1003;transform:translate(-50%,-100%);background:rgba(30,30,30,.92);color:#fff;font-size:13px;padding:8px 12px;border-radius:12px;white-space:nowrap;animation:vrIn .2s ease-out;pointer-events:none}
@keyframes vrIn{from{opacity:0}to{opacity:1}}
@keyframes vrPop{from{transform:scale(.4);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes vrBlink{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes vrNudge{0%,100%{transform:translateX(0)}50%{transform:translateX(-5px)}}
@keyframes vrBin{0%{transform:translateY(-14px) scale(.6);opacity:0}30%{transform:translateY(0) scale(1.1);opacity:1}60%{transform:rotate(-10deg)}80%{transform:rotate(8deg)}100%{transform:scale(.8);opacity:0}}
.vr-pw{display:contents}
.vr-pend{display:flex;align-items:center;gap:10px;width:188px;max-width:100%}
.vr-pbtn{position:relative;width:34px;height:34px;border-radius:50%;background:#0c0b09;border:none;padding:0;flex-shrink:0;display:flex;align-items:center;justify-content:center;cursor:pointer;-webkit-tap-highlight-color:transparent}
.vr-pbtn svg.x{width:14px;height:14px;fill:none;stroke:var(--gold);stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.vr-ring{position:absolute;left:-1px;top:-1px;width:36px;height:36px;transform:rotate(-90deg)}
.vr-ring circle{fill:none;stroke-width:2.5}
.vr-ring .bg{stroke:rgba(var(--gold-rgb),.28)}
.vr-ring .fg{stroke:var(--gold);stroke-dasharray:94.2;stroke-linecap:round;transition:stroke-dashoffset .2s}
.vr-ring.spin{animation:vrSpin 1s linear infinite}
.vr-ptrack{flex:1;height:3px;border-radius:2px;background:rgba(12,11,9,.28)}
.vr-ptime{font-size:10.5px;color:rgba(12,11,9,.72);font-variant-numeric:tabular-nums}
.vr-pend.cm .vr-pbtn{background:var(--gold)}
.vr-pend.cm .vr-pbtn svg.x{stroke:#0c0b09}
.vr-pend.cm .vr-ring .fg{stroke:#0c0b09}
.vr-pend.cm .vr-ring .bg{stroke:rgba(12,11,9,.2)}
.vr-pend.cm .vr-ptrack{background:var(--border)}
.vr-pend.cm .vr-ptime{color:var(--text3)}
@keyframes vrSpin{to{transform:rotate(270deg)}}
.reply-quote{margin:4px 0 6px;padding:4px 8px 4px 10px;border-left:3px solid var(--gold);background:rgba(var(--gold-rgb),.10);border-radius:0 8px 8px 0;cursor:pointer;max-width:100%;-webkit-tap-highlight-color:transparent}
.reply-quote:active{background:rgba(var(--gold-rgb),.22)}
.reply-quote.gone{cursor:default;opacity:.7}
.reply-quote.gone .rq-text{font-style:italic}
.rq-name{font-size:12.5px;font-weight:600;color:var(--gold);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rq-text{font-size:13px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.comment-body{min-width:0}
.cm-flash{animation:cmFlash 1.5s ease-out}
@keyframes cmFlash{0%,35%{background:rgba(var(--gold-rgb),.28)}100%{background:transparent}}
.cr-msgs{overflow-x:hidden}
.msg-row{touch-action:pan-y;position:relative}
.msg-quote{margin:0 0 6px;padding:4px 8px 4px 9px;border-left:3px solid var(--gold);background:rgba(var(--gold-rgb),.12);border-radius:0 8px 8px 0;cursor:pointer;min-width:120px;max-width:100%}
.msg-row.me .msg-quote{border-left-color:#0c0b09;background:rgba(12,11,9,.13)}
.msg-row.me .msg-quote .rq-name{color:#0c0b09}
.msg-row.me .msg-quote .rq-text{color:rgba(12,11,9,.72)}
.msg-bubble.msg-flash{animation:msgFlash 1.3s ease-out}
@keyframes msgFlash{0%,40%{filter:brightness(1.4)}100%{filter:none}}
.msg-swipe-ico{position:absolute;left:-40px;top:50%;margin-top:-15px;width:30px;height:30px;border-radius:50%;background:var(--bg3);color:var(--text2);display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none}
.msg-swipe-ico svg{width:16px;height:16px}
.cr-input .rb-x{width:32px;height:32px;background:none;border:none;padding:0;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.cr-input .rb-x svg{width:18px;height:18px;stroke:var(--text3);fill:none;stroke-width:2}
.reply-bar{display:flex;align-items:center;gap:10px;padding:0 2px 8px;animation:vrIn .15s ease-out}
.reply-bar .rb-line{width:3px;align-self:stretch;background:var(--gold);border-radius:2px}
.reply-bar .rb-body{flex:1;min-width:0;cursor:pointer}
.comment-input-bar .rb-x{width:32px;height:32px;background:none;box-shadow:none;border:none}
.comment-input-bar .rb-x svg{width:18px;height:18px;stroke:var(--text3);fill:none;stroke-width:2}

/* ----- Direct reaction buttons ----- */
.reaction-buttons{
  display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;
}
.reaction-btn{
  display:flex;align-items:center;gap:5px;
  padding:6px 12px;border-radius:20px;background:var(--bg2);
  border:1px solid var(--border);cursor:pointer;font-size:14px;font-weight:600;
  transition:all 0.15s;font-family:'Inter',sans-serif;color:var(--text2);
}
.reaction-btn.on{background:rgba(var(--gold-rgb),0.14);border-color:var(--gold);color:var(--gold)}
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
.chat-item-name{font-size:15px;font-weight:600;color:var(--text)}
.chat-item-time{font-size:12px;color:var(--text3)}
.chat-item-preview{font-size:13px;color:var(--text3);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.unread-badge{
  background:var(--gold);color:#0c0b09;font-size:11px;font-weight:800;
  min-width:19px;height:19px;border-radius:10px;
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
.cr-head button svg{width:23px;height:23px;stroke:var(--text);fill:none;stroke-width:2}
body.light .cr-head{background:rgba(245,243,240,0.97)}
body.light .cr-head button svg{stroke:#1a1a1a}
.cr-name{font-size:17px;font-weight:700}
.cr-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.cr-msgs::-webkit-scrollbar{display:none}
.msg-row{display:flex;flex-direction:column;max-width:75%}
.msg-row.me{align-self:flex-end;align-items:flex-end}
.msg-row.them{align-self:flex-start}
.msg-bubble{
  padding:11px 15px;border-radius:18px;font-size:15px;line-height:1.45;
  word-break:break-word;position:relative;
}
.msg-row.me .msg-bubble{
  background:var(--gold);color:#0c0b09;font-weight:500;
  border-bottom-right-radius:4px;
}
.msg-row.them .msg-bubble{
  background:var(--bg3);border:0.5px solid var(--border);color:var(--text);
  border-bottom-left-radius:4px;
}
.msg-time{font-size:11px;color:var(--text3);margin-top:4px;padding:0 4px}
.msg-deleted{font-style:italic;opacity:0.6;background:var(--bg3)!important;color:var(--text3)!important;border:0.5px solid var(--border)!important;font-weight:400!important}
.msg-menu-btn{margin-left:8px;cursor:pointer;opacity:0.6;font-weight:700;padding:0 2px}
.msg-menu-btn:hover{opacity:1}
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
  border-radius:20px;padding:11px 16px;color:var(--text);
  font-family:'Inter',sans-serif;font-size:16px;outline:none;
  resize:none;min-height:42px;max-height:100px;
}
.cr-input textarea:focus{border-color:rgba(var(--gold-rgb),0.4)}
.cr-send{
  width:42px;height:42px;border-radius:50%;
  background:var(--gold);border:none;cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 2px 8px rgba(var(--gold-rgb),0.35);
  -webkit-tap-highlight-color:transparent;
}
.cr-send svg{width:18px;height:18px;stroke:#0c0b09;fill:none;stroke-width:2.2}
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
.auth-label{font-size:16px;font-weight:600;color:var(--gold)}
.section-label{
  font-size:12px;font-weight:700;letter-spacing:1.2px;
  text-transform:uppercase;color:var(--text3);
  padding:18px 16px 8px;
}
.divider{height:0.5px;background:var(--border);margin:0}
.input-label{font-size:13px;font-weight:600;color:var(--text3);margin-bottom:6px;display:block;letter-spacing:0.3px;text-transform:uppercase}
.emoji-picker{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:8px 0 0}
.emoji-opt{
  aspect-ratio:1;background:var(--bg2);border:1.5px solid transparent;
  border-radius:var(--radius-xs);display:flex;align-items:center;justify-content:center;
  font-size:22px;cursor:pointer;transition:all 0.15s;
  -webkit-tap-highlight-color:transparent;
}
.emoji-opt.sel{border-color:var(--gold);background:rgba(var(--gold-rgb),0.1)}
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
  padding:5px 11px;border-radius:20px;font-size:13px;font-weight:600;
  background:var(--bg2);border:0.5px solid var(--border);color:var(--text2);
  cursor:pointer;transition:all 0.15s;
}
.rx-pill.on{background:rgba(var(--gold-rgb),0.12);border-color:var(--border2);color:var(--gold)}
.reaction-trigger{
  background:var(--bg2);border:0.5px solid var(--border);border-radius:20px;
  padding:5px 13px;font-size:13px;color:var(--text3);cursor:pointer;
}
.reaction-trigger:hover{color:var(--gold);border-color:var(--gold);}
.page-head-wrap{
  background:linear-gradient(180deg,rgba(var(--gold-rgb),0.05) 0%,transparent 100%);
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
  position:absolute;top:12px;right:16px;font-size:24px;cursor:pointer;color:var(--text3);
}
.modal-close:hover{color:var(--gold);}
.modal-avatar{width:80px;height:80px;border-radius:50%;margin:0 auto 12px;background:linear-gradient(145deg,rgba(var(--gold-rgb),0.24),var(--bg2));display:flex;align-items:center;justify-content:center;font-size:32px;border:2px solid var(--gold);box-shadow:0 6px 18px rgba(var(--gold-rgb),0.25),inset 0 1px 1px rgba(255,255,255,0.1);}
.modal-name{font-size:20px;font-weight:700;color:var(--gold);}
.modal-stats{display:flex;justify-content:space-around;margin:16px 0;}
.modal-stat{text-align:center;}
.modal-stat-num{font-size:19px;font-weight:700;color:var(--text);}
.modal-stat-lbl{font-size:12px;color:var(--text3);}
.modal-btn{width:100%;padding:14px;margin-top:10px;border:none;border-radius:40px;font-size:15px;font-weight:700;cursor:pointer;}
.modal-btn-primary{background:var(--gold);color:#0c0b09;box-shadow:0 4px 14px rgba(var(--gold-rgb),0.3);}
.modal-btn-primary:active{transform:scale(0.97);}
.modal-btn-secondary{background:var(--bg2);border:1.5px solid var(--border);color:var(--text);}
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
      <div id="vent-sex-row" style="display:none;padding:0 16px;margin-top:12px">
        <div style="font-size:12.5px;color:var(--text2);line-height:1.4;margin-bottom:6px">Show your sex under the vent number on this post?</div>
        <div style="display:flex;gap:18px">
          <label style="font-size:13px;color:var(--text);display:flex;align-items:center;gap:6px"><input type="radio" name="vent-show-sex" value="no" checked> No</label>
          <label style="font-size:13px;color:var(--text);display:flex;align-items:center;gap:6px"><input type="radio" name="vent-show-sex" value="yes"> Yes</label>
        </div>
      </div>
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
    <div class="page" id="page-edit">
      <button class="back-btn" onclick="meBack()"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>Me</button>
      <div class="ed-head"><h1>Edit profile</h1><div class="page-head-sub">How you appear to others</div></div>
      <div class="ed-preview" id="ed-preview"></div>
      <div class="ed-card">
        <div class="ed-label"><span>Avatar</span><button type="button" class="ed-link" onclick="clearAvatar()">Use default</button></div>
        <div class="av-tabs" id="av-tabs"></div>
        <div class="av-grid" id="ep-emoji"></div>
      </div>
      <div class="ed-card">
        <label class="ed-label" for="ep-name"><span>Display name</span><span class="ed-count" id="ep-name-cnt">0/30</span></label>
        <input id="ep-name" class="ed-input" type="text" maxlength="30" placeholder="Your anonymous name" autocomplete="off">
        <label class="ed-label" for="ep-bio" style="margin-top:18px"><span>Bio</span><span class="ed-count" id="ep-bio-cnt">0/150</span></label>
        <textarea id="ep-bio" class="ed-input" rows="3" maxlength="150" placeholder="A line or two about you" style="resize:none"></textarea>
        <div class="ed-hint">Your name and bio show next to your vents and replies. You can hide the bio under Privacy.</div>
      </div>
      <div class="ed-actions"><button class="btn-gold" id="save-profile-btn" disabled>Save changes</button><button type="button" class="ed-cancel" onclick="meBack()">Cancel</button></div>
    </div>
    <div class="page" id="page-settings">
      <div class="me-head"><h1>Me</h1><div class="page-head-sub">Your profile and preferences</div></div>
      <div id="me-hero"></div>
      <div id="me-recent"></div>

      <div class="me-label">Appearance</div>
      <div class="me-group">
        <div class="me-row stack">
          <div class="me-row-head"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></div><div class="me-text"><div class="me-title">Theme</div><div class="me-sub">Auto follows your device</div></div></div>
          <div class="seg" id="seg-theme">
            <button type="button" data-v="dark" onclick="setTheme('dark')"><svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>Dark</button>
            <button type="button" data-v="light" onclick="setTheme('light')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>Light</button>
            <button type="button" data-v="auto" onclick="setTheme('auto')"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/></svg>Auto</button>
          </div>
        </div>
        <div class="me-row stack">
          <div class="me-row-head"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg></div><div class="me-text"><div class="me-title">Accent color</div><div class="me-sub" id="accent-name">Gold</div></div></div>
          <div class="swatches" id="swatches"></div>
        </div>
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M2 8v8"/><path d="M6 6v12"/><rect x="9" y="3" width="6" height="18" rx="2"/><path d="M18 6v12"/><path d="M22 8v8"/></svg></div><div class="me-text"><div class="me-title">Haptic feedback</div><div class="me-sub">A light tap when you press things</div></div><label class="toggle"><input type="checkbox" id="set-haptics" onchange="setHaptics(this.checked)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
      </div>

      <div class="me-label">Notifications</div>
      <div class="me-group">
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></div><div class="me-text"><div class="me-title">Replies and interactions</div><div class="me-sub">Get a message from the bot when someone responds</div></div><label class="toggle"><input type="checkbox" id="set-notif" onchange="saveSetting('notifications',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
      </div>

      <div class="me-label">Privacy</div>
      <div class="me-group">
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></div><div class="me-text"><div class="me-title">Public profile</div><div class="me-sub">Let others see your profile stats</div></div><label class="toggle"><input type="checkbox" id="set-priv" onchange="saveSetting('privacy_public',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div><div class="me-text"><div class="me-title">Hide aura and points</div><div class="me-sub">Others see Hidden instead</div></div><label class="toggle"><input type="checkbox" id="set-hide-aura" onchange="saveSetting('hide_aura',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><line x1="17" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="17" y1="18" x2="3" y2="18"/></svg></div><div class="me-text"><div class="me-title">Hide bio</div><div class="me-sub">Keep your bio to yourself</div></div><label class="toggle"><input type="checkbox" id="set-hide-bio" onchange="saveSetting('hide_bio',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
        <div class="me-row"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></div><div class="me-text"><div class="me-title">Hide follower count</div><div class="me-sub">Your followers stay private</div></div><label class="toggle"><input type="checkbox" id="set-hide-followers" onchange="saveSetting('hide_follower_count',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
        <div class="me-row" id="row-hide-role" style="display:none"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div><div class="me-text"><div class="me-title">Hide role</div><div class="me-sub">Don't show that you're an administrator</div></div><label class="toggle"><input type="checkbox" id="set-hide-role" onchange="saveSetting('hide_role',this)"><div class="toggle-track"></div><div class="toggle-thumb"></div></label></div>
      </div>
      <div class="me-note">You and administrators always see your full profile. Changes save as soon as you toggle them.</div>

      <div class="me-label">Community</div>
      <div class="me-group">
        <div class="me-row tap" onclick="inviteFriends()"><div class="me-ico"><svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg></div><div class="me-text"><div class="me-title">Invite friends</div><div class="me-sub">Share the bot link in Telegram</div></div><svg class="me-chev" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></div>
        <div class="me-row tap" onclick="openSupport()"><div class="me-ico"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/><line x1="4.93" y1="4.93" x2="9.17" y2="9.17"/><line x1="14.83" y1="14.83" x2="19.07" y2="19.07"/><line x1="14.83" y1="9.17" x2="19.07" y2="4.93"/><line x1="4.93" y1="19.07" x2="9.17" y2="14.83"/></svg></div><div class="me-text"><div class="me-title">Contact support</div><div class="me-sub">Report a problem or ask a question</div></div><svg class="me-chev" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></div>
      </div>
      <div class="me-foot">Christian Vent · Built by <a href="https://t.me/YIDIDIYATAMIRUU">@YIDIDIYATAMIRUU</a></div>
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
  <div id="reply-bar" class="reply-bar" style="display:none"></div>
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
    <div id="cr-reply-bar" class="reply-bar" style="display:none"></div>
    <div id="chat-media-preview" style="display:none"></div>
    <div style="display:flex;align-items:center;gap:8px">
      <button type="button" class="media-attach-btn" id="chat-attach-btn" title="Attach media"><svg viewBox="0 0 24 24"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
      <input type="file" id="chat-file-input" style="display:none" accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.gif">
      <textarea id="cr-txt" placeholder="Message…" rows="1" style="flex:1;background:var(--bg2);border:0.5px solid var(--border);border-radius:20px;padding:11px 16px;color:var(--text);font-family:'Inter',sans-serif;font-size:16px;outline:none;resize:none;min-height:42px;max-height:100px;"></textarea>
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

<script>
'use strict';
const API = location.origin;
let UID = null, profileCache = null, crPartnerId = null, crPoll = null, currentPostAuthorId = null;
let pendingMedia = null, pendingCommentMedia = null, pendingChatMedia = null;
let feedPage = 1, feedHasMore = true, feedLoading = false, searchQ = '', currentPostId = null;
let chatsCache = [];
let crMsgsCache = [];
let crOlderMsgs = [], crHasMore = false, crLoadingOlder = false;
let chatsPage = 1, chatsHasMore = false, chatsLoadingMore = false;
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
  man: ic('<circle cx="12" cy="7" r="4"/><rect x="7" y="13" width="10" height="8" rx="1.5"/>'),
  woman: ic('<circle cx="12" cy="7" r="4"/><path d="M12 11.5L6.3 21h11.4z"/>'),
  mic: ic('<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>'),
  paperclip: ic('<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>'),
  close: ic('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  reply: ic('<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>'),
  thumbsUp: ic('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>'),
  thumbsDown: ic('<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/>')
};
// ===== Premium avatar set =====
// Avatars are still STORED as the original emoji (the Telegram bot shows them too).
// The mini app renders each one as a hand-drawn duotone SVG, looked up by the emoji's
// first code point (hex), so old and new variants of the same emoji resolve identically.
const AV_HUE={gold:'#e8c97a',amber:'#f4a259',rose:'#f28ba8',sky:'#7ec8ff',mint:'#6fe0b0',violet:'#b9a2ff',ivory:'#efe8d8',coral:'#ff8a75'};
const AV_CATS=[['faith','Faith'],['light','Light'],['creatures','Creatures'],['nature','Nature'],['mood','Mood'],['life','Life'],['care','Tech & care']];
const AV=(function(){
  const F=' fill="currentColor" fill-opacity=".22"';
  const f=d=>'<path d="'+d+'"'+F+'/>';
  const s=d=>'<path d="'+d+'"/>';
  const c=(x,y,r)=>'<circle cx="'+x+'" cy="'+y+'" r="'+r+'"'+F+'/>';
  const o=(x,y,r)=>'<circle cx="'+x+'" cy="'+y+'" r="'+r+'"/>';
  const k=(x,y,r)=>'<circle cx="'+x+'" cy="'+y+'" r="'+(r||1)+'" fill="currentColor" stroke="none"/>';
  const b=(x,y,w,h,rx)=>'<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" rx="'+(rx||0)+'"'+F+'/>';
  const solid=el=>el.replace('fill-opacity=".22"','fill-opacity=".8"');
  const rot=(a,cx,cy,inner)=>'<g transform="rotate('+a+' '+cx+' '+cy+')">'+inner+'</g>';
  const star=(cx,cy,R,r,n,off)=>{let p='';for(let i=0;i<n*2;i++){const a=(off||0)+i*Math.PI/n-Math.PI/2,rad=i%2?r:R;p+=(i?'L':'M')+(cx+rad*Math.cos(a)).toFixed(2)+' '+(cy+rad*Math.sin(a)).toFixed(2)}return p+'z'};
  const sparkle=(cx,cy,R)=>'M'+cx+' '+(cy-R)+'Q'+cx+' '+cy+' '+(cx+R)+' '+cy+'Q'+cx+' '+cy+' '+cx+' '+(cy+R)+'Q'+cx+' '+cy+' '+(cx-R)+' '+cy+'Q'+cx+' '+cy+' '+cx+' '+(cy-R)+'z';
  const scallop=(cx,cy,R,n,rr)=>{let p='';for(let i=0;i<=n;i++){const a=i*2*Math.PI/n-Math.PI/2,x=(cx+R*Math.cos(a)).toFixed(2),y=(cy+R*Math.sin(a)).toFixed(2);p+=i?('A'+rr+' '+rr+' 0 0 1 '+x+' '+y):('M'+x+' '+y)}return p+'z'};
  const A={};
  const add=(code,cat,name,hue,vs,body)=>{A[code]={c:cat,n:name,h:AV_HUE[hue],vs:vs,g:body}};

  // ---- Faith ----
  add('271d','faith','Cross','gold',1, f('M13 3h6v6h7v6h-7v14h-6V15H6V9h7z')+s('M16 6.5v19'));
  add('1f64f','faith','Prayer','gold',0, f('M16 5.500C12.800 8.500 9.800 13 9.200 18.500L10.500 26.500 16 24z')+f('M16 5.500c3.200 3 6.200 7.500 6.800 13l-1.300 8L16 24z')+s('M16 5.500V24M9.300 19.500c-1.600-1.100-3.400-.5-3.600 1M22.700 19.500c1.600-1.100 3.400-.5 3.600 1M16 1.500v1.800M10.500 3.200l1.100 1.400M21.500 3.200l-1.100 1.400'));
  add('1f54a','faith','Dove','ivory',1, f('M23.500 15.500C22.500 20 18.500 24.500 12 25.200L7.200 27.800 8.600 24.600 3.500 25 8.300 22.200C14.500 21.800 19 18.800 21 14.800z')+c(23.200,12.200,3.100)+f('M26 11.500l3.800 1.300-3.800 1.600z')+f('M17.500 19C12 17.500 8.500 11.500 9.500 4.500c5.200 1.800 9 7 10 12.500z')+k(24,11.600,.9)+s('M12.200 8.500c2 2.200 3.500 5 4.200 8'));
  add('26ea','faith','Church','gold',0, f('M7 28V16l9-5.5 9 5.5v12z')+s('M16 10.5V4.5M13.8 7h4.4M4 28h24M13 28v-6a3 3 0 0 1 6 0v6')+k(16,16.5,1.2));
  add('1f492','faith','Chapel','rose',0, f('M6 28V15l10-6 10 6v13z')+s('M16 9V3.5M13.8 6h4.4M3.5 28h25')+f('M16 25c-3.2-2.1-4.7-3.8-4.7-5.8a2.5 2.5 0 0 1 4.7-1.3 2.5 2.5 0 0 1 4.7 1.3c0 2-1.5 3.7-4.7 5.8z'));
  (function(){let beads='';for(let i=0;i<9;i++){const a=Math.PI*0.5+0.5+i*(2*Math.PI-1.0)/8;beads+=c((16+9*Math.cos(a)).toFixed(2),(12.5+9*Math.sin(a)).toFixed(2),2.1)}
    add('1f4ff','faith','Beads','violet',0, beads+s('M16 21.5v3')+f('M14.5 29h3v-4.5h-3z')); })();
  add('1f56f','faith','Candle','amber',1, b(10.5,15,11,13,1.8)+s('M16 15v-2M7.5 28h17M10.5 19.5c1.7 0 2.4 1 2.4 2.6')+f('M16 3.5c2.7 2.9 3.8 5 3.8 6.8a3.8 3.8 0 0 1-7.6 0c0-1.8 1.1-3.9 3.8-6.8z'));
  (function(){let g=s('M16 29V9');for(let i=0;i<3;i++){const y=12+i*5.5;g+=f('M16 '+y+'c-3.2-.4-5-2.6-5-5.4 3.3.3 5 2.3 5 5.4z')+f('M16 '+y+'c3.2-.4 5-2.6 5-5.4-3.3.3-5 2.3-5 5.4z')}
    g+=f('M16 3c1.7 1.7 2.3 3.4 0 5.6-2.3-2.2-1.7-3.9 0-5.6z');add('1f33e','faith','Wheat','amber',0,g)})();
  add('1f4d6','faith','Bible','sky',0, '<g transform="translate(2 2) scale(1.1667)" stroke-width="1.37">'+f('M2 4h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z')+f('M22 4h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z')+'</g>');
  add('1f397','faith','Ribbon','rose',1, f('M16 3a7.5 7.5 0 0 1 5.4 12.7L16 20.5l-5.4-4.8A7.5 7.5 0 0 1 16 3z')+f('M11.5 17.5L8 29l5-2.6 3.5 2.6 1.5-7M20.5 17.5L24 29l-5-2.6')+k(16,10,1.4));

  // ---- Light & strength ----
  add('1f31f','light','Glow star','gold',0, f(star(16,16,8.5,3.7,5))+s('M16 2.5v3M16 26.5v3M2.5 16h3M26.5 16h3M6.2 6.2l2 2M23.8 6.2l-2 2M6.2 25.8l2-2M23.8 25.8l-2-2'));
  add('2b50','light','Star','gold',0, f(star(16,16.5,12.5,5.4,5)));
  add('2728','light','Sparkle','amber',0, f(sparkle(13.5,17.5,10.5))+f(sparkle(24.5,7.5,5))+f(sparkle(25,25,3.4)));
  add('1f320','light','Shooting star','sky',0, f(star(22,10.5,6.5,2.9,5))+s('M17.5 15L5 27.5M12.8 10.8L6.5 17M21.5 19.5L15 26'));
  (function(){let r='';for(let i=0;i<8;i++){const a=i*Math.PI/4,x1=16+8.5*Math.cos(a),y1=16+8.5*Math.sin(a),x2=16+12.5*Math.cos(a),y2=16+12.5*Math.sin(a);r+=s('M'+x1.toFixed(2)+' '+y1.toFixed(2)+'L'+x2.toFixed(2)+' '+y2.toFixed(2))}
    add('1f506','light','Radiance','amber',0, c(16,16,5.2)+r)})();
  add('26a1','light','Lightning','amber',0, f('M18.5 2.5L6.5 17.5h8L12.8 29.5l12.7-16h-8z'));
  add('1f4a5','light','Burst','coral',0, f(star(16,16,13,6.2,9,0.2))+f(star(16,16,5.2,2.6,5,0.4)));
  add('1f525','light','Flame','coral',0, f('M16 2.5c1.2 4.3 7.5 8 7.5 15a7.5 7.5 0 0 1-15 0c0-3.4 1.7-5.4 3.4-7.2.3 2.1 1.1 3.4 2.3 4.2C13.4 10.6 13.2 6 16 2.5z')+s('M16 27a3.8 3.8 0 0 0 3.8-3.8c0-2.4-1.8-3.6-3.8-6-2 2.4-3.8 3.6-3.8 6A3.8 3.8 0 0 0 16 27z'));
  add('1f48e','light','Gem','sky',0, f('M8.5 4.5h15l5 6.5L16 28 3.5 11z')+s('M3.5 11h25M12.5 4.5L10 11l6 17M19.5 4.5L22 11l-6 17'));
  add('1f6e1','light','Shield','gold',1, f('M16 3l10 3.6v8.4c0 6.5-4.3 11.3-10 14-5.7-2.7-10-7.5-10-14V6.6z')+s('M16 9v12M11 13.5h10'));
  add('2764','light','Heart','rose',1, f('M16 28.5C6.2 21.8 3.5 16.4 3.5 11.8A6.3 6.3 0 0 1 16 9.5a6.3 6.3 0 0 1 12.5 2.3c0 4.6-2.7 10-12.5 16.7z')+s('M8.5 11.5a3.2 3.2 0 0 1 3.2-3.2'));
  add('2694','light','Swords','ivory',1, s('M6 6l14 14M26 6L12 20M17 23l6-6M9 17l6 6M20 20l6 6M12 20l-6 6')+k(27.2,27.2,1.6)+k(4.8,27.2,1.6)+f('M6 6l3.2.8-.8 3.2zM26 6l-3.2.8.8 3.2z'));
  add('1f396','light','Medal','amber',1, f('M10.5 3l5.5 9.5M21.5 3L16 12.5')+c(16,19.5,7.5)+f(star(16,19.5,3.8,1.7,5)));
  add('1f511','light','Key','gold',0, c(9.5,22.5,5.5)+k(9.5,22.5,1.4)+s('M13.6 18.6L27 5.2M22.5 9.7l3.3 3.3M19 13.2l2.6 2.6'));

  // ---- Creatures ----
  add('1f981','creatures','Lion','amber',0, f(scallop(16,16.5,11.5,12,3.1))+c(16,17,6.7)+k(13.2,15.8,1.1)+k(18.8,15.8,1.1)+f('M14.3 19.2h3.4L16 21z')+s('M16 21v1.4M13.6 23.2c1.2.9 3.6.9 4.8 0'));
  add('1f98a','creatures','Fox','coral',0, f('M3.5 4.5L12 9h8l8.5-4.5L27.5 16 16 28.5 4.5 16z')+s('M4.5 16L16 20l11.5-4M7.5 8l3.2 2M24.5 8l-3.2 2')+k(11.5,14.5,1.2)+k(20.5,14.5,1.2)+k(16,26.5,1.6));
  add('1f409','creatures','Dragon','mint',0, f('M16 29c-5.2 0-8.7-3-9.2-8.3.5-5.4 3.3-9 9.2-9s8.7 3.6 9.2 9C24.7 26 21.2 29 16 29z')+s('M10.2 13.5C6.5 11 5 7 6.6 3c1.7 3 4 5.2 6.4 6.3M21.8 13.5C25.5 11 27 7 25.4 3c-1.7 3-4 5.2-6.4 6.3M10.5 17.5l3.2 1.3M21.5 17.5l-3.2 1.3M12 26c2.6 1.4 5.4 1.4 8 0M13.5 12c.8-1.7 1.6-2.7 2.5-3.3.9.6 1.7 1.6 2.5 3.3')+k(14,23.4,.9)+k(18,23.4,.9));
  add('1f43c','creatures','Panda','ivory',0, solid(c(7.2,8,3.6))+solid(c(24.8,8,3.6))+c(16,17.5,10.8)+solid('<ellipse cx="11.2" cy="16" rx="2.8" ry="3.7" transform="rotate(22 11.2 16)"'+F+'/>')+solid('<ellipse cx="20.8" cy="16" rx="2.8" ry="3.7" transform="rotate(-22 20.8 16)"'+F+'/>')+solid('<ellipse cx="16" cy="20.7" rx="2" ry="1.4"'+F+'/>')+s('M13.4 23.3c1.2 1.1 4 1.1 5.2 0'));
  add('1f984','creatures','Unicorn','violet',0, f('M12 28.500c-1-6.500-.5-11.500 2.500-15.500L16 9.200c1.500-1.200 3.500-1.200 5 0l.5 2.800 5.300 4.200c1.200 1 1 2.800-.3 3.600l-3 1c-.5 1.700-2 3.400-4 4L19 28.500z')+f('M17.500 8.500L19.800 1.500 21 9z')+f('M15.500 10l-1-4.500 3 3z')+s('M12.500 12C8.500 14 7 19 8 26M13.500 16c-3 2-3.700 5.500-3 9')+k(21,13.500,1.100)+k(25.500,18.200,.8));
  add('1f985','creatures','Eagle','amber',0, f('M5 28C4 20 8 13 14 10c1.500-2.800 4.500-4.500 8-3.500 2 .6 3.500 2.600 3.500 5L29 15.500l-4.500.7c-.5 2.300-2 3.800-4 4.800L22 28z')+s('M18 9.500l5 2.200M9 24.500c2-1 4-1 6 0M10.200 19.500c2-1 3.500-1 5 0')+k(21,12.200,1.100));
  add('1f989','creatures','Owl','amber',0, f('M16 29.5c-5 0-9-4-9-10V8.5c2.2 1 4.2.2 5.4-1.8 1.5 1.1 5.7 1.1 7.2 0 1.2 2 3.2 2.8 5.4 1.8v11c0 6-4 10-9 10z')+c(11.4,14.5,3.7)+c(20.6,14.5,3.7)+k(11.4,14.5,1.3)+k(20.6,14.5,1.3)+f('M14.4 18.4h3.2L16 21.6z')+s('M12.2 25c1.2.9 2.4 1.3 3.8 1.3s2.6-.4 3.8-1.3'));
  add('1f98b','creatures','Butterfly','violet',0, f('M16 16C11 8 5 6 4.4 9.8 4 13 7.5 16 16 16z')+f('M16 16c-6 0-10 4-9.4 8 .4 3 4.2 3.4 6.2 1.4 2-2 3-5.4 3.2-9.4z')+f('M16 16c5-8 11-10 11.6-6.2.4 3.2-3.1 6.2-11.6 6.2z')+f('M16 16c6 0 10 4 9.4 8-.4 3-4.2 3.4-6.2 1.4-2-2-3-5.4-3.2-9.4z')+s('M16 9v17M16 9c-.9-2.8-2.6-4.4-4-5M16 9c.9-2.8 2.6-4.4 4-5'));
  add('1f422','creatures','Turtle','mint',0, f('M6 20c0-6 4-10 10-10s10 4 10 10z')+s('M16 10v10M11.2 12.4l2.4 7.6M20.8 12.4L18.4 20M5 20h22M9.5 20v3.8M14 20v3.8M18 20v3.8M22.5 20v3.8M5.5 20.5L3 22')+c(28.4,17.6,2.8)+k(29.2,17,.7));
  add('1f98c','creatures','Deer','amber',0, f('M11 14.5h10l-1.6 10c-.3 2.1-1.5 3.6-3.4 3.6s-3.1-1.5-3.4-3.6z')+f('M11 14.5L5 12.6c-.3 2.7 1.1 4.6 4.4 5.3zM21 14.5l6-1.9c.3 2.7-1.1 4.6-4.4 5.3z')+s('M12.2 14.2c-1-4-1.2-7-3.2-10.2M10 9.6L6 8.4M11.2 7.2L10 4M19.8 14.2c1-4 1.2-7 3.2-10.2M22 9.6l4-1.2M20.8 7.2L22 4')+k(13.6,18.8,1)+k(18.4,18.8,1)+k(16,25.2,1.5));
  add('1f41d','creatures','Bee','amber',0, f('M13.5 14C10 9.5 11.5 4 15 5c2.4 1 2.2 5.5.4 9zM19 13.5c3.6-4.5 2.4-9.7-1-9-2.5.8-2.3 5.2-.7 9z')+c(16.5,20,8.5)+s('M13.2 13.6v12.8M18 12.6v14.8M6 20h-2M26.5 20h2')+c(6.6,19,3.1)+s('M5.4 16.2L4 13.6M8.2 16l.4-2.9'));

  // ---- Nature ----
  add('1f308','nature','Rainbow','rose',0, s('M3 25a13 13 0 0 1 26 0')+s('M7.5 25a8.5 8.5 0 0 1 17 0')+s('M12 25a4 4 0 0 1 8 0')+c(4,25.5,1.8)+c(28,25.5,1.8));
  (function(){let p='';for(let i=0;i<5;i++)p+=rot(i*72,16,16,f('M16 16c-4.2-3.8-4.4-9.4 0-12.5 4.4 3.1 4.2 8.7 0 12.5z'));add('1f338','nature','Blossom','rose',0,p+c(16,16,3.1)+k(16,16,1))})();
  add('1f319','nature','Moon','gold',0, '<g transform="translate(2 2) scale(1.1667)" stroke-width="1.37">'+f('M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z')+'</g>'+k(24.5,6.5,1)+k(27.5,12.5,.8));
  add('1f34e','nature','Apple','coral',0, f('M16 10.5c-3-2-9-1.4-10 5.2-.8 6 3 13 6.4 13 1.6 0 2.2-.9 3.6-.9s2 .9 3.6.9c3.4 0 7.2-7 6.4-13-1-6.6-7-7.2-10-5.2z')+s('M16 10.5c0-3 1-5.2 3-6.7')+f('M19.5 5c2-1.6 5-1.6 6.2 0-1.6 2.2-4.6 2.5-6.2 0z'));
  (function(){let p='';for(let i=0;i<4;i++)p+=rot(i*90,16,16,f('M16 16C12 14 9.8 11 11.4 8.4c1.4-2.2 3.8-1.6 4.6.6.8-2.2 3.2-2.8 4.6-.6C22.2 11 20 14 16 16z'));add('1f340','nature','Clover','mint',0,p+s('M16 16.5c.2 5 2 9 5 12'))})();
  add('1f33f','nature','Herb','mint',0, s('M7 29C9 18 14 9.5 25.5 4.5')+f('M11.8 21.6C8 21.2 5.8 18.8 5.6 15.6c3.8.2 6.2 2.4 6.2 6z')+f('M13.6 17.8c3.4.4 5.6-1.2 6.6-4.2-3.4-.4-6 .8-6.6 4.2z')+f('M16.6 13.2C13.4 12 12 9.4 12.4 6.4c3.2 1.2 4.6 3.6 4.2 6.8z')+f('M19.6 10.4c3.2.6 5.4-.8 6.6-3.6-3.2-.6-5.8.4-6.6 3.6z'));
  (function(){let p='';for(let i=0;i<12;i++)p+=rot(i*30,16,16,'<ellipse cx="16" cy="7" rx="2.3" ry="4.1"'+F+'/>');add('1f33b','nature','Sunflower','amber',0,p+solid(c(16,16,5))+k(14.4,14.6,.7)+k(17.6,14.6,.7)+k(16,17.6,.7)+k(14.2,17.2,.7)+k(17.8,17.2,.7))})();
  add('1f30a','nature','Waves','sky',0, s('M3 10c3-3 5-3 8 0s5 3 8 0 5-3 8 0')+s('M3 17c3-3 5-3 8 0s5 3 8 0 5-3 8 0')+s('M3 24c3-3 5-3 8 0s5 3 8 0 5-3 8 0'));

  // ---- Mood ----
  const face=function(inner,cy){return c(16,cy||16,12)+inner};
  add('1f60a','mood','Happy','gold',0, face(s('M10.2 14.4c.8-1.5 2.4-1.5 3.2 0M18.6 14.4c.8-1.5 2.4-1.5 3.2 0M10.5 19.4c1.5 2.9 3.3 3.8 5.5 3.8s4-.9 5.5-3.8')));
  add('1f604','mood','Joy','gold',0, face(s('M10.2 13.6c.8-1.5 2.4-1.5 3.2 0M18.6 13.6c.8-1.5 2.4-1.5 3.2 0')+f('M9.6 18.2h12.8c-.5 4.3-3 6.8-6.4 6.8s-5.9-2.5-6.4-6.8z')));
  add('1f622','mood','Sad','sky',0, face(k(11.600,15,1.300)+k(20.400,15,1.300)+s('M9.600 12.800l4-1.200M22.400 12.800l-4-1.200M11.400 23.600c1.400-1.800 3-2.600 4.600-2.600s3.200.800 4.600 2.600')+f('M21.400 18.800c1.500 2 2.100 3.100 2.100 4.100a2.100 2.100 0 0 1-4.200 0c0-1 .6-2.100 2.100-4.100z')));
  add('1f614','mood','Pensive','violet',0, face(s('M10 14.800c1 1.200 2.700 1.200 3.700 0M18.300 14.800c1 1.200 2.700 1.200 3.700 0M9.600 12.200l4.200-.9M22.400 12.200l-4.200-.9M12.600 22.800c2-1.400 4.800-1.400 6.800 0')));
  add('1f60c','mood','Calm','mint',0, face(s('M10 15c1.300 1.500 3 1.500 4.300 0M17.700 15c1.300 1.500 3 1.500 4.300 0M11.800 20.200c2.600 2 5.800 2 8.400 0')));
  add('1f607','mood','Angelic','gold',0, face(s('M10.600 18c.8-1.400 2.200-1.400 3 0M18.400 18c.8-1.400 2.200-1.400 3 0M11.500 22.500c2.500 1.800 6.500 1.800 9 0'),18.500)+'<ellipse cx="16" cy="4.800" rx="6.500" ry="2"/>');
  add('1f972','mood','Grateful','rose',0, face(k(11.400,15,1.300)+k(20.600,15,1.300)+s('M10.200 19.800c1.500 2.500 3.300 3.500 5.800 3.500s4.300-1 5.800-3.500')+f('M23.800 13.600c1.100 1.400 1.600 2.300 1.600 3.100a1.600 1.600 0 0 1-3.200 0c0-.8.500-1.700 1.600-3.100z'),16.500));
  add('1f634','mood','Sleepy','sky',0, c(14,18.200,10.700)+s('M9 17h4.200M15.200 17h4.200')+c(14.200,23,1.700)+s('M21.500 4.500H26L21.500 10H26M27 1.500h3l-3 3.500h3'));

  // ---- Life ----
  add('1f476','life','Little one','rose',0, c(6.200,17,1.900)+c(25.800,17,1.900)+c(16,17.200,10)+s('M16 7.200c-1.300-3.500 2.700-5.200 4-2.600')+k(12.200,16.500,1.200)+k(19.800,16.500,1.200)+s('M13.500 21c1.600 1.300 3.400 1.300 5 0'));
  add('1f9d1','life','Person','gold',0, c(16,10.500,5.500)+f('M5.500 28.500c0-6.200 4.500-10.200 10.500-10.200s10.500 4 10.500 10.200z')+s('M12.200 18.800L16 23.500l3.800-4.700'));
  add('1f474','life','Elder','ivory',0, f('M8.500 13.500C8.500 8.500 11.500 5.500 16 5.500s7.500 3 7.500 8c0 5.500-3 8.500-7.500 8.500s-7.500-3-7.500-8.500z')+s('M8.300 12c-2 .5-3 2.500-2.500 5M23.700 12c2 .5 3 2.500 2.500 5M9.500 18.500C9.800 24.500 12.500 28.500 16 28.500s6.200-4 6.500-10M10.800 10.600h3.600M17.600 10.600h3.600')+k(12.600,13.600,1.100)+k(19.400,13.600,1.100)+s('M12.500 19c1.800-1.300 2.800-1.300 3.500 0 .7-1.300 1.700-1.300 3.500 0M14 22.800c1.200.6 2.800.6 4 0'));
  add('1f6b6','life','Walker','sky',0, c(17.500,5.500,2.800)+s('M17 10.500l-2.500 7 3.500 3.500V29M14.500 17.500L11 29M15.800 12.500l-4 2.400-.8 4.100M16.600 12l4.200 2.600 2.600 3.400'));
  add('1f3c3','life','Runner','coral',0, c(21.500,6,2.800)+s('M20 10.500l-3.500 6.500 4 3.500 1.500 7.500M16.500 17l-5 .5-3 5.500M19 11.500l-5.500 1.800-.8 3.700M20.300 11.800l4.200 2.200 3 .5M16.500 17l-.5 5-5 4.500'));
  add('1f4da','life','Books','amber',0, b(4.500,6,6,22,1.600)+b(11.500,3,6,25,1.600)+f('M19 8.500l5.200-1.600 4.200 20.500-5.400 1.300z')+s('M4.500 12h6M4.500 23h6M11.500 9h6M11.500 22h6'));
  add('1f4d3','life','Notebook','violet',0, b(8,3,18,26,2.500)+s('M4.500 8.500h6M4.500 14h6M4.500 19.500h6M4.500 25h6M14.500 10.500h8M14.500 15.500h8M14.500 20.500h5'));
  add('1f3a8','life','Palette','coral',0, f('M16 3C8.800 3 3 8.800 3 16s5.800 13 13 13c1.700 0 3-1.300 3-3 0-.8-.3-1.500-.8-2-.5-.5-.7-1.200-.7-1.800 0-1.400 1.100-2.500 2.500-2.500H24c3 0 5-2.200 5-5C29 8.400 23.200 3 16 3z')+k(9.500,15,1.700)+k(12.500,9,1.700)+k(19.500,8.500,1.700)+k(24,13,1.700));
  add('1f3b5','life','Melody','violet',0, s('M12 25.500V7l14-3v18')+s('M12 12l14-3')+c(8.500,25.500,3.500)+c(22.500,22.500,3.500));
  add('1f3b9','life','Keys','ivory',0, b(3,7,26,18,2.500)+s('M9.500 7v18M16 7v18M22.500 7v18')+solid('<rect x="7.400" y="7" width="4.200" height="10" rx="1"'+F+'/>')+solid('<rect x="13.900" y="7" width="4.200" height="10" rx="1"'+F+'/>')+solid('<rect x="20.400" y="7" width="4.200" height="10" rx="1"'+F+'/>'));
  add('1f3a3','life','Fishing','mint',0, s('M5 28C9 18 15 9 24 5')+s('M24 5v13')+c(24,20.500,2.500)+s('M3 28.500c2-1.500 4-1.500 6 0s4 1.500 6 0 4-1.500 6 0 4 1.500 6 0'));
  add('1f9d7','life','Summit','sky',0, f('M2.500 28L13 9l5.500 8.500 3-4L29.500 28z')+s('M13 9V3l5.500 2.200L13 7.500M9.500 15.500l3 2.500 2.500-2.500')+'');

  // ---- Tech & care ----
  add('1f4bb','care','Laptop','sky',0, b(6,6.500,20,14.500,2)+f('M2.500 24h27l-2 3.500H4.500z'));
  add('1f4f1','care','Phone','sky',0, b(9,3,14,26,3.200)+s('M14 6.500h4M14 25.500h4'));
  add('231a','care','Watch','sky',0, f('M12 3h8l1.200 6.500h-10.400z')+f('M10.800 22.500h10.400L20 29h-8z')+c(16,16,7.500)+s('M16 11.800V16.500l3 1.800'));
  add('1f5a5','care','Desktop','sky',1, b(3,4,26,17,2)+s('M12 28h8M16 21v7M3 16h26'));
  add('2695','care','Medical','mint',1, s('M16 5v24')+c(16,4,1.500)+s('M19.500 9.500c-7 0-7.500 5.500-3.500 7.500s3.500 6.500-3.500 6.500')+s('M16 7.500c-2.500-2.500-5-3-8-2 1.500 2.500 4.500 3.500 8 2zM16 7.500c2.500-2.500 5-3 8-2-1.500 2.500-4.500 3.500-8 2z'));
  add('1fa7a','care','Stethoscope','mint',0, s('M8 3.500v8a6 6 0 0 0 12 0v-8M14 17.500V21a5.500 5.500 0 0 0 11 0v-2.500')+c(25,16,3)+k(8,3.500,1.300)+k(20,3.500,1.300));
  add('1f48a','care','Capsule','coral',0, rot(-45,16,16,b(2.500,10,27,12,6)+solid(f('M8.500 10H16v12H8.500a6 6 0 0 1 0-12z'))+s('M16 10v12')));
  add('1f9ec','care','Helix','violet',0, s('M10 3c0 7.500 12 7.500 12 13s-12 5.500-12 13M22 3c0 7.500-12 7.500-12 13s12 5.500 12 13M11.800 8h8.400M10.400 16h11.200M11.800 24h8.400'));

  // ---- Defaults (not in the picker) ----
  add('1f464','','You','gold',0, c(16,10.500,5.500)+f('M5.500 28.500c0-6.200 4.500-10.200 10.500-10.200s10.500 4 10.500 10.200z'));
  add('1f468','','Man','sky',0, c(16,10.500,5.500)+f('M5.500 28.500c0-6.200 4.500-10.200 10.500-10.200s10.500 4 10.500 10.200z')+s('M16 19.500l-1.600 3.200L16 28l1.600-5.300z'));
  add('1f469','','Woman','rose',0, f('M8.500 14C8 7.500 11.500 4 16 4s8 3.500 7.500 10c0 3 1 5.200 2.200 7H6.300c1.200-1.800 2.200-4 2.200-7z')+c(16,12,5)+f('M6.500 29c0-5.500 4-9 9.500-9s9.500 3.500 9.500 9z'));
  return A;
})();
function avKey(v){if(!v)return '';const cp=String(v).codePointAt(0);return cp?cp.toString(16):''}
function avEmoji(k){return String.fromCodePoint(parseInt(k,16))+(AV[k]&&AV[k].vs?String.fromCharCode(0xFE0F):'')}
function avSvg(k){const a=AV[k];return '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="av-svg">'+a.g+'</svg>'}
function avaHtml(v){const k=avKey(v);if(AV[k])return avSvg(k);if(!v)return avSvg('1f464');return esc(v)}

const CATS = [
  ['StoryTime',ICONS.book,'Story Time'],
  ['PrayForMe',ICONS.sparkles,'Pray For Me'],['Bible',ICONS.book,'Bible'],['WorkLife',ICONS.briefcase,'Work & Life'],
  ['SpiritualLife',ICONS.feather,'Spiritual Life'],['ChristianChallenges',ICONS.swords,'Challenges'],
  ['Relationship',ICONS.heart,'Relationship'],['Marriage',ICONS.gem,'Marriage'],['Youth',ICONS.users,'Youth'],
  ['Finance',ICONS.dollar,'Finance'],['WorshipMusic',ICONS.music,'Worship'],['Family',ICONS.home,'Family'],
  ['Testimony',ICONS.megaphone,'Testimony'],['AddictionRecovery',ICONS.pill,'Recovery'],
  ['BibleQuestion',ICONS.book,'Bible Q&A'],['Other',ICONS.bookmark,'Other']
];

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),3000)}
function showBanScreen(msg){
  if(document.getElementById('banScreen'))return;
  const o=document.createElement('div');o.id='banScreen';
  o.style.cssText='position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;padding:32px;text-align:center;background:#0b0b0f;color:#fff;font:16px/1.5 Inter,system-ui,sans-serif';
  o.innerHTML='<div><div style="font-size:44px;margin-bottom:12px">🚫</div><div style="font-size:20px;font-weight:700;margin-bottom:8px">Your account has been banned</div><div style="opacity:.75">'+esc(msg||'')+'</div></div>';
  document.body.appendChild(o);
}
async function api(path,opts={}){
  const r=await fetch(API+path,{headers:{'Content-Type':'application/json'},...opts});
  const d=await r.json();if(d&&d.banned)showBanScreen(d.error);
  if(!r.ok||!d.success)throw new Error(d.error||'Error');return d;
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

// ========== VOICE RECORDING (Telegram-style: hold, slide left to cancel, slide up to lock) ==========
let mediaRecorder = null;      // kept for backward compatibility
let recordedChunks = [];
let currentVoiceTarget = null; // 'vent' | 'comment' | 'chat'
let voiceCancel = false;
const VR_LOCK_DY = 70, VR_CANCEL_DX = 110, VR_MIN_MS = 1000, VR_MAX_MS = 10 * 60 * 1000;
const VR_IC = {
  mic: '<svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>',
  send: '<svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>',
  lock: '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  up: '<svg viewBox="0 0 24 24"><polyline points="6 15 12 9 18 15"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
  left: '<svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>',
  play: '<svg viewBox="0 0 24 24"><polygon points="7 4 20 12 7 20 7 4"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
};
let vr = null; // active recording session

function vrFmt(ms) {
  const t = Math.max(0, Math.floor(ms / 100)), s = Math.floor(t / 10);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') + ',' + (t % 10);
}
function vrHaptic(kind) {
  try {
    const h = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.HapticFeedback;
    if (!h) return;
    if (kind === 'warn') h.notificationOccurred('warning'); else h.impactOccurred(kind || 'light');
  } catch (e) {}
}
function vrSwipes(on) { // stop Telegram's swipe-down-to-close while the finger slides up to lock
  try {
    const w = window.Telegram && window.Telegram.WebApp;
    if (w && w.isVersionAtLeast && w.isVersionAtLeast('7.7')) { on ? w.enableVerticalSwipes() : w.disableVerticalSwipes(); }
  } catch (e) {}
}
function getPreferredVoiceMimeType() {
  const candidates = ['audio/ogg;codecs=opus', 'audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  for (const type of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) return type;
  }
  return '';
}
function vrTip(btn, text) {
  const r = btn.getBoundingClientRect();
  const tip = document.createElement('div');
  tip.className = 'vr-tip'; tip.textContent = text;
  tip.style.left = Math.min(Math.max(r.left + r.width / 2, 90), window.innerWidth - 90) + 'px';
  tip.style.top = (r.top - 8) + 'px';
  document.body.appendChild(tip);
  setTimeout(() => tip.remove(), 2200);
}

// ---- Shared microphone: ask once, reuse for every recording, release when idle ----
let vrMic = null, vrMicTimer = 0;
const VR_MIC_IDLE_MS = 3 * 60 * 1000;
const VR_AUDIO = { channelCount: 1, sampleRate: 48000, echoCancellation: false, noiseSuppression: true, autoGainControl: true };
function vrHasMic() { return !!(vrMic && vrMic.getAudioTracks().some(t => t.readyState === 'live')); }
function vrTouchMic() { clearTimeout(vrMicTimer); vrMicTimer = setTimeout(vrReleaseMic, VR_MIC_IDLE_MS); }
function vrReleaseMic() {
  if (vr) { vrTouchMic(); return; }
  if (vrMic) { vrMic.getTracks().forEach(t => t.stop()); vrMic = null; }
}
function vrGetMic() {
  if (vrHasMic()) { vrTouchMic(); return Promise.resolve(vrMic); }
  return navigator.mediaDevices.getUserMedia({ audio: VR_AUDIO })
    .catch(err => { // retry plain only if the device rejected the settings; never re-prompt after a denial
      if (err && (err.name === 'OverconstrainedError' || err.name === 'ConstraintNotSatisfiedError')) return navigator.mediaDevices.getUserMedia({ audio: true });
      throw err;
    })
    .then(stream => { vrMic = stream; vrTouchMic(); return stream; });
}
document.addEventListener('visibilitychange', () => { if (document.hidden) vrReleaseMic(); });
window.addEventListener('pagehide', vrReleaseMic);

function setupVoiceButton(btnId, target) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.addEventListener('contextmenu', e => e.preventDefault());
  btn.addEventListener('pointerdown', e => {
    if (vr || btn._vrWait) return;
    e.preventDefault();
    try { btn.setPointerCapture(e.pointerId); } catch (_) {}
    btn._vrDown = true;
    const x = e.clientX, y = e.clientY;
    if (vrHasMic()) { vrTouchMic(); vrStart(btn, target, x, y); return; }
    btn._vrWait = true; // first use: only ask for permission, no recording UI under the system dialog
    vrGetMic().then(() => {
      btn._vrWait = false;
      if (btn._vrDown && !vr) vrStart(btn, target, x, y);          // already allowed and finger still down
      else vrTip(btn, 'Microphone ready. Hold to record.');          // the permission dialog ate the press
    }).catch(() => { btn._vrWait = false; toast('Microphone access denied'); });
  });
  btn.addEventListener('pointermove', e => { if (vr && vr.btn === btn && vr.state === 'rec') vrMove(e.clientX, e.clientY); });
  btn.addEventListener('pointerup', () => { btn._vrDown = false; if (vr && vr.btn === btn && vr.state === 'rec') vrFinish('send'); });
  btn.addEventListener('pointercancel', () => { btn._vrDown = false; if (vr && vr.btn === btn && vr.state === 'rec') vrFinish('cancel'); });
}

function vrStart(btn, target, x, y) {
  const row = btn.parentElement;
  const br = btn.getBoundingClientRect(), rr = row.getBoundingClientRect();
  const cx = br.left + br.width / 2, cy = br.top + br.height / 2;
  const cs = getComputedStyle(row);
  const prevPos = row.style.position;
  if (cs.position === 'static') row.style.position = 'relative';

  const bar = document.createElement('div');
  bar.className = 'vr-bar';
  bar.style.paddingLeft = (parseFloat(cs.paddingLeft) || 0) + 6 + 'px';
  bar.innerHTML = '<span class="vr-dot"></span><span class="vr-time">0:00,0</span>' +
    '<div class="vr-slide">' + VR_IC.left + '<span>Slide to cancel</span></div>';
  row.appendChild(bar);

  const mk = (cls, html) => { const d = document.createElement('div'); d.className = cls; if (html) d.innerHTML = html; d.style.left = cx + 'px'; d.style.top = cy + 'px'; document.body.appendChild(d); return d; };
  const halo = mk('vr-halo'), orb = mk('vr-orb', VR_IC.mic);
  const lock = mk('vr-lock', VR_IC.lock + VR_IC.up);
  lock.style.top = (cy - 110) + 'px';

  vr = { btn, row, prevPos, bar, halo, orb, lock, cx, cy, target, x0: x, y0: y, state: 'rec',
         t0: Date.now(), dur: 0, chunks: [], rec: null, stream: null, analyser: null, ctx: null,
         action: null, aborted: false, dead: false, raf: 0, audio: null, url: null };
  voiceCancel = false; currentVoiceTarget = target;
  vrHaptic('medium'); vrSwipes(false);
  const v = vr;

  const stream = vrMic;
  v.stream = stream;
  const type = getPreferredVoiceMimeType();
  const opts = { audioBitsPerSecond: 64000 }; // clear speech; the browser default on phones is far lower
  if (type) opts.mimeType = type;
  try { v.rec = new MediaRecorder(stream, opts); } catch (e) { v.rec = new MediaRecorder(stream); }
  mediaRecorder = v.rec;
  v.rec.ondataavailable = e => { if (e.data.size > 0) v.chunks.push(e.data); };
  v.rec.onstop = () => vrStopped(v);
  try {
    v.ctx = new (window.AudioContext || window.webkitAudioContext)();
    v.analyser = v.ctx.createAnalyser(); v.analyser.fftSize = 256;
    v.ctx.createMediaStreamSource(stream).connect(v.analyser);
  } catch (e) { v.analyser = null; }
  v.rec.start();
  v.t0 = Date.now();

  const buf = new Uint8Array(128);
  const loop = () => {
    if (v.dead) return;
    if (v.state === 'rec' || v.state === 'locked') {
      const el = Date.now() - v.t0;
      bar.querySelector('.vr-time').textContent = vrFmt(el);
      if (v.analyser) {
        v.analyser.getByteTimeDomainData(buf);
        let sum = 0; for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; sum += d * d; }
        halo.style.transform = 'scale(' + (1 + Math.min(Math.sqrt(sum / buf.length) * 6, 1) * 0.9).toFixed(2) + ')';
      }
      if (el > VR_MAX_MS) { vrFinish('send'); return; }
    }
    v.raf = requestAnimationFrame(loop);
  };
  v.raf = requestAnimationFrame(loop);
}

function vrMove(x, y) {
  const v = vr, dx = Math.min(0, x - v.x0), dy = Math.max(0, v.y0 - y);
  const slide = v.bar.querySelector('.vr-slide');
  slide.style.transform = 'translateX(' + dx + 'px)';
  slide.style.opacity = String(Math.max(0, 1 - Math.abs(dx) / VR_CANCEL_DX));
  v.orb.style.transform = 'translateY(' + (-Math.min(dy, 100)) + 'px)';
  if (-dx > VR_CANCEL_DX) { vrHaptic('warn'); vrFinish('cancel'); }
  else if (dy > VR_LOCK_DY) vrLock();
}

function vrLock() { // finger slid up: keep recording hands-free
  const v = vr; if (!v || v.state !== 'rec') return;
  v.state = 'locked'; vrHaptic('medium');
  v.orb.style.transform = ''; v.orb.classList.add('sm'); v.orb.innerHTML = VR_IC.send;
  v.orb.onclick = () => vrFinish('send');
  v.lock.className = 'vr-lock stop'; v.lock.innerHTML = VR_IC.stop;
  v.lock.style.top = (v.cy - 64) + 'px';
  v.lock.onclick = () => vrFinish('preview');
  v.halo.style.display = 'none';
  const slide = v.bar.querySelector('.vr-slide');
  slide.outerHTML = '<button type="button" class="vr-textbtn">Cancel</button>';
  v.bar.querySelector('.vr-textbtn').onclick = () => vrFinish('cancel');
  vrSwipes(true);
}

function vrFinish(action) {
  const v = vr; if (!v || v.action) return;
  v.dur = Date.now() - v.t0;
  if (action === 'send' && v.dur < VR_MIN_MS) action = 'short'; // like Telegram: under 1s is discarded
  v.action = action;
  if (!v.rec || v.rec.state === 'inactive') { v.aborted = true; vrEnd(v, action, true); return; }
  v.rec.stop();
}

function vrStopped(v) {
  vrTouchMic();
  try { v.ctx && v.ctx.close(); } catch (e) {}
  if (v.action === 'cancel' || v.action === 'short') { vrEnd(v, v.action, true); return; }
  const mime = (v.rec && v.rec.mimeType) || 'audio/webm';
  const raw = new Blob(v.chunks, { type: mime });
  const done = blob => v.action === 'preview' ? vrPreview(v, blob, mime) : vrSend(v, blob, mime);
  if (mime.includes('webm') && window.ysFixWebmDuration) {
    ysFixWebmDuration(raw, v.dur, { logger: false }).then(done).catch(() => done(raw));
  } else done(raw);
}

function vrEnd(v, kind, animate) {
  v.dead = true; cancelAnimationFrame(v.raf); vrSwipes(true);
  const cleanup = () => {
    [v.bar, v.halo, v.orb, v.lock].forEach(el => el && el.remove());
    v.row.style.position = v.prevPos;
    if (v.audio) { v.audio.pause(); }
    if (v.url) URL.revokeObjectURL(v.url);
    if (vr === v) vr = null;
  };
  if (kind === 'cancel' && animate) { // trash animation like Telegram
    v.halo.remove(); v.orb.remove(); v.lock.remove();
    v.bar.innerHTML = '<span class="vr-bin">' + VR_IC.trash + '</span>';
    setTimeout(cleanup, 380);
  } else {
    cleanup();
    if (kind === 'short') vrTip(v.btn, 'Hold to record audio.');
  }
}

function vrPreview(v, blob, mime) { // after tapping stop in locked mode: listen, delete or send
  v.state = 'preview'; v.blob = blob; v.mime = mime;
  v.url = URL.createObjectURL(blob);
  v.audio = new Audio(v.url);
  v.lock.remove(); v.halo.remove();
  v.bar.innerHTML = '<button type="button" class="vr-iconbtn vr-del">' + VR_IC.trash + '</button>' +
    '<button type="button" class="vr-iconbtn play">' + VR_IC.play + '</button>' +
    '<div class="vr-track"><i></i></div>';
  const timeEl = document.createElement('span');
  timeEl.className = 'vr-time'; timeEl.style.cssText = 'position:absolute;right:56px;font-size:14px;min-width:0';
  timeEl.textContent = vrFmt(v.dur); v.bar.appendChild(timeEl);
  const play = v.bar.querySelector('.play'), prog = v.bar.querySelector('.vr-track i');
  const total = () => (isFinite(v.audio.duration) && v.audio.duration > 0 ? v.audio.duration * 1000 : v.dur);
  v.audio.ontimeupdate = () => { prog.style.width = Math.min(100, v.audio.currentTime * 1000 / total() * 100) + '%'; timeEl.textContent = vrFmt(v.audio.currentTime * 1000); };
  v.audio.onended = () => { play.innerHTML = VR_IC.play; prog.style.width = '0'; timeEl.textContent = vrFmt(v.dur); };
  play.onclick = () => { if (v.audio.paused) { v.audio.play(); play.innerHTML = VR_IC.pause; } else { v.audio.pause(); play.innerHTML = VR_IC.play; } };
  v.bar.querySelector('.vr-del').onclick = () => { v.action = 'cancel'; vrEnd(v, 'cancel', true); };
  v.orb.onclick = () => { v.audio.pause(); vrSend(v, blob, mime); };
}

// ---- Optimistic "sending" bubbles for chats and responses ----
VR_IC.x = '<svg class="x" viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
VR_IC.retry = '<svg class="x" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.5 15a9 9 0 1 1-2.1-9.4L23 10"/></svg>';
let vrPendings = [], vrPendSeq = 0;

function vrOff(p) { return (94.2 * (1 - Math.max(p, 0.08))).toFixed(1); }
function vrPendHtml(e) {
  const failed = e.status === 'failed';
  if (e.text !== undefined) { // optimistic text message
    const q = e.replyId ? crQuoteHtml(crInfo(e.replyId)) : '';
    const inf = failed
      ? '<span style="color:#f44336">Failed to send · <a onclick="vrRetry(' + e.id + ')" style="text-decoration:underline;cursor:pointer">Retry</a> · <a onclick="vrAbort(' + e.id + ')" style="text-decoration:underline;cursor:pointer">Delete</a></span>'
      : 'Sending…';
    return '<div class="vr-pw"><div class="msg-row me"><div class="msg-bubble">' + q + esc(e.text) + '</div><div class="msg-time">' + inf + '</div></div></div>';
  }
  const btn = failed
    ? '<button type="button" class="vr-pbtn" onclick="vrRetry(' + e.id + ')">' + VR_IC.retry + '</button>'
    : '<button type="button" class="vr-pbtn" onclick="vrAbort(' + e.id + ')"><svg class="vr-ring' + (e.progress > 0 ? '' : ' spin') + '" viewBox="0 0 36 36"><circle class="bg" cx="18" cy="18" r="15"/><circle class="fg" cx="18" cy="18" r="15" style="stroke-dashoffset:' + vrOff(e.progress) + '"/></svg>' + VR_IC.x + '</button>';
  const info = failed
    ? '<span style="color:#f44336">Failed to send · <a onclick="vrAbort(' + e.id + ')" style="text-decoration:underline;cursor:pointer">Delete</a></span>'
    : 'Sending…';
  const core = '<div class="vr-pend' + (e.kind === 'comment' ? ' cm' : '') + '" data-pid="' + e.id + '">' + btn +
    '<div class="vr-ptrack"></div><span class="vr-ptime">' + vrFmt(e.dur).replace(/,.*/, '') + '</span></div>';
  if (e.kind === 'chat') {
    return '<div class="vr-pw"><div class="msg-row me"><div class="msg-bubble">' + (e.replyId ? crQuoteHtml(crInfo(e.replyId)) : '') + core + '</div><div class="msg-time">' + info + '</div></div></div>';
  }
  let av = ''; try { av = avaHtml(); } catch (_) {}
  return '<div class="vr-pw"><div class="comment-item"><div class="ava" style="width:30px;height:30px;font-size:14px">' + av + '</div>' +
    '<div class="comment-body"><div class="comment-name">You</div>' + (e.parentId ? cmtQuoteHtml(cmtInfo(e.parentId)) : '') + core + '<div class="msg-time">' + info + '</div></div></div></div>';
}
// used by the existing render functions so a poll/refresh never wipes a pending bubble
function vrPendHtmlFor(kind) {
  const ref = kind === 'chat' ? crPartnerId : currentPostId;
  return vrPendings.filter(e => e.kind === kind && e.ref === ref).map(vrPendHtml).join('');
}
function vrPendRender(kind) {
  const box = document.getElementById(kind === 'chat' ? 'cr-msgs' : 'detail-comments');
  if (!box) return;
  box.querySelectorAll('.vr-pw').forEach(n => n.remove());
  const html = vrPendHtmlFor(kind);
  if (!html) return;
  if (kind === 'comment' && !box.querySelector('.comment-item')) box.innerHTML = '';
  box.insertAdjacentHTML('beforeend', html);
  const last = box.querySelector('.vr-pw:last-child');
  if (kind === 'chat') box.scrollTop = box.scrollHeight; else if (last) last.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function vrUpload(file, onProg, reg) { // same endpoint as uploadMedia, but reports progress
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file); fd.append('user_id', UID); fd.append('intent', 'voice');
    const x = new XMLHttpRequest(); reg(x);
    x.open('POST', API + '/api/mini-app/upload-media');
    x.upload.onprogress = ev => { if (ev.lengthComputable) onProg(ev.loaded / ev.total); };
    x.onload = () => {
      let d = {}; try { d = JSON.parse(x.responseText); } catch (_) {}
      if (x.status >= 200 && x.status < 300 && d.success) resolve({ media_type: d.media_type, media_id: d.file_id });
      else reject(new Error(d.error || 'Upload failed'));
    };
    x.onerror = () => reject(new Error('Upload failed'));
    x.onabort = () => reject(new Error('aborted'));
    x.send(fd);
  });
}
async function vrRun(e) {
  e.status = 'uploading'; e.progress = 0; e.cancelled = false;
  vrPendRender(e.kind);
  try {
    const media = await vrUpload(e.file, p => {
      e.progress = p;
      document.querySelectorAll('[data-pid="' + e.id + '"] .vr-ring').forEach(r => r.classList.remove('spin'));
      document.querySelectorAll('[data-pid="' + e.id + '"] .fg').forEach(c => { c.style.strokeDashoffset = vrOff(p); });
    }, x => { e.xhr = x; });
    if (e.cancelled) return;
    if (e.kind === 'chat') {
      const res = await api('/api/mini-app/chats/send', { method: 'POST', body: JSON.stringify({ sender_id: UID, receiver_id: e.ref, content: '', media_type: media.media_type, media_id: media.media_id, reply_to_id: e.replyId || 0 }) });
      crAddLocal(res.data, { content: '', media_type: media.media_type, media_id: media.media_id, reply_to: e.replyId ? crInfo(e.replyId) : null }, e);
      return;
    } else {
      await api('/api/mini-app/post/' + e.ref + '/comment', { method: 'POST', body: JSON.stringify({ user_id: UID, content: '', parent_comment_id: e.parentId, media_type: media.media_type, media_id: media.media_id }) });
      await fetchAndRenderComments(e.ref, e.authorId);
    }
    // same task as the refresh above, so the real message replaces the bubble with no flicker
    vrPendings = vrPendings.filter(x => x !== e);
    vrPendRender(e.kind);
  } catch (err) {
    if (e.cancelled) return;
    e.status = 'failed'; vrPendRender(e.kind); toast(err.message);
  }
}
function vrAbort(id) {
  const e = vrPendings.find(x => x.id === id); if (!e) return;
  e.cancelled = true; try { e.xhr && e.xhr.abort(); } catch (_) {}
  vrPendings = vrPendings.filter(x => x !== e); vrPendRender(e.kind);
}
function vrRetry(id) { const e = vrPendings.find(x => x.id === id); if (e) { if (e.text !== undefined) crRunText(e); else vrRun(e); } }

async function vrSend(v, blob, mime) {
  const target = v.target, dur = v.dur;
  const file = new File([blob], 'voice.' + (mime.includes('ogg') ? 'ogg' : mime.includes('mp4') ? 'm4a' : 'webm'), { type: mime });
  vrEnd(v, 'send', false);
  vrHaptic('light');
  if (target === 'chat' || target === 'comment') {
    // Telegram-style: bubble appears instantly with upload progress; no attachment preview above the composer
    if (target === 'chat' ? !crPartnerId : !currentPostId) return;
    const e = { id: ++vrPendSeq, kind: target, file, dur, status: 'uploading', progress: 0,
                replyId: (target === 'chat' && crReplyTo) ? crReplyTo.id : 0,
                ref: target === 'chat' ? crPartnerId : currentPostId,
                parentId: target === 'comment' ? replyToId : 0, authorId: currentPostAuthorId };
    if (target === 'comment') cancelReply(); else if (crReplyTo) crCancelReply();
    vrPendings.push(e);
    vrRun(e);
    return;
  }
  await handleVoiceFile(file, target); // vents: stays attached, user still taps "Post Anonymously"
}

// Telegram behaviour: mic shows when the input is empty, send arrow shows once there is text/media
function vrSyncComposer() {
  if (vr) return;
  const cfg = [
    ['comment-voice-btn', document.getElementById('send-comment'), 'comment-txt', () => pendingCommentMedia],
    ['chat-voice-btn', document.querySelector('.cr-send'), 'cr-txt', () => pendingChatMedia]
  ];
  cfg.forEach(([micId, sendBtn, txtId, media]) => {
    const mic = document.getElementById(micId), txt = document.getElementById(txtId);
    if (!mic || !sendBtn || !txt) return;
    const has = !!(txt.value.trim() || media());
    mic.style.display = has ? 'none' : 'flex';
    sendBtn.style.display = has ? 'flex' : 'none';
  });
}
setInterval(vrSyncComposer, 200);

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
    return true;
  } catch (e) { toast(e.message); return false; }
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
  if(name==='profile')name='settings';
  if(!btn&&(name==='settings'||name==='edit'))btn=document.querySelector('.nav-item[data-page="settings"]');
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
  if(name==='vent')refreshVentSexRow();
  if(name==='leaderboard')loadLB();
  if(name==='settings')loadMe();
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
  let st;document.getElementById('search-inp').addEventListener('input',e=>{
    clearTimeout(st);searchQ=e.target.value.trim();st=setTimeout(()=>{feedPage=1;loadFeed()},500);
  });
  buildEmojiPicker();
  renderCats();
  initAppearance();
  ['ep-name','ep-bio'].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener('input',updateEditUI)});
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

// Per-vent "show my sex" question: only offered to users who actually have a sex saved.
let ventSexCheckedAt=0;
function ventShowSexChoice(){
  const row=document.getElementById('vent-sex-row');
  if(!row||row.style.display==='none')return false;
  const picked=document.querySelector('input[name="vent-show-sex"]:checked');
  return !!picked&&picked.value==='yes';
}
function resetVentShowSex(){
  const no=document.querySelector('input[name="vent-show-sex"][value="no"]');
  if(no)no.checked=true;
}
async function refreshVentSexRow(){
  const row=document.getElementById('vent-sex-row');
  if(!row||!UID)return;
  if(Date.now()-ventSexCheckedAt<60000)return;
  try{
    const d=await api(`/api/mini-app/profile/${UID}?viewer_id=${UID}`);
    const sx=d&&d.data?d.data.sex:null;
    const has=(sx==='👨'||sx==='👩');
    row.style.display=has?'block':'none';
    if(!has)resetVentShowSex();
    ventSexCheckedAt=Date.now();
  }catch(e){row.style.display='none';resetVentShowSex()}
}
function ventLabel(p){
  if(p.vent_number===null||p.vent_number===undefined)return '';
  const n=String(p.vent_number).padStart(3,'0');
  const sx=(p.revealed_sex==='👨'||p.revealed_sex==='👩')?`<div class="vent-sex">${esc(p.revealed_sex)}</div>`:'';
  return `<div class="vent-label"><div class="vent-num">Vent - ${n}</div>${sx}</div>`;
}

async function submitVent(){
  const txt=document.getElementById('vent-txt').value.trim();
  const cats=[...selCats];
  if(!txt&&!pendingMedia)return toast('Write something first');
  if(!cats.length)return toast('Pick at least one category');
  const btn=document.getElementById('submit-vent');
  btn.disabled=true;btn.textContent='Posting…';
  try{
    const payload={user_id:UID,content:txt,categories:cats,explicit:document.getElementById('vent-explicit-check').checked,reveal_sex:ventShowSexChoice()};
    if(pendingMedia){payload.media_type=pendingMedia.media_type;payload.media_id=pendingMedia.media_id}
    await api('/api/mini-app/submit-vent',{method:'POST',body:JSON.stringify(payload)});
    toast('✅ Shared — awaiting review');
    document.getElementById('vent-txt').value='';
    document.getElementById('vent-cnt').textContent='0';
    document.getElementById('vent-explicit-check').checked=false;
    resetVentShowSex();
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
  const unread=p.unread_comments>0?`<span class="pill pill-sm" style="background:rgba(var(--gold-rgb),0.2);border-color:var(--gold)">${p.unread_comments} new</span>`:'';
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
    <div class="post-meta"><div class="ava" style="width:34px;height:34px">${avaHtml(p.author?.avatar||p.author?.sex)}</div><div><div class="post-name"${p.author?.is_admin ? '' : ` onclick="event.stopPropagation(); showUserProfile('${p.author?.id}')"`}>${esc(p.author?.name||'Anonymous')} <span style="font-size:13px">${esc(p.author?.aura||'')}</span></div></div><div class="post-time">${esc(p.time_ago||'')}</div></div>
    ${ventLabel(p)}
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
          <div style="font-size:16px;line-height:1.65;color:var(--text3);font-style:italic;padding:16px;display:flex;align-items:center;gap:8px;"><span style="width:18px;height:18px;flex-shrink:0;display:inline-flex">${ICONS.alert}</span> This post has been deleted by the author.</div>
        </div>`;
      await fetchAndRenderComments(id,null,revealParam);
      return;
    }
    if(p.content_hidden){
      currentPostAuthorId = p.author_id;
      document.getElementById('detail-post').innerHTML=`
        <div class="post-card" style="cursor:default;margin-bottom:0;border-radius:0;margin:0;border-left:none;border-right:none;border-top:none;background:var(--glass2)">
          <div style="padding:20px;text-align:center">
            <div style="width:32px;height:32px;margin:0 auto 8px;color:var(--gold)">${ICONS.alert}</div>
            <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:6px">Explicit Content Warning</div>
            <div style="font-size:14px;color:var(--text3);margin-bottom:14px">${esc(p.content)}</div>
            <button class="btn-gold" onclick="openPost(${id},true)">View Content</button>
          </div>
        </div>`;
      document.getElementById('detail-comments').innerHTML='<div style="text-align:center;padding:20px;color:var(--text3);font-size:14px">Comments are hidden until you view the post.</div>';
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
    const explicitTag=p.explicit?`<div style="display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:var(--gold);border:1px solid var(--gold);border-radius:10px;padding:3px 9px;margin-bottom:8px">${ICONS.alert.replace('class="icon"','class="icon badge-icon"')} Explicit</div>`:'';
    document.getElementById('detail-post').innerHTML=`
      <div class="post-card" style="cursor:default;margin-bottom:0;border-radius:0;margin:0;border-left:none;border-right:none;border-top:none;background:var(--glass2)">
        <div class="post-meta"><div class="ava" style="width:38px;height:38px">${avaHtml(p.author?.avatar||p.author?.sex)}</div><div><div class="post-name" style="font-size:15px;cursor:pointer"${p.author?.is_admin ? '' : ` onclick="showUserProfile('${p.author?.id}')"`}>${ICONS.shield.replace('class="icon"','class="icon badge-icon"')} Vent author</div><div style="font-size:12px;color:var(--text3)">${esc(p.time_ago||'')}</div></div></div>
        ${ventLabel(p)}
        ${explicitTag}
        ${cats?`<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:12px">${cats}</div>`:''}
        <div style="font-size:17px;line-height:1.65;color:var(--text)">${esc(p.content)}</div>
        ${p.media_id?renderMedia(p.media_type,p.media_id):''}
        <div>
          ${renderReactionButtons(p.id, 'post', p.reactions?.counts || {}, p.reactions?.user_reaction)}
        </div>
      </div>`;
    await fetchAndRenderComments(id,p.author_id,revealParam);
  }catch(e){document.getElementById('detail-post').innerHTML='<div style="padding:20px;color:var(--text3)">Could not load</div>'}
}

// Comments come newest-100-first-window (oldest-first inside the window); "Load older" walks back.
let cmtAll=[], cmtHasMore=false, cmtLoadingOlder=false, cmtReveal='';
async function fetchAndRenderComments(postId,authorId,revealParam){
  if(revealParam!==undefined) cmtReveal=revealParam;
  const cd=await api(`/api/mini-app/post/${postId}/comments?viewer_id=${UID}${cmtReveal}&limit=100`);
  cmtAll=cd.data||[]; cmtHasMore=!!cd.has_more;
  renderComments(cmtAll,authorId);
}
async function loadOlderComments(){
  if(cmtLoadingOlder||!cmtHasMore||!cmtAll.length||!currentPostId)return;
  cmtLoadingOlder=true;
  try{
    const pid=currentPostId;
    const cd=await api(`/api/mini-app/post/${pid}/comments?viewer_id=${UID}${cmtReveal}&limit=100&before_id=${cmtAll[0].id}`);
    if(pid!==currentPostId)return;
    cmtAll=(cd.data||[]).concat(cmtAll); cmtHasMore=!!cd.has_more;
    renderComments(cmtAll,currentPostAuthorId);
  }catch(e){toast(e.message)}finally{cmtLoadingOlder=false}
}
function renderComments(comments,postAuthorId){
  const box=document.getElementById('detail-comments');
  if(!comments.length){box.innerHTML='<div style="text-align:center;padding:30px 20px;color:var(--text3);font-size:14px">No responses yet — be the first!</div>'+vrPendHtmlFor('comment');return}
  const roots=comments; // flat, chronological: replies carry a quote instead of being nested
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
    return `<div class="comment-item" id="cmt-${c.id}"><div class="ava" style="width:30px;height:30px;font-size:14px">${avaHtml(c.author?.sex)}</div><div class="comment-body"><div class="comment-name"${c.author?.is_admin ? '' : ` onclick="showUserProfile('${c.author_id}')"`}>${nameBadge}${esc(name)} <span style="font-size:11px;color:var(--text3)">${esc(c.time_ago||'')}</span></div>${cmtQuoteHtml(c.reply_to)}<div class="comment-text">${esc(c.content)}</div>${c.media_id?renderMedia(c.media_type,c.media_id):''}
      ${renderReactionButtons(c.id, 'comment', c.reactions?.counts || {}, c.reactions?.user_reaction)}
      <div class="comment-actions"><button class="ca-btn" onclick="replyTo(${c.id})">${ICONS.reply} Reply</button>${mine?`<button class="ca-btn" onclick="delComment(${c.id})">Delete</button>`:''}</div></div></div>`;
  };
  const olderBtn=cmtHasMore?'<div style="text-align:center;padding:4px 0 12px"><button class="btn-ghost" onclick="loadOlderComments()">Load older responses</button></div>':'';
  box.innerHTML=olderBtn+roots.map(c=>rr(c,0)).join('')+vrPendHtmlFor('comment');
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
// ---- Telegram-style replies: quote the message, no nesting, tap quote to jump ----
function cmtSnippet(rt) {
  const t = (rt.content || '').trim();
  if (t) return t;
  const m = { voice: '🎤 Voice message', audio: '🎵 Audio', photo: '🖼 Photo', video: '🎬 Video', gif: '🎞 GIF', sticker: '🏷 Sticker' };
  return m[rt.media_type] || (rt.media_type && rt.media_type !== 'text' ? '📎 Attachment' : '');
}
function cmtQuoteName(rt) {
  return String(rt.author_id) === String(currentPostAuthorId) ? 'Vent author' : (rt.author_name || 'Anonymous');
}
function cmtQuoteHtml(rt) {
  if (!rt) return '';
  if (rt.deleted) return '<div class="reply-quote gone"><div class="rq-text">Deleted message</div></div>';
  return '<div class="reply-quote" onclick="jumpToComment(' + rt.id + ')"><div class="rq-name">' + esc(cmtQuoteName(rt)) +
    '</div><div class="rq-text">' + esc(cmtSnippet(rt)) + '</div></div>';
}
function cmtInfo(id) { // quote data for a comment that is already loaded
  const c = cmtAll.find(x => x.id === id);
  return c ? { id: c.id, author_id: c.author_id, author_name: c.author && c.author.name, content: c.content, media_type: c.media_type } : null;
}
async function jumpToComment(id) {
  let el = document.getElementById('cmt-' + id);
  for (let i = 0; !el && cmtHasMore && i < 10; i++) { await loadOlderComments(); el = document.getElementById('cmt-' + id); }
  if (!el) return toast('Message not found');
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  el.classList.remove('cm-flash'); void el.offsetWidth; el.classList.add('cm-flash');
}
function replyTo(id) {
  const rt = cmtInfo(id);
  replyToId = id;
  const bar = document.getElementById('reply-bar');
  if (rt && bar) {
    bar.dataset.post = currentPostId;
    bar.innerHTML = '<div class="rb-line"></div><div class="rb-body" onclick="jumpToComment(' + id + ')"><div class="rq-name">Reply to ' + esc(cmtQuoteName(rt)) +
      '</div><div class="rq-text">' + esc(cmtSnippet(rt)) + '</div></div><button type="button" class="rb-x" onclick="cancelReply()">' + ICONS.close + '</button>';
    bar.style.display = 'flex';
  }
  const t = document.getElementById('comment-txt'); if (t) t.focus();
}
function cancelReply() {
  replyToId = 0;
  const bar = document.getElementById('reply-bar');
  if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
}
setInterval(() => { // a pending reply never leaks into another post
  const b = document.getElementById('reply-bar');
  if (replyToId && b && String(b.dataset.post) !== String(currentPostId)) cancelReply();
}, 300);

async function postComment(){
  const txt=document.getElementById('comment-txt').value.trim();
  if((!txt&&!pendingCommentMedia)||!currentPostId)return;
  const btn=document.getElementById('send-comment');btn.disabled=true;
  try{
    const payload={user_id:UID,content:txt,parent_comment_id:replyToId};
    if(pendingCommentMedia){payload.media_type=pendingCommentMedia.media_type;payload.media_id=pendingCommentMedia.media_id}
    await api(`/api/mini-app/post/${currentPostId}/comment`,{method:'POST',body:JSON.stringify(payload)});
    document.getElementById('comment-txt').value='';cancelReply();toast('Posted');
    pendingCommentMedia=null;document.getElementById('comment-file-input').value='';
    document.getElementById('comment-attach-btn').classList.remove('has-media');
    renderMediaPreview(document.getElementById('comment-media-preview'),null);
    await fetchAndRenderComments(currentPostId,currentPostAuthorId);
  }catch(e){toast(e.message)}finally{btn.disabled=false}
}
async function delComment(id){
  if(!confirm('Delete this response?'))return;
  try{await api(`/api/mini-app/comment/${id}`,{method:'DELETE',body:JSON.stringify({user_id:UID})});
    toast('Deleted');await fetchAndRenderComments(currentPostId,currentPostAuthorId);}catch(e){toast(e.message)}
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

// ===== Me tab =====
const PENCIL_SVG='<svg viewBox="0 0 24 24"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
const CHECK_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const AURA_TIERS=[[0,'White'],[10,'Yellow'],[25,'Green'],[50,'Blue'],[100,'Purple'],[500,'Crown']];
const ACCENTS=[
  {id:'gold',name:'Gold',rgb:'201,168,76',c1:'#c9a84c',c2:'#e8c97a',c3:'#f5e4b0',dark:'#8a6d1f'},
  {id:'rose',name:'Rose',rgb:'217,112,147',c1:'#d97093',c2:'#ec9db8',c3:'#f8d2df',dark:'#a14465'},
  {id:'sapphire',name:'Sapphire',rgb:'91,141,239',c1:'#5b8def',c2:'#8fb1f5',c3:'#cadbfa',dark:'#2f5ec4'},
  {id:'emerald',name:'Emerald',rgb:'72,187,138',c1:'#48bb8a',c2:'#7fd6ae',c3:'#c3ecda',dark:'#23825a'},
  {id:'violet',name:'Violet',rgb:'155,126,232',c1:'#9b7ee8',c2:'#bba6f1',c3:'#dfd5f8',dark:'#6a4fc0'}
];
let meRecent=[], botLink='', edCat='faith';

function lsGet(k,d){try{const v=localStorage.getItem(k);return v===null?d:v}catch(e){return d}}
function lsSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function hap(kind){if(lsGet('haptics','1')==='0')return;vrHaptic(kind||'light')}

// ---- appearance ----
function themeMode(){const t=lsGet('theme','dark');return(t==='light'||t==='auto')?t:'dark'}
function applyTheme(mode){
  let light=mode==='light';
  if(mode==='auto'){try{light=window.matchMedia('(prefers-color-scheme: light)').matches}catch(e){light=false}}
  document.body.classList.toggle('light',light);
}
function setTheme(mode){lsSet('theme',mode);applyTheme(mode);syncAppearanceUI();hap('light')}
function accentId(){const a=lsGet('accent','gold');return ACCENTS.some(x=>x.id===a)?a:'gold'}
function applyAccent(id){
  const a=ACCENTS.find(x=>x.id===id)||ACCENTS[0],st=document.documentElement.style;
  st.setProperty('--gold',a.c1);st.setProperty('--gold2',a.c2);st.setProperty('--gold3',a.c3);
  st.setProperty('--gold-rgb',a.rgb);st.setProperty('--gold-dark',a.dark);
}
function setAccent(id){lsSet('accent',id);applyAccent(id);syncAppearanceUI();hap('light')}
function setHaptics(on){lsSet('haptics',on?'1':'0');if(on)hap('medium')}
function buildSwatches(){
  const box=document.getElementById('swatches');if(!box)return;
  box.innerHTML=ACCENTS.map(a=>`<button type="button" class="sw" data-id="${a.id}" style="--c:${a.c1}" aria-label="${a.name}" onclick="setAccent('${a.id}')">${CHECK_SVG}</button>`).join('');
}
function syncAppearanceUI(){
  const m=themeMode(),id=accentId();
  document.querySelectorAll('#seg-theme button').forEach(b=>b.classList.toggle('on',b.dataset.v===m));
  document.querySelectorAll('#swatches .sw').forEach(s=>s.classList.toggle('on',s.dataset.id===id));
  const nm=document.getElementById('accent-name');
  if(nm){const a=ACCENTS.find(x=>x.id===id);nm.textContent=a?a.name:'Gold'}
  const hp=document.getElementById('set-haptics');
  if(hp)hp.checked=lsGet('haptics','1')!=='0';
}
function initAppearance(){
  applyTheme(themeMode());applyAccent(accentId());buildSwatches();syncAppearanceUI();
  try{
    const mq=window.matchMedia('(prefers-color-scheme: light)');
    const h=()=>{if(themeMode()==='auto')applyTheme('auto')};
    if(mq.addEventListener)mq.addEventListener('change',h);else if(mq.addListener)mq.addListener(h);
  }catch(e){}
}

// ---- profile hub ----
function fmtNum(n){const v=Number(n);return Number.isFinite(v)?(Number.isInteger(v)?String(v):v.toFixed(1)):String(n)}
function auraProgress(rating){
  const r=Number(rating);
  if(!Number.isFinite(r)||r<0)return null;
  let i=0;
  for(let t=0;t<AURA_TIERS.length;t++){if(r>=AURA_TIERS[t][0])i=t}
  if(i===AURA_TIERS.length-1)return{pct:100,text:'Top aura reached'};
  const lo=AURA_TIERS[i][0],hi=AURA_TIERS[i+1][0];
  return{pct:Math.max(2,Math.round((r-lo)/(hi-lo)*100)),text:Math.ceil(hi-r)+' pts to '+AURA_TIERS[i+1][1]+' aura'};
}
function meHeroHtml(p){
  const bio=(p.bio||'').trim();
  const showAura=!!p.aura&&!p.is_admin;
  const prog=(p.is_admin||!p.aura)?null:auraProgress(p.rating);
  const stat=(n,l)=>`<div><div class="me-stat-num">${esc(String(n))}</div><div class="me-stat-lbl">${l}</div></div>`;
  return `<div class="me-hero">
    <div class="me-top">
      <div class="me-ava" onclick="setupEdit()">${avaHtml(p.avatar||p.sex)}</div>
      <div class="me-id">
        <div class="me-name">${esc(p.weekly_badge||'')} ${esc(p.name)}</div>
        <div class="me-meta">${showAura?`<span>${esc(p.aura)} ${esc(fmtNum(p.rating))} pts</span>`:''}${p.role?`<span>${ICONS.shield}${esc(p.role)}</span>`:''}</div>
      </div>
    </div>
    ${bio?`<div class="me-bio">${esc(bio)}</div>`:`<div class="me-bio empty" onclick="setupEdit()">Add a short bio</div>`}
    <div class="me-stats">${stat(p.stats?.posts||0,'Vents')}${stat(p.stats?.followers||0,'Followers')}${stat(p.stats?.comments||0,'Replies')}</div>
    ${prog?`<div class="me-prog"><div class="me-prog-top">${esc(prog.text)}</div><div class="me-prog-bar"><div class="me-prog-fill" style="width:${prog.pct}%"></div></div></div>`:''}
    <button type="button" class="me-edit-btn" onclick="setupEdit()">${PENCIL_SVG}Edit profile</button>
  </div>`;
}
function renderMe(){
  const hero=document.getElementById('me-hero'),rec=document.getElementById('me-recent');
  if(!hero||!profileCache)return;
  hero.innerHTML=meHeroHtml(profileCache);
  rec.innerHTML=meRecent.length?`<div class="me-label">Recent vents</div><div style="padding:0 16px">${meRecent.map(p=>`<div class="post-card" onclick="openPost(${p.id})" style="margin:0 0 10px"><div class="post-body" style="-webkit-line-clamp:2">${esc(p.content)}</div><div style="font-size:12px;color:var(--text3);margin-top:6px">${esc(p.time_ago)}</div></div>`).join('')}</div>`:'';
}
async function loadMe(){
  if(!UID)return;
  const hero=document.getElementById('me-hero');
  if(profileCache)renderMe();else hero.innerHTML=skelProfile();
  try{
    const res=await Promise.all([
      api(`/api/mini-app/profile/${UID}?viewer_id=${UID}`),
      api(`/api/mini-app/settings/${UID}`).catch(()=>null),
      api(`/api/mini-app/get-posts?user_id=${UID}&page=1`).catch(()=>({data:[]}))
    ]);
    profileCache=res[0].data;
    meRecent=(res[2].data||[]).filter(x=>x.author&&x.author.is_me).slice(0,3);
    renderMe();
    if(res[1])applySettings(res[1].data);
  }catch(e){
    if(!profileCache)hero.innerHTML='<div style="padding:40px;text-align:center;color:var(--text3)">Could not load your profile</div>';
  }
}
function applySettings(d){
  const set=(id,v)=>{const el=document.getElementById(id);if(el)el.checked=!!v};
  set('set-notif',d.notifications);set('set-priv',d.privacy_public);
  set('set-hide-aura',d.hide_aura);set('set-hide-bio',d.hide_bio);
  set('set-hide-followers',d.hide_follower_count);set('set-hide-role',d.hide_role);
  const rr=document.getElementById('row-hide-role');
  if(rr)rr.style.display=d.is_admin?'flex':'none';
  botLink=d.bot_link||botLink;
}
async function saveSetting(key,input){
  const val=input.checked;input.disabled=true;
  try{
    await api(`/api/mini-app/settings/${UID}`,{method:'POST',body:JSON.stringify({[key]:val})});
    hap('light');toast('Saved');
  }catch(e){input.checked=!val;toast(e.message||'Could not save')}
  finally{input.disabled=false}
}
function openTg(url){
  const tg=window.Telegram&&window.Telegram.WebApp;
  try{if(tg&&tg.openTelegramLink&&url.indexOf('https://t.me/')===0){tg.openTelegramLink(url);return}}catch(e){}
  window.open(url,'_blank');
}
function inviteFriends(){
  if(!botLink){toast('Link not available yet');return}
  hap('light');
  openTg('https://t.me/share/url?url='+encodeURIComponent(botLink)+'&text='+encodeURIComponent('Come join me on Christian Vent.'));
}
function openSupport(){hap('light');openTg('https://t.me/YIDIDIYATAMIRUU')}
function meBack(){go('settings',document.querySelector('.nav-item[data-page="settings"]'))}

// ---- edit profile ----
function setupEdit(){
  const p=profileCache;if(!p)return;
  document.getElementById('ep-name').value=p.name||'';
  document.getElementById('ep-bio').value=p.bio||'';
  selEmoji=p.avatar||null;
  const k=avKey(selEmoji);
  edCat=(AV[k]&&AV[k].c)||'faith';
  buildEmojiPicker();updateEditUI();
  go('edit',null);
}
function buildEmojiPicker(){
  const tabs=document.getElementById('av-tabs'),grid=document.getElementById('ep-emoji');
  if(!tabs||!grid)return;
  tabs.innerHTML=AV_CATS.map(c=>`<button type="button" class="av-tab${c[0]===edCat?' on':''}" onclick="setAvCat('${c[0]}')">${c[1]}</button>`).join('');
  const selK=avKey(selEmoji);
  grid.innerHTML=Object.keys(AV).filter(k=>AV[k].c===edCat).map(k=>`<button type="button" class="av-tile${k===selK?' sel':''}" title="${AV[k].n}" aria-label="${AV[k].n}" onclick="pickAvatar('${k}')">${avSvg(k)}<span class="av-check">${CHECK_SVG}</span></button>`).join('');
}
function setAvCat(c){edCat=c;buildEmojiPicker();hap('light')}
function pickAvatar(k){selEmoji=avEmoji(k);buildEmojiPicker();updateEditUI();hap('light')}
function clearAvatar(){selEmoji=null;buildEmojiPicker();updateEditUI();hap('light')}
function updateEditUI(){
  const name=document.getElementById('ep-name').value,bio=document.getElementById('ep-bio').value;
  document.getElementById('ep-name-cnt').textContent=name.length+'/30';
  document.getElementById('ep-bio-cnt').textContent=bio.length+'/150';
  const p=profileCache||{};
  document.getElementById('ed-preview').innerHTML=`<div class="ed-prev-ava">${avaHtml(selEmoji||p.sex)}</div><div class="ed-prev-txt"><div class="ed-prev-name">${esc(name.trim()||'Your name')}</div><div class="ed-prev-bio${bio.trim()?'':' empty'}">${esc(bio.trim()||'Your bio shows up here')}</div></div>`;
  const dirty=name.trim()!==(p.name||'')||bio.trim()!==(p.bio||'')||avKey(selEmoji)!==avKey(p.avatar);
  document.getElementById('save-profile-btn').disabled=!dirty||!name.trim();
}
async function saveProfile(){
  const name=document.getElementById('ep-name').value.trim();
  const bio=document.getElementById('ep-bio').value.trim();
  if(!name)return toast('Name required');
  const btn=document.getElementById('save-profile-btn');btn.disabled=true;
  try{
    await api(`/api/mini-app/profile/${UID}`,{method:'PUT',body:JSON.stringify({name,bio,avatar:selEmoji||''})});
    if(profileCache)Object.assign(profileCache,{name,bio,avatar:selEmoji||''});
    hap('medium');toast('Profile updated');meBack();
  }catch(e){toast(e.message||'Could not save');updateEditUI()}
}

let isAdminUser = false;
let adminMonitorPoll = null;
let adminViewingPair = null;
let adminTranscriptLimit = 60;
let adminTranscriptHasMore = false;
let adminTranscriptLoadingOlder = false;
let adminChatsPage = 1;
let adminChatsSearch = '';
let adminChatsHasMore = false;

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
      st = setTimeout(()=>loadAdminChats(e.target.value.trim(), 1), 400);
    });
  }catch(e){console.error('checkAdminStatus failed:', e);}
}

async function loadAdminChats(search='', page=1){
  const list = document.getElementById('admin-chats-list');
  adminChatsSearch = search;
  adminChatsPage = page;
  list.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text3)">Loading…</div>';
  try{
    const q = search ? `&search=${encodeURIComponent(search)}` : '';
    const d = await api(`/api/mini-app/admin/chats?admin_id=${UID}&page=${page}${q}`);
    const convos = d.data || [];
    adminChatsHasMore = !!d.has_more;
    if(!convos.length){
      list.innerHTML = page > 1
        ? '<div style="text-align:center;padding:40px;color:var(--text3)">No more conversations</div>'
        : '<div style="text-align:center;padding:40px;color:var(--text3)">No conversations found</div>';
      if(page > 1){
        // Ran past the last page (e.g. list shrank) — step back one page automatically.
        adminChatsPage = page - 1;
        loadAdminChats(search, adminChatsPage);
      }
      return;
    }
    const rows = convos.map(c => `
      <div class="chat-item" data-user-a="${esc(c.user_a)}" data-user-b="${esc(c.user_b)}" data-name-a="${esc(c.name_a)}" data-name-b="${esc(c.name_b)}">
        <div class="ava" style="width:44px;height:44px;font-size:14px">${avaHtml(c.avatar_a)}${avaHtml(c.avatar_b)}</div>
        <div class="chat-item-right">
          <div class="chat-item-top">
            <span class="chat-item-name">${esc(c.name_a)} ↔ ${esc(c.name_b)}</span>
            <span class="chat-item-time">${c.msg_count} msgs</span>
          </div>
          <div class="chat-item-preview">${esc(c.last_content || ('[' + (c.last_media_type || 'media') + ']'))}</div>
        </div>
      </div>`).join('');
    const nav = `
      <div style="display:flex;align-items:center;justify-content:center;gap:16px;padding:16px 0">
        <button class="btn-ghost" id="admin-chats-prev" style="${page<=1?'opacity:0.4;cursor:not-allowed':''}" ${page<=1?'disabled':''} onclick="loadAdminChats(adminChatsSearch, adminChatsPage-1)">◀ Prev</button>
        <span style="color:var(--text3);font-size:13px">Page ${page}</span>
        <button class="btn-ghost" id="admin-chats-next" style="${adminChatsHasMore?'':'opacity:0.4;cursor:not-allowed'}" ${adminChatsHasMore?'':'disabled'} onclick="loadAdminChats(adminChatsSearch, adminChatsPage+1)">Next ▶</button>
      </div>`;
    list.innerHTML = rows + nav;
    list.querySelectorAll('.chat-item').forEach(el=>{
      el.onclick = ()=>openAdminTranscript(el.dataset.userA, el.dataset.userB, el.dataset.nameA, el.dataset.nameB);
    });
  }catch(e){
    list.innerHTML = '<div style="padding:20px;color:var(--text3)">Failed to load</div>';
  }
}

function openAdminTranscript(userA, userB, nameA, nameB){
  clearInterval(crPoll); crPoll = null; crPartnerId = null;
  adminViewingPair = [userA, userB];
  adminTranscriptLimit = 60;
  adminTranscriptHasMore = false;
  document.getElementById('cr-name').textContent = `🔴 ${nameA} ↔ ${nameB}`;
  document.getElementById('cr-ava').innerHTML = ICONS.shield;
  document.getElementById('chat-room').classList.add('open');
  document.querySelector('.cr-input').style.display = 'none'; // admins observe, don't send
  fetchAdminTranscript(true);
  clearInterval(adminMonitorPoll);
  adminMonitorPoll = setInterval(fetchAdminTranscript, 4000);
}

async function loadOlderAdminMessages(){
  if(adminTranscriptLoadingOlder || !adminTranscriptHasMore) return;
  adminTranscriptLoadingOlder = true;
  adminTranscriptLimit += 60;
  await fetchAdminTranscript(false, true);
  adminTranscriptLoadingOlder = false;
}

async function fetchAdminTranscript(scroll=false, preserveAnchor=false){
  if(!adminViewingPair) return;
  const [a, b] = adminViewingPair;
  try{
    const box = document.getElementById('cr-msgs');
    // Same fix as fetchCRMsgs: don't tear down the DOM while a voice note is
    // actively playing OR still downloading (spinner phase) - the innerHTML
    // replace recreates every <audio> element from scratch, which both kills
    // playback and abandons an in-flight download before it can ever start.
    const isBusyVoice = ()=>Array.from(box.querySelectorAll('.voice-player-audio')).some(el=>!el.paused || el.dataset.loading==='1');
    if(isBusyVoice()) return;
    const d = await api(`/api/mini-app/admin/chats/${a}/${b}?admin_id=${UID}&limit=${adminTranscriptLimit}`);
    if(isBusyVoice()) return; // re-check: user may have started playing while this request was in flight
    adminTranscriptHasMore = !!d.has_more;
    const wasBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 80;
    // When prepending older history, anchor the viewport to what the admin was
    // already looking at instead of jumping to the top or bottom.
    const prevScrollHeight = box.scrollHeight;
    const prevScrollTop = box.scrollTop;
    const olderBtn = adminTranscriptHasMore
      ? `<div style="text-align:center;padding:4px 0 12px"><button class="btn-ghost" onclick="loadOlderAdminMessages()" id="admin-load-older-btn">Load older messages</button></div>`
      : '';
    box.innerHTML = olderBtn + (d.data || []).map(m => {
      if(m.is_deleted){
        return `<div class="msg-row ${String(m.sender_id)===String(a) ? 'them' : 'me'}"><div class="msg-bubble msg-deleted">Message deleted</div><div class="msg-time">${esc(m.time_display||'')}</div></div>`;
      }
      const editedTag = m.is_edited ? ' · edited' : '';
      return `
      <div class="msg-row ${String(m.sender_id)===String(a) ? 'them' : 'me'}">
        <div class="msg-bubble">${esc(m.content||'')}${m.media_id ? renderMedia(m.media_type, m.media_id) : ''}</div>
        <div class="msg-time">${esc(m.time_display||'')}${editedTag}</div>
      </div>`;
    }).join('');
    if(preserveAnchor){
      box.scrollTop = box.scrollHeight - prevScrollHeight + prevScrollTop;
    } else if(scroll || wasBottom){
      box.scrollTop = box.scrollHeight;
    }
  }catch(e){}
}


function chatRowsHtml(chats){
  return chats.map(c=>`<div class="chat-item" onclick="openCR('${c.partner_id}')"><div class="ava" style="width:44px;height:44px;font-size:18px">${avaHtml(c.partner_avatar||c.partner_sex)}</div><div class="chat-item-right"><div class="chat-item-top"><span class="chat-item-name">${esc(c.partner_name||'Anonymous')}</span><span class="chat-item-time">${esc(c.time_ago||'')}</span></div><div style="display:flex;align-items:center"><div class="chat-item-preview">${c.is_mine?'You: ':''}${esc(c.last_message||'')}</div>${c.unread_count>0?`<span class="unread-badge" style="margin-left:8px">${c.unread_count}</span>`:''}</div></div></div>`).join('');
}
function renderChatList(){
  const list=document.getElementById('chats-list');
  const unread=chatsCache.reduce((a,c)=>a+(c.unread_count||0),0);
  document.getElementById('chat-unread-label').textContent=unread?`${unread} unread message${unread>1?'s':''}`:'All caught up';
  const more=chatsHasMore?'<div style="text-align:center;padding:12px"><button class="btn-ghost" onclick="loadMoreChats()">Load more conversations</button></div>':'';
  list.innerHTML=chatRowsHtml(chatsCache)+more;
}
async function loadChats(){
  const list=document.getElementById('chats-list');list.innerHTML=skelChats();
  try{
    const d=await api(`/api/mini-app/chats?user_id=${UID}&page=1`);
    const chats=d.data||[];
    chatsPage=1; chatsHasMore=!!d.has_more;
    document.getElementById('chat-unread-label').textContent='All caught up';
    if(!chats.length){list.innerHTML='<div style="text-align:center;padding:40px;color:var(--text3);font-size:14px">No messages yet</div>';return}
    chatsCache=chats;
    renderChatList();
  }catch(e){list.innerHTML='<div style="padding:20px;color:var(--text3)">Failed to load</div>'}
}
async function loadMoreChats(){
  if(chatsLoadingMore||!chatsHasMore)return;
  chatsLoadingMore=true;
  try{
    const d=await api(`/api/mini-app/chats?user_id=${UID}&page=${chatsPage+1}`);
    chatsPage+=1; chatsHasMore=!!d.has_more;
    chatsCache=chatsCache.concat(d.data||[]);
    renderChatList();
  }catch(e){toast(e.message)}finally{chatsLoadingMore=false}
}

function openCR(pid,name,ava){
  clearInterval(adminMonitorPoll); adminMonitorPoll = null; adminViewingPair = null;
  document.querySelector('.cr-input').style.display = ''; // in case admin view had hidden it
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
  crOlderMsgs=[]; crMsgsCache=[]; crHasMore=false;
  fetchCRMsgs(true);
  clearInterval(crPoll);crPoll=setInterval(fetchCRMsgs,3000);
}
function closeCR(){
  document.getElementById('chat-room').classList.remove('open');
  clearInterval(crPoll); crPoll = null;
  clearInterval(adminMonitorPoll); adminMonitorPoll = null;
  crPartnerId = null;
  adminViewingPair = null;
  document.querySelector('.cr-input').style.display = ''; // restore input bar hidden by admin view
  loadChats();
}
function crRenderMsgs(scroll,preserveAnchor){
  const box=document.getElementById('cr-msgs');
  const wasBottom=box.scrollHeight-box.scrollTop<=box.clientHeight+80;
  const prevScrollHeight=box.scrollHeight, prevScrollTop=box.scrollTop;
  const olderBtn=crHasMore?'<div style="text-align:center;padding:4px 0 12px"><button class="btn-ghost" onclick="loadOlderCRMsgs()">Load older messages</button></div>':'';
  box.innerHTML=olderBtn+crMsgsCache.map(m=>{
    if(m.is_deleted){
      return `<div class="msg-row ${m.is_mine?'me':'them'}" id="msg-${m.id}"><div class="msg-bubble msg-deleted">Message deleted</div><div class="msg-time">${esc(m.timestamp||'')}</div></div>`;
    }
    const editedTag=m.is_edited?' · edited':'';
    const menuBtn='<span class="msg-menu-btn">⋯</span>';
    return `<div class="msg-row ${m.is_mine?'me':'them'}" data-mid="${m.id}" id="msg-${m.id}"><div class="msg-bubble">${crQuoteHtml(m.reply_to)}${esc(m.content)}${m.media_id?renderMedia(m.media_type,m.media_id):''}${menuBtn}</div><div class="msg-time">${esc(m.timestamp||'')}${editedTag}</div></div>`;
  }).join('')+vrPendHtmlFor('chat');
  box.querySelectorAll('.msg-menu-btn').forEach(btn=>{
    btn.onclick=(e)=>{e.stopPropagation();msgActions(btn.closest('.msg-row').dataset.mid);};
  });
  if(preserveAnchor)box.scrollTop=box.scrollHeight-prevScrollHeight+prevScrollTop;
  else if(scroll||wasBottom)box.scrollTop=box.scrollHeight;
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
    const isBusyVoice = ()=>Array.from(box.querySelectorAll('.voice-player-audio')).some(a=>!a.paused || a.dataset.loading==='1');
    if(isBusyVoice()) return;
    const partner=crPartnerId;
    // Poll only the newest 50; anything loaded via "Load older" is kept and merged in front.
    const d=await api(`/api/mini-app/chats/${partner}?user_id=${UID}&limit=50`);
    if(partner!==crPartnerId) return;   // user switched chats while this was in flight
    if(isBusyVoice()) return; // re-check: user may have started playing while this request was in flight
    const latest=d.data||[];
    if(!crOlderMsgs.length) crHasMore=!!d.has_more;
    const seen=new Set(latest.map(m=>m.id));
    crMsgsCache=crOlderMsgs.filter(m=>!seen.has(m.id)).concat(latest);
    crMsgsCache=crMergeLocal(crMsgsCache,latest);
    crRenderMsgs(scroll,false);
  }catch(e){}
}
async function loadOlderCRMsgs(){
  if(crLoadingOlder||!crHasMore||!crPartnerId||!crMsgsCache.length)return;
  crLoadingOlder=true;
  try{
    const partner=crPartnerId;
    const earliest=crMsgsCache[0].id;   // earliest message currently on screen
    const d=await api(`/api/mini-app/chats/${partner}?user_id=${UID}&limit=50&before_id=${earliest}`);
    if(partner!==crPartnerId)return;
    const older=d.data||[];
    crHasMore=!!d.has_more;
    crOlderMsgs=older.concat(crOlderMsgs);
    crMsgsCache=older.concat(crMsgsCache);
    crRenderMsgs(false,true);   // keep the viewport anchored to what the user was reading
  }catch(e){toast(e.message)}finally{crLoadingOlder=false}
}
function msgActions(id){
  const m=crMsgsCache.find(x=>String(x.id)===String(id));
  if(!m||m.is_deleted)return;
  const mask=document.createElement('div');
  mask.className='modal-mask active';
  mask.onclick=(e)=>{if(e.target===mask)mask.remove();};
  mask.innerHTML=`<div class="modal-container" style="padding:16px">
    <div style="font-weight:700;margin-bottom:12px">Message options</div>
    <button class="modal-btn modal-btn-secondary" id="msgReplyBtn">Reply</button>
    ${m.is_mine?`<button class="modal-btn modal-btn-secondary" id="msgEditBtn">Edit</button>
    <button class="modal-btn modal-btn-secondary" id="msgDelBtn" style="color:#e05252">Delete</button>`:''}
    <button class="modal-btn modal-btn-primary" id="msgCancelBtn">Cancel</button>
  </div>`;
  document.body.appendChild(mask);
  mask.querySelector('#msgCancelBtn').onclick=()=>mask.remove();
  mask.querySelector('#msgReplyBtn').onclick=()=>{mask.remove();crReplyStart(m.id);};
  if(m.is_mine){
    mask.querySelector('#msgEditBtn').onclick=()=>{mask.remove();startEditMsg(m);};
    mask.querySelector('#msgDelBtn').onclick=()=>{mask.remove();delMsg(m.id);};
  }
}
function startEditMsg(m){
  const newText=prompt('Edit message:',m.content||'');
  if(newText===null)return;
  const trimmed=newText.trim();
  if(!trimmed||trimmed===m.content)return;
  editMsg(m.id,trimmed);
}
async function editMsg(id,content){
  try{
    await api(`/api/mini-app/message/${id}`,{method:'PUT',body:JSON.stringify({user_id:UID,content})});
    fetchCRMsgs(true);
  }catch(e){toast(e.message)}
}
async function delMsg(id){
  if(!confirm("Delete this message? It'll be removed from their chat too — no trace left behind."))return;
  try{
    await api(`/api/mini-app/message/${id}?user_id=${UID}`,{method:'DELETE'});
    fetchCRMsgs(true);
  }catch(e){toast(e.message)}
}
// ---- Telegram-style replies in private chats: quote, no nesting, tap quote to jump ----
let crReplyTo = null; // { id, partner }
function crPartnerName(){ const n=document.getElementById('cr-name'); return (n&&n.textContent)||'Chat'; }
function crInfo(id){
  const m=crMsgsCache.find(x=>String(x.id)===String(id));
  return m?{id:m.id,is_mine:m.is_mine,deleted:!!m.is_deleted,content:m.content,media_type:m.media_type}:null;
}
function crQuoteHtml(rt){
  if(!rt) return '';
  const name=rt.is_mine?'You':crPartnerName();
  const text=rt.deleted?'Deleted message':cmtSnippet(rt);
  return '<div class="msg-quote" onclick="event.stopPropagation();crJump('+rt.id+')"><div class="rq-name">'+esc(name)+'</div><div class="rq-text">'+esc(text)+'</div></div>';
}
async function crJump(id){
  let el=document.getElementById('msg-'+id);
  for(let i=0;!el&&crHasMore&&i<10;i++){ await loadOlderCRMsgs(); el=document.getElementById('msg-'+id); }
  if(!el) return toast('Message not found');
  el.scrollIntoView({block:'center',behavior:'smooth'});
  const b=el.querySelector('.msg-bubble');
  if(b){ b.classList.remove('msg-flash'); void b.offsetWidth; b.classList.add('msg-flash'); }
}
function crReplyStart(id){
  const rt=crInfo(id); if(!rt||rt.deleted) return;
  crReplyTo={id:rt.id,partner:crPartnerId};
  const bar=document.getElementById('cr-reply-bar');
  bar.innerHTML='<div class="rb-line"></div><div class="rb-body" onclick="crJump('+rt.id+')"><div class="rq-name">Reply to '+esc(rt.is_mine?'You':crPartnerName())+
    '</div><div class="rq-text">'+esc(cmtSnippet(rt))+'</div></div><button type="button" class="rb-x" onclick="crCancelReply()">'+ICONS.close+'</button>';
  bar.style.display='flex';
  const t=document.getElementById('cr-txt'); if(t) t.focus();
}
function crCancelReply(){
  crReplyTo=null;
  const bar=document.getElementById('cr-reply-bar');
  if(bar){ bar.style.display='none'; bar.innerHTML=''; }
}
setInterval(()=>{ if(crReplyTo && crReplyTo.partner!==crPartnerId) crCancelReply(); },300); // never leaks into another chat
(function(){ // swipe a message to the right to reply, like Telegram
  const box=document.getElementById('cr-msgs'); if(!box) return;
  let row=null,x0=0,y0=0,dx=0,drag=false,ico=null,buzzed=false;
  const reset=()=>{
    if(!row) return;
    const r=row,i=ico;
    r.style.transition='transform .18s ease'; r.style.transform='';
    setTimeout(()=>{ r.style.transition=''; if(i) i.remove(); },200);
    row=null; ico=null; drag=false; dx=0; buzzed=false;
  };
  box.addEventListener('pointerdown',e=>{
    if(e.target.closest('button,a,audio,video,.msg-menu-btn,.msg-quote,.voice-player')) return;
    const r=e.target.closest('.msg-row');
    if(!r||!r.dataset.mid) return;
    row=r; x0=e.clientX; y0=e.clientY; dx=0; drag=false; buzzed=false;
  });
  box.addEventListener('pointermove',e=>{
    if(!row) return;
    const mx=e.clientX-x0, my=e.clientY-y0;
    if(!drag){
      if(Math.abs(my)>12 && Math.abs(my)>Math.abs(mx)){ row=null; return; } // vertical scroll
      if(mx>10 && mx>Math.abs(my)*1.5){
        drag=true; ico=document.createElement('div'); ico.className='msg-swipe-ico'; ico.innerHTML=ICONS.reply||''; row.appendChild(ico);
        try{ box.setPointerCapture(e.pointerId); }catch(_){}
      } else return;
    }
    dx=Math.max(0,Math.min(mx,64));
    row.style.transform='translateX('+dx+'px)';
    ico.style.opacity=String(Math.min(1,dx/48));
    if(dx>=56 && !buzzed){ buzzed=true; vrHaptic('light'); }
  });
  box.addEventListener('pointerup',()=>{ if(row && drag && dx>=56) crReplyStart(row.dataset.mid); reset(); });
  box.addEventListener('pointercancel',reset);
})();

// Messages we just sent are added straight from the server reply (no full chat re-download).
// They are merged back in on every poll until the server list contains them, so they never flicker away.
let crLocalSent = [];
function crAddLocal(d, extra, pend) {
  const m = Object.assign({ id: d.id, sender_id: d.sender_id, receiver_id: d.receiver_id, content: d.content, media_type: 'text', media_id: null,
    timestamp: d.timestamp, is_read: false, is_mine: true, is_edited: false, is_deleted: false, reply_to: null }, extra || {});
  m.partner = String(d.receiver_id); m._t = Date.now();
  crLocalSent.push(m);
  if (pend) vrPendings = vrPendings.filter(x => x !== pend);
  if (String(crPartnerId) === m.partner && !crMsgsCache.some(x => x.id === m.id)) { crMsgsCache.push(m); crRenderMsgs(true, false); }
}
function crMergeLocal(cache, latest) {
  const have = new Set(latest.map(m => m.id)), now = Date.now();
  crLocalSent = crLocalSent.filter(m => !have.has(m.id) && now - m._t < 60000);
  const extra = crLocalSent.filter(m => m.partner === String(crPartnerId) && !cache.some(x => x.id === m.id));
  return extra.length ? cache.concat(extra) : cache;
}
async function crRunText(e) {
  e.status = 'sending'; vrPendRender('chat');
  try {
    const payload = { sender_id: UID, receiver_id: e.ref, content: e.text };
    if (e.replyId) payload.reply_to_id = e.replyId;
    const res = await api('/api/mini-app/chats/send', { method: 'POST', body: JSON.stringify(payload) });
    crAddLocal(res.data, { reply_to: e.replyId ? crInfo(e.replyId) : null }, e);
  } catch (err) { e.status = 'failed'; vrPendRender('chat'); toast(err.message); }
}
async function crSend(){
  const ta=document.getElementById('cr-txt');
  const txt=ta.value.trim();
  if((!txt&&!pendingChatMedia)||!crPartnerId)return;
  const replyId=crReplyTo?crReplyTo.id:0;
  if(crReplyTo)crCancelReply();
  ta.value='';
  if(!pendingChatMedia){
    // Text: the bubble appears instantly, the request runs in the background
    const e={id:++vrPendSeq,kind:'chat',text:txt,replyId,ref:crPartnerId,status:'sending'};
    vrPendings.push(e);
    vrPendRender('chat');
    crRunText(e);
    return;
  }
  const media=pendingChatMedia;
  const payload={sender_id:UID,receiver_id:crPartnerId,content:txt,media_type:media.media_type,media_id:media.media_id};
  if(replyId)payload.reply_to_id=replyId;
  try{
    const res=await api('/api/mini-app/chats/send',{method:'POST',body:JSON.stringify(payload)});
    pendingChatMedia=null;
    document.getElementById('chat-file-input').value='';
    document.getElementById('chat-attach-btn').classList.remove('has-media');
    renderMediaPreview(document.getElementById('chat-media-preview'),null);
    crAddLocal(res.data,{content:txt,media_type:media.media_type,media_id:media.media_id,reply_to:replyId?crInfo(replyId):null});
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
        ${u.role?`<div class="modal-role">${ICONS.shield}${esc(u.role)}</div>`:''}
        ${u.bio?`<div class="modal-bio">${esc(u.bio)}</div>`:''}
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
  if(UID){loadFeed(); checkAdminStatus(); refreshVentSexRow();}
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
        # Strict boolean on purpose: the string "false" must not count as a yes.
        reveal_sex = data.get('reveal_sex') is True

        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
        
        if not content and not media_id:
            return jsonify({'success': False, 'error': 'Content cannot be empty'}), 400
            
        if not categories:
            return jsonify({'success': False, 'error': 'At least one category is required'}), 400
        
        # Check if user exists
        user = get_user_cached(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        if not media_id:
            media_type = 'text'
        elif not media_type or media_type == 'text':
            # Never store (media_id set, media_type 'text'): fall back to the generic
            # detector's default ('document') when the client didn't say what it uploaded.
            media_type = _detect_mini_app_media_type(None, None)[0]

        # Per-post "show my sex": the emoji is read from the user's saved profile at
        # submission time (never trusted from the client). Users with no sex set (👤)
        # get None, i.e. nothing is shown, even if the client sent reveal_sex=true.
        revealed_sex = None
        if reveal_sex:
            sex_row = db_fetch_one("SELECT sex FROM users WHERE user_id = %s", (str(user_id),))
            revealed_sex = normalize_revealed_sex(sex_row['sex'] if sex_row else None)

        # Insert the post
        post_row = db_execute(
            "INSERT INTO posts (content, author_id, media_type, media_id, approved, explicit, revealed_sex) VALUES (%s, %s, %s, %s, FALSE, %s, %s) RETURNING post_id",
            (content, user_id, media_type, media_id, explicit, revealed_sex),
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
    """Sync version of notify_admin_of_new_post. Now sends the actual media (photo/voice/audio)
    when present, instead of leaving the admin a blank text notification when a mini-app vent
    has no typed caption."""
    try:
        if not ADMIN_ID:
            return
        
        post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        
        author = get_user_cached(post['author_id'])
        author_name = get_display_name(author)
        
        media_type = post.get('media_type') or 'text'
        media_id = post.get('media_id')
        content_text = post['content'] or ''
        explicit_line = "Marked as explicit\n\n" if post.get('explicit') else ""
        media_label = {'voice': '[Voice message — no caption]', 'audio': '[Audio — no caption]', 'photo': '[Photo — no caption]'}.get(media_type, '')

        logger.info(f"Mini App Post awaiting approval from {author_name}: {content_text[:100] or media_label}")

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"approve_post_{post_id}"},
                    {"text": "Reject", "callback_data": f"reject_post_{post_id}"}
                ],
                [
                    {"text": "Unmark Explicit" if post.get('explicit') else "Mark Explicit",
                     "callback_data": f"toggle_explicit_{post_id}"}
                ],
                [{"text": "🛡 Moderate author", "callback_data": f"mod_post_{post_id}"}]
            ]
        }

        if media_id and media_type != 'text':
            caption_body = content_text[:900] + ('...' if len(content_text) > 900 else '') if content_text else media_label
            caption = f"New post awaiting approval from {author_name}:\n\n{explicit_line}{caption_body}"[:1024]
            _fire_and_forget(
                send_telegram_media_sync,
                chat_id=ADMIN_ID, media_type=media_type, media_id=media_id,
                caption=caption, parse_mode=None, reply_markup=keyboard
            )
        else:
            post_preview = content_text[:4000] + ('...' if len(content_text) > 4000 else '')
            header = f"New post awaiting approval from {author_name}:\n\n{explicit_line}{post_preview}"
            _fire_and_forget(send_telegram_message_sync, ADMIN_ID, header, parse_mode=None, reply_markup=keyboard)
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
        resp = _tg_session.post(url, json=payload, timeout=10)
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
        payload["caption"] = truncate_for_telegram(caption, 1024)  # Telegram's caption hard limit
        if parse_mode:
            payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        resp = _tg_session.post(url, json=payload, timeout=15)
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


def notify_user_of_private_message_sync(sender_id, receiver_id, message_content, media_type='text', media_id=None, message_id=None):
    """Sync replacement for notify_user_of_private_message — actually delivers the media file."""
    try:
        is_blocked = db_fetch_one(
            "SELECT * FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (receiver_id, sender_id)
        )
        if is_blocked:
            return

        receiver = get_user_cached(receiver_id)
        if not receiver or not receiver.get('notifications_enabled'):
            return

        sender = get_user_cached(sender_id)
        sender_name = get_display_name(sender)
        safe_sender_name = html.escape(sender_name)

        preview_content = truncate_for_telegram(message_content or "", PM_TEXT_CONTENT_LIMIT)
        safe_preview = html.escape(preview_content) if preview_content else ""

        keyboard = {
            "inline_keyboard": [[
                {"text": "Reply", "callback_data": f"reply_msg_{sender_id}"},
                {"text": "Block", "callback_data": f"block_user_{sender_id}"}
            ]]
        }

        header = f"<b>New Private Message</b>\n\nFrom: <b>{safe_sender_name}</b>\n\n"
        footer = "\n\n<i>Use /inbox to view all messages</i>"

        def _deliver():
            result = None
            if media_id and media_type and media_type != 'text':
                caption = header + safe_preview + footer
                result = send_telegram_media_sync(
                    chat_id=receiver_id, media_type=media_type, media_id=media_id,
                    caption=caption, parse_mode='HTML', reply_markup=keyboard
                )

            if not (result and result.get('ok')):
                # if media send failed outright (or there was no media), fall through to plain text
                fallback_body = safe_preview if safe_preview else "<i>[attachment]</i>"
                notification_text = header + fallback_body + footer
                result = send_telegram_message_sync(receiver_id, notification_text, parse_mode='HTML', reply_markup=keyboard)

            # Remember the live notification's message_id so a later edit/delete of
            # this private message can be applied natively to the real Telegram message.
            # (This write depends on the HTTP result, so it has to run with it, in the pool.)
            if message_id and result and result.get('ok') and result.get('result'):
                notif_message_id = result['result'].get('message_id')
                if notif_message_id:
                    db_execute(
                        "UPDATE private_messages SET notif_message_id = %s WHERE message_id = %s",
                        (notif_message_id, message_id)
                    )

        _fire_and_forget(_deliver)

    except Exception as e:
        logger.error(f"notify_user_of_private_message_sync failed: {e}")


def edit_native_pm_notification_sync(receiver_id, notif_message_id, sender_id, new_content, media_type='text', media_id=None):
    """Sync (HTTP) version of edit_native_pm_notification, for use from Flask
    endpoints (the mini app) where no PTB bot context is available."""
    try:
        sender = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (sender_id,))
        sender_name = get_display_name(sender) if sender else "Someone"
        safe_sender_name = html.escape(sender_name)

        preview_content = truncate_for_telegram(new_content or "", PM_TEXT_CONTENT_LIMIT)
        safe_preview = html.escape(preview_content) if preview_content else ""

        header = f"<b>New Private Message</b>\n\nFrom: <b>{safe_sender_name}</b>\n\n"
        footer = "\n\n<i>Use /inbox to view all messages</i>"

        is_media = bool(media_id) and media_type and media_type != 'text'
        if is_media:
            caption = header + safe_preview + footer
            url = f"https://api.telegram.org/bot{TOKEN}/editMessageCaption"
            payload = {
                "chat_id": receiver_id,
                "message_id": notif_message_id,
                "caption": caption[:1024],
                "parse_mode": "HTML"
            }
        else:
            fallback_body = safe_preview if safe_preview else "<i>[attachment]</i>"
            text = header + fallback_body + footer
            url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
            payload = {
                "chat_id": receiver_id,
                "message_id": notif_message_id,
                "text": text,
                "parse_mode": "HTML"
            }

        resp = _tg_session.post(url, json=payload, timeout=10)
        result = resp.json()
        if not result.get('ok'):
            logger.warning(f"edit_native_pm_notification_sync failed: {result}")
        return result
    except Exception as e:
        logger.error(f"edit_native_pm_notification_sync error: {e}")
        return None


def delete_native_pm_notification_sync(chat_id, notif_message_id):
    """Natively delete a previously-delivered notification message via the Bot
    API (sync/HTTP), so a deleted private message leaves no placeholder behind."""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
        resp = _tg_session.post(url, json={"chat_id": chat_id, "message_id": notif_message_id}, timeout=10)
        result = resp.json()
        if not result.get('ok'):
            logger.warning(f"delete_native_pm_notification_sync failed: {result}")
        return result
    except Exception as e:
        logger.error(f"delete_native_pm_notification_sync error: {e}")
        return None


def notify_vent_author_of_comment_sync(post_id, commenter_id, comment_id=None, comment_content=None, media_type='text', media_id=None):
    """Sync replacement for notify_vent_author_of_comment, for use from Flask routes."""
    try:
        post = db_fetch_one("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        author_id = post['author_id']
        if str(author_id) == str(commenter_id):
            return

        author = get_user_cached(author_id)
        if not author or not author.get('notifications_enabled'):
            return

        commenter = get_user_cached(commenter_id)
        commenter_name = get_display_name(commenter)

        post_preview = post['content'][:50] + '...' if post['content'] and len(post['content']) > 50 else (post['content'] or "")
        safe_commenter = html.escape(commenter_name)
        safe_post_preview = html.escape(post_preview)
        media_labels = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🏷 Sticker', 'photo': '🖼 Photo'}
        safe_comment = html.escape(truncate_for_telegram(comment_content or '', COMMENT_TEXT_CONTENT_LIMIT)) if comment_content else media_labels.get(media_type, '')

        lines = ["💬 <b>New comment on your vent</b>", "", f"<b>{safe_commenter}</b> wrote:"]
        if safe_comment:
            lines.append(f"<blockquote>{safe_comment}</blockquote>")
        lines.append(f"<i>Your vent: {safe_post_preview}</i>")
        lines.append(f"\n<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>")
        notification_text = "\n".join(lines)

        reply_markup = None
        if comment_id:
            reply_markup = {"inline_keyboard": [[
                {"text": "↩ Reply", "callback_data": f"reply_{post_id}_{comment_id}"}
            ]]}

        def _deliver():
            if media_id and media_type and media_type != 'text':
                result = send_telegram_media_sync(author_id, media_type, media_id, caption=notification_text, parse_mode='HTML', reply_markup=reply_markup)
                if result and result.get('ok'):
                    return
            send_telegram_message_sync(author_id, notification_text, parse_mode='HTML', reply_markup=reply_markup)

        _fire_and_forget(_deliver)
    except Exception as e:
        logger.error(f"notify_vent_author_of_comment_sync failed: {e}")


def notify_user_of_reply_sync(post_id, parent_comment_id, replier_id, new_comment_id=None, comment_content=None, media_type='text', media_id=None):
    """Sync replacement for notify_user_of_reply, for use from Flask routes."""
    try:
        parent_comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = %s", (parent_comment_id,))
        if not parent_comment:
            return

        original_author = get_user_cached(parent_comment['author_id'])
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
            replier = get_user_cached(replier_id)
            safe_replier_name = html.escape(get_display_name(replier))

        post_preview = post['content'][:50] + '...' if post['content'] and len(post['content']) > 50 else (post['content'] or "")
        safe_post_preview = html.escape(post_preview)
        safe_parent_preview = html.escape((parent_comment['content'] or '[media]')[:100])
        media_labels = {'voice': '🎤 Voice message', 'gif': '🎞 GIF', 'sticker': '🏷 Sticker', 'photo': '🖼 Photo'}
        safe_comment = html.escape(truncate_for_telegram(comment_content or '', COMMENT_TEXT_CONTENT_LIMIT)) if comment_content else media_labels.get(media_type, '')

        lines = [f"↩ <b>{safe_replier_name}</b> replied to your comment", ""]
        if safe_comment:
            lines.append(f"<blockquote>{safe_comment}</blockquote>")
        lines.append(f"<i>Replying to: {safe_parent_preview}</i>")
        lines.append(f"<i>Post: {safe_post_preview}</i>")
        lines.append(f"\n<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>")
        notification_text = "\n".join(lines)

        reply_markup = None
        if new_comment_id:
            reply_markup = {"inline_keyboard": [[
                {"text": "↩ Reply", "callback_data": f"replytoreply_{post_id}_{parent_comment_id}_{new_comment_id}"}
            ]]}

        target_user_id = original_author['user_id']

        def _deliver():
            if media_id and media_type and media_type != 'text':
                result = send_telegram_media_sync(target_user_id, media_type, media_id, caption=notification_text, parse_mode='HTML', reply_markup=reply_markup)
                if result and result.get('ok'):
                    return
            send_telegram_message_sync(target_user_id, notification_text, parse_mode='HTML', reply_markup=reply_markup)

        _fire_and_forget(_deliver)
    except Exception as e:
        logger.error(f"notify_user_of_reply_sync failed: {e}")

def notify_post_author_of_thread_reply_sync(post_id, parent_comment_id, replier_id, comment_content=None, media_type='text', media_id=None):
    """Sync version for Flask routes: tell the vent author about replies others leave under comments."""
    try:
        post = db_fetch_one("SELECT author_id, content FROM posts WHERE post_id = %s", (post_id,))
        if not post:
            return
        author_id = str(post['author_id'])
        if author_id == str(replier_id):
            return

        parent = db_fetch_one("SELECT author_id, content FROM comments WHERE comment_id = %s", (parent_comment_id,))
        if not parent:
            return
        if str(parent['author_id']) == author_id:
            return  # already notified by notify_user_of_reply_sync

        author = get_user_cached(author_id)
        if not author or not author.get('notifications_enabled'):
            return

        replier = get_user_cached(replier_id)
        replier_name = get_display_name(replier)
        parent_author = get_user_cached(parent['author_id'])
        parent_name = get_display_name(parent_author)

        post_preview = (post['content'][:60] + '...') if post['content'] and len(post['content']) > 60 else (post['content'] or "")
        media_labels = {'voice': '[Voice message]', 'gif': '[GIF]', 'sticker': '[Sticker]', 'photo': '[Photo]'}
        body = truncate_for_telegram(comment_content, COMMENT_TEXT_CONTENT_LIMIT) if comment_content else media_labels.get(media_type, '')

        lines = [f"<b>{html.escape(replier_name)}</b> replied to {html.escape(parent_name)} on your vent:", ""]
        if body:
            lines.append(f"<blockquote>{html.escape(body)}</blockquote>")
        lines.append(f"They were replying to: {html.escape((parent['content'] or '[media]')[:100])}")
        lines.append(f"Your vent: {html.escape(post_preview)}")
        lines.append(f"\n<a href='https://t.me/{BOT_USERNAME}?start=comments_{post_id}'>View conversation</a>")
        text = "\n".join(lines)

        def _deliver():
            if media_id and media_type and media_type != 'text':
                result = send_telegram_media_sync(author_id, media_type, media_id, caption=text, parse_mode='HTML')
                if result and result.get('ok'):
                    return
            send_telegram_message_sync(author_id, text, parse_mode='HTML')

        _fire_and_forget(_deliver)
    except Exception as e:
        logger.error(f"notify_post_author_of_thread_reply_sync failed: {e}")

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
        _tg_session.post(url, json=payload, timeout=5)
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
            resp = _tg_session.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=30)
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
    """Streams a Telegram file to the mini app. The bot token stays server-side: the client
    never gets a redirect to a URL that contains it. Range requests are forwarded so audio and
    video can still seek (iOS/Safari refuses to play media that isn't range-capable)."""
    upstream = None
    try:
        resp = _tg_session.get(f"https://api.telegram.org/bot{TOKEN}/getFile", params={'file_id': file_id}, timeout=10)
        result = resp.json()
        if not result.get('ok'):
            return jsonify({'success': False, 'error': 'File not found'}), 404
        file_path = result['result']['file_path']

        fwd_headers = {}
        if request.headers.get('Range'):
            fwd_headers['Range'] = request.headers['Range']
        upstream = _tg_session.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
            headers=fwd_headers, stream=True, timeout=(5, 30)
        )
        if upstream.status_code not in (200, 206):
            upstream.close()
            return jsonify({'success': False, 'error': 'File not found'}), 404

        def generate(up=upstream):
            try:
                for chunk in up.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                up.close()

        headers = {'Cache-Control': 'private, max-age=3600'}
        for name in ('Content-Range', 'Accept-Ranges'):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        # Only forward Content-Length when the body is passed through untouched.
        if 'Content-Length' in upstream.headers and 'Content-Encoding' not in upstream.headers:
            headers['Content-Length'] = upstream.headers['Content-Length']
        return Response(
            generate(),
            status=upstream.status_code,
            content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
            headers=headers
        )
    except Exception as e:
        if upstream is not None:
            upstream.close()
        # requests' error text can embed the request URL, i.e. the bot token: scrub it,
        # and never send it to the client.
        logger.error(f"Error proxying file: {str(e).replace(str(TOKEN), '<token>')}")
        return jsonify({'success': False, 'error': 'Could not load file'}), 500

# One query for a whole feed page: page slice -> categories, unread count, reaction counts,
# the viewer's reaction, the viewer's admin flag and every author's aura score (via the shared
# score CTE, aggregated once per *author on this page*, not per row). total_count comes from
# a window function so the separate COUNT(*) is only needed for out-of-range pages.
_FEED_SQL = f"""
    WITH page_posts AS (
        SELECT p.post_id, p.content, p.timestamp, p.comment_count, p.media_type,
               p.media_id, p.explicit, p.author_id, p.vent_number, p.revealed_sex,
               COUNT(*) OVER () AS total_count
        FROM posts p
        WHERE p.approved = TRUE AND p.deleted = FALSE
        ORDER BY p.timestamp DESC, p.post_id DESC
        LIMIT %s OFFSET %s
    ),
    score_authors AS (
        SELECT DISTINCT author_id FROM page_posts
    ),
    {_SCORE_CTES_SQL}
    SELECT
        pp.post_id, pp.content, pp.timestamp, pp.comment_count, pp.media_type,
        pp.media_id, pp.explicit, pp.total_count, pp.vent_number, pp.revealed_sex,
        u.user_id AS author_id,
        u.sex AS author_sex,
        u.avatar_emoji AS author_avatar,
        u.anonymous_name AS author_name,
        u.is_admin AS author_is_admin,
        COALESCE(u.hide_aura, FALSE) AS author_hide_aura,
        COALESCE(cat.categories, '') AS categories,
        COALESCE(uc.n, 0) AS unread_comments,
        COALESCE(rx.counts, '{{}}'::jsonb) AS reaction_counts,
        ur.type AS user_reaction,
        COALESCE(asr.score, 0) AS author_score,
        COALESCE(v.is_admin, FALSE) AS viewer_is_admin
    FROM page_posts pp
    JOIN users u ON u.user_id = pp.author_id
    LEFT JOIN LATERAL (
        SELECT STRING_AGG(DISTINCT pc.category_code, ',') AS categories
        FROM post_categories pc WHERE pc.post_id = pp.post_id
    ) cat ON TRUE
    LEFT JOIN post_views pv ON pv.user_id = %s AND pv.post_id = pp.post_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS n FROM comments c
        WHERE c.post_id = pp.post_id
          AND c.timestamp > COALESCE(pv.last_viewed, '1970-01-01'::timestamp)
    ) uc ON TRUE
    LEFT JOIN LATERAL (
        SELECT jsonb_object_agg(t.type, t.cnt) AS counts FROM (
            SELECT r.type, COUNT(*) AS cnt FROM reactions r
            WHERE r.post_id = pp.post_id GROUP BY r.type
        ) t
    ) rx ON TRUE
    LEFT JOIN LATERAL (
        SELECT r.type FROM reactions r
        WHERE r.post_id = pp.post_id AND r.user_id = %s LIMIT 1
    ) ur ON TRUE
    LEFT JOIN author_scores asr ON asr.author_id = pp.author_id
    LEFT JOIN users v ON v.user_id = %s
    ORDER BY pp.timestamp DESC, pp.post_id DESC
"""


@flask_app.route('/api/mini-app/get-posts', methods=['GET'])
def mini_app_get_posts():
    """API endpoint for getting posts from mini app - With Pagination and Unread Counts"""
    try:
        user_id = request.args.get('user_id')
        uid = str(user_id) if user_id else None
        page = _clamp_int(request.args.get('page'), 1, 100000, 1)
        per_page = _clamp_int(request.args.get('per_page'), 1, 50, 10)
        offset = (page - 1) * per_page

        posts = db_fetch_all(_FEED_SQL, (per_page, offset, uid, uid, uid))

        formatted_posts = []
        is_admin_viewer = bool(posts and posts[0].get('viewer_is_admin'))
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
            
            rating = int(post.get('author_score') or 0)
            is_owner = str(post['author_id']) == str(user_id) if user_id else False
            show_aura = not post.get('author_hide_aura') or is_owner or is_admin_viewer
            aura_sticker = format_aura(rating) if (not post['author_is_admin'] and show_aura) else ""
            
            category_list = post['categories'].split(',') if post['categories'] else ['Other']
            
            is_explicit = bool(post.get('explicit'))
            hide_content = is_explicit and not is_owner and not is_admin_viewer
            if hide_content:
                content_preview = "This post contains explicit content that may not be suitable for all viewers."

            reaction_counts = post.get('reaction_counts') or {}
            if isinstance(reaction_counts, str):  # jsonb normally arrives already parsed
                reaction_counts = json.loads(reaction_counts)
            
            formatted_posts.append({
                'id': post['post_id'],
                'vent_number': post.get('vent_number'),
                'revealed_sex': normalize_revealed_sex(post.get('revealed_sex')),
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
                    'counts': reaction_counts,
                    'user_reaction': post.get('user_reaction')
                }
            })

        # total_posts now counts exactly what the feed shows (approved AND not deleted).
        if posts:
            total = int(posts[0]['total_count'])
        elif offset > 0:
            # Page past the end: the window count has no rows to ride on, so ask directly.
            total_row = db_fetch_one("SELECT COUNT(*) AS count FROM posts WHERE approved = TRUE AND deleted = FALSE")
            total = int(total_row['count']) if total_row else 0
        else:
            total = 0
        
        return jsonify({
            'success': True,
            'data': formatted_posts,
            'page': page,
            'total_posts': total,
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
                p.post_id, p.vent_number, p.revealed_sex, p.content, p.timestamp, p.comment_count, p.media_type, p.media_id, p.deleted, p.explicit,
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
            'revealed_sex': normalize_revealed_sex(post.get('revealed_sex')),
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
    """API endpoint for fetching a post's comments with threading support.
    Paginated like the chat endpoint: ?limit=100 (1..500) returns the NEWEST `limit` comments
    (oldest-first, so the tree still reads top-down); pass ?before_id=<comment_id> to get the
    `limit` comments immediately before that one. Response carries has_more."""
    try:
        # Get viewer_id
        viewer_id = request.args.get('viewer_id')
        reveal_requested = request.args.get('reveal') == '1'
        limit = _clamp_int(request.args.get('limit'), 1, 500, 100)
        before_id = _parse_positive_int(request.args.get('before_id'))
        viewer_row = db_fetch_one("SELECT is_admin FROM users WHERE user_id = %s", (str(viewer_id),)) if viewer_id else None
        is_viewer_admin = bool(viewer_row and viewer_row.get('is_admin'))

        # One lookup serves both the explicit-content gate and the vent-author badge below.
        post_gate = db_fetch_one("SELECT author_id, explicit FROM posts WHERE post_id = %s", (post_id,))
        if post_gate and post_gate.get('explicit'):
            is_privileged_viewer = bool(viewer_id) and (
                str(viewer_id) == str(post_gate['author_id']) or is_viewer_admin
            )
            if not is_privileged_viewer and not reveal_requested:
                return jsonify({'success': True, 'data': [], 'content_hidden': True})

        before_clause = ""
        params = [post_id]
        if before_id:
            before_clause = "AND (c.timestamp, c.comment_id) < (SELECT b.timestamp, b.comment_id FROM comments b WHERE b.comment_id = %s)"
            params.append(before_id)
        params.append(limit + 1)  # one extra row tells us whether older comments exist

        comments = db_fetch_all(f'''
            SELECT * FROM (
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
                    u.is_admin as author_is_admin,
                    COALESCE(u.hide_aura, FALSE) as author_hide_aura
                FROM comments c
                JOIN users u ON c.author_id = u.user_id
                WHERE c.post_id = %s {before_clause}
                ORDER BY c.timestamp DESC, c.comment_id DESC
                LIMIT %s
            ) win
            ORDER BY win.time_ago ASC, win.comment_id ASC
        ''', tuple(params))
        has_more = len(comments) > limit
        if has_more:
            comments = comments[1:]  # drop the extra, oldest row

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
        post_author_id = post_gate['author_id'] if post_gate else None
        ratings_map = get_user_ratings_batch([c['author_id'] for c in comments])
        # Quoted-message previews (Telegram-style reply). Looked up by id so the quote still works
        # when the original sits on an older page that is not loaded yet. Same post only.
        parent_ids = list({c['parent_comment_id'] for c in comments if c['parent_comment_id']})
        parent_map = {}
        if parent_ids:
            prow = db_fetch_all('''
                SELECT c.comment_id, c.content, c.type AS media_type, c.author_id, u.anonymous_name AS author_name
                FROM comments c LEFT JOIN users u ON c.author_id = u.user_id
                WHERE c.comment_id IN %s AND c.post_id = %s
            ''', (tuple(parent_ids), post_id))
            parent_map = {r['comment_id']: r for r in (prow or [])}
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

            rating = ratings_map.get(c['author_id'], 0)
            is_owner = str(c['author_id']) == str(viewer_id) if viewer_id else False
            show_aura = not c.get('author_hide_aura') or is_owner or is_viewer_admin
            aura_str = format_aura(rating) if (not c['author_is_admin'] and show_aura) else ""

            _pid = c['parent_comment_id']
            _pr = parent_map.get(_pid) if _pid else None
            if not _pid:
                _reply_to = None
            elif not _pr:
                _reply_to = {'id': _pid, 'deleted': True}
            else:
                _reply_to = {'id': _pid, 'author_id': _pr['author_id'], 'author_name': _pr['author_name'] or 'Anonymous',
                             'content': (_pr['content'] or '')[:160], 'media_type': _pr['media_type']}
            formatted_comments.append({
                'id': c['comment_id'],
                'parent_id': c['parent_comment_id'] or 0,
                'reply_to': _reply_to,
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
                    'aura': aura_str,
                    'is_admin': c['author_is_admin'],
                    'is_vent_author': str(c['author_id']) == str(post_author_id) if post_author_id else False
                },
                'reactions': {
                    'counts': comment_reactions_map.get(c['comment_id'], {}),
                    'user_reaction': comment_user_reactions_map.get(c['comment_id'], None)
                }
            })

        # Mark as read up to the newest comment actually returned (so "unread" only clears
        # for what was shown). GREATEST keeps a later view from being moved backwards when the
        # client is paging through older comments.
        if viewer_id and comments:
            try:
                db_execute("""
                    INSERT INTO post_views (user_id, post_id, last_viewed)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, post_id)
                    DO UPDATE SET last_viewed = GREATEST(post_views.last_viewed, EXCLUDED.last_viewed)
                """, (str(viewer_id), post_id, comments[-1]['time_ago']))
            except Exception as pv_err:  # e.g. unknown viewer id (FK) - never fail the read
                logger.warning(f"Could not update post_views for {viewer_id}/{post_id}: {pv_err}")

        return jsonify({'success': True, 'data': formatted_comments, 'has_more': has_more})
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
        _leaderboard_cache_bust()

        # Notify the right person: parent-comment author for a reply, otherwise the vent author
        if parent_comment_id and parent_comment_id != 0:
            notify_user_of_reply_sync(
                post_id, parent_comment_id, user_id, new_comment_id,
                comment_content=content, media_type=media_type, media_id=file_id
            )
            notify_post_author_of_thread_reply_sync(
                post_id, parent_comment_id, user_id,
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
        _leaderboard_cache_bust()
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
        user_id = str(user_id)
        page = _clamp_int(request.args.get('page'), 1, 100000, 1)
        per_page = _clamp_int(request.args.get('per_page'), 1, 100, 30)
        offset = (page - 1) * per_page

        # Latest message per conversation pair (uses idx_pm_conversation_pair), paginated
        # BEFORE the unread lateral join so the COUNT only runs for the rows on this page.
        # per_page + 1 rows are fetched to learn whether another page exists.
        rows = db_fetch_all("""
            WITH last_messages AS (
                SELECT DISTINCT ON (LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id))
                    CASE WHEN sender_id = %s THEN receiver_id ELSE sender_id END AS partner_id,
                    content, timestamp, is_read, sender_id, is_deleted, is_edited
                FROM private_messages
                WHERE sender_id = %s OR receiver_id = %s
                ORDER BY LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id),
                         timestamp DESC, message_id DESC
            ),
            page AS (
                SELECT * FROM last_messages ORDER BY timestamp DESC LIMIT %s OFFSET %s
            )
            SELECT
                lm.partner_id, lm.content, lm.timestamp, lm.is_read, lm.sender_id,
                lm.is_deleted, lm.is_edited,
                u.anonymous_name as partner_name,
                u.sex as partner_sex,
                u.avatar_emoji as partner_avatar,
                u.is_admin as partner_is_admin,
                COALESCE(unread.cnt, 0) as unread_count
            FROM page lm
            JOIN users u ON u.user_id = lm.partner_id
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS cnt FROM private_messages pm
                WHERE pm.receiver_id = %s AND pm.sender_id = lm.partner_id AND pm.is_read = FALSE
            ) unread ON TRUE
            ORDER BY lm.timestamp DESC
        """, (user_id, user_id, user_id, per_page + 1, offset, user_id))
        has_more = len(rows or []) > per_page
        rows = (rows or [])[:per_page]
        
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

            if r.get('is_deleted'):
                last_message = "Message deleted"
            else:
                last_message = r['content']
                if r.get('is_edited'):
                    last_message = f"{last_message} (edited)" if last_message else last_message

            chats.append({
                'partner_id': r['partner_id'],
                'partner_name': r['partner_name'] or 'Anonymous',
                'partner_sex': r['partner_sex'] or '👤',
                'partner_avatar': r['partner_avatar'] or '',
                'partner_is_admin': r['partner_is_admin'],
                'last_message': last_message,
                'time_ago': time_str,
                'is_mine': str(r['sender_id']) == str(user_id),
                'unread_count': r['unread_count']
            })
            
        return jsonify({'success': True, 'data': chats, 'page': page, 'has_more': has_more})
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

try:
    _ADDIS_TZ = ZoneInfo("Africa/Addis_Ababa") if ZoneInfo else timezone(timedelta(hours=3))
except Exception:
    _ADDIS_TZ = timezone(timedelta(hours=3))


def format_ethiopian_time(dt):
    """Format datetime into Western (EAT) + Ethiopian Amharic time format."""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
        except:
            return dt
            
    # Naive DB timestamps are in the server's local zone (UTC on Render); convert to Addis
    # Ababa time. Ethiopia has no DST, so the fixed +3 fallback is exact if tzdata is missing.
    dt = dt.astimezone(_ADDIS_TZ).replace(tzinfo=None)
        
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
        limit = _clamp_int(request.args.get('limit'), 1, 200, 50)
        before_id = _parse_positive_int(request.args.get('before_id'))
            
        # Mark incoming messages from this partner as read (only on the live/newest page -
        # paging back through history can't contain anything new).
        if not before_id:
            db_execute("""
                UPDATE private_messages 
                SET is_read = TRUE 
                WHERE sender_id = %s AND receiver_id = %s AND is_read = FALSE
            """, (partner_id, user_id))
        
        # Newest `limit` messages (or the `limit` before `before_id`), returned oldest-first.
        before_clause = ""
        params = [user_id, partner_id, partner_id, user_id]
        if before_id:
            before_clause = "AND (timestamp, message_id) < (SELECT b.timestamp, b.message_id FROM private_messages b WHERE b.message_id = %s)"
            params.append(before_id)
        params.append(limit + 1)  # one extra row => has_more
        rows = db_fetch_all(f"""
            SELECT * FROM (
                SELECT message_id, sender_id, receiver_id, content, timestamp, is_read, media_type, media_id,
                       is_edited, is_deleted, reply_to_id
                FROM private_messages
                WHERE ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
                  {before_clause}
                ORDER BY timestamp DESC, message_id DESC
                LIMIT %s
            ) win
            ORDER BY win.timestamp ASC, win.message_id ASC
        """, tuple(params))
        has_more = len(rows or []) > limit
        if has_more:
            rows = rows[1:]  # drop the extra, oldest row
        
        # Quoted-message previews, looked up by id so a quote works even when the original is on an
        # older page that is not loaded yet. Restricted to this conversation.
        reply_ids = list({r['reply_to_id'] for r in (rows or []) if r.get('reply_to_id')})
        reply_map = {}
        if reply_ids:
            prow = db_fetch_all("""
                SELECT message_id, sender_id, content, media_type, is_deleted
                FROM private_messages
                WHERE message_id IN %s
                  AND ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
            """, (tuple(reply_ids), user_id, partner_id, partner_id, user_id))
            reply_map = {p['message_id']: p for p in (prow or [])}

        def _reply_info(rid):
            if not rid:
                return None
            p = reply_map.get(rid)
            if not p:
                return {'id': rid, 'deleted': True}
            gone = bool(p.get('is_deleted'))
            return {'id': rid, 'is_mine': str(p['sender_id']) == str(user_id), 'deleted': gone,
                    'content': None if gone else (p['content'] or '')[:160],
                    'media_type': None if gone else p.get('media_type')}

        messages = []
        for r in (rows or []):
            if isinstance(r['timestamp'], str):
                msg_time = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                msg_time = r['timestamp']

            is_deleted = bool(r.get('is_deleted'))

            messages.append({
                'id': r['message_id'],
                'sender_id': r['sender_id'],
                'receiver_id': r['receiver_id'],
                'content': None if is_deleted else r['content'],
                'media_type': None if is_deleted else r.get('media_type', 'text'),
                'media_id': None if is_deleted else r.get('media_id'),
                'timestamp': format_ethiopian_time(msg_time),
                'is_read': r['is_read'],
                'is_mine': str(r['sender_id']) == str(user_id),
                'is_edited': bool(r.get('is_edited')),
                'is_deleted': is_deleted,
                'reply_to': None if is_deleted else _reply_info(r.get('reply_to_id'))
            })
            
        return jsonify({'success': True, 'data': messages, 'has_more': has_more})
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

        # Optional reply target: must be a message of THIS conversation, otherwise ignored.
        reply_to_id = _parse_positive_int(data.get('reply_to_id'))
        if reply_to_id:
            in_convo = db_fetch_one("""
                SELECT 1 FROM private_messages
                WHERE message_id = %s
                  AND ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
            """, (reply_to_id, sender_id, receiver_id, receiver_id, sender_id))
            if not in_convo:
                reply_to_id = None

        res = db_execute("""
            INSERT INTO private_messages (sender_id, receiver_id, content, media_type, media_id, reply_to_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING message_id, timestamp
        """, (sender_id, receiver_id, content, media_type, media_id, reply_to_id), fetchone=True)

        # Deliver a real notification — including the actual media file if present
        # Notification setup (block check, user lookups, Telegram call) no longer delays the response.
        _fire_and_forget(
            notify_user_of_private_message_sync,
            sender_id=sender_id,
            receiver_id=receiver_id,
            message_content=content,
            media_type=media_type,
            media_id=media_id,
            message_id=res['message_id'] if res else None
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

@flask_app.route('/api/mini-app/message/<int:message_id>', methods=['PUT'])
def mini_app_update_message(message_id):
    """API endpoint for editing a private message the current user sent."""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id') or request.args.get('user_id')
        content = (data.get('content') or '').strip()

        if not user_id:
            return jsonify({'success': False, 'error': 'Missing user_id'}), 400
        if not content:
            return jsonify({'success': False, 'error': 'Content required'}), 400

        msg = db_fetch_one(
            "SELECT sender_id, receiver_id, is_deleted, media_type, media_id, notif_message_id "
            "FROM private_messages WHERE message_id = %s",
            (message_id,)
        )
        if not msg or str(msg['sender_id']) != str(user_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        if msg.get('is_deleted'):
            return jsonify({'success': False, 'error': 'Message was deleted'}), 400

        db_execute(
            "UPDATE private_messages SET content = %s, is_edited = TRUE, edited_at = CURRENT_TIMESTAMP WHERE message_id = %s",
            (content, message_id)
        )

        # Reflect the edit live in the receiver's chat using Telegram's native
        # edit, instead of the change only existing in our own DB copy.
        if msg.get('notif_message_id'):
            edit_native_pm_notification_sync(
                receiver_id=msg['receiver_id'],
                notif_message_id=msg['notif_message_id'],
                sender_id=user_id,
                new_content=content,
                media_type=msg.get('media_type'),
                media_id=msg.get('media_id')
            )

        return jsonify({'success': True, 'message': 'Message updated'})
    except Exception as e:
        logger.error(f"Message update error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/message/<int:message_id>', methods=['DELETE'])
def mini_app_delete_message(message_id):
    """API endpoint for deleting a private message the current user sent — a
    real, clean delete. Removes the live notification from the receiver's chat
    natively and drops the row entirely, so nothing embarrassing (a "Message
    deleted" placeholder) is left for the other person to see."""
    try:
        user_id = request.args.get('user_id')
        if not user_id:
            body = request.get_json(silent=True) or {}
            user_id = body.get('user_id')

        msg = db_fetch_one(
            "SELECT sender_id, receiver_id, is_deleted, notif_message_id FROM private_messages WHERE message_id = %s",
            (message_id,)
        )
        if not msg or str(msg['sender_id']) != str(user_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        if msg.get('is_deleted'):
            return jsonify({'success': True, 'message': 'Message already deleted'})

        # Best-effort: remove the live notification from the receiver's chat first.
        if msg.get('notif_message_id'):
            delete_native_pm_notification_sync(msg['receiver_id'], msg['notif_message_id'])

        db_execute("DELETE FROM private_messages WHERE message_id = %s", (message_id,))
        return jsonify({'success': True, 'message': 'Message deleted'})
    except Exception as e:
        logger.error(f"Message delete error: {e}")
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
        _tg_session.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Failed to send chat request notification: {e}")
    
    return jsonify({'success': True, 'status': 'pending', 'message': 'Chat request sent'})
@flask_app.route('/api/mini-app/leaderboard', methods=['GET'])
def mini_app_leaderboard():
    """API endpoint for leaderboard data"""
    try:
        # Top 10 users with weighted aura (single CTE query, cached for ~60s)
        top_users = get_leaderboard_rows(10)

        
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
        
        # Bio and role follow the same privacy switches the bot's profile card uses.
        is_target_admin = bool(user.get('is_admin'))
        bio_display = (user.get('bio') or "").strip()
        role_display = "Administrator" if is_target_admin else ""
        if not is_viewer_admin and not is_owner:
            if user.get('hide_bio'):
                bio_display = ""
            if user.get('hide_role'):
                role_display = ""

        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
                'avatar': user['avatar_emoji'] or "",
                'bio': bio_display,
                'role': role_display,
                'weekly_badge': user['weekly_badge'] or "",
                'rating': rating_display,
                'aura': aura_display,
                'is_admin': is_target_admin,
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

    page = _clamp_int(request.args.get('page'), 1, 100000, 1)
    search = (request.args.get('search') or '').strip()[:100] or None
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

    limit = _clamp_int(request.args.get('limit'), 1, 500, 100)
    msgs = get_admin_conversation_transcript(user_a, user_b, limit=limit)
    total = get_admin_conversation_message_count(user_a, user_b)

    data = []
    for m in msgs:
        ts = m['timestamp']
        is_deleted = bool(m.get('is_deleted'))
        data.append({
            'id': m['message_id'],
            'sender_id': m['sender_id'],
            'receiver_id': m['receiver_id'],
            'content': "[deleted]" if is_deleted else m['content'],
            'media_type': None if is_deleted else m.get('media_type', 'text'),
            'media_id': None if is_deleted else m.get('media_id'),
            'is_deleted': is_deleted,
            'is_edited': bool(m.get('is_edited')),
            'time_display': format_ethiopian_time(ts)
        })

    return jsonify({'success': True, 'data': data, 'total': total, 'has_more': total > len(data)})

@flask_app.route('/api/mini-app/admin/pending-posts', methods=['GET'])
def mini_app_admin_pending_posts():
    """API endpoint for admin to get pending posts"""
    try:
        # Was completely unauthenticated. Same gate as the other admin endpoints, same
        # generic 403 (no hint whether the id exists / is an admin).
        admin_id = request.args.get('admin_id') or request.args.get('user_id')
        if not admin_id or not _require_admin(admin_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
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
        data = request.get_json(silent=True) or {}
        admin_id = data.get('admin_id') or data.get('user_id') or request.args.get('admin_id')
        if not admin_id or not _require_admin(admin_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        post_id = data.get('post_id')
        
        if not post_id:
            return jsonify({'success': False, 'error': 'Post ID required'}), 400
        
        # Update the post to approved
        success = db_execute(
            "UPDATE posts SET approved = TRUE WHERE post_id = %s",
            (post_id,)
        )
        calculate_user_rating.cache_clear()  # an approved post is +10 aura
        _leaderboard_cache_bust()
        
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
            SELECT p.post_id, p.vent_number, p.revealed_sex, p.content, p.timestamp, p.comment_count, p.explicit, p.media_type, p.media_id,
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
        ratings_map = get_user_ratings_batch([p['author_id'] for p in posts])
        for post in posts:
            rating = ratings_map.get(post['author_id'], 0)
            is_owner = str(post['author_id']) == str(user_id)
            is_explicit = bool(post.get('explicit'))
            hide_content = is_explicit and not is_owner and not is_admin_viewer
            content_preview = post['content'][:300] + '...' if len(post['content']) > 300 else post['content']
            if hide_content:
                content_preview = "This post contains explicit content that may not be suitable for all viewers."
            formatted_posts.append({
                'id': post['post_id'],
                'vent_number': post.get('vent_number'),
                'revealed_sex': normalize_revealed_sex(post.get('revealed_sex')),
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
        data = request.get_json(silent=True) or {}
        # Values can arrive as null (e.g. no avatar chosen), so coerce before strip().
        name = (data.get('name') or '').strip()
        bio = (data.get('bio') or '').strip()
        avatar = (data.get('avatar') or '').strip()

        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        # Same limits the bot enforces: 30 for the name, 150 for the bio; avatar_emoji is VARCHAR(10).
        if len(name) > 30:
            return jsonify({'success': False, 'error': 'Name must be 30 characters or fewer'}), 400
        if len(bio) > 150:
            return jsonify({'success': False, 'error': 'Bio must be 150 characters or fewer'}), 400
        if len(avatar) > 10:
            return jsonify({'success': False, 'error': 'Invalid avatar'}), 400

        db_update_user(user_id, anonymous_name=name, bio=bio, avatar_emoji=avatar or None)
        
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
        if not user_id:
            data = request.get_json(silent=True) or {}
            user_id = data.get('user_id')

        if not user_id:
            return jsonify({'success': False, 'error': 'Missing user_id'}), 400

        comment = db_fetch_one("SELECT author_id, post_id FROM comments WHERE comment_id = %s", (comment_id,))
        
        if not comment or str(comment['author_id']) != str(user_id):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
            
        post_id = comment['post_id']
        
        # Cascade re-parent child comments
        db_execute("UPDATE comments SET parent_comment_id = 0 WHERE parent_comment_id = %s", (comment_id,))
        # Delete reactions and comment
        db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment_id,))
        db_execute("DELETE FROM comments WHERE comment_id = %s", (comment_id,))
        calculate_user_rating.cache_clear()
        _leaderboard_cache_bust()
        
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
        user = db_fetch_one(
            "SELECT notifications_enabled, privacy_public, is_admin, "
            "COALESCE(hide_aura, FALSE) AS hide_aura, COALESCE(hide_bio, FALSE) AS hide_bio, "
            "COALESCE(hide_follower_count, FALSE) AS hide_follower_count, "
            "COALESCE(hide_role, FALSE) AS hide_role "
            "FROM users WHERE user_id = %s",
            (user_id,)
        )
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        return jsonify({
            'success': True,
            'data': {
                'notifications': user['notifications_enabled'],
                'privacy_public': user['privacy_public'],
                'hide_aura': bool(user['hide_aura']),
                'hide_bio': bool(user['hide_bio']),
                'hide_follower_count': bool(user['hide_follower_count']),
                'hide_role': bool(user['hide_role']),
                'is_admin': bool(user['is_admin']),
                'bot_link': f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else ""
            }
        })
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/settings/<user_id>', methods=['POST'])
def mini_app_update_settings(user_id):
    """API endpoint for updating user settings"""
    try:
        data = request.get_json(silent=True) or {}

        # request key -> users column. Only these boolean switches can be changed from here.
        setting_columns = {
            'notifications': 'notifications_enabled',
            'privacy_public': 'privacy_public',
            'hide_aura': 'hide_aura',
            'hide_bio': 'hide_bio',
            'hide_follower_count': 'hide_follower_count',
            'hide_role': 'hide_role',
        }
        fields = {}
        for key, column in setting_columns.items():
            if key in data and data[key] is not None:
                if not isinstance(data[key], bool):
                    return jsonify({'success': False, 'error': f'{key} must be true or false'}), 400
                fields[column] = data[key]

        if not fields:
            return jsonify({'success': False, 'error': 'No settings to update'}), 400

        db_update_user(user_id, **fields)

        return jsonify({'success': True, 'message': 'Settings updated'})
    except Exception as e:
        logger.error(f"Error updating settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == "__main__": 
    # The main() function already handles initializing the DB, 
    # starting the Flask server, and running the bot polling.
    main()
