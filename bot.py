# Add these imports at the top of bot.py (after the existing imports)
import jwt 
import requests
from telegram import WebAppInfo
from threading import Thread
import subprocess
import os 
import logging
import psycopg2
import json
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
from flask import Flask, jsonify, request, redirect, render_template_string 
from contextlib import closing
from datetime import datetime, timedelta, timezone
import random
import time
import asyncio
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
                async def schedule_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
                    """Schedule a broadcast for later"""
                    # Similar to execute_broadcast but stores in database
                    pass
                
                async def check_scheduled_broadcasts(context: ContextTypes.DEFAULT_TYPE):
                    """Check and send scheduled broadcasts"""
                    scheduled = db_fetch_all('''
                        SELECT * FROM scheduled_broadcasts 
                        WHERE status = 'scheduled' 
                        AND scheduled_time <= CURRENT_TIMESTAMP
                    ''')
                    
                    for broadcast in scheduled:
                        # Send the broadcast
                        # Update status to 'sent'
                        pass
                
                # Schedule this to run every minute in main():
                job_queue.run_repeating(check_scheduled_broadcasts, interval=60, first=10)

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
                
                # Add other missing columns if needed in the future
                # Example for future migrations:
                # c.execute("""
                #     SELECT column_name 
                #     FROM information_schema.columns 
                #     WHERE table_name='users' AND column_name='new_column'
                # """)
                # if not c.fetchone():
                #     c.execute("ALTER TABLE users ADD COLUMN new_column TEXT DEFAULT NULL")

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
                try:
                    # Update the channel post
                    vent_number_str = f"Vent - {next_vent_number:03d}"
                    hashtag = f"#{post_data['category']}"
                    
                    new_caption = (
                        f"`{vent_number_str}`\n\n"
                        f"{post_data['content']}\n\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"{hashtag}\n"
                        f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
                    )
                    
                    # We can't edit the message here without the bot instance
                    # This would need to be run in a context where we have access to the bot
                    logger.info(f"Post {post['post_id']} should be updated to Vent - {next_vent_number:03d}")
                    
                except Exception as e:
                    logger.error(f"Error updating post {post['post_id']}: {e}")
            
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
            selected_category = NULL,
            comment_post_id = NULL,
            comment_idx = NULL,
            private_message_target = NULL
        WHERE user_id = %s
    ''', (user_id,))
    
    # If chat_id and context are provided, restore main menu
    if chat_id and context:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="What would you like to do next?",
                reply_markup=main_menu
            )
        except Exception as e:
            logger.error(f"Error restoring main menu: {e}")
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
    """Show login page for mini app"""
    bot_username = BOT_USERNAME
    
    html = '''<!DOCTYPE html>
