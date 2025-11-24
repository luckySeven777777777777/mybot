# bot.py (Enhanced version)
import telebot
import json
import time
import threading
import websocket
import logging
import os
import sys
import traceback
from datetime import datetime
import psutil  # pip install psutil

# ----------------------------
# Configuration (replace or use env variables)
# ----------------------------
# You gave these values; for production prefer to use environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8344095901:AAEZUTB0FZQooWVsIK1p-cTg_3lu6ARR4Ec")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6062973135"))  # your TG ID

# Log file path
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
os.makedirs(LOG_DIR, exist_ok=True)

# ----------------------------
# Logging setup
# ----------------------------
logger = logging.getLogger("mybot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(formatter)
logger.addHandler(fh)

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(formatter)
logger.addHandler(sh)

# ----------------------------
# Bot init
# ----------------------------
bot = telebot.TeleBot(BOT_TOKEN)
START_TIME = time.time()

# Markets to watch (OKX format SYMBOL-USDT)
WATCH_SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "DOGE-USDT", "SOL-USDT",
    "BNB-USDT", "XRP-USDT", "TRX-USDT", "USDC-USDT"
]

# Shared market state
MARKET = {s: {"price": "Retrieving..."} for s in WATCH_SYMBOLS}
MARKET_LOCK = threading.Lock()

# ----------------------------
# Helper functions
# ----------------------------
def tail_file(path, n_lines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            block_size = 1024
            data = b""
            lines = []
            while file_size > 0 and len(lines) < n_lines:
                read_size = min(block_size, file_size)
                f.seek(file_size - read_size)
                chunk = f.read(read_size) + data
                lines = chunk.splitlines()
                file_size -= read_size
                data = chunk
            text = b"\n".join(lines[-n_lines:]).decode("utf-8", errors="replace")
            return text
    except Exception as e:
        return f"Error reading log: {e}"

def is_admin(user_id):
    return int(user_id) == int(ADMIN_ID)

def safe_send_message(chat_id, text, **kwargs):
    try:
        bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.exception("Failed to send message: %s", e)

def uptime():
    seconds = int(time.time() - START_TIME)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m}m {s}s"

# ----------------------------
# OKX WebSocket Thread
# ----------------------------
OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

def build_okx_subscribe():
    args = []
    # subscribe tickers for each symbol
    for s in WATCH_SYMBOLS:
        args.append({"channel": "tickers", "instId": s})
    return {"op": "subscribe", "args": args}

def on_okx_open(ws):
    logger.info("OKX WebSocket connected, subscribing...")
    sub = build_okx_subscribe()
    try:
        ws.send(json.dumps(sub))
    except Exception as e:
        logger.exception("Failed to send subscribe: %s", e)

def on_okx_message(ws, message):
    try:
        data = json.loads(message)
        # OKX sends {"arg": {...}} and {"data":[{...}]}
        if "data" in data and isinstance(data["data"], list) and len(data["data"])>0:
            item = data["data"][0]
            # tickers channel: item has 'instId' and 'last' or 'lastSz' or 'lastPx' depending
            inst = item.get("instId")
            price = item.get("last") or item.get("lastSz") or item.get("px") or item.get("lastPx") or item.get("last")
            if inst and price:
                with MARKET_LOCK:
                    if inst in MARKET:
                        MARKET[inst]["price"] = str(price)
    except Exception as e:
        logger.exception("OKX parse error: %s", e)

def on_okx_error(ws, error):
    logger.error("OKX WebSocket error: %s", error)

def on_okx_close(ws, code, reason):
    logger.warning("OKX WebSocket closed: %s / %s. Reconnecting in 5s...", code, reason)
    time.sleep(5)
    start_okx_ws()

def start_okx_ws():
    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    OKX_WS_URL,
                    on_open=lambda w: on_okx_open(w),
                    on_message=lambda w, m: on_okx_message(w, m),
                    on_error=lambda w, e: on_okx_error(w, e),
                    on_close=lambda w, c, r: on_okx_close(w, c, r)
                )
                # run_forever blocks, will return on close/error
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.exception("OKX WS thread exception: %s", e)
            logger.info("OKX WS restarting in 5 seconds...")
            time.sleep(5)
    t = threading.Thread(target=run, daemon=True, name="okx-ws")
    t.start()
    logger.info("OKX WS thread started.")

# ----------------------------
# Heartbeat / keepalive thread
# ----------------------------
def heartbeat_loop():
    while True:
        try:
            # light heartbeat to keep session alive
            bot.get_me()
            logger.debug("Heartbeat OK")
        except Exception as e:
            logger.exception("Heartbeat failed, will continue: %s", e)
        time.sleep(30)

def start_heartbeat():
    t = threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    logger.info("Heartbeat thread started.")

# ----------------------------
# Admin commands & helpers
# ----------------------------
def require_admin(func):
    def wrapper(message, *args, **kwargs):
        uid = message.from_user.id
        if not is_admin(uid):
            logger.warning("Unauthorized admin attempt: %s", uid)
            safe_send_message(message.chat.id, "🔒 只有管理员可以执行此命令。")
            return
        return func(message, *args, **kwargs)
    return wrapper

