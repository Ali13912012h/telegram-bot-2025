import sqlite3
import asyncio
import aiofiles
import zlib
import base64
import os
import tempfile
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from telegram.error import NetworkError, TelegramError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import secrets
import random
import matplotlib.pyplot as plt
from datetime import datetime
import matplotlib.dates as mdates
import jdatetime

# تنظیم لاگ‌گذاری
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# تcw'
SALT = b'my_super_secret_salt_123'
active_nodes = set()
node_cache = {}
user_cache = {}
balance_cache = {}
transaction_cache = {}
decryption_cache = {}
MAX_CACHE_SIZE = 1000
ADMIN_ID = 6931397471  # آیدی ادمین برای دسترسی به لیست کاربران

# دیتابیس کاربران
USER_DB_PATH = 'users.db'

# تابع برای تلاش دوباره در صورت خطای شبکه
async def retry_on_network_error(func, max_retries=3, delay=2):
    for attempt in range(max_retries):
        try:
            return await func()
        except NetworkError as e:
            logger.warning(f"خطای شبکه در تلاش {attempt + 1}/{max_retries}: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(delay * (2 ** attempt))
        except TelegramError as e:
            logger.error(f"خطای تلگرام: {e}")
            raise

# ساخت کلید از user_id
def generate_user_key(user_id):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=100000,
    )
    key = kdf.derive(str(user_id).encode())
    return key

# رمزنگاری
async def encrypt_amount(amount, user_id):
    loop = asyncio.get_running_loop()
    compressed = await loop.run_in_executor(None, zlib.compress, str(amount).encode(), 9)
    key = generate_user_key(user_id)
    iv = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(iv))
    encryptor = cipher.encryptor()
    encrypted = await loop.run_in_executor(None, lambda: encryptor.update(compressed) + encryptor.finalize())
    tag = encryptor.tag
    return base64.b64encode(iv).decode('ascii'), base64.b64encode(encrypted).decode('ascii'), base64.b64encode(tag).decode('ascii')

# رمزگشایی با کش
async def decrypt_amount(iv, encrypted, tag, user_id):
    cache_key = f"{user_id}:{iv}:{encrypted}:{tag}"
    if cache_key in decryption_cache:
        return decryption_cache[cache_key]
    
    try:
        loop = asyncio.get_running_loop()
        key = generate_user_key(user_id)
        cipher = Cipher(algorithms.AES(key), modes.GCM(base64.b64decode(iv), base64.b64decode(tag)))
        decryptor = cipher.decryptor()
        decrypted_compressed = await loop.run_in_executor(None, lambda: decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize())
        amount = float(zlib.decompress(decrypted_compressed).decode())
        if len(decryption_cache) >= MAX_CACHE_SIZE:
            decryption_cache.pop(next(iter(decryption_cache)))
        decryption_cache[cache_key] = amount
        return amount
    except Exception as e:
        logger.error(f"خطا در رمزگشایی برای user_id {user_id}: {e}")
        return 0

# دیتابیس کاربر
async def get_user_db(user_id):
    db_path = f'{user_id}_finance.db'
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=OFF')
    conn.execute('PRAGMA cache_size=10000')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            iv TEXT NOT NULL,
            encrypted_amount TEXT NOT NULL,
            tag TEXT NOT NULL,
            description TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON transactions (date)')
    conn.commit()
    return conn, cursor

