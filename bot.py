import os
import json
import logging
import threading
import psycopg2
import psycopg2.extras
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
from flask import Flask, jsonify

# --------------------------
# Database Functions
# --------------------------
def get_db_connection():
    conn = psycopg2.connect(
        os.getenv('DATABASE_URL'),
        sslmode='require'
    )
    return conn

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Create users table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    anonymous_name TEXT,
                    sex TEXT,
                    followers JSONB,
                    waiting_for_post BOOLEAN DEFAULT FALSE,
                    selected_category TEXT,
                    waiting_for_comment BOOLEAN DEFAULT FALSE,
                    comment_post_id INTEGER,
                    comment_idx INTEGER,
                    reply_idx INTEGER,
                    nested_idx INTEGER,
                    awaiting_name BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Create posts table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    post_id SERIAL PRIMARY KEY,
                    content TEXT,
                    author_id TEXT REFERENCES users(user_id),
                    author_name TEXT,
                    category TEXT,
                    channel_message_id INTEGER,
                    comments JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("Database tables initialized successfully")

def get_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
                user = cur.fetchone()
                return dict(user) if user else None
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return None

def save_user(user_id, user_data):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (user_id, anonymous_name, sex, followers, 
                                       waiting_for_post, selected_category, 
                                       waiting_for_comment, comment_post_id, 
                                       comment_idx, reply_idx, nested_idx, awaiting_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        anonymous_name = EXCLUDED.anonymous_name,
                        sex = EXCLUDED.sex,
                        followers = EXCLUDED.followers,
                        waiting_for_post = EXCLUDED.waiting_for_post,
                        selected_category = EXCLUDED.selected_category,
                        waiting_for_comment = EXCLUDED.waiting_for_comment,
                        comment_post_id = EXCLUDED.comment_post_id,
                        comment_idx = EXCLUDED.comment_idx,
                        reply_idx = EXCLUDED.reply_idx,
                        nested_idx = EXCLUDED.nested_idx,
                        awaiting_name = EXCLUDED.awaiting_name
                """, (
                    user_id,
                    user_data.get('anonymous_name'),
                    user_data.get('sex'),
                    json.dumps(user_data.get('followers', [])),
                    user_data.get('waiting_for_post', False),
                    user_data.get('selected_category'),
                    user_data.get('waiting_for_comment', False),
                    user_data.get('comment_post_id'),
                    user_data.get('comment_idx'),
                    user_data.get('reply_idx'),
                    user_data.get('nested_idx'),
                    user_data.get('awaiting_name', False)
                ))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Error saving user {user_id}: {e}")
        return False

def create_post(post_data):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO posts (content, author_id, author_name, category, 
                                       channel_message_id, comments)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING post_id
                """, (
                    post_data['content'],
                    post_data['author_id'],
                    post_data['author_name'],
                    post_data['category'],
                    post_data['channel_message_id'],
                    json.dumps(post_data.get('comments', []))
                ))
                post_id = cur.fetchone()[0]
                conn.commit()
                return post_id
    except Exception as e:
        logger.error(f"Error creating post: {e}")
        return None

