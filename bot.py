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
import threading
from flask import Flask, jsonify 
from contextlib import closing
from datetime import datetime

# Initialize database
DB_FILE = 'bot.db'

# Initialize database tables
def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        
        # Create tables
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
            nested_idx INTEGER
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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER,
            parent_comment_id INTEGER DEFAULT 0,  -- 0 = top-level comment
            author_id TEXT,
            content TEXT,
            type TEXT,  -- 'text', 'photo', 'voice'
            file_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES posts (post_id)
        )''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS reactions (
            reaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER,
            user_id TEXT,
            type TEXT,  -- 'like' or 'dislike'
            FOREIGN KEY (comment_id) REFERENCES comments (comment_id),
            UNIQUE(comment_id, user_id)
        )''')
        
        # Update existing records to use new defaults
        c.execute("UPDATE users SET sex = '👤' WHERE sex = '❓'")
        conn.commit()
    logging.info("Database initialized successfully")

# Initialize database on startup
init_db()

# Database helper functions
def db_execute(query, params=(), fetch=False):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        if fetch:
            return c.fetchall()
        return c.lastrowid if c.lastrowid else True

def db_fetch_one(query, params=()):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchone()

def db_fetch_all(query, params=()):
    with sqlite3.connect(DB_FILE) as conn:
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
        [KeyboardButton("👤 View Profile")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("❓ Help"), KeyboardButton("ℹ️ About Us")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
) 

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__) 

def create_anonymous_name():
    return "Anonymous"

def calculate_user_rating(user_id):
    # Count posts
    post_count = db_fetch_one(
        "SELECT COUNT(*) FROM posts WHERE author_id = ?",
        (user_id,)
    )[0] if db_fetch_one(
        "SELECT COUNT(*) FROM posts WHERE author_id = ?",
        (user_id,)
    ) else 0
    
    # Count comments
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
    result = db_fetch_one("SELECT COUNT(*) FROM comments WHERE post_id = ?", (post_id,))
    return result[0] if result else 0

def get_display_name(user_data):
    """Get display name with fallback to 'Anonymous'"""
    return user_data['anonymous_name'] if user_data and user_data['anonymous_name'] else "Anonymous"

def get_display_sex(user_data):
    """Get display sex with fallback to '👤'"""
    return user_data['sex'] if user_data and user_data['sex'] else '👤'

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    # Check if user exists
    user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not user:
        anon = create_anonymous_name()
        db_execute(
            "INSERT INTO users (user_id, anonymous_name) VALUES (?, ?)",
            (user_id, anon)
        )
    
    args = context.args  # deep link args after /start 

    if args:
        arg = args[0] 

        # Show comment menu for a post
        if arg.startswith("comments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                await show_comments_menu(update, context, post_id, page=1)
            return 

        # Show the comments list for a post
        elif arg.startswith("viewcomments_"):
            parts = arg.split("_")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                post_id = int(parts[1])
                page = int(parts[2])
                await show_comments_page(update, context, post_id, page)
            return 

        # Start writing comment on a post
        elif arg.startswith("writecomment_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                db_execute(
                    "UPDATE users SET waiting_for_comment = 1, comment_post_id = ? WHERE user_id = ?",
                    (post_id, user_id)
                )
                
                # Get post content for preview
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

        # Show profile (from deep link)
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
                    else:
                        btn.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{user_data["user_id"]}')])
                display_name = get_display_name(user_data)
                display_sex = get_display_sex(user_data)
                await update.message.reply_text(
                    f"👤 *{display_name}* 🎖 Verified\n"
                    f"📌 Sex: {display_sex}\n"
                    f"👥 Followers: {len(followers)}\n"
                    f"🎖 Batch: User\n"
                    f"⭐️ Contributions: {rating} {stars}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
                    f"_Use /menu to return_",
                    reply_markup=InlineKeyboardMarkup(btn) if btn else None,
                    parse_mode=ParseMode.MARKDOWN)
                return 

    # Default welcome menu if no deep link argument
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
        "🌟✝️ *እንኳን ወደ Christian Chat Bot በሰላም መጡ* ✝️🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "ማንነታችሁ ሳይገለጽ ሃሳባችሁን ማጋራት ትችላላችሁ.\n\n የሚከተሉትን ምረጁ :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN)
    
    # Send main menu buttons
    await update.message.reply_text(
        "You can use the buttons below to navigate:",
        reply_markup=main_menu
    ) 

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

async def show_comments_page(update, context, post_id, page=1):
    # Get chat ID from effective_chat
    if update.effective_chat is None:
        logger.error("Cannot determine chat from update: %s", update)
        return
    chat_id = update.effective_chat.id
        
    post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
    if not post:
        await context.bot.send_message(chat_id, "❌ Post not found.", reply_markup=main_menu)
        return 

    # Pagination settings
    per_page = 5
    offset = (page - 1) * per_page
    
    # Get top-level comments for this page
    comments = db_fetch_all(
        "SELECT * FROM comments WHERE post_id = ? AND parent_comment_id = 0 LIMIT ? OFFSET ?",
        (post_id, per_page, offset)
    )
    
    # Get total comment count for pagination
    total_comments = count_all_comments(post_id)
    total_pages = (total_comments + per_page - 1) // per_page
    
    post_text = post['content']
    # Escape the original post content properly
    header = f"{escape_markdown(post_text, version=2)}\n\n" 

    if not comments and page == 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text=header + "_No comments yet._", 
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_menu
        )
        return 

    # Send header message
    header_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=header, 
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu
    )
    header_message_id = header_msg.message_id
    
    for comment in comments:
        commenter_id = comment['author_id']
        commenter = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (commenter_id,))
        display_name = get_display_name(commenter)
        display_sex = get_display_sex(commenter)
        rating = calculate_user_rating(commenter_id)
        stars = format_stars(rating)
        profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{display_name}" 

        # Get like/dislike counts
        likes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
            (comment['comment_id'],)
        )
        likes = likes_row['cnt'] if likes_row and 'cnt' in likes_row else 0
        
        dislikes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
            (comment['comment_id'],)
        )
        dislikes = dislikes_row['cnt'] if dislikes_row and 'cnt' in dislikes_row else 0
        
        # Create clean comment text
        comment_text = escape_markdown(comment['content'], version=2)
        
        # Create author text as clickable link
        author_text = f"[{escape_markdown(display_name, version=2)}]({profile_url}) {display_sex} {stars}" 

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{comment['comment_id']}"),
                InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{comment['comment_id']}"),
                InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment['comment_id']}")
            ]
        ]) 

        # Send comment as a reply to the header message for proper threading
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{comment_text}\n\n{author_text}",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=header_message_id
        )
        
        # Display replies to this comment as threaded replies
        replies = db_fetch_all(
            "SELECT * FROM comments WHERE parent_comment_id = ?",
            (comment['comment_id'],)
        )
        for reply in replies:
            reply_user_id = reply['author_id']
            reply_user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (reply_user_id,))
            reply_display_name = get_display_name(reply_user)
            reply_display_sex = get_display_sex(reply_user)
            rating_reply = calculate_user_rating(reply_user_id)
            stars_reply = format_stars(rating_reply)
            profile_url_reply = f"https://t.me/{BOT_USERNAME}?start=profile_{reply_display_name}"
            safe_reply = escape_markdown(reply['content'], version=2)
            
            # Get like/dislike counts for this reply
            reply_likes_row = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
                (reply['comment_id'],)
            )
            reply_likes = reply_likes_row['cnt'] if reply_likes_row and 'cnt' in reply_likes_row else 0
            
            reply_dislikes_row = db_fetch_one(
                "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
                (reply['comment_id'],)
            )
            reply_dislikes = reply_dislikes_row['cnt'] if reply_dislikes_row and 'cnt' in reply_dislikes_row else 0
            
            # Create keyboard for the reply
            reply_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {reply_likes}", callback_data=f"likereply_{reply['comment_id']}"),
                    InlineKeyboardButton(f"👎 {reply_dislikes}", callback_data=f"dislikereply_{reply['comment_id']}"),
                    InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{comment['comment_id']}_{reply['comment_id']}")
                ]
            ])
            
            # Send as threaded reply
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{safe_reply}\n\n[{reply_display_name}]({profile_url_reply}) {reply_display_sex} {stars_reply}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=msg.message_id,
                reply_markup=reply_kb
            )
    
    # Add pagination buttons
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
    
    # Also send the main menu buttons
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
    
    # Get follower count
    followers = db_fetch_all(
        "SELECT * FROM followers WHERE followed_id = ?",
        (user_id,)
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤 *{display_name}* 🎖 Verified\n"
            f"📌 Sex: {display_sex}\n"
            f"⭐️ Rating: {rating} {stars}\n"
            f"🎖 Batch: User\n"
            f"👥 Followers: {len(followers)}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"_Use /menu to return_"
        ),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN) 

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Get top 10 users by contribution score
    top_users = db_fetch_all(
        "SELECT user_id, anonymous_name, sex FROM users ORDER BY ("
        "(SELECT COUNT(*) FROM posts WHERE author_id = user_id) + "
        "(SELECT COUNT(*) FROM comments WHERE author_id = user_id)"
        ") DESC LIMIT 10"
    )
    
    leaderboard_text = "🏆 *Top Contributors* 🏆\n\n"
    for idx, user in enumerate(top_users):
        display_name = get_display_name(user)
        display_sex = get_display_sex(user)
        rating = calculate_user_rating(user['user_id'])
        leaderboard_text += f"{idx+1}. {display_name} {display_sex} - ⭐️ {rating}\n"
    
    leaderboard_text += "\n_Keep contributing to climb the leaderboard!_"
    
    keyboard = [[InlineKeyboardButton("📱 Main Menu", callback_data='menu')]]
    if isinstance(update, Update):
        await update.message.reply_text(leaderboard_text, 
                                      reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode=ParseMode.MARKDOWN)
    else:  # CallbackQuery
        query = update
        await query.message.reply_text(leaderboard_text, 
                                     reply_markup=InlineKeyboardMarkup(keyboard),
                                     parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id) 

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
        await show_leaderboard(update, context)  # Pass update instead of query

    elif query.data == 'settings':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
            [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ])
        await query.message.reply_text("⚙️ *Settings*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    elif query.data == 'help':
        help_text = (
            "ℹ️ *How to Use This Bot:*\n"
            "• Use the menu buttons to navigate.\n"
            "• Tap 'Ask Question' to share your thoughts anonymously.\n"
            "• Choose a category and type or send your message (text, photo, or voice).\n"
            "• After posting, others can comment on your posts.\n"
            "• View your profile, set your name and sex anytime.\n"
            "• Use the comments button on channel posts to join the conversation here."
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
                pass  # Already following
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
                await show_comments_page(update, context, post_id, page)  # Pass update instead of query
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
            
            # Get post content for preview
            post = db_fetch_one("SELECT * FROM posts WHERE post_id = ?", (post_id,))
            preview_text = "Original content not found"
            if post:
                # Truncate long posts
                content = post['content'][:100] + '...' if len(post['content']) > 100 else post['content']
                preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n✍️ Please type your comment:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    elif query.data.startswith(("likecomment_", "dislikecomment_", "likereply_", "dislikereply_")):
        # Extract comment ID from callback data
        parts = query.data.split('_')
        comment_id = int(parts[1])
        reaction_type = 'like' if 'like' in parts[0] else 'dislike'
        
        # Remove existing reaction
        db_execute(
            "DELETE FROM reactions WHERE comment_id = ? AND user_id = ?",
            (comment_id, user_id)
        )
        
        # Add new reaction
        db_execute(
            "INSERT INTO reactions (comment_id, user_id, type) VALUES (?, ?, ?)",
            (comment_id, user_id, reaction_type)
        )
        
        # Get updated counts
        likes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'like'",
            (comment_id,)
        )
        likes = likes_row['cnt'] if likes_row and 'cnt' in likes_row else 0
        
        dislikes_row = db_fetch_one(
            "SELECT COUNT(*) as cnt FROM reactions WHERE comment_id = ? AND type = 'dislike'",
            (comment_id,)
        )
        dislikes = dislikes_row['cnt'] if dislikes_row and 'cnt' in dislikes_row else 0
        
        # Build new keyboard with updated counts
        if 'reply' in parts[0]:  # For replies
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"likereply_{comment_id}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikereply_{comment_id}"),
                    InlineKeyboardButton("Reply", callback_data=f"replytoreply_{comment_id}")
                ]
            ])
        else:  # For top-level comments
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{comment_id}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{comment_id}"),
                    InlineKeyboardButton("Reply", callback_data=f"reply_{comment_id}")
                ]
            ]) 

        try:
            # Update the message with new counts
            await query.message.edit_reply_markup(reply_markup=new_kb)
        except Exception as e:
            logger.warning(f"Could not update buttons: {e}")
            
    elif query.data.startswith("reply_"):
        parts = query.data.split("_")
        if len(parts) == 3:
            post_id = int(parts[1])
            comment_id = int(parts[2])
            db_execute(
                "UPDATE users SET waiting_for_comment = 1, comment_post_id = ?, comment_idx = ? WHERE user_id = ?",
                (post_id, comment_id, user_id)
            )
            
            # Get the comment content for preview
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
            preview_text = "Original comment not found"
            if comment:
                # Truncate long comments
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
            
            # Get the reply content for preview
            comment = db_fetch_one("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
            preview_text = "Original reply not found"
            if comment:
                # Truncate long replies
                content = comment['content'][:100] + '...' if len(comment['content']) > 100 else comment['content']
                preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n↩️ Please type your *reply*:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.message.from_user.id)
    message = update.message
    user = db_fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    # Handle new posts
    if user and user['waiting_for_post']:
        category = user['selected_category']
        db_execute(
            "UPDATE users SET waiting_for_post = 0, selected_category = NULL WHERE user_id = ?",
            (user_id,)
        )
        display_name = get_display_name(user)
        
        post_content = ""
        media_to_send = None
        try:
            if update.message.text:
                post_content = update.message.text
            elif update.message.photo:
                photo = update.message.photo[-1]
                file_id = photo.file_id
                media_to_send = ('photo', file_id)
                post_content = update.message.caption or ""
            elif update.message.voice:
                voice = update.message.voice
                file_id = voice.file_id
                media_to_send = ('voice', file_id)
                post_content = update.message.caption or ""
            else:
                post_content = "(Unsupported content type)"
        except Exception as e:
            logger.error(f"Error reading media: {e}")
            post_content = "(Unsupported content type)" 

        # Save post to database
        post_id = db_execute(
            "INSERT INTO posts (content, author_id, category) VALUES (?, ?, ?)",
            (post_content, user_id, category)
        )
        
        hashtag = f"#{category}"
        caption_text = (
            f"{post_content}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{hashtag}\n"
            f"[Telegram](https://t.me/gospelyrics)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
        ]) 

        try:
            if media_to_send is None:
                msg = await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb)
            else:
                media_type, file_id = media_to_send
                if media_type == 'photo':
                    msg = await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=file_id,
                        caption=caption_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb)
                else:  # voice
                    msg = await context.bot.send_voice(
                        chat_id=CHANNEL_ID,
                        voice=file_id,
                        caption=caption_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb) 

            # Update post with channel message ID
            db_execute(
                "UPDATE posts SET channel_message_id = ? WHERE post_id = ?",
                (msg.message_id, post_id)
            )
            
            await update.message.reply_text("✅ Your question has been posted!", reply_markup=main_menu)
        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            await update.message.reply_text("❌ Failed to post your question. Please try again later.")
        return 

    # Handle comments and replies
    elif user and user['waiting_for_comment']:
        post_id = user['comment_post_id']
        parent_comment_id = 0
        comment_type = 'text'
        file_id = None
        
        # Determine if this is a reply to a comment or reply
        if user['reply_idx'] is not None:   # This is set when replying to a reply
            parent_comment_id = user['reply_idx']
        elif user['comment_idx'] is not None:   # This is set when replying to a top-level comment
            parent_comment_id = user['comment_idx']
        
        # Determine content type
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
        
        # Save comment to database
        db_execute(
            """INSERT INTO comments 
            (post_id, parent_comment_id, author_id, content, type, file_id) 
            VALUES (?, ?, ?, ?, ?, ?)""",
            (post_id, parent_comment_id, user_id, content, comment_type, file_id)
        )
        
        # Update comment count in channel
        total_comments = count_all_comments(post_id)
        try:
            # Get the channel message ID for this post
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
        
        # Clear comment state
        db_execute(
            "UPDATE users SET waiting_for_comment = 0, comment_post_id = NULL, comment_idx = NULL, reply_idx = NULL, nested_idx = NULL WHERE user_id = ?",
            (user_id,)
        )
        
        await update.message.reply_text("✅ Your comment has been added!", reply_markup=main_menu)
        return

    # Handle profile name updates
    if user and user['awaiting_name']:
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            db_execute(
                "UPDATE users SET anonymous_name = ?, awaiting_name = 0 WHERE user_id = ?",
                (new_name, user_id)
            )
            await update.message.reply_text(f"✅ Name updated to *{new_name}*!", parse_mode=ParseMode.MARKDOWN)
            await send_updated_profile(user_id, update.message.chat_id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle clicks on reply keyboard buttons:
    if text == "🙏 Ask Question":
        await update.message.reply_text(
            "📚 *Choose a category:*",
            reply_markup=build_category_buttons(),
            parse_mode=ParseMode.MARKDOWN
        )
        return 

    elif text == "👤 View Profile":
        await send_updated_profile(user_id, update.message.chat_id, context)
        return 

    elif text == "🏆 Leaderboard":
        await show_leaderboard(update, context)
        return

    elif text == "⚙️ Settings":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
            [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
            [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
        ])
        await update.message.reply_text("⚙️ *Settings*", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        return

    elif text == "❓ Help":
        help_text = (
            "ℹ️ *How to Use This Bot:*\n"
            "• Use the menu buttons to navigate.\n"
            "• Tap 'Ask Question' to share your thoughts anonymously.\n"
            "• Choose a category and type or send your message (text, photo, or voice).\n"
            "• After posting, others can comment on your posts.\n"
            "• View your profile, set your name and sex anytime.\n"
            "• Use the comments button on channel posts to join the conversation here."
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
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("menu", "📱 Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Ask a question"),
        BotCommand("leaderboard", "🏆 Show top contributors"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
    ]) 

def main():
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leaderboard", show_leaderboard))
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