<html>
<head>
    <title>Christian Vent - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #272F32;
            color: #E0E0E0;
            margin: 0;
            padding: 20px;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .login-container {
            background: #2E3A40;
            padding: 40px;
            border-radius: 12px;
            border: 1px solid #3A4A50;
            max-width: 500px;
            width: 100%;
            text-align: center;
        }
        
        .logo {
            width: 90px;
            height: auto;
            margin-bottom: 15px;
        }
        .title {
            color: #BF970B;
            font-size: 2.8rem;
            font-weight: 700;
            letter-spacing: 3px;
            font-family: 'Oswald', sans-serif;
            text-transform: uppercase;
            margin: 0;
        }
        h1 {
            color: #BF970B;
            margin-bottom: 10px;
        }
        
        p {
            opacity: 0.8;
            line-height: 1.6;
            margin-bottom: 30px;
        }
        
        .telegram-btn {
            background: #0088cc;
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 8px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            margin-bottom: 20px;
            text-decoration: none;
            display: inline-block;
        }
        
        .telegram-btn:hover {
            background: #0077b3;
        }
        
        .bot-link {
            color: #BF970B;
            text-decoration: none;
            font-weight: 600;
        }
        
        .bot-link:hover {
            text-decoration: underline;
        }
        
        .features {
            text-align: left;
            margin-top: 30px;
            background: rgba(191, 151, 11, 0.1);
            padding: 20px;
            border-radius: 8px;
        }
        
        .features h3 {
            color: #BF970B;
            margin-top: 0;
        }
        
        .features ul {
            padding-left: 20px;
        }
        
        .features li {
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="brand">
            <img src="/static/images/vent%20logo.jpg" class="logo" alt="Christian Vent Logo">
            <h1 class="title">CHRISTIAN VENT</h1>
        </div>

        <p>Share your thoughts anonymously with the Christian community</p>
        
        <p>To use the mini app, you need to authenticate with the Telegram bot:</p>
        
        <a href="https://t.me/''' + bot_username + '''" class="telegram-btn" target="_blank">
            Open Telegram Bot
        </a>
        
        <p>Or use this link: <a href="https://t.me/''' + bot_username + '''" class="bot-link" target="_blank">@''' + bot_username + '''</a></p>
        
        <div class="features">
            <h3>Features:</h3>
            <ul>
                <li>Share anonymous vents and prayers</li>
                <li>Join Christian community discussions</li>
                <li>View and comment on posts</li>
                <li>Check leaderboard of top contributors</li>
                <li>Manage your profile and settings</li>
            </ul>
        </div>
        
        <p style="margin-top: 30px; font-size: 0.9rem; opacity: 0.7;">
            After opening the bot, use the /webapp command to get authenticated access to the mini app.
        </p>
    </div>
</body>
</html>'''
    
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

# Create main menu keyboard with improved buttons
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🌟 Share My Thoughts")],
        [KeyboardButton("👤 View Profile"), KeyboardButton("📚 My Previous Posts")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("🌐 Web App"), KeyboardButton("❓ Help")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)
# Cancel-only menu for input states
cancel_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("❌ Cancel")]
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
    # Simply return "Anonymous" without numbers for all new users
    return "Anonymous"

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

def format_aura(rating):
    """Create aura based on contribution points."""
    if rating >= 100:
        return "🟣"  # Purple aura for elite users (100+ points)
    elif rating >= 50:
        return "🔵"  # Blue aura for advanced users (50-99 points)
    elif rating >= 25:
        return "🟢"  # Green aura for intermediate users (25-49 points)
    elif rating >= 10:
        return "🟡"  # Yellow aura for active users (10-24 points)
    else:
        return "⚪️"  # White aura for new users (0-9 points)

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
    
    # Get top 10 users
    top_users = db_fetch_all('''
        SELECT user_id, anonymous_name, sex,
               (SELECT COUNT(*) FROM posts WHERE author_id = users.user_id AND approved = TRUE) + 
               (SELECT COUNT(*) FROM comments WHERE author_id = users.user_id) AS total
        FROM users
        ORDER BY total DESC
        LIMIT 10
    ''')
    
    # Create clean header
    leaderboard_text = "*🏆 Christian Vent Leaderboard*\n\n"
    
    # Define medal emojis for top 3
    medal_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    
    # Format each user
    for idx, user in enumerate(top_users, start=1):
        aura = format_aura(user['total'])
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{user['user_id']}"
        
        # Create clean line
        if idx <= 3:
            rank_prefix = medal_emojis[idx]
        else:
            rank_prefix = f"{idx}."
        
        leaderboard_text += (
            f"{rank_prefix} {user['sex']} "
            f"[{user['anonymous_name']}]({profile_link})\n"
            f"   {user['total']} pts {aura}\n\n"
        )
    
    # Add current user's rank
    user_id = str(update.effective_user.id)
    user_rank = get_user_rank(user_id)
    
    if user_rank:
        user_data = db_fetch_one("SELECT anonymous_name, sex FROM users WHERE user_id = %s", (user_id,))
        if user_data:
            user_contributions = calculate_user_rating(user_id)
            aura = format_aura(user_contributions)
            profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{user_id}"
            
            leaderboard_text += f"*Your position:* {user_rank}\n"
            leaderboard_text += f"{user_data['sex']} {user_data['anonymous_name']} • {user_contributions} pts {aura}\n\n"
    
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
                    f"📝 *Post Preview* [{category}]\n\n"
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
        [InlineKeyboardButton(f"📊 Statistics", callback_data='admin_stats')],
        [InlineKeyboardButton("📢 Send Broadcast", callback_data='admin_broadcast')],  # This is the broadcast button
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
    await query.answer()
    
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
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_broadcast_type(update: Update, context: ContextTypes.DEFAULT_TYPE, broadcast_type: str):
    """Handle broadcast type selection"""
    query = update.callback_query
    await query.answer()
    
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
        message = query.message
        user_id = str(query.from_user.id)
        is_callback = True
    else:
        # Handle case when called from handle_message
        message = update.message
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
        # Get the next vent number FIRST
        max_vent = db_fetch_one("SELECT MAX(vent_number) as max_num FROM posts WHERE approved = TRUE")
        next_vent_number = (max_vent['max_num'] or 0) + 1
        
        # Format the post content for the channel with vent number
        hashtag = f"#{post['category']}"
        
        # Create the vent number text (copyable format)
        vent_display = f"Vent - {next_vent_number:03d}"
        
        caption_text = (
            f"`{vent_display}`\n\n"
            f"{post['content']}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/christianvent)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        
        # Create the comments button
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
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
        if post['media_type'] == 'text':
            msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        elif post['media_type'] == 'photo':
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id
            )
        elif post['media_type'] == 'voice':
            msg = await context.bot.send_voice(
                chat_id=CHANNEL_ID,
                voice=post['media_id'],
                caption=caption_text,
                parse_mode=ParseMode.MARKDOWN,
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
        
        # =============================================
        # CRITICAL FIX: Update the admin's original message to remove Approve/Reject buttons
        # =============================================
        try:
            # Edit the original admin notification message to show it's approved
            await query.edit_message_text(
                f"✅ **Post Approved and Published!**\n\n"
                f"**Vent Number:** {vent_display}\n"
                f"**Category:** {post['category']}\n"
                f"**Published to channel:** ✅\n\n"
                f"**Content Preview:**\n{post['content'][:150]}...",
                parse_mode=ParseMode.MARKDOWN
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
        
        # =============================================
        # FIX: Update the admin's message to show it's rejected
        # =============================================
        try:
            # Edit the original admin notification message
            await query.edit_message_text(
                f"❌ **Post Rejected**\n\n"
                f"**Post ID:** #{post_id}\n"
                f"**Category:** {post['category']}\n"
                f"**Action:** Deleted from database\n\n"
                f"**Content Preview:**\n{post['content'][:100]}...",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except BadRequest:
            # If editing fails, send a new message
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
                
                await query.message.reply_text(
                    f"{preview_text}\n\n✍️ Please type your comment or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
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
                    f"🌀 *Aura:* {format_aura(rating)} (Level {rating // 10 + 1})\n"
                    f"⭐️ Contributions: {rating}\n"
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
            InlineKeyboardButton("📚 My Content", callback_data='my_content_menu'),
            InlineKeyboardButton("🏆 Leaderboard", callback_data='leaderboard')
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data='settings'),
            InlineKeyboardButton("❓ Help", callback_data='help')
        ]
    ]
    
    await update.message.reply_text(
        "✝️ *እንኳን ወደ Christian vent በሰላም መጡ* ✝️\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "ማንነታችሁ ሳይገለጽ ሃሳባችሁን ማጋራት ትችላላችሁ.\n\n የሚከተሉትን ምረጡ :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    
    await update.message.reply_text(
        "You can also use the buttons below to navigate:",
        reply_markup=main_menu
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
        msg_number = (page - 1) * per_page + idx
        
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
    
    # Create clean action buttons (like WhatsApp/Telegram)
    keyboard = [
        [
            InlineKeyboardButton("💬 Reply", callback_data=f"reply_msg_{message['sender_id']}"),
            InlineKeyboardButton("👤 View Profile", callback_data=f"profileid_{message['sender_id']}")
        ],
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_message_{message_id}_{from_page}"),
            InlineKeyboardButton("⛔ Block", callback_data=f"block_user_{message['sender_id']}")
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

def escape_markdown_v2(text):
    """Escape all special characters for MarkdownV2"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, '\\' + char)
    return text

async def send_comment_message(context, chat_id, comment, author_text, reply_to_message_id=None):
    """Helper function to send comments with proper media handling"""
    comment_id = comment['comment_id']
    comment_type = comment['type']
    file_id = comment['file_id']
    content = comment['content']
    
    # Get user reaction for buttons
    user_id = getattr(context, '_user_id', None)
    user_reaction = None
    if user_id:
        user_reaction = db_fetch_one(
            "SELECT type FROM reactions WHERE comment_id = %s AND user_id = %s",
            (comment_id, user_id)
        )
    
    # Get reaction counts
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

    like_emoji = "👍" if user_reaction and user_reaction['type'] == 'like' else "👍"
    dislike_emoji = "👎" if user_reaction and user_reaction['type'] == 'dislike' else "👎"

    # Build keyboard
    kb_buttons = [
        [
            InlineKeyboardButton(f"{like_emoji} {likes}", callback_data=f"likecomment_{comment_id}"),
            InlineKeyboardButton(f"{dislike_emoji} {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
            InlineKeyboardButton("Reply", callback_data=f"reply_{comment['post_id']}_{comment_id}")
        ]
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
        
        if comment_type == 'text':
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )
            return msg.message_id
            
        elif comment_type == 'voice' and file_id:
            msg = await context.bot.send_voice(
                chat_id=chat_id,
                voice=file_id,
                caption=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id
            )
            return msg.message_id
            
        elif comment_type == 'gif' and file_id:
            msg = await context.bot.send_animation(
                chat_id=chat_id,
                animation=file_id,
                caption=message_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id
            )
            return msg.message_id
            
        elif comment_type == 'sticker' and file_id:
            msg = await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=file_id,
                reply_to_message_id=reply_to_message_id
            )
            return msg.message_id
            
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
            try:
                await loading_msg.delete()
            except:
                pass
        await context.bot.send_message(chat_id, "❌ Post not found.", reply_markup=main_menu)
        return

    post_author_id = post['author_id']

    per_page = 5  # Top-level comments per page
    offset = (page - 1) * per_page

    # Show oldest first, newest last
    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = %s AND parent_comment_id = 0 ORDER BY timestamp ASC LIMIT %s OFFSET %s",
        (post_id, per_page, offset)
    )

    # Count only top-level comments for pagination
    total_comments_row = db_fetch_one(
        "SELECT COUNT(*) as cnt FROM comments WHERE post_id = %s AND parent_comment_id = 0",
        (post_id,)
    )
    total_comments = total_comments_row['cnt'] if total_comments_row else 0
    total_pages = (total_comments + per_page - 1) // per_page

    if not comments and page == 1:
        if loading_msg:
            try:
                await loading_msg.delete()
            except:
                pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="\\_No comments yet.\\_",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu
        )
        return

    user_id = str(update.effective_user.id)
    context._user_id = user_id
    context._post_author_id = post_author_id

    if reply_pages is None:
        reply_pages = {}

    # Delete loading message if it exists
    if loading_msg:
        try:
            await loading_msg.delete()
        except:
            pass

    # Show each top-level comment with LIMITED replies
    for comment in comments:
        commenter_id = comment['author_id']
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (commenter_id,))
        display_sex = get_display_sex(commenter)
        display_name = get_display_name(commenter)
        rating = calculate_user_rating(commenter_id)
        profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{commenter_id}"

        # Check if commenter is the vent author
        if str(commenter_id) == str(post_author_id):
            author_text = (
                f"{display_sex} "
                f"✅ _[vent author]({escape_markdown(profile_link, version=2)})_ "
                f"⚡ _Aura_ {rating} {format_aura(rating)}"
            )
        else:
            author_text = (
                f"{display_sex} "
                f"_[{escape_markdown(display_name, version=2)}]({escape_markdown(profile_link, version=2)})_ "
                f"⚡ _Aura_ {rating} {format_aura(rating)}"
            )

        # Send the top-level comment
        msg_id = await send_comment_message(context, chat_id, comment, author_text, None)

        # Show LIMITED replies for this comment (first 3 replies)
        replies_per_comment = 3
        replies = db_fetch_all(
            "SELECT * FROM comments WHERE parent_comment_id = %s ORDER BY timestamp ASC LIMIT %s",
            (comment['comment_id'], replies_per_comment)
        )
        
        # Count total replies for this comment
        total_replies_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM comments WHERE parent_comment_id = %s",
            (comment['comment_id'],)
        )
        total_replies = total_replies_row['cnt'] if total_replies_row else 0
        
        for reply in replies:
            await send_reply_message(context, chat_id, reply, post_author_id, msg_id)

        # Add "Show more replies" button if there are more replies
        if total_replies > replies_per_comment:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📨 Show more replies ({total_replies - replies_per_comment} more)", 
                    callback_data=f"show_more_replies_{comment['comment_id']}_1"
                )]
            ])
            await context.bot.send_message(
                chat_id=chat_id,
                text="",
                reply_markup=keyboard,
                reply_to_message_id=msg_id
            )
    
    # Pagination buttons for top-level comments
    pagination_buttons = []
    if page > 1:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Older Comments", callback_data=f"viewcomments_{post_id}_{page-1}"))
    if page < total_pages:
        pagination_buttons.append(InlineKeyboardButton("Newer Comments ➡️", callback_data=f"viewcomments_{post_id}_{page+1}"))
    
    if pagination_buttons:
        pagination_markup = InlineKeyboardMarkup([pagination_buttons])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📄 Page {page}/{total_pages} (Oldest to Newest)",
            reply_markup=pagination_markup,
            disable_web_page_preview=True
        )
async def send_reply_message(context, chat_id, reply, post_author_id, reply_to_message_id):
    """Send a single reply message with proper formatting"""
    reply_user_id = reply['author_id']
    reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (reply_user_id,))
    reply_display_name = get_display_name(reply_user)
    reply_display_sex = get_display_sex(reply_user)
    rating_reply = calculate_user_rating(reply_user_id)
    
    reply_profile_link = f"https://t.me/{BOT_USERNAME}?start=profileid_{reply_user_id}"
    
    # Check if reply author is the vent author
    if str(reply_user_id) == str(post_author_id):
        reply_author_text = (
            f"{reply_display_sex} "
            f"✅ _[vent author]({reply_profile_link})_ "
            f"⚡ _Aura_ {rating_reply} {format_aura(rating_reply)}"
        )
    else:
        reply_author_text = (
            f"{reply_display_sex} "
            f"_[{escape_markdown(reply_display_name, version=2)}]({reply_profile_link})_ "
            f"⚡ _Aura_ {rating_reply} {format_aura(rating_reply)}"
        )

    # Send the reply
    await send_comment_message(context, chat_id, reply, reply_author_text, reply_to_message_id)

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
    offset = (page - 1) * replies_per_page
    
    # Get replies for this page
    replies = db_fetch_all(
        "SELECT * FROM comments WHERE parent_comment_id = %s ORDER BY timestamp ASC LIMIT %s OFFSET %s",
        (comment_id, replies_per_page, offset)
    )
    
    # Count total replies
    total_replies_row = db_fetch_one(
        "SELECT COUNT(*) as cnt FROM comments WHERE parent_comment_id = %s",
        (comment_id,)
    )
    total_replies = total_replies_row['cnt'] if total_replies_row else 0
    total_pages = (total_replies + replies_per_page - 1) // replies_per_page
    
    # Delete the "Show more replies" button
    try:
        await query.message.delete()
    except:
        pass
    
    # Send the replies for this page
    for reply in replies:
        await send_reply_message(context, chat_id, reply, post_author_id, query.message.reply_to_message.message_id)
    
    # If there are more replies, show another "Show more" button
    if page < total_pages:
        remaining = total_replies - (page * replies_per_page)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📨 Show more replies ({remaining} more)", 
                callback_data=f"show_more_replies_{comment_id}_{page + 1}"
            )]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text="",
            reply_markup=keyboard,
            reply_to_message_id=query.message.reply_to_message.message_id
        )
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile", callback_data='profile')
        ],
        [
            InlineKeyboardButton("📚 My Content", callback_data='my_content_menu'),
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
    
    
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = %s",
        (user_id,)
    )
    
    # UPDATED: Changed to "My Content" menu
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📚 My Content", callback_data='my_content_menu')],  # Changed to menu
        [InlineKeyboardButton("📭 Inbox", callback_data='inbox')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='settings')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    await context.bot.send_message(
    chat_id=chat_id,
    text=(
        f"👤 *{display_name}* \n"
        f"📌 Sex: {display_sex}\n"
        f"🌀 *Aura:* {format_aura(rating)} (Level {rating // 10 + 1})\n"
        f"🎯 Contributions: {rating} points\n"
        f"👥 Followers: {len(followers)}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"_Use /menu to return_"
    ),
    reply_markup=kb,
    parse_mode=ParseMode.MARKDOWN)

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
    
    # Get post details
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = %s", (post_id,))
    
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
    escaped_category = escape_markdown(post['category'], version=2)
    
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
        f"📌 **Category:** {escaped_category}\n"
        f"📅 **Posted on:** {escape_markdown(timestamp, version=2)}\n"
        f"💬 **Comments:** {comment_count}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**Content:**\n\n"
        f"{escaped_content}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    
    # Create action buttons for this post
    keyboard = [
        [
            InlineKeyboardButton("💬 View Comments", callback_data=f"viewcomments_{post_id}_1"),
            InlineKeyboardButton("🧵 Continue Thread", callback_data=f"continue_post_{post_id}")
        ],
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
        text = f"💬 \\*My Comments\\* \\(Page {page}/{total_pages}\\)\n\n"
        
        for idx, comment in enumerate(comments):
            comment_num = (page - 1) * per_page + idx + 1
            
            # Truncate content
            comment_preview = comment['content'][:80] + '...' if len(comment['content']) > 80 else comment['content']
            escaped_comment_preview = escape_markdown(comment_preview, version=2)
            
            text += f"\\*\\*{comment_num}\\.\\*\\* {escaped_comment_preview}\n\n"
        
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Error answering callback query: {e}")
    
    user_id = str(query.from_user.id)
    
    # Log the callback data for debugging
    logger.info(f"Callback data received: {query.data} from user {user_id}")
    
    try:
        # ... rest of your code
        # FIXED: Handle noop callback (do nothing for separator buttons)
        if query.data == 'noop':
            return  # Do nothing and exit the function
            
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
                f"✍️ *Please type your thought for #{category}:*\n\nYou may also send a photo or voice message.\n\nTap ❌ Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )
        
        elif query.data == 'menu':
            keyboard = [
                [
                    InlineKeyboardButton("🌟 Share My Thoughts", callback_data='ask'),
                    InlineKeyboardButton("👤 View Profile", callback_data='profile')
                ],
                [
                    InlineKeyboardButton("📚 My Content", callback_data='my_content_menu'),
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

        # Handle cancel input button
        elif query.data == 'cancel_input':
            # Reset all waiting states and restore main menu
            await reset_user_waiting_states(
                user_id, 
                query.message.chat.id, 
                context
            )
            
            # Clear any context data
            if 'editing_comment' in context.user_data:
                del context.user_data['editing_comment']
            if 'editing_post' in context.user_data:
                del context.user_data['editing_post']
            if 'thread_from_post_id' in context.user_data:
                del context.user_data['thread_from_post_id']
            
            # Send confirmation and restore main menu
            await query.answer("❌ Input cancelled")
            
            await query.message.reply_text(
                "❌ *Input cancelled*\n\nWhat would you like to do next?",
                reply_markup=main_menu,
                parse_mode=ParseMode.MARKDOWN
            )
            
            return

        elif query.data == 'profile':
            await send_updated_profile(user_id, query.message.chat.id, context)

        elif query.data == 'leaderboard':
            await query.answer()
            await typing_animation(context, query.message.chat_id, 0.3)
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
            await query.message.reply_text(
                "✏️ Please type your new anonymous name:\n\nTap ❌ Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )

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
                    # Use simple text without markdown for preview
                    preview_text = f"💬 Replying to:\n{content}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n✍️ Please type your comment or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML  # Changed to HTML to avoid markdown issues
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
                    "SELECT post_id, parent_comment_id, author_id, type FROM comments WHERE comment_id = %s",
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
                
                # Delete the comment and its reactions
                db_execute("DELETE FROM reactions WHERE comment_id = %s", (comment_id,))
                db_execute("DELETE FROM comments WHERE comment_id = %s", (comment_id,))
                
                await query.answer("✅ Comment deleted")
                await query.message.delete()
                
                # Update comment count
                await update_channel_post_comment_count(context, post_id)
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

        
        elif query.data.startswith('reply_msg_'):
            # Handle private message reply button
            # The format is: reply_msg_<user_id>
            try:
                # Extract everything after 'reply_msg_'
                target_id = query.data[len('reply_msg_'):]
                
                if not target_id or not target_id.isdigit():
                    logger.error(f"Invalid target_id in reply_msg callback: {query.data}")
                    await query.answer("❌ Invalid user ID", show_alert=True)
                    return
                    
                # Check if target user exists
                target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
                if not target_user:
                    await query.answer("❌ User not found", show_alert=True)
                    return
                
                # Set up the user to send a private message
                db_execute(
                    "UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s",
                    (target_id, user_id)
                )
                
                target_name = target_user['anonymous_name']
                
                await query.message.reply_text(
                    f"↩️ *Replying to {target_name}*\n\nPlease type your message:\n\nTap ❌ Cancel to return to menu.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=cancel_menu
                )
                
            except Exception as e:
                logger.error(f"Error in reply_msg handler: {e}, data: {query.data}")
                await query.answer("❌ Error processing reply", show_alert=True)        
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
                    # Use simple text without markdown for preview
                    preview_text = f"💬 Replying to:\n{content}"
                
                await query.message.reply_text(
                    f"{preview_text}\n\n↩️ Please type your *reply* or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML  # Changed to HTML
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
                    # Use simple text without markdown for preview
                    preview_text = f"💬 Replying to:\n{content}"
        
                await query.message.reply_text(
                    f"{preview_text}\n\n↩️ Please type your *reply* or send a voice message, GIF, or sticker:\n\nTap ❌ Cancel to return to menu.",
                    reply_markup=cancel_menu,
                    parse_mode=ParseMode.HTML  # Changed to HTML
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
            await query.answer()
            await typing_animation(context, query.message.chat_id, 0.3)
            try:
                page = int(query.data.split('_')[2])
                await show_previous_posts(update, context, page)
            except (IndexError, ValueError):
                await show_previous_posts(update, context, 1)

        elif query.data == 'my_posts':
            await show_previous_posts(update, context, 1)

        elif query.data.startswith("viewpost_"):
            await query.answer()
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
            await query.answer()
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
            
        elif query.data.startswith('message_'):
            target_id = query.data.split('_', 1)[1]
            db_execute(
                "UPDATE users SET waiting_for_private_message = TRUE, private_message_target = %s WHERE user_id = %s",
                (target_id, user_id)
            )
            
            target_user = db_fetch_one("SELECT anonymous_name FROM users WHERE user_id = %s", (target_id,))
            target_name = target_user['anonymous_name'] if target_user else "this user"
            
            await query.message.reply_text(
                f"✉️ *Composing message to {target_name}*\n\nPlease type your message:\n\nTap ❌ Cancel to return to menu.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_menu
            )
            
        
                    
        # Add this in the button_handler function where you handle other callbacks
        elif query.data == 'refresh_mini_app':
            await query.answer("Refreshing...")
            await mini_app_command(update, context)
        elif query.data.startswith("viewpost_"):
            post_id = int(query.data.split('_')[1])
            await view_post(update, context, post_id)    
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
    
    # Handle cancel command from text
    if text.lower() in ["❌ cancel", "cancel", "/cancel"]:
        # Check if user is in input state
        if user and (user['waiting_for_post'] or user['waiting_for_comment'] or 
                     user['awaiting_name'] or user['waiting_for_private_message']):
            # Reset all waiting states
            await reset_user_waiting_states(
                user_id, 
                update.message.chat.id, 
                context
            )
            
            # Clear any context data
            context_keys = ['editing_comment', 'editing_post', 'thread_from_post_id', 
                           'pending_post', 'broadcasting', 'broadcast_step', 'broadcast_type']
            for key in context_keys:
                if key in context.user_data:
                    del context.user_data[key]
            
            await update.message.reply_text(
                "❌ Input cancelled.",
                reply_markup=main_menu
            )
        else:
            # User not in input state, just show main menu
            await update.message.reply_text(
                "You're not currently in an input state.",
                reply_markup=main_menu
            )
        return
    
    # Rest of your handle_message code...

    # NEW: Handle comment editing
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
                reply_markup=main_menu
            )
            return
    # FIX: Handle pending post editing (NEW CODE ENDS HERE)

    # If user doesn't exist, create them
        # Handle broadcast messages from admin
        # Handle broadcast messages from admin
    if user and user['is_admin'] and context.user_data.get('broadcasting'):
        broadcast_step = context.user_data.get('broadcast_step')
        broadcast_type = context.user_data.get('broadcast_type', 'text')
        # Check for cancel button
        if text == "❌ Cancel" or text.lower() == "cancel":
            context.user_data.pop('broadcasting', None)
            context.user_data.pop('broadcast_step', None)
            context.user_data.pop('broadcast_type', None)
            context.user_data.pop('broadcast_data', None)
            await update.message.reply_text("📢 Broadcast cancelled.", reply_markup=main_menu)
            return
        
        if broadcast_step == 'waiting_for_content':
            # Store broadcast data
            broadcast_data = {
                'type': broadcast_type,
                'timestamp': datetime.now().isoformat()
            }
            
            if update.message.text and broadcast_type == 'text':
                broadcast_data['content'] = update.message.text
                context.user_data['broadcast_data'] = broadcast_data
                # Now call confirm_broadcast with the regular message update
                await confirm_broadcast(update, context)
                return
                
            elif update.message.photo and broadcast_type == 'photo':
                photo = update.message.photo[-1]
                broadcast_data['media_id'] = photo.file_id
                broadcast_data['caption'] = update.message.caption or ""
                context.user_data['broadcast_data'] = broadcast_data
                await confirm_broadcast(update, context)
                return
                
            elif update.message.voice and broadcast_type == 'voice':
                voice = update.message.voice
                broadcast_data['media_id'] = voice.file_id
                broadcast_data['caption'] = update.message.caption or ""
                context.user_data['broadcast_data'] = broadcast_data
                await confirm_broadcast(update, context)
                return
                
            elif broadcast_type == 'other':
                # Handle various media types
                if update.message.document:
                    broadcast_data['type'] = 'document'
                    broadcast_data['media_id'] = update.message.document.file_id
                    broadcast_data['caption'] = update.message.caption or ""
                elif update.message.video:
                    broadcast_data['type'] = 'video'
                    broadcast_data['media_id'] = update.message.video.file_id
                    broadcast_data['caption'] = update.message.caption or ""
                elif update.message.audio:
                    broadcast_data['type'] = 'audio'
                    broadcast_data['media_id'] = update.message.audio.file_id
                    broadcast_data['caption'] = update.message.caption or ""
                elif update.message.text:
                    broadcast_data['type'] = 'text'
                    broadcast_data['content'] = update.message.text
                else:
                    await update.message.reply_text(
                        "❌ Unsupported media type. Please send text, photo, voice, video, or document.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                
                context.user_data['broadcast_data'] = broadcast_data
                await confirm_broadcast(update, context)
                return
                
            else:
                # Mismatch between expected and actual content type
                await update.message.reply_text(
                    f"❌ Expected {broadcast_type} but received different content. Please try again or cancel.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
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
                await update.message.reply_text(
                    "❌ Unsupported media type. Please send text, photo, or voice message.",
                    reply_markup=main_menu
                )
                # Reset state
                db_execute(
                    "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
                    (user_id,)
                )
                return
            
            # FIX: Reset user state for BOTH text and media posts
            db_execute(
                "UPDATE users SET waiting_for_post = FALSE, selected_category = NULL WHERE user_id = %s",
                (user_id,)
            )
            
            # Send confirmation
            await send_post_confirmation(update, context, post_content, category, media_type, media_id, thread_from_post_id=thread_from_post_id)
            return
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            await update.message.reply_text(
                "❌ Error processing your media. Please try again.",
                reply_markup=main_menu
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

    elif text == "🌐 Web App":
        await mini_app_command(update, context)
        return

    # If none of the above, show main menu
    await update.message.reply_text(
        "How can I help you?",
        reply_markup=main_menu
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

async def mini_app_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the mini app link with authentication token"""
    user_id = str(update.effective_user.id)
    
    # Generate a secure JWT token
    token = jwt.encode(
        {
            'user_id': user_id,
            'exp': datetime.now(timezone.utc) + timedelta(days=30)
        },
        TOKEN,
        algorithm='HS256'
    )
    
    # Create the mini app URL with token
    render_url = os.getenv('RENDER_URL', 'https://your-render-url.onrender.com')
    mini_app_url = f"{render_url}/?token={token}"
    
    # Create WebApp button
    web_app_info = WebAppInfo(url=mini_app_url)
    
    # Create keyboard
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Web App", web_app=web_app_info)],
        [InlineKeyboardButton("📱 Open in Browser", url=mini_app_url)],
        [InlineKeyboardButton("🔄 Refresh Token", callback_data='refresh_mini_app')],
        [InlineKeyboardButton("🤖 Back to Bot", callback_data='menu')]
    ])
    
    await update.message.reply_text(
        "🌐 *Christian Vent Web App*\n\n"
        "Click the button below to open our web interface.\n\n"
        "📋 *Features:*\n"
        "• Share anonymous vents\n"
        "• View community posts\n"
        "• See the leaderboard\n"
        "• Manage your profile\n\n"
        "🔒 *Secure Access:*\n"
        "Your token is valid for 30 days.\n\n"
        "_Note: Always use this link from the bot to stay authenticated._",
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
    
    # Start polling
    logger.info("Starting bot polling...")
    app.run_polling()

# In bot.py, replace the simple /mini_app route with this:

@flask_app.route('/mini_app')
def mini_app_page():
    """Complete Mini App served from the bot service"""
    bot_username = BOT_USERNAME
    app_name = "Christian Vent"
    
    # Build the HTML for the mini app - FIXED VERSION
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{app_name} - Mini App</title>
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #272F32;
            color: #E0E0E0;
            min-height: 100vh;
            padding: 0;
        }}
        
        h1, h2, h3, h4, h5, h6 {{
            font-family: 'Oswald', sans-serif;
            font-weight: 600;
        }}
        
        .app-container {{
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            min-height: 100vh;
        }}
        
        /* Header */
                /* Header */
        .app-header {{
            text-align: center;
            padding: 20px 0;
            margin-bottom: 20px;
            border-bottom: 1px solid #3A4A50;
        }}
        
        .app-header .brand {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 10px;
        }}
        
        .app-header .logo {{
            width: 80px;
            height: 80px;
            object-fit: contain;
            border-radius: 50%;
            border: 2px solid #BF970B;
            background: #2E3A40;
            padding: 5px;
        }}
        
        .app-title {{
            color: #BF970B;
            font-size: 2.2rem;
            margin: 0;
            font-weight: 700;
            letter-spacing: 1px;
            font-family: 'Oswald', sans-serif;
            text-transform: uppercase;
        }}
        
        .app-subtitle {{
            opacity: 0.8;
            margin-top: 8px;
            font-size: 0.95rem;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }}
        
        /* Tabs */
        .tab-navigation {{
            display: flex;
            background: #2E3A40;
            border-radius: 10px;
            margin-bottom: 25px;
            overflow: hidden;
            border: 1px solid #3A4A50;
        }}
        
        .tab-btn {{
            flex: 1;
            padding: 15px;
            background: none;
            border: none;
            color: #E0E0E0;
            cursor: pointer;
            transition: all 0.3s;
            opacity: 0.7;
            font-size: 0.9rem;
            font-weight: 500;
        }}
        
        .tab-btn:hover {{
            opacity: 1;
            background: rgba(191, 151, 11, 0.1);
        }}
        
        .tab-btn.active {{
            opacity: 1;
            color: #BF970B;
            background: rgba(191, 151, 11, 0.1);
        }}
        
        /* Tab Content */
        .tab-content {{
            margin-top: 10px;
        }}
        
        .tab-pane {{
            display: none;
            animation: fadeIn 0.3s ease;
        }}
        
        .tab-pane.active {{
            display: block;
        }}
        
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        
        /* Vent Form */
        .vent-form-container {{
            background: #2E3A40;
            padding: 25px;
            border-radius: 12px;
            border: 1px solid #3A4A50;
            margin-bottom: 25px;
        }}
        
        .form-title {{
            color: #BF970B;
            margin-bottom: 10px;
            font-weight: 600;
            font-family: 'Oswald', sans-serif;
            font-size: 1.4rem;
            text-transform: uppercase;
        }}
        
        .form-description {{
            opacity: 0.8;
            margin-bottom: 20px;
            line-height: 1.6;
        }}
        
        .category-select {{
            width: 100%;
            padding: 12px 15px;
            background: #272F32;
            border: 1px solid #3A4A50;
            color: #E0E0E0;
            border-radius: 8px;
            font-size: 1rem;
            margin-bottom: 20px;
            cursor: pointer;
        }}
        
        .category-select:focus {{
            outline: none;
            border-color: #BF970B;
        }}
        
        .vent-textarea {{
            width: 100%;
            min-height: 150px;
            padding: 15px;
            background: #272F32;
            border: 1px solid #3A4A50;
            color: #E0E0E0;
            border-radius: 8px;
            font-size: 1rem;
            font-family: inherit;
            line-height: 1.6;
            resize: vertical;
            margin-bottom: 10px;
        }}
        
        .vent-textarea:focus {{
            outline: none;
            border-color: #BF970B;
            box-shadow: 0 0 0 2px rgba(191, 151, 11, 0.2);
        }}
        
        .textarea-footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            font-size: 0.9rem;
            opacity: 0.8;
        }}
        
        .privacy-note {{
            color: #BF970B;
            font-size: 0.85rem;
        }}
        
        .submit-btn {{
            width: 100%;
            padding: 15px;
            background: #BF970B;
            color: #272F32;
            border: none;
            border-radius: 8px;
            font-size: 1.1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        
        .submit-btn:hover {{
            background: #d4a90f;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(191, 151, 11, 0.3);
        }}
        
        .submit-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }}
        
        .form-note {{
            text-align: center;
            margin-top: 15px;
            font-size: 0.9rem;
            opacity: 0.7;
        }}
        
        /* Posts Section */
        .posts-container {{
            margin-top: 10px;
        }}
        
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        
        .section-title {{
            color: #BF970B;
            font-weight: 600;
            margin: 0;
            font-family: 'Oswald', sans-serif;
            font-size: 1.3rem;
            text-transform: uppercase;
        }}
        
        .refresh-btn {{
            background: #3A4A50;
            border: 1px solid #3A4A50;
            color: #E0E0E0;
            padding: 8px 16px;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.3s;
        }}
        
        .refresh-btn:hover {{
            background: #BF970B;
            color: #272F32;
        }}
        
        /* Post Cards */
        .post-card {{
            background: #2E3A40;
            border: 1px solid #3A4A50;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 15px;
            transition: all 0.3s;
            cursor: pointer;
        }}
        
        .post-card:hover {{
            border-color: #BF970B;
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.2);
        }}
        
        .post-header {{
            display: flex;
            align-items: center;
            margin-bottom: 15px;
        }}
        
        .author-avatar {{
            width: 40px;
            height: 40px;
            background: rgba(191, 151, 11, 0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #BF970B;
            font-weight: bold;
            margin-right: 12px;
        }}
        
        .author-info h4 {{
            font-size: 1rem;
            font-weight: 500;
            margin: 0 0 5px 0;
            font-family: 'Oswald', sans-serif;
        }}
        
        .post-meta {{
            font-size: 0.85rem;
            opacity: 0.8;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        
        .post-category {{
            display: inline-block;
            background: rgba(191, 151, 11, 0.1);
            color: #BF970B;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.8rem;
        }}
        
        .post-content {{
            margin: 15px 0;
            line-height: 1.7;
            font-size: 1rem;
        }}
        
        .post-footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid #3A4A50;
            font-size: 0.9rem;
            opacity: 0.8;
        }}
        
        /* Leaderboard */
        .leaderboard-container {{
            background: #2E3A40;
            border-radius: 12px;
            border: 1px solid #3A4A50;
            overflow: hidden;
        }}
        
        .leaderboard-item {{
            display: flex;
            align-items: center;
            padding: 15px 20px;
            border-bottom: 1px solid #3A4A50;
            transition: background 0.3s;
        }}
        
        .leaderboard-item:last-child {{
            border-bottom: none;
        }}
        
        .leaderboard-item:hover {{
            background: rgba(191, 151, 11, 0.05);
        }}
        
        .leaderboard-rank {{
            width: 40px;
            font-size: 1.2rem;
            font-weight: 600;
            color: #BF970B;
        }}
        
        .rank-1 {{ color: gold; }}
        .rank-2 {{ color: silver; }}
        .rank-3 {{ color: #cd7f32; }}
        
        .leaderboard-user {{
            flex: 1;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        
        .user-avatar-small {{
            width: 40px;
            height: 40px;
            background: rgba(191, 151, 11, 0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            color: #BF970B;
        }}
        
        .user-info-small h4 {{
            font-size: 1rem;
            font-weight: 500;
            margin: 0 0 4px 0;
            font-family: 'Oswald', sans-serif;
        }}
        
        .user-info-small p {{
            font-size: 0.85rem;
            opacity: 0.7;
            margin: 0;
        }}
        
        .leaderboard-points {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #BF970B;
        }}
        
        /* Profile */
        .profile-container {{
            background: #2E3A40;
            border-radius: 12px;
            border: 1px solid #3A4A50;
            padding: 25px;
        }}
        
        .profile-header {{
            text-align: center;
            margin-bottom: 25px;
        }}
        
        .profile-avatar {{
            width: 100px;
            height: 100px;
            background: rgba(191, 151, 11, 0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 2.5rem;
            color: #BF970B;
            margin: 0 auto 15px;
        }}
        
        .profile-header h2 {{
            font-size: 1.8rem;
            margin: 0 0 10px 0;
            font-family: 'Oswald', sans-serif;
            font-weight: 600;
        }}
        
        .profile-rating {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(191, 151, 11, 0.1);
            padding: 8px 16px;
            border-radius: 20px;
            margin-top: 10px;
        }}
        
        /* Footer */
        .app-footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #3A4A50;
            text-align: center;
            font-size: 0.9rem;
            opacity: 0.7;
        }}
        
        .telegram-link {{
            color: #BF970B;
            text-decoration: none;
        }}
        
        .telegram-link:hover {{
            text-decoration: underline;
        }}
        
        /* Messages */
        .message {{
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
            text-align: center;
            animation: slideIn 0.3s ease;
        }}
        
        .error-message {{
            background: rgba(255, 0, 0, 0.1);
            border: 1px solid rgba(255, 0, 0, 0.3);
            color: #ff6b6b;
        }}
        
        .success-message {{
            background: rgba(0, 255, 0, 0.1);
            border: 1px solid rgba(0, 255, 0, 0.3);
            color: #51cf66;
        }}
        
        @keyframes slideIn {{
            from {{ opacity: 0; transform: translateY(-10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        
        /* Loading */
        .loading {{
            text-align: center;
            padding: 40px;
            color: #BF970B;
        }}
        
        /* Empty States */
        .empty-state {{
            text-align: center;
            padding: 40px;
            opacity: 0.7;
        }}
        
        /* Responsive */
        @media (max-width: 768px) {{
            .app-container {{
                padding: 15px;
            }}
            
            .app-title {{
                font-size: 1.5rem;
            }}
            
            .tab-btn {{
                padding: 12px;
                font-size: 0.85rem;
            }}
            
            .vent-form-container {{
                padding: 20px;
            }}
            
            .post-card {{
                padding: 15px;
            }}
        }}
    </style>
</head>
<body>
    <div class="app-container" id="appContainer">
        <!-- Header -->
                <!-- Header -->
                <header class="app-header">
                    <div class="brand">
                        <img src="/static/images/vent%20logo.jpg" class="logo" alt="Christian Vent Logo">
                        <h1 class="app-title"> {app_name}</h1>
                    </div>
                    <p class="app-subtitle">A safe space for Christian anonymous venting</p>
            <div id="userInfo" class="user-info" style="margin-top: 15px; display: none;">
                <!-- User info will be loaded here -->
            </div>
        </header>
        
        <!-- Navigation Tabs -->
        <nav class="tab-navigation">
            <button class="tab-btn active" data-tab="vent">✍️ Vent</button>
            <button class="tab-btn" data-tab="posts">📖 Feed</button>
            <button class="tab-btn" data-tab="leaderboard">🏆 Leaderboard</button>
            <button class="tab-btn" data-tab="profile">👤 Profile</button>
            <button class="tab-btn" data-tab="admin" id="adminTab" style="display: none;">🛠 Admin</button>
        </nav>
        
        <!-- Tab Content -->
        <div class="tab-content">
            <!-- Vent Tab -->
            <div id="vent-tab" class="tab-pane active">
                <div class="vent-form-container">
                    <h2 class="form-title">Share Your Burden</h2>
                    <p class="form-description">You are anonymous here. Share what's on your heart without fear.</p>
                    
                    <select class="category-select" id="categorySelect">
                        <option value="PrayForMe">🙏 Pray For Me</option>
                        <option value="Bible">📖 Bible Study</option>
                        <option value="WorkLife">💼 Work and Life</option>
                        <option value="SpiritualLife">🕊️ Spiritual Life</option>
                        <option value="ChristianChallenges">⚔️ Christian Challenges</option>
                        <option value="Relationship">❤️ Relationship</option>
                        <option value="Marriage">💍 Marriage</option>
                        <option value="Youth">👥 Youth</option>
                        <option value="Finance">💰 Finance</option>
                        <option value="Other" selected>📝 Other</option>
                    </select>
                    
                    <textarea 
                        class="vent-textarea" 
                        id="ventText" 
                        placeholder="What's on your heart? Share your thoughts, prayers, or struggles..."
                        maxlength="5000"
                    ></textarea>
                    
                    <div class="textarea-footer">
                        <span id="charCount">0/5000 characters</span>
                        <span class="privacy-note">Your identity is protected</span>
                    </div>
                    
                    <button class="submit-btn" id="submitVent">
                        Post Anonymously
                    </button>
                    
                    <p class="form-note">Posts are reviewed before appearing in the feed</p>
                </div>
            </div>
            
            <!-- Posts Tab -->
            <div id="posts-tab" class="tab-pane">
                <div class="section-header">
                    <h2 class="section-title">Recent Vents</h2>
                    <button class="refresh-btn" id="refreshPosts">Refresh</button>
                </div>
                <div class="posts-container" id="postsContainer">
                    <div class="loading">Loading community posts...</div>
                </div>
            </div>
            
            <!-- Leaderboard Tab -->
            <div id="leaderboard-tab" class="tab-pane">
                <div class="section-header">
                    <h2 class="section-title">Top Contributors</h2>
                    <button class="refresh-btn" id="refreshLeaderboard">Refresh</button>
                </div>
                <div class="leaderboard-container" id="leaderboardContainer">
                    <div class="loading">Loading leaderboard...</div>
                </div>
            </div>
            
            <!-- Profile Tab -->
            <div id="profile-tab" class="tab-pane">
                <div class="profile-container" id="profileContainer">
                    <div class="loading">Loading your profile...</div>
                </div>
            </div>
        </div>
        
        <!-- Footer -->
        <footer class="app-footer">
            <p>
                Connect with our community on Telegram: 
                <a href="https://t.me/{bot_username}" class="telegram-link" target="_blank">
                    @{bot_username}
                </a>
            </p>
            <p style="margin-top: 10px; font-size: 0.85rem;">
                This is the Christian Vent Mini App. Your identity is protected.
            </p>
        </footer>
    </div>
    
    <script>
        // Christian Vent Mini App - Main JavaScript
        class ChristianVentApp {{
            constructor() {{
                this.user = null;
                this.token = null;
                this.userId = null;
                this.botUsername = "{bot_username}";
                this.apiBaseUrl = window.location.origin;
                this.isAdmin = false;
                this.init();
            }}
            
            async init() {{
                this.setupEventListeners();
                
                // Get token from URL
                const urlParams = new URLSearchParams(window.location.search);
                this.token = urlParams.get('token');
                
                if (!this.token) {{
                    this.showMessage('❌ Authentication required. Please use the /webapp command in the Telegram bot.', 'error');
                    setTimeout(() => {{
                        window.location.href = '/login';
                    }}, 3000);
                    return;
                }}
                
                // Verify token
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/verify-token/${{this.token}}`);
                    const data = await response.json();
                    
                    if (!data.success) {{
                        this.showMessage('❌ Session expired. Please get a new link from the Telegram bot.', 'error');
                        setTimeout(() => {{
                            window.location.href = '/login';
                        }}, 3000);
                        return;
                    }}
                    
                    this.userId = data.user_id;
                    await this.loadUserData();
                    
                }} catch (error) {{
                    console.error('Error verifying token:', error);
                    this.showMessage('❌ Authentication error. Please try again.', 'error');
                    setTimeout(() => {{
                        window.location.href = '/login';
                    }}, 3000);
                    return;
                }}
                
                // Load initial data
                await this.loadPosts();
                await this.loadLeaderboard();
            }}
            
            async loadUserData() {{
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/mini-app/profile/${{this.userId}}`);
                    const data = await response.json();
                    if (data.success) {{
                        this.user = data.data;
                    }}
                }} catch (error) {{
                    console.error('Error loading user data:', error);
                }}
            }}
            
            setupEventListeners() {{
                // Tab switching
                document.querySelectorAll('.tab-btn').forEach(btn => {{
                    btn.addEventListener('click', (e) => {{
                        const tab = e.target.dataset.tab;
                        this.switchTab(tab);
                    }});
                }});
                
                // Character counter
                const ventText = document.getElementById('ventText');
                const charCount = document.getElementById('charCount');
                if (ventText && charCount) {{
                    ventText.addEventListener('input', () => {{
                        charCount.textContent = `${{ventText.value.length}}/5000 characters`;
                    }});
                }}
                
                // Submit vent
                const submitBtn = document.getElementById('submitVent');
                if (submitBtn) {{
                    submitBtn.addEventListener('click', () => this.submitVent());
                }}
                
                // Refresh buttons
                document.getElementById('refreshPosts')?.addEventListener('click', () => this.loadPosts());
                document.getElementById('refreshLeaderboard')?.addEventListener('click', () => this.loadLeaderboard());
            }}
            
            switchTab(tabName) {{
                // Update active tab button
                document.querySelectorAll('.tab-btn').forEach(btn => {{
                    btn.classList.toggle('active', btn.dataset.tab === tabName);
                }});
                
                // Update active tab pane
                document.querySelectorAll('.tab-pane').forEach(pane => {{
                    pane.classList.toggle('active', pane.id === `${{tabName}}-tab`);
                }});
                
                // Load data for the tab if needed
                if (tabName === 'profile' && this.userId) {{
                    this.loadProfile(this.userId);
                }}
            }}
            
            async loadPosts() {{
                const container = document.getElementById('postsContainer');
                if (!container) return;
                
                container.innerHTML = '<div class="loading">Loading community posts...</div>';
                
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/mini-app/get-posts?page=1&per_page=10`);
                    const data = await response.json();
                    
                    if (data.success) {{
                        this.renderPosts(data.data);
                    }} else {{
                        container.innerHTML = `
                            <div class="error-message">
                                Failed to load posts: ${{data.error || 'Unknown error'}}
                            </div>
                        `;
                    }}
                }} catch (error) {{
                    console.error('Error loading posts:', error);
                    container.innerHTML = `
                        <div class="error-message">
                            Network error. Please check your connection.
                        </div>
                    `;
                }}
            }}
            
            renderPosts(posts) {{
                const container = document.getElementById('postsContainer');
                if (!container) return;
                
                if (!posts || posts.length === 0) {{
                    container.innerHTML = `
                        <div class="empty-state">
                            <h3 style="color: #BF970B;">No posts yet</h3>
                            <p style="opacity: 0.8; margin-bottom: 20px;">Be the first to share what's on your heart</p>
                            <button onclick="app.switchTab('vent')" 
                                    style="background: #BF970B; color: #272F32; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer;">
                                Share Your First Vent
                            </button>
                        </div>
                    `;
                    return;
                }}
                
                container.innerHTML = posts.map(post => `
                    <div class="post-card">
                        <div class="post-header">
                            <div class="author-avatar">
                                ${{post.author.sex || '👤'}}
                            </div>
                            <div class="author-info">
                                <h4>${{post.author.name}}</h4>
                                <div class="post-meta">
                                    <span class="post-category">${{post.category}}</span>
                                    <span>•</span>
                                    <span>${{post.time_ago}}</span>
                                </div>
                            </div>
                        </div>
                        
                        <div class="post-content">
                            ${{this.escapeHtml(post.content)}}
                        </div>
                        
                        <div class="post-footer">
                            <div class="comment-count">
                                💬 ${{post.comments}} comment${{post.comments !== 1 ? 's' : ''}}
                            </div>
                            <button onclick="window.open('https://t.me/${{this.botUsername}}?start=comments_${{post.id}}', '_blank')" 
                                    style="background: transparent; color: #BF970B; border: 1px solid #BF970B; padding: 5px 15px; border-radius: 5px; font-size: 0.9rem; cursor: pointer;">
                                View in Bot
                            </button>
                        </div>
                    </div>
                `).join('');
            }}
            
            async loadLeaderboard() {{
                const container = document.getElementById('leaderboardContainer');
                if (!container) return;
                
                container.innerHTML = '<div class="loading">Loading leaderboard...</div>';
                
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/mini-app/leaderboard`);
                    const data = await response.json();
                    
                    if (data.success) {{
                        this.renderLeaderboard(data.data);
                    }} else {{
                        container.innerHTML = '<div class="error-message">Failed to load leaderboard</div>';
                    }}
                }} catch (error) {{
                    console.error('Error loading leaderboard:', error);
                    container.innerHTML = '<div class="error-message">Network error</div>';
                }}
            }}
            
            renderLeaderboard(users) {{
                const container = document.getElementById('leaderboardContainer');
                if (!container) return;
                
                container.innerHTML = users.map((user, index) => `
                    <div class="leaderboard-item">
                        <div class="leaderboard-rank rank-${{index + 1}}">${{index + 1}}</div>
                        <div class="leaderboard-user">
                            <div class="user-avatar-small">${{user.sex || '👤'}}</div>
                            <div class="user-info-small">
                                <h4>${{user.name}}</h4>
                                <p>${{user.aura}} Contributor</p>
                            </div>
                        </div>
                        <div class="leaderboard-points">${{user.points}} pts</div>
                    </div>
                `).join('');
            }}
            
            async loadProfile(userId) {{
                const container = document.getElementById('profileContainer');
                if (!container) return;
                
                container.innerHTML = '<div class="loading">Loading profile...</div>';
                
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/mini-app/profile/${{userId}}`);
                    const data = await response.json();
                    
                    if (data.success) {{
                        const profile = data.data;
                        container.innerHTML = `
                            <div class="profile-header">
                                <div class="profile-avatar">
                                    ${{profile.sex || '👤'}}
                                </div>
                                <h2>${{profile.name}}</h2>
                                <div class="profile-rating">
                                    ${{profile.aura}} ${{profile.rating}} points
                                </div>
                            </div>
                            
                            <div style="background: rgba(191, 151, 11, 0.1); padding: 20px; border-radius: 8px; margin: 20px 0;">
                                <h4 style="color: #BF970B; margin-bottom: 10px;">Your Statistics</h4>
                                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; text-align: center;">
                                    <div>
                                        <div style="font-size: 2rem; font-weight: 600; color: #BF970B;">${{profile.stats.posts}}</div>
                                        <div style="font-size: 0.9rem; opacity: 0.8;">Vents</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 2rem; font-weight: 600; color: #BF970B;">${{profile.stats.comments}}</div>
                                        <div style="font-size: 0.9rem; opacity: 0.8;">Comments</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 2rem; font-weight: 600; color: #BF970B;">${{profile.stats.followers}}</div>
                                        <div style="font-size: 0.9rem; opacity: 0.8;">Followers</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    }} else {{
                        container.innerHTML = '<div class="error-message">Failed to load profile</div>';
                    }}
                }} catch (error) {{
                    console.error('Error loading profile:', error);
                    container.innerHTML = '<div class="error-message">Network error</div>';
                }}
            }}
            
            async submitVent() {{
                const ventText = document.getElementById('ventText');
                const categorySelect = document.getElementById('categorySelect');
                const submitBtn = document.getElementById('submitVent');
                
                if (!ventText || !categorySelect || !submitBtn) return;
                
                const content = ventText.value.trim();
                const category = categorySelect.value;
                
                if (!content) {{
                    this.showMessage('Please write something before posting', 'error');
                    return;
                }}
                
                if (content.length > 5000) {{
                    this.showMessage('Text is too long (max 5000 characters)', 'error');
                    return;
                }}
                
                // Disable button and show loading
                const originalText = submitBtn.textContent;
                submitBtn.textContent = 'Posting...';
                submitBtn.disabled = true;
                
                try {{
                    const response = await fetch(`${{this.apiBaseUrl}}/api/mini-app/submit-vent`, {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json'
                        }},
                        body: JSON.stringify({{
                            user_id: this.userId,
                            content: content,
                            category: category
                        }})
                    }});
                    
                    const data = await response.json();
                    
                    if (data.success) {{
                        this.showMessage(data.message, 'success');
                        
                        // Clear the form
                        ventText.value = '';
                        document.getElementById('charCount').textContent = '0/5000 characters';
                        
                        // Switch to posts tab after 2 seconds
                        setTimeout(() => {{
                            this.switchTab('posts');
                            this.loadPosts();
                        }}, 2000);
                        
                    }} else {{
                        this.showMessage(data.error || 'Failed to submit vent', 'error');
                    }}
                }} catch (error) {{
                    console.error('Error submitting vent:', error);
                    this.showMessage('Network error. Please try again.', 'error');
                }} finally {{
                    submitBtn.textContent = originalText;
                    submitBtn.disabled = false;
                }}
            }}
            
            showMessage(message, type = 'success') {{
                // Remove any existing messages
                const existingMessages = document.querySelectorAll('.message');
                existingMessages.forEach(msg => msg.remove());
                
                // Create new message element
                const messageEl = document.createElement('div');
                messageEl.className = `message ${{type === 'error' ? 'error-message' : 'success-message'}}`;
                messageEl.textContent = message;
                
                // Add to top of app container
                const appContainer = document.getElementById('appContainer');
                if (appContainer) {{
                    appContainer.insertBefore(messageEl, appContainer.firstChild);
                    
                    // Remove after 5 seconds
                    setTimeout(() => {{
                        if (messageEl.parentNode) {{
                            messageEl.remove();
                        }}
                    }}, 5000);
                }}
            }}
            
            escapeHtml(text) {{
                const div = document.createElement('div');
                div.textContent = text;
                return div.innerHTML;
            }}
        }}
        
        // Initialize the app when DOM is loaded
        document.addEventListener('DOMContentLoaded', () => {{
            window.app = new ChristianVentApp();
        }});
    </script>
</body>
</html>'''
    
    return html

# ==================== MINI APP API ENDPOINTS ====================

# ==================== MINI APP API ENDPOINTS ====================

@flask_app.route('/api/mini-app/submit-vent', methods=['POST'])
def mini_app_submit_vent():
    """API endpoint for submitting vents from mini app - SIMPLIFIED"""
    try:
        # Get data from request
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        user_id = data.get('user_id')
        content = data.get('content', '').strip()
        category = data.get('category', 'Other')
        
        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
        
        if not content:
            return jsonify({'success': False, 'error': 'Content cannot be empty'}), 400
        
        # Check if user exists
        user = db_fetch_one("SELECT * FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Insert the post (simple and clean)
        post_row = db_execute(
            "INSERT INTO posts (content, author_id, category, media_type, approved) VALUES (%s, %s, %s, 'text', FALSE) RETURNING post_id",
            (content, user_id, category),
            fetchone=True
        )
        
        if post_row:
            post_id = post_row['post_id']
            
            # Log it (optional)
            logger.info(f"📝 Mini App Post submitted: ID {post_id} by {user_id}")
            
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

# Helper function for sync context (since Flask routes can't be async)
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
        
        # Create a simple text notification (in real app, you'd send via bot)
        logger.info(f"🆕 Mini App Post awaiting approval from {author_name}: {post_preview}")
        
        # You could also send to a webhook or store in a queue for bot to process
        # For now, just log it
        
    except Exception as e:
        logger.error(f"Error in sync admin notification: {e}")

@flask_app.route('/api/mini-app/get-posts', methods=['GET'])
def mini_app_get_posts():
    """API endpoint for getting posts from mini app - SHOW SEX ONLY"""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        offset = (page - 1) * per_page
        
        # Get approved posts WITH sex but WITHOUT name
        posts = db_fetch_all('''
            SELECT 
                p.post_id,
                p.content,
                p.category,
                p.timestamp,
                p.comment_count,
                p.media_type,
                u.sex as author_sex
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            WHERE p.approved = TRUE
            ORDER BY p.timestamp DESC
            LIMIT %s OFFSET %s
        ''', (per_page, offset))
        
        # Format posts - ANONYMOUS NAME BUT SHOW SEX
        formatted_posts = []
        for post in posts:
            # Format timestamp
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
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'full_content': post['content'],
                'category': post['category'],
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'author': {
                    'name': 'Anonymous',  # ANONYMOUS NAME
                    'sex': post['author_sex'] or '👤'  # SHOW REAL SEX
                },
                'has_media': post['media_type'] != 'text'
            })
        
        # Get total count
        total_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = TRUE")
        
        return jsonify({
            'success': True,
            'data': formatted_posts,
            'page': page,
            'total_posts': total_posts['count'] if total_posts else 0,
            'has_more': len(posts) == per_page
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app get posts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/leaderboard', methods=['GET'])
def mini_app_leaderboard():
    """API endpoint for leaderboard data"""
    try:
        # Get top 10 users
        top_users = db_fetch_all('''
            SELECT 
                u.user_id,
                u.anonymous_name,
                u.sex,
                (SELECT COUNT(*) FROM posts WHERE author_id = u.user_id AND approved = TRUE) + 
                (SELECT COUNT(*) FROM comments WHERE author_id = u.user_id) AS total
            FROM users u
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
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['user_id'],
                'name': user['anonymous_name'],
                'sex': user['sex'],
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
                p.category,
                p.timestamp,
                p.media_type,
                u.anonymous_name as author_name,
                u.sex as author_sex
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            WHERE p.approved = FALSE
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

@flask_app.route('/api/mini-app/admin/reject-post', methods=['POST'])
def mini_app_admin_reject_post():
    """API endpoint for admin to reject posts"""
    try:
        data = request.get_json()
        post_id = data.get('post_id')
        
        if not post_id:
            return jsonify({'success': False, 'error': 'Post ID required'}), 400
        
        # Delete the post
        success = db_execute(
            "DELETE FROM posts WHERE post_id = %s",
            (post_id,)
        )
        
        if success:
            return jsonify({'success': True, 'message': 'Post rejected and deleted'})
        else:
            return jsonify({'success': False, 'error': 'Failed to reject post'}), 500
            
    except Exception as e:
        logger.error(f"Error in mini-app reject post: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
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