# ----------------------------
# User commands (kept original replies)
# ----------------------------
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    text = """
🤖 **Welcome to NEXBIT-BOT** 🤖

Available Commands:
/register - Registration
/market - View Real-Time Market Data
/analysis - Market analysis
/safe - Security tips
/deposit - Deposit Now
/Bind - Link wallet address
/withdraw - Withdraw Now
/mobile - Mobile Version
/feature - Platform Features 
/support - Customer Support
/alert - Price alert (coming soon)
"""
    bot.reply_to(msg, text)

@bot.message_handler(commands=["market"])
def cmd_market(msg):
    text = "📊 **Real-time Market Data**\n\n"
    with MARKET_LOCK:
        for s in WATCH_SYMBOLS:
            text += f"{s}: {MARKET[s]['price']}\n"
    bot.reply_to(msg, text)

@bot.message_handler(commands=["analysis"])
def cmd_analysis(msg):
    text = """
📈 **Market Analysis**
• Increased volatility observed
• BTC is testing a key support level
• Monitor major coins closely
• Market sentiment: Neutral → Bullish
"""
    bot.reply_to(msg, text)

@bot.message_handler(commands=["safe"])
def cmd_safe(msg):
    text = """
🛡 **Security Tips**
• Do NOT click unknown links
• Never disclose your seed phrase/private key
• Beware of phishing websites
• Official support will NEVER ask for your password
"""
    bot.reply_to(msg, text)

@bot.message_handler(commands=["mobile"])
def cmd_mobile(msg):
    text = """
📱 **Mobile App Guide**
• Web version recommended
• Supports Android & iOS
• Enable Face ID / Fingerprint for safety
• Keep app up to date
"""
    bot.reply_to(msg, text)

@bot.message_handler(commands=["feature"])
def cmd_feature(msg):
    text = """
✨ **Platform Features**
• Real-time market data
• Automatic analysis
• Advanced alerts
• In-depth data push
"""
    bot.reply_to(msg, text)

@bot.message_handler(commands=["Register"])
def cmd_register(msg):
    bot.reply_to(msg, "📝 **Registration Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Deposit"])
def cmd_deposit(msg):
    bot.reply_to(msg, "💰 **Deposit Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Withdraw"])
def cmd_withdraw(msg):
    bot.reply_to(msg, "💵 **Withdraw Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Alert"])
def cmd_alert(msg):
    bot.reply_to(msg, "⏳ **Price alert feature coming soon...**")

@bot.message_handler(commands=["Bind"])
def cmd_bind(msg):
    bot.reply_to(msg, "⏳ **Price alert feature coming soon...**")

@bot.message_handler(commands=["support"])
def cmd_support(msg):
    text = """
💬 **Customer Support**
• 24-hour online customer service
• Telegram: https://t.me/monsterman197  
• Email: lucky077779999@gmail.com
"""
    bot.reply_to(msg, text)

# ----------------------------
# Admin-only commands
# ----------------------------
@bot.message_handler(commands=["status"])
@require_admin
def cmd_status(msg):
    p = psutil.Process(os.getpid())
    mem = p.memory_info().rss / (1024*1024)
    threads = threading.active_count()
    text = f"✅ Running\nUptime: {uptime()}\nMemory: {mem:.1f} MB\nThreads: {threads}\nWatch symbols: {', '.join(WATCH_SYMBOLS)}"
    bot.reply_to(msg, text)

@bot.message_handler(commands=["log"])
@require_admin
def cmd_log(msg):
    tail = tail_file(LOG_FILE, n_lines=300)
    # If too long, send as file
    if len(tail) > 3500:
        with open(LOG_FILE, "rb") as f:
            bot.send_document(msg.chat.id, f)
    else:
        bot.reply_to(msg, f"📝 Last logs:\n\n{tail}")

@bot.message_handler(commands=["restart"])
@require_admin
def cmd_restart(msg):
    bot.reply_to(msg, "🔄 Restarting bot now...")
    logger.info("Admin requested restart. Exiting process to allow restart.")
    # flush log handlers
    for h in logger.handlers:
        try:
            h.flush()
        except:
            pass
    time.sleep(1)
    # exit; platform (Railway) will restart process
    os._exit(0)

# ----------------------------
# Generic error handling wrapper for handlers
# ----------------------------
def handler_safe(fn):
    def wrapped(message):
        try:
            return fn(message)
        except Exception as e:
            logger.exception("Handler error: %s", e)
            try:
                bot.reply_to(message, "🚨 机器人发生错误，管理员已被通知。")
            except:
                pass
    return wrapped

# apply wrapper to handlers that need it (if you add more handlers, wrap them)
# (In this file we've defined handlers already; for new ones, use decorator or wrapper)

# ----------------------------
# Start background threads & bot polling
# ----------------------------
def start_background():
    logger.info("Starting background services...")
    start_okx_ws()
    start_heartbeat()

def run_bot_polling():
    # keep polling forever, auto-reconnect on exceptions
    while True:
        try:
            logger.info("Starting bot polling...")
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.exception("Polling exception, will sleep and restart: %s", e)
            time.sleep(5)

# ----------------------------
# Entry point
# ----------------------------
if __name__ == "__main__":
    logger.info("Program starting...")
    try:
        start_background()

        # Start polling in main thread (so os._exit will work on restart)
        run_bot_polling()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, exiting.")
        sys.exit(0)
    except SystemExit:
        logger.info("SystemExit called, exiting.")
        raise
    except Exception:
        logger.exception("Unhandled exception in main, exiting in 3s.")
        time.sleep(3)
        # exit to let platform restart
        os._exit(1)