# دیتابیس کاربران
def get_users_db():
    conn = sqlite3.connect(USER_DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    return conn, cursor

# ثبت کاربر جدید
async def register_user(user_id, username):
    conn, cursor = get_users_db()
    try:
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
    finally:
        conn.close()

# محاسبه موجودی
async def calculate_balance_distributed(user_id, cursor, context):
    if user_id in balance_cache:
        return balance_cache[user_id]
    
    cursor.execute("SELECT type, iv, encrypted_amount, tag FROM transactions ORDER BY date DESC")
    rows = cursor.fetchall()
    total_income, total_expense = 0, 0
    for row in rows:
        amount = await decrypt_amount(row[1], row[2], row[3], user_id)
        if row[0] == 'add_income':
            total_income += amount
        else:
            total_expense += amount
    balance = total_income - total_expense
    balance_cache[user_id] = (total_income, total_expense, balance)
    if len(balance_cache) >= MAX_CACHE_SIZE:
        balance_cache.pop(next(iter(balance_cache)))
    return total_income, total_expense, balance

# تبدیل تاریخ به شمسی
def to_jalali(dt):
    j_date = jdatetime.datetime.fromgregorian(datetime=dt)
    return j_date

# ساخت نمودار
async def generate_chart(user_id, cursor):
    cursor.execute("SELECT type, iv, encrypted_amount, tag, date FROM transactions ORDER BY date")
    rows = cursor.fetchall()
    if not rows:
        return None
    
    dates, balances = [], [0]
    total = 0
    for row in rows:
        amount = await decrypt_amount(row[1], row[2], row[3], user_id)
        total += amount if row[0] == 'add_income' else -amount
        dt = datetime.strptime(row[4], '%Y-%m-%d %H:%M:%S')
        dates.append(dt)
        balances.append(total)
    
    jalali_dates = [to_jalali(dt) for dt in dates]
    plt.figure(figsize=(14, 7), dpi=100)
    plt.plot(dates, balances[1:], marker='o', linestyle='-', color='#1f77b4', linewidth=2, markersize=8)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))
    plt.gca().xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=45)
    num_points = len(dates)
    step = max(1, num_points // 5)
    for i, (dt, j_date, balance) in enumerate(zip(dates, jalali_dates, balances[1:])):
        if i % step == 0:
            plt.annotate(
                f'{balance:,.0f} تومن\n{j_date.strftime("%Y/%m/%d %H:%M")}',
                (dt, balance),
                textcoords="offset points",
                xytext=(0, 15),
                ha='center',
                fontsize=10,
                bbox=dict(boxstyle="round,pad=0.3", edgecolor="gray", facecolor="white")
            )
    plt.title(f'نمودار تغییرات جیب {user_id}', fontsize=14, pad=20)
    plt.xlabel('تاریخ (میلادی برای محور، شمسی در برچسب‌ها)', fontsize=12)
    plt.ylabel('موجودی (تومن)', fontsize=12)
    plt.grid(True, linestyle='--', alpha=0.7)
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmpfile:
        chart_path = tmpfile.name
        plt.savefig(chart_path, bbox_inches='tight')
        plt.close()
    return chart_path

# منوی اصلی
main_menu = ReplyKeyboardMarkup([
    ['💰 بزن به جیب!', '💸 خرج کن حالشو ببر!'],
    ['📜 تاریخچه باحالم', '💎 جیبات چقدر پره؟'],
    ['📊 نمودار خفنم', '⏰ تاریخ و ساعت الان'],
    ['🌟 چجوری کار می‌کنم؟', '🔒 امنیت فول‌خفن'],
    ['🗑️ پاک کن همه‌چیز', '📞 پشتیبانی خفن']
], resize_keyboard=True, one_time_keyboard=False)

# منوی ادمین
admin_menu = ReplyKeyboardMarkup([
    ['💰 بزن به جیب!', '💸 خرج کن حالشو ببر!'],
    ['📜 تاریخچه باحالم', '💎 جیبات چقدر پره؟'],
    ['📊 نمودار خفنم', '⏰ تاریخ و ساعت الان'],
    ['🌟 چجوری کار می‌کنم؟', '🔒 امنیت فول‌خفن'],
    ['🗑️ پاک کن همه‌چیز', '📞 پشتیبانی خفن'],
    ['👥 لیست کاربران خفن']
], resize_keyboard=True, one_time_keyboard=False)

# تابع شروع
async def start(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or "بدون یوزرنیم"
    active_nodes.add(user_id)
    await register_user(user_id, username)
    menu = admin_menu if user_id == ADMIN_ID else main_menu
    await update.message.reply_text(
        "🌠 سلام رفیق باحالم! به ربات جیب‌گردونم خوش اومدی! 😎\n"
        "اینجا جیباتو با کمک همه رفقا مدیریت می‌کنم و انقدر خفنم که خودت کیف می‌کنی!\n"
        "با این دکمه‌های باحالم بترکون:",
        reply_markup=menu
    )

# مدیریت پیام‌ها
async def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    text = update.message.text
    conn, cursor = await get_user_db(user_id)
    menu = admin_menu if user_id == ADMIN_ID else main_menu
    
    try:
        if text == '💰 بزن به جیب!':
            user_cache[user_id] = {'action': 'add_income'}
            await update.message.reply_text(
                "💰 چقدر پول به جیبات اضافه کردی، رفیق؟ یه عدد بگو (مثلاً 2000):",
                reply_markup=menu
            )
        elif text == '💸 خرج کن حالشو ببر!':
            user_cache[user_id] = {'action': 'add_expense'}
            await update.message.reply_text(
                "💸 چقدر می‌خوای خرج کنی و حالشو ببری؟ یه عدد بگو (مثلاً 500):",
                reply_markup=menu
            )
        elif text == '📜 تاریخچه باحالم':
            cursor.execute("SELECT type, iv, encrypted_amount, tag, description, date FROM transactions ORDER BY date DESC")
            rows = cursor.fetchall()
            if rows:
                message = "📜 تاریخچه باحالت اینجاست، رفیق!\n🔥 همه تراکنشاتو برات آوردم:\n━━━━━━━━━━━━━━━\n"
                for row in rows:
                    amount = await decrypt_amount(row[1], row[2], row[3], user_id)
                    type_text = "💰 پول اومد" if row[0] == 'add_income' else "💸 پول رفت"
                    dt = datetime.strptime(row[5], '%Y-%m-%d %H:%M:%S')
                    j_date = to_jalali(dt)
                    message += f"{type_text} | {amount:,} تومن | {row[4]} | 📅 {j_date.strftime('%Y/%m/%d %H:%M')}\n"
                message += "━━━━━━━━━━━━━━━\nچی دیگه می‌خوای ببینی، پادشاه جیب‌ها؟ 😎"
                await update.message.reply_text(message, reply_markup=menu)
            else:
                await update.message.reply_text(
                    "🌌 هنوز هیچی ثبت نکردی، داداش! یه پول بزن به جیبات تا بترکونیم!",
                    reply_markup=menu
                )
        elif text == '💎 جیبات چقدر پره؟':
            total_income, total_expense, balance = await calculate_balance_distributed(user_id, cursor, context)
            await update.message.reply_text(
                f"💎 جیبات اینجوریه، رفیق باحالم:\n"
                f"💰 کل پولی که اومده: {total_income:,} تومن\n"
                f"➡️ الان تو جیبات: {balance:,} تومن\n"
                f"{'🎉 جیبات پره، بترکون!' if balance > 0 else '😅 جیبات خالیه، یه پول بزن!'}",
                reply_markup=menu
            )
        elif text == '📊 نمودار خفنم':
            chart_path = await generate_chart(user_id, cursor)
            if chart_path:
                with open(chart_path, 'rb') as chart:
                    await retry_on_network_error(
                        lambda: update.message.reply_photo(
                            photo=chart,
                            caption="📊 اینم نمودار خفنت، رفیق! تغییرات جیباتو ببین و حال کن!"
                        )
                    )
                os.remove(chart_path)
            else:
                await update.message.reply_text(
                    "🌌 هنوز تراکنشی نداری که نمودارش کنم، یه پول بزن به جیبات!",
                    reply_markup=menu
                )
        elif text == '⏰ تاریخ و ساعت الان':
            now = datetime.now()
            j_now = to_jalali(now)
            await update.message.reply_text(
                f"⏰ تاریخ و ساعت الان به شمسی:\n"
                f"{j_now.strftime('%Y/%m/%d %H:%M:%S')}",
                reply_markup=menu
            )
        elif text == '🌟 چجوری کار می‌کنم؟':
            await update.message.reply_text(
                "🌟 من چجوری کار می‌کنم، داداش؟\n"
                "خیلی ساده‌ست، گوش کن:\n"
                "۱. 💰 بزن به جیب: هر پولی که گرفتی رو بگو، من برات ثبتش می‌کنم.\n"
                "۲. 💸 خرج کن حالشو ببر: هر چی خرج کردی بگو، من کمش می‌کنم.\n"
                "۳. 📜 تاریخچه باحالم: همه تراکنشاتو از اول تا آخر نشون می‌دم.\n"
                "۴. 💎 جیبات چقدر پره: می‌گم چقدر پول اومده و چقدر الان داری.\n"
                "۵. 📊 نمودار خفنم: تغییرات جیباتو تو یه نمودار باحال نشون می‌دم.\n"
                "۶. ⏰ تاریخ و ساعت الان: تاریخ و ساعت فعلی رو بهت نشون می‌دم.\n"
                "۷. 🗑️ پاک کن همه‌چیز: اگه بخوای، همه‌چیزو صفر می‌کنم که از اول شروع کنی.\n"
                "۸. 📞 پشتیبانی خفن: هر مشکلی داشتی، بهم بگو تا درستش کنم!\n"
                "سوال داری؟ بگو تا بیشتر توضیح بدم، رفیق! 😎",
                reply_markup=menu
            )
        elif text == '🔒 امنیت فول‌خفن':
            await update.message.reply_text(
                "🔒 امنیت فول‌خفن من اینجوریه:\n"
                "۱. هر چی می‌نویسی رو با AES-256-GCM قفل می‌کنم که فقط خودت بتونی بازش کنی.\n"
                "۲. کلید قفل فقط از user_id تو ساخته می‌شه و دست هیچ‌کس نیست.\n"
                "۳. داده‌هات فقط تو دیتابیس خودت ذخیره می‌شه، خیالت تخت!\n"
                "سوال داری؟ بگو تا بیشتر بترکونم برات! 💪",
                reply_markup=menu
            )
        elif text == '🗑️ پاک کن همه‌چیز':
            cursor.execute("DELETE FROM transactions")
            conn.commit()
            balance_cache.pop(user_id, None)
            transaction_cache.pop(user_id, None)
            await update.message.reply_text(
                "🗑️ همه‌چیز پاک شد، رفیق! جیبات صفر شد، مثل روز اول!\n"
                "حالا از اول بترکون، چی کار می‌خوای بکنی؟ 😎",
                reply_markup=menu
            )
        elif text == '📞 پشتیبانی خفن':
            await update.message.reply_text(
                "📞 هر سوالی داری برو اینجا: @Ali202577_bot\n"
                "مستقیم بهم بگو تا سریع درستش کنم، رفیق! 😎",
                reply_markup=menu
            )
        elif text == '👥 لیست کاربران خفن' and user_id == ADMIN_ID:
            conn_users, cursor_users = get_users_db()
            cursor_users.execute("SELECT user_id, username, join_date FROM users ORDER BY join_date")
            rows = cursor_users.fetchall()
            if rows:
                message = "👥 لیست کاربران خفن:\n━━━━━━━━━━━━━━━\n"
                for row in rows:
                    j_date = to_jalali(datetime.strptime(row[2], '%Y-%m-%d %H:%M:%S'))
                    username = row[1] if row[1] else "بدون یوزرنیم"
                    message += f"🆔 {row[0]} | @{username} | 📅 {j_date.strftime('%Y/%m/%d %H:%M')}\n"
                message += "━━━━━━━━━━━━━━━\n"
                await update.message.reply_text(message, reply_markup=menu)
            else:
                await update.message.reply_text("🌌 هنوز هیچ کاربری نداریم!", reply_markup=menu)
            conn_users.close()
        else:
            if user_id in user_cache:
                action_data = user_cache[user_id]
                if 'amount' not in action_data:
                    try:
                        amount = float(text)
                        action_data['amount'] = amount
                        await update.message.reply_text(
                            "✍️ یه توضیح باحال بگو که یادم بمونه (مثلاً 'حقوق' یا 'پیتزا'):",
                            reply_markup=menu
                        )
                    except ValueError:
                        await update.message.reply_text(
                            "🤪 داداش، یه عدد درست بگو! مثلاً 1000 یا 500:",
                            reply_markup=menu
                        )
                else:
                    description = text if text.strip() else "بدون توضیح"
                    amount = action_data['amount']
                    if action_data['action'] == 'add_expense':
                        total_income, total_expense, balance = await calculate_balance_distributed(user_id, cursor, context)
                        if balance < amount:
                            await update.message.reply_text(
                                f"🚨 اوه اوه! پول کافی نداری، رفیق! فقط {balance:,} تومن تو جیباته!",
                                reply_markup=menu
                            )
                            user_cache.pop(user_id)
                            return
                    iv, encrypted_amount, tag = await encrypt_amount(amount, user_id)
                    cursor.execute(
                        "INSERT INTO transactions (type, iv, encrypted_amount, tag, description) VALUES (?, ?, ?, ?, ?)",
                        (action_data['action'], iv, encrypted_amount, tag, description)
                    )
                    conn.commit()
                    balance_cache.pop(user_id, None)
                    transaction_cache.pop(user_id, None)
                    type_text = "پول اومد" if action_data['action'] == 'add_income' else "پول رفت"
                    await update.message.reply_text(
                        f"🎉 ثبت شد، رفیق باحالم!\n"
                        f"💰 {type_text}: {amount:,} تومن\n"
                        f"✍️ توضیح: {description}\n"
                        "حالا چی کار می‌خوای بکنی؟",
                        reply_markup=menu
                    )
                    user_cache.pop(user_id)
    finally:
        conn.close()

# راه‌اندازی ربات
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()