def get_post(post_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM posts WHERE post_id = %s", (post_id,))
                post = cur.fetchone()
                if post:
                    post = dict(post)
                    post['comments'] = json.loads(post['comments']) if post['comments'] else []
                return post
    except Exception as e:
        logger.error(f"Error getting post {post_id}: {e}")
        return None

def update_post(post_id, post_data):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE posts
                    SET comments = %s
                    WHERE post_id = %s
                """, (
                    json.dumps(post_data['comments']),
                    post_id
                ))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Error updating post {post_id}: {e}")
        return False

def get_all_posts():
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM posts")
                posts = cur.fetchall()
                result = []
                for post in posts:
                    post = dict(post)
                    post['comments'] = json.loads(post['comments']) if post['comments'] else []
                    result.append(post)
                return result
    except Exception as e:
        logger.error(f"Error getting all posts: {e}")
        return []

# --------------------------
# Bot Configuration
# --------------------------
# Initialize database tables before anything else
load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
init_db()

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
        [KeyboardButton("❓ Help"), KeyboardButton("ℹ️ About Us")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# --------------------------
# Helper Functions
# --------------------------
def create_anonymous_name(user_id):
    try:
        uid_int = int(user_id)
    except ValueError:
        uid_int = abs(hash(user_id)) % 10000
    names = ["Hopeful", "Believer", "Forgiven", "ChildOfGod", "Redeemed",
             "Graceful", "Faithful", "Blessed", "Peaceful", "Joyful", "Loved"]
    return f"{names[uid_int % len(names)]}{uid_int % 1000}"

def calculate_user_rating(user_id):
    posts = get_all_posts()
    count = 0
    
    # Count posts by this user
    for post in posts:
        if post['author_id'] == str(user_id):
            count += 1
    
    # Count comments and replies by this user
    for post in posts:
        for comment in post.get('comments', []):
            if comment.get('author_id') == str(user_id):
                count += 1
            for reply in comment.get('replies', []):
                if reply.get('user_id') == str(user_id):
                    count += 1
                for nested_reply in reply.get('replies', []):
                    if nested_reply.get('user_id') == str(user_id):
                        count += 1
    return count

def format_stars(rating, max_stars=5):
    full = '⭐️' * min(rating, max_stars)
    empty = '☆' * max(0, max_stars - rating)
    return full + empty

def count_all_comments(comments):
    total = 0
    for comment in comments:
        total += 1  # Count the comment itself
        if 'replies' in comment:
            total += count_all_comments(comment['replies'])
    return total

# --------------------------
# Command Handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    user = get_user(user_id)

    # Add user if new
    if not user:
        anon = create_anonymous_name(user_id)
        save_user(user_id, {
            "anonymous_name": anon,
            "followers": [],
            "sex": "❓"
        })
        user = get_user(user_id)

    args = context.args  # deep link args after /start

    if args:
        arg = args[0]

        # Show comment menu for a post
        if arg.startswith("comments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                post = get_post(post_id)
                if not post:
                    await update.message.reply_text("❌ Post not found.", reply_markup=main_menu)
                    return

                comment_count = count_all_comments(post.get('comments', []))
                keyboard = [
                    [
                        InlineKeyboardButton(f"👁 View Comments ({comment_count})", callback_data=f"viewcomments_{post_id}"),
                        InlineKeyboardButton("✍️ Write Comment", callback_data=f"writecomment_{post_id}")
                    ]
                ]

                post_text = post.get('content', '(No content)')
                escaped_text = escape_markdown(post_text, version=2)

                await update.message.reply_text(
                    f"💬\n{escaped_text}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                return

        # Show the comments list for a post
        elif arg.startswith("viewcomments_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                post = get_post(post_id)
                if not post:
                    await update.message.reply_text("❌ Post not found.", reply_markup=main_menu)
                    return

                comments = post.get('comments', [])
                post_text = post.get('content', '(No text content)')
  
                # Escape the original post content properly
                header = f"{escape_markdown(post_text, version=2)}\n\n"

                if not comments:
                    await update.message.reply_text(header + "_No comments yet._", 
                                                  parse_mode=ParseMode.MARKDOWN_V2,
                                                  reply_markup=main_menu)
                    return

                await update.message.reply_text(header, 
                                              parse_mode=ParseMode.MARKDOWN_V2,
                                              reply_markup=main_menu)

                # Store the message ID of the header message for threading
                context.user_data['comment_header_id'] = update.message.message_id + 1
                
                for idx, c in enumerate(comments):
                    commenter_id = c.get('author_id')
                    commenter = get_user(commenter_id) or {}
                    anon = commenter.get('anonymous_name', "Unknown")
                    sex = commenter.get('sex', '❓')
                    rating = calculate_user_rating(commenter_id)
                    stars = format_stars(rating)
                    profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{anon}"

                    likes = len(c.get('likes', []))
                    dislikes = len(c.get('dislikes', []))
                    
                    # Create clean comment text
                    comment_text = escape_markdown(c.get('content', ''), version=2)
                    
                    # Create author text as clickable link
                    author_text = f"[{escape_markdown(anon, version=2)}]({profile_url}) {sex} {stars}"

                    kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{post_id}_{idx}"),
                            InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{post_id}_{idx}"),
                            InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{idx}")
                        ]
                    ])

                    # Send comment as a reply to the header message for proper threading
                    msg = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"{comment_text}\n\n{author_text}",
                        reply_markup=kb,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=context.user_data.get('comment_header_id')
                    )
                    
                    # Display replies to this comment as threaded replies
                    replies = c.get('replies', [])
                    for r_idx, r in enumerate(replies):
                        reply_user_id = r.get('user_id')
                        reply_user = get_user(reply_user_id) or {}
                        reply_anon = reply_user.get('anonymous_name', 'Unknown')
                        reply_sex = reply_user.get('sex', '❓')
                        rating_reply = calculate_user_rating(reply_user_id)
                        stars_reply = format_stars(rating_reply)
                        profile_url_reply = f"https://t.me/{BOT_USERNAME}?start=profile_{reply_anon}"
                        safe_reply = escape_markdown(r.get('text', ''), version=2)
                        
                        # Create reply author text as clickable link
                        reply_author_text = f"[{escape_markdown(reply_anon, version=2)}]({profile_url_reply}) {reply_sex} {stars_reply}"
                        
                        # Get like/dislike counts for this reply
                        reply_likes = len(r.get('likes', []))
                        reply_dislikes = len(r.get('dislikes', []))
                        
                        # Create keyboard for the reply
                        reply_kb = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(f"👍 {reply_likes}", callback_data=f"likereply_{post_id}_{idx}_{r_idx}"),
                                InlineKeyboardButton(f"👎 {reply_dislikes}", callback_data=f"dislikereply_{post_id}_{idx}_{r_idx}"),
                                InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{idx}_{r_idx}")
                            ]
                        ])
                        
                        # Send as threaded reply
                        reply_msg = await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=f"{safe_reply}\n\n{reply_author_text}",
                            parse_mode=ParseMode.MARKDOWN_V2,
                            reply_to_message_id=msg.message_id,
                            reply_markup=reply_kb
                        )
                        
                        # Display replies to this reply (nested replies)
                        nested_replies = r.get('replies', [])
                        for nr_idx, nr in enumerate(nested_replies):
                            nr_user_id = nr.get('user_id')
                            nr_user = get_user(nr_user_id) or {}
                            nr_anon = nr_user.get('anonymous_name', 'Unknown')
                            nr_sex = nr_user.get('sex', '❓')
                            nr_rating = calculate_user_rating(nr_user_id)
                            nr_stars = format_stars(nr_rating)
                            nr_profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{nr_anon}"
                            nr_safe_reply = escape_markdown(nr.get('text', ''), version=2)
                            
                            # Create nested reply author text as clickable link
                            nr_author_text = f"[{escape_markdown(nr_anon, version=2)}]({nr_profile_url}) {nr_sex} {nr_stars}"
                            
                            # Get like/dislike counts for nested reply
                            nr_likes = len(nr.get('likes', []))
                            nr_dislikes = len(nr.get('dislikes', []))
                            
                            # Create keyboard for nested reply
                            nr_kb = InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton(f"👍 {nr_likes}", callback_data=f"likenested_{post_id}_{idx}_{r_idx}_{nr_idx}"),
                                    InlineKeyboardButton(f"👎 {nr_dislikes}", callback_data=f"dislikenested_{post_id}_{idx}_{r_idx}_{nr_idx}"),
                                    InlineKeyboardButton("Reply", callback_data=f"replytonested_{post_id}_{idx}_{r_idx}_{nr_idx}")
                                ]
                            ])
                            
                            # Send nested reply
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=f"{nr_safe_reply}\n\n{nr_author_text}",
                                parse_mode=ParseMode.MARKDOWN_V2,
                                reply_to_message_id=reply_msg.message_id,
                                reply_markup=nr_kb
                            )
            return

        # Start writing comment on a post
        elif arg.startswith("writecomment_"):
            post_id_str = arg.split("_", 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                user = get_user(user_id) or {}
                user['waiting_for_comment'] = True
                user['comment_post_id'] = post_id
                save_user(user_id, user)
                
                # Get post content for preview
                post = get_post(post_id)
                preview_text = "Original content not found"
                if post:
                    content = post.get('content', '')[:100] + '...' if len(post.get('content', '')) > 100 else post.get('content', '')
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
            all_users = []
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT * FROM users")
                    all_users = [dict(row) for row in cur.fetchall()]
            
            for u in all_users:
                if u.get('anonymous_name') == target_name:
                    uid = u['user_id']
                    followers = u.get('followers', [])
                    rating = calculate_user_rating(uid)
                    stars = format_stars(rating)
                    current = user_id
                    btn = []
                    if uid != current:
                        if current in followers:
                            btn.append([InlineKeyboardButton("🚫 Unfollow", callback_data=f'unfollow_{uid}')])
                        else:
                            btn.append([InlineKeyboardButton("🫂 Follow", callback_data=f'follow_{uid}')])
                    await update.message.reply_text(
                        f"👤 *{target_name}* 🎖 Verified\n"
                        f"📌 Sex: {u.get('sex')}\n"
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

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("✍️ Ask Question 🙏", callback_data='ask'),
            InlineKeyboardButton("👤 View Profile 🎖", callback_data='profile')
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
    user = get_user(user_id) or {}
    anon = user.get('anonymous_name', create_anonymous_name(user_id))
    rating = calculate_user_rating(user_id)
    stars = format_stars(rating)
    followers = user.get('followers', [])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Set My Name", callback_data='edit_name')],
        [InlineKeyboardButton("⚧️ Set My Sex", callback_data='edit_sex')],
        [InlineKeyboardButton("📱 Main Menu", callback_data='menu')]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤 *{anon}* 🎖 Verified\n"
            f"📌 Sex: {user.get('sex', '❓')}\n"
            f"⭐️ Rating: {rating} {stars}\n"
            f"🎖 Batch: User\n"
            f"👥 Followers: {len(followers)}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"_Use /menu to return_"
        ),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN)

# --------------------------
# Button Handler
# --------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id) or {}

    if query.data == 'ask':
        await query.message.reply_text(
            "📚 *Choose a category:*",
            reply_markup=build_category_buttons(),
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data.startswith('category_'):
        category = query.data.split('_', 1)[1]
        user['waiting_for_post'] = True
        user['selected_category'] = category
        save_user(user_id, user)

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
        await send_updated_profile(user_id, query.message.chat_id, context)

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
        user['awaiting_name'] = True
        save_user(user_id, user)
        await query.message.reply_text("✏️ Please type your new anonymous name:", parse_mode=ParseMode.MARKDOWN)

    elif query.data == 'edit_sex':
        btns = [
            [InlineKeyboardButton("👨 Male", callback_data='sex_male')],
            [InlineKeyboardButton("👩 Female", callback_data='sex_female')]
        ]
        await query.message.reply_text("⚧️ Select your sex:", reply_markup=InlineKeyboardMarkup(btns))

    elif query.data.startswith('sex_'):
        sex = '👨' if 'male' in query.data else '👩'
        user['sex'] = sex
        save_user(user_id, user)
        await query.message.reply_text("✅ Sex updated!")
        await send_updated_profile(user_id, query.message.chat_id, context)

    elif query.data.startswith(('follow_', 'unfollow_')):
        target_uid = query.data.split('_', 1)[1]
        target_user = get_user(target_uid) or {}
        followers = target_user.get('followers', [])
        if query.data.startswith('follow_'):
            if user_id not in followers:
                followers.append(user_id)
        else:
            if user_id in followers:
                followers.remove(user_id)
        target_user['followers'] = followers
        save_user(target_uid, target_user)
        await query.message.reply_text("✅ Successfully updated!")
        await send_updated_profile(target_uid, query.message.chat_id, context)
    elif query.data.startswith('viewcomments_'):
        try:
            post_id_str = query.data.split('_', 1)[1]
            if post_id_str.isdigit():
                post_id = int(post_id_str)
                post = get_post(post_id)
                if not post:
                    await query.answer("❌ Post not found.")
                    return
    
                comments = post.get('comments', [])

                if not comments:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="_No comments yet._",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    return
    
                # Store header message ID for threading
                header_msg = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="💬 *Comments:*",
                    parse_mode=ParseMode.MARKDOWN_V2
                )
                context.user_data['comment_header_id'] = header_msg.message_id

                # Send each comment as a reply to the header message
                for idx, c in enumerate(comments):
                    try:
                        commenter_id = c.get('author_id')
                        commenter = get_user(commenter_id) or {}
                        anon = commenter.get('anonymous_name', "Unknown")
                        sex = commenter.get('sex', '❓')
                        rating = calculate_user_rating(commenter_id)
                        stars = format_stars(rating)
                        profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{anon}"
    
                        likes = len(c.get('likes', []))
                        dislikes = len(c.get('dislikes', []))
    
                        # Get comment text safely
                        comment_text = c.get('content') or c.get('caption') or ''
                        safe_comment = escape_markdown(comment_text, version=2)
    
                        # Build clean comment message with clickable name
                        comment_msg = f"{safe_comment}\n\n[{anon}]({profile_url}) {sex} {stars}"
    
                        # Build keyboard
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton(f"👍 {likes}", callback_data=f"likecomment_{post_id}_{idx}"),
                            InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislikecomment_{post_id}_{idx}"),
                            InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{idx}")
                        ]])
    
                        # Send comment as a reply to the header
                        msg = await context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=comment_msg,
                            reply_markup=kb,
                            parse_mode=ParseMode.MARKDOWN_V2,
                            reply_to_message_id=context.user_data['comment_header_id'],
                            disable_web_page_preview=True
                        )
                        
                        # Display replies as threaded messages
                        replies = c.get('replies', [])
                        for r_idx, r in enumerate(replies):
                            reply_user_id = r.get('user_id')
                            reply_user = get_user(reply_user_id) or {}
                            reply_anon = reply_user.get('anonymous_name', 'Unknown')
                            reply_sex = reply_user.get('sex', '❓')
                            rating_reply = calculate_user_rating(reply_user_id)
                            stars_reply = format_stars(rating_reply)
                            profile_url_reply = f"https://t.me/{BOT_USERNAME}?start=profile_{reply_anon}"
                            safe_reply = escape_markdown(r.get('text', ''), version=2)
                            
                            # Get like/dislike counts for this reply
                            reply_likes = len(r.get('likes', []))
                            reply_dislikes = len(r.get('dislikes', []))
                            
                            # Create reply author text as clickable link
                            reply_author_text = f"[{reply_anon}]({profile_url_reply}) {reply_sex} {stars_reply}"
                            
                            # Create keyboard for the reply
                            reply_kb = InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton(f"👍 {reply_likes}", callback_data=f"likereply_{post_id}_{idx}_{r_idx}"),
                                    InlineKeyboardButton(f"👎 {reply_dislikes}", callback_data=f"dislikereply_{post_id}_{idx}_{r_idx}"),
                                    InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{idx}_{r_idx}")
                                ]
                            ])
                            
                            # Send as threaded reply
                            reply_msg = await context.bot.send_message(
                                chat_id=query.message.chat_id,
                                text=f"{safe_reply}\n\n{reply_author_text}",
                                parse_mode=ParseMode.MARKDOWN_V2,
                                reply_to_message_id=msg.message_id,
                                reply_markup=reply_kb,
                                disable_web_page_preview=True
                            )
                            
                            # Display nested replies to this reply
                            nested_replies = r.get('replies', [])
                            for nr_idx, nr in enumerate(nested_replies):
                                nr_user_id = nr.get('user_id')
                                nr_user = get_user(nr_user_id) or {}
                                nr_anon = nr_user.get('anonymous_name', 'Unknown')
                                nr_sex = nr_user.get('sex', '❓')
                                nr_rating = calculate_user_rating(nr_user_id)
                                nr_stars = format_stars(nr_rating)
                                nr_profile_url = f"https://t.me/{BOT_USERNAME}?start=profile_{nr_anon}"
                                nr_safe_reply = escape_markdown(nr.get('text', ''), version=2)
                                
                                # Create nested reply author text as clickable link
                                nr_author_text = f"[{nr_anon}]({nr_profile_url}) {nr_sex} {nr_stars}"
                                
                                # Get like/dislike counts for nested reply
                                nr_likes = len(nr.get('likes', []))
                                nr_dislikes = len(nr.get('dislikes', []))
                                
                                # Create keyboard for nested reply
                                nr_kb = InlineKeyboardMarkup([
                                    [
                                        InlineKeyboardButton(f"👍 {nr_likes}", callback_data=f"likenested_{post_id}_{idx}_{r_idx}_{nr_idx}"),
                                        InlineKeyboardButton(f"👎 {nr_dislikes}", callback_data=f"dislikenested_{post_id}_{idx}_{r_idx}_{nr_idx}"),
                                        InlineKeyboardButton("Reply", callback_data=f"replytonested_{post_id}_{idx}_{r_idx}_{nr_idx}")
                                    ]
                                ])
                                
                                # Send nested reply
                                await context.bot.send_message(
                                    chat_id=query.message.chat_id,
                                    text=f"{nr_safe_reply}\n\n{nr_author_text}",
                                    parse_mode=ParseMode.MARKDOWN_V2,
                                    reply_to_message_id=reply_msg.message_id,
                                    reply_markup=nr_kb,
                                    disable_web_page_preview=True
                                )
                    except Exception as e:
                        logger.error(f"Error sending comment {idx}: {e}")
        except Exception as e:
            logger.error(f"ViewComments error: {e}")
            await query.answer("❌ Error loading comments")
  
    elif query.data.startswith('writecomment_'):
        post_id_str = query.data.split('_', 1)[1]
        if post_id_str.isdigit():
            post_id = int(post_id_str)
            user['waiting_for_comment'] = True
            user['comment_post_id'] = post_id
            save_user(user_id, user)
            
            # Get post content for preview
            post = get_post(post_id)
            preview_text = "Original content not found"
            if post:
                # Truncate long posts
                content = post.get('content', '')[:100] + '...' if len(post.get('content', '')) > 100 else post.get('content', '')
                preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n✍️ Please type your comment:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
    elif query.data.startswith(("likecomment_", "dislikecomment_")):
        parts = query.data.split("_")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            post = get_post(post_id)
            if not post:
                await query.answer("❌ Post not found.")
                return
    
            comments = post.get('comments', [])
            if comment_idx >= len(comments):
                await query.answer("❌ Comment not found.")
                return
            comment = comments[comment_idx]
            likes = comment.setdefault('likes', [])
            dislikes = comment.setdefault('dislikes', []) 
            
            if query.data.startswith("likecomment_"):
                # Remove from dislikes if present
                if user_id in dislikes:
                    dislikes.remove(user_id)
                # Toggle like
                if user_id in likes:
                    likes.remove(user_id)
                else:
                    likes.append(user_id)
                await query.answer("👍 Like updated!")
                
            elif query.data.startswith("dislikecomment_"):
                # Remove from likes if present
                if user_id in likes:
                    likes.remove(user_id)
                # Toggle dislike
                if user_id in dislikes:
                    dislikes.remove(user_id)
                else:
                    dislikes.append(user_id)
                await query.answer("👎 Dislike updated!")
                
            post['comments'][comment_idx] = comment
            update_post(post_id, post)        
    
            # Build new keyboard with updated counts
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {len(likes)}", callback_data=f"likecomment_{post_id}_{comment_idx}"),
                    InlineKeyboardButton(f"👎 {len(dislikes)}", callback_data=f"dislikecomment_{post_id}_{comment_idx}"),
                    InlineKeyboardButton("Reply", callback_data=f"reply_{post_id}_{comment_idx}")
                ]
            ])

            try:
                # Update the message with new counts
                await query.message.edit_reply_markup(reply_markup=new_kb)
            except Exception as e:
                logger.warning(f"Could not update buttons: {e}")
                
    elif query.data.startswith(("likereply_", "dislikereply_")):
        parts = query.data.split("_")
        if len(parts) == 4 and all(p.isdigit() for p in parts[1:4]):
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            reply_idx = int(parts[3])
            post = get_post(post_id)
            if not post:
                await query.answer("❌ Post not found.")
                return
            comments = post.get('comments', [])
            if comment_idx >= len(comments):
                await query.answer("❌ Comment not found.")
                return
            comment = comments[comment_idx]
            replies = comment.get('replies', [])
            if reply_idx >= len(replies):
                await query.answer("❌ Reply not found.")
                return
            reply = replies[reply_idx]
            likes = reply.setdefault('likes', [])
            dislikes = reply.setdefault('dislikes', [])
            
            if query.data.startswith("likereply_"):
                # Remove from dislikes if present
                if user_id in dislikes:
                    dislikes.remove(user_id)
                # Toggle like
                if user_id in likes:
                    likes.remove(user_id)
                else:
                    likes.append(user_id)
                await query.answer("👍 Like updated!")
                
            elif query.data.startswith("dislikereply_"):
                # Remove from likes if present
                if user_id in likes:
                    likes.remove(user_id)
                # Toggle dislike
                if user_id in dislikes:
                    dislikes.remove(user_id)
                else:
                    dislikes.append(user_id)
                await query.answer("👎 Dislike updated!")
                
            replies[reply_idx] = reply
            comment['replies'] = replies
            comments[comment_idx] = comment
            post['comments'] = comments
            update_post(post_id, post)
            
            # Build new keyboard with updated counts
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {len(likes)}", callback_data=f"likereply_{post_id}_{comment_idx}_{reply_idx}"),
                    InlineKeyboardButton(f"👎 {len(dislikes)}", callback_data=f"dislikereply_{post_id}_{comment_idx}_{reply_idx}"),
                    InlineKeyboardButton("Reply", callback_data=f"replytoreply_{post_id}_{comment_idx}_{reply_idx}")
                ]
            ])
            
            try:
                # Update the message with new counts
                await query.message.edit_reply_markup(reply_markup=new_kb)
            except Exception as e:
                logger.warning(f"Could not update reply buttons: {e}")
                
    elif query.data.startswith(("likenested_", "dislikenested_")):
        parts = query.data.split("_")
        if len(parts) == 5 and all(p.isdigit() for p in parts[1:5]):
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            reply_idx = int(parts[3])
            nested_idx = int(parts[4])
            post = get_post(post_id)
            if not post:
                await query.answer("❌ Post not found.")
                return
            comments = post.get('comments', [])
            if comment_idx >= len(comments):
                await query.answer("❌ Comment not found.")
                return
            comment = comments[comment_idx]
            replies = comment.get('replies', [])
            if reply_idx >= len(replies):
                await query.answer("❌ Reply not found.")
                return
            reply = replies[reply_idx]
            nested_replies = reply.get('replies', [])
            if nested_idx >= len(nested_replies):
                await query.answer("❌ Nested reply not found.")
                return
            nested_reply = nested_replies[nested_idx]
            likes = nested_reply.setdefault('likes', [])
            dislikes = nested_reply.setdefault('dislikes', [])
            
            if query.data.startswith("likenested_"):
                # Remove from dislikes if present
                if user_id in dislikes:
                    dislikes.remove(user_id)
                # Toggle like
                if user_id in likes:
                    likes.remove(user_id)
                else:
                    likes.append(user_id)
                await query.answer("👍 Like updated!")
                
            elif query.data.startswith("dislikenested_"):
                # Remove from likes if present
                if user_id in likes:
                    likes.remove(user_id)
                # Toggle dislike
                if user_id in dislikes:
                    dislikes.remove(user_id)
                else:
                    dislikes.append(user_id)
                await query.answer("👎 Dislike updated!")
                
            nested_replies[nested_idx] = nested_reply
            reply['replies'] = nested_replies
            replies[reply_idx] = reply
            comment['replies'] = replies
            comments[comment_idx] = comment
            post['comments'] = comments
            update_post(post_id, post)
            
            # Build new keyboard with updated counts
            new_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"👍 {len(likes)}", callback_data=f"likenested_{post_id}_{comment_idx}_{reply_idx}_{nested_idx}"),
                    InlineKeyboardButton(f"👎 {len(dislikes)}", callback_data=f"dislikenested_{post_id}_{comment_idx}_{reply_idx}_{nested_idx}"),
                    InlineKeyboardButton("Reply", callback_data=f"replytonested_{post_id}_{comment_idx}_{reply_idx}_{nested_idx}")
                ]
            ])
            
            try:
                # Update the message with new counts
                await query.message.edit_reply_markup(reply_markup=new_kb)
            except Exception as e:
                logger.warning(f"Could not update nested reply buttons: {e}")
                
    elif query.data.startswith("reply_"):
        parts = query.data.split("_")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            user['waiting_for_comment'] = True
            user['comment_post_id'] = post_id
            user['comment_idx'] = comment_idx  # Store which comment we're replying to
            save_user(user_id, user)
            
            # Get the comment content for preview
            post = get_post(post_id)
            preview_text = "Original comment not found"
            if post and 0 <= comment_idx < len(post.get('comments', [])):
                comment = post['comments'][comment_idx]
                # Truncate long comments
                content = comment.get('content', '')[:100] + '...' if len(comment.get('content', '')) > 100 else comment.get('content', '')
                preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n↩️ Please type your *reply*:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
    elif query.data.startswith("replytoreply_"):
        parts = query.data.split("_")
        if len(parts) == 4 and all(p.isdigit() for p in parts[1:4]):
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            reply_idx = int(parts[3])
            user['waiting_for_comment'] = True
            user['comment_post_id'] = post_id
            user['comment_idx'] = comment_idx
            user['reply_idx'] = reply_idx  # Store which reply we're replying to
            save_user(user_id, user)
            
            # Get the reply content for preview
            post = get_post(post_id)
            preview_text = "Original reply not found"
            if post and 0 <= comment_idx < len(post.get('comments', [])):
                comment = post['comments'][comment_idx]
                replies = comment.get('replies', [])
                if 0 <= reply_idx < len(replies):
                    reply = replies[reply_idx]
                    # Truncate long replies
                    content = reply.get('text', '')[:100] + '...' if len(reply.get('text', '')) > 100 else reply.get('text', '')
                    preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n↩️ Please type your *reply*:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
    elif query.data.startswith("replytonested_"):
        parts = query.data.split("_")
        if len(parts) == 5 and all(p.isdigit() for p in parts[1:5]):
            post_id = int(parts[1])
            comment_idx = int(parts[2])
            reply_idx = int(parts[3])
            nested_idx = int(parts[4])
            user['waiting_for_comment'] = True
            user['comment_post_id'] = post_id
            user['comment_idx'] = comment_idx
            user['reply_idx'] = reply_idx
            user['nested_idx'] = nested_idx  # Store which nested reply we're replying to
            save_user(user_id, user)
            
            # Get the nested reply content for preview
            post = get_post(post_id)
            preview_text = "Original reply not found"
            if post and 0 <= comment_idx < len(post.get('comments', [])):
                comment = post['comments'][comment_idx]
                replies = comment.get('replies', [])
                if 0 <= reply_idx < len(replies):
                    reply = replies[reply_idx]
                    nested_replies = reply.get('replies', [])
                    if 0 <= nested_idx < len(nested_replies):
                        nested_reply = nested_replies[nested_idx]
                        # Truncate long replies
                        content = nested_reply.get('text', '')[:100] + '...' if len(nested_reply.get('text', '')) > 100 else nested_reply.get('text', '')
                        preview_text = f"💬 *Replying to:*\n{escape_markdown(content, version=2)}"
            
            await query.message.reply_text(
                f"{preview_text}\n\n↩️ Please type your *reply*:",
                reply_markup=ForceReply(selective=True),
                parse_mode=ParseMode.MARKDOWN_V2
            )
        
# --------------------------
# Message Handler
# --------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or update.message.caption or ""
    user_id = str(update.message.from_user.id)
    message = update.message
    user = get_user(user_id) or {}

    # Check if user is replying to a comment or a reply
    if user.get("waiting_for_comment"):
        post_id = user.get('comment_post_id')
        comment_idx = user.get('comment_idx')  # Get the comment index
        reply_idx = user.get('reply_idx')      # Get the reply index (if exists)
        nested_idx = user.get('nested_idx')    # Get the nested reply index (if exists)
        
        post = get_post(post_id)
        if not post:
            await message.reply_text("❌ Post not found.")
            return
    
        # Handle replies to nested replies (level 4+)
        if nested_idx is not None:
            comments = post.get("comments", [])
            if comment_idx >= len(comments):
                await message.reply_text("❌ Comment not found.")
                return
                
            comment = comments[comment_idx]
            replies = comment.get("replies", [])
            if reply_idx >= len(replies):
                await message.reply_text("❌ Reply not found.")
                return
                
            reply = replies[reply_idx]
            nested_replies = reply.get("replies", [])
            if nested_idx >= len(nested_replies):
                await message.reply_text("❌ Nested reply not found.")
                return
                
            # Create new reply to this nested reply
            new_nested_reply = {
                "user_id": user_id,
                "text": text,
                "timestamp": message.date.isoformat(),
                "likes": [],
                "dislikes": []
            }
    
            nested_replies[nested_idx].setdefault("replies", []).append(new_nested_reply)
            reply['replies'] = nested_replies
            replies[reply_idx] = reply
            comment['replies'] = replies
            comments[comment_idx] = comment
            post['comments'] = comments
            update_post(post_id, post)
            await message.reply_text("✅ Your reply has been added!")
            
            # Update comment count button on channel post
            try:
                total_comments = count_all_comments(post['comments'])
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=post.get('channel_message_id'),
                    reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to update comment count: {e}")
    
        # Handle replies to replies (level 3)
        elif reply_idx is not None:
            comments = post.get("comments", [])
            if comment_idx >= len(comments):
                await message.reply_text("❌ Comment not found.")
                return
                
            comment = comments[comment_idx]
            replies = comment.get("replies", [])
            if reply_idx >= len(replies):
                await message.reply_text("❌ Reply not found.")
                return
    
            # Create new reply to this reply
            new_reply = {
                "user_id": user_id,
                "text": text,
                "timestamp": message.date.isoformat(),
                "likes": [],
                "dislikes": []
            }
    
            replies[reply_idx].setdefault("replies", []).append(new_reply)
            comment['replies'] = replies
            comments[comment_idx] = comment
            post['comments'] = comments
            update_post(post_id, post)
            await message.reply_text("✅ Your reply has been added!")
            
            # Update comment count button on channel post
            try:
                total_comments = count_all_comments(post['comments'])
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=post.get('channel_message_id'),
                    reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to update comment count: {e}")
    
        # Handle replies to comments (level 2)
        elif comment_idx is not None:
            comments = post.get("comments", [])
            if comment_idx >= len(comments):
                await message.reply_text("❌ Comment not found.")
                return
    
            # Create new reply to comment
            new_reply = {
                "user_id": user_id,
                "text": text,
                "timestamp": message.date.isoformat(),
                "likes": [],
                "dislikes": []
            }
    
            comments[comment_idx].setdefault("replies", []).append(new_reply)
            post['comments'] = comments
            update_post(post_id, post)
            await message.reply_text("✅ Your reply has been added!")
            
            # Update comment count button on channel post
            try:
                total_comments = count_all_comments(post['comments'])
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=post.get('channel_message_id'),
                    reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to update comment count: {e}")
        
        # Handle top-level comments
        else:
            anon = user.get('anonymous_name', create_anonymous_name(user_id))

            comment = {
                "author": anon,
                "author_id": user_id,
                "likes": [],
                "dislikes": [],
                "replies": []
            }
    
            # Detect comment type and content
            if update.message.text:
                comment['content'] = update.message.text
                comment['type'] = 'text'
            elif update.message.photo:
                photo = update.message.photo[-1]
                comment['file_id'] = photo.file_id
                comment['caption'] = update.message.caption or ""
                comment['type'] = 'photo'
            elif update.message.voice:
                voice = update.message.voice
                comment['file_id'] = voice.file_id
                comment['caption'] = update.message.caption or ""
                comment['type'] = 'voice'
            else:
                # Unsupported type
                await update.message.reply_text("❌ Unsupported comment type. Please send text, photo, or voice message.")
                return
    
            post.setdefault('comments', []).append(comment)
            update_post(post_id, post)
    
            # Update comment count button on channel post
            try:
                total_comments = count_all_comments(post['comments'])
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"💬 Comments ({total_comments})", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
                ])
                await context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=post.get('channel_message_id'),
                    reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Failed to update comment count: {e}")
    
            await update.message.reply_text("✅ Your comment has been added!", reply_markup=main_menu)
            
        
        # Clear reply mode
        user["waiting_for_comment"] = False
        user["comment_post_id"] = None
        user["comment_idx"] = None
        user["reply_idx"] = None
        user["nested_idx"] = None
        save_user(user_id, user)
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

    user = get_user(user_id) or {}

    # Handle waiting for new anonymous name
    if user.get('awaiting_name'):
        new_name = text.strip()
        if new_name and len(new_name) <= 30:
            user['anonymous_name'] = new_name
            user['awaiting_name'] = False
            save_user(user_id, user)
            await update.message.reply_text(f"✅ Name updated to *{new_name}*!", parse_mode=ParseMode.MARKDOWN)
            await send_updated_profile(user_id, update.message.chat_id, context)
        else:
            await update.message.reply_text("❌ Name cannot be empty or longer than 30 characters. Please try again.")
        return

    # Handle waiting for a new post (question/thought)
    if user.get('waiting_for_post'):
        category = user.pop('selected_category', 'Other')
        user['waiting_for_post'] = False
        save_user(user_id, user)
        anon = user.get('anonymous_name', create_anonymous_name(user_id))

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

        hashtag = f"#{category}"
        caption = (
            f"{post_content}\n\n"
            f"{hashtag}\n\n"
            f"[Telegram](https://t.me/gospelyrics)| [Bot](https://t.me/{BOT_USERNAME})"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{0}")]
        ])

        try:
            caption_text = (
                f"{post_content}\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{hashtag}\n"
                f"[Telegram](https://t.me/gospelyrics)| [Bot](https://t.me/{BOT_USERNAME})"
            )
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

            post_id = create_post({
                "content": post_content,
                "author_id": user_id,
                "author_name": anon,
                "category": category,
                "channel_message_id": msg.message_id,
                "comments": []
            })

            # Update comment button with correct post ID
            new_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 Comments (0)", url=f"https://t.me/{BOT_USERNAME}?start=comments_{post_id}")]
            ])
            await context.bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=msg.message_id,
                reply_markup=new_kb
            )

            await update.message.reply_text("✅ Your question has been posted!", reply_markup=main_menu)
        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            await update.message.reply_text("❌ Failed to post your question. Please try again later.")
        return

# --------------------------
# Error Handling & Setup
# --------------------------
async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True)

from telegram import BotCommand

async def set_bot_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot and open the menu"),
        BotCommand("menu", "📱 Open main menu"),
        BotCommand("profile", "View your profile"),
        BotCommand("ask", "Ask a question"),
        BotCommand("help", "How to use the bot"),
        BotCommand("about", "About the bot"),
    ])

def main():
    app = Application.builder().token(TOKEN).post_init(set_bot_commands).build()
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("start", start))
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
