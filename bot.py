#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced NEXBIT BOT - full rewrite
Features:
 - Admin system (ADMIN_ID)
 - Auto market push (symbols)
 - WS thread (OKX preferred, fallback to Binance REST)
 - Heartbeat / process monitoring
 - Safe logging (RotatingFileHandler)
 - Admin commands: /status, /restart, /log
 - Original user commands preserved with original reply content
 - Self-recovering polling loop
"""

import os
import sys
import time
import json
import logging
import threading
import traceback
import requests
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
import telebot
import websocket

# ----------------------------
# CONFIG (use environment variables in production)
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # set in Railway variables
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)  # set in Railway variables
MARKET_PUSH_CHAT_ID = int(os.getenv("MARKET_PUSH_CHAT_ID", str(ADMIN_ID)) or ADMIN_ID)

# Symbols to monitor (user requested)
SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "DOGE-USDT", "SOL-USDT",
    "BNB-USDT", "XRP-USDT", "TRX-USDT", "USDC-USDT"
]

# Provide lowercase symbol forms for exchanges:
BINANCE_SYMBOLS = [s.replace("-", "").lower() for s in SYMBOLS]  # e.g. btcusdt
OKX_SYMBOLS = [s.replace("-", "-").lower() for s in SYMBOLS]    # OKX uses e.g. BTC-USDT

# Logging / files
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

# Timing settings
MARKET_PUSH_INTERVAL = int(os.getenv("MARKET_PUSH_INTERVAL", "300"))  # seconds
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))  # seconds
WS_RECONNECT_DELAY = 5  # seconds

# ----------------------------
# Ensure logs directory exists (safe check)
# ----------------------------
try:
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
except Exception:
    # fallback, if exists or race condition, ignore
    pass

# ----------------------------
# Configure logger
# ----------------------------
logger = logging.getLogger("nexbit_bot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=3 * 1024 * 1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# ----------------------------
# Basic state
# ----------------------------
START_TIME = datetime.utcnow()
STATE = {
    "okx_ws": False,
    "binance_ws": False,
    "last_market": {},
    "last_push": None,
    "heartbeat": None,
}

# ----------------------------
# Initialize bot
# ----------------------------
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Set environment variable BOT_TOKEN.")
    raise RuntimeError("BOT_TOKEN is required")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ----------------------------
# Utility helpers
# ----------------------------
def is_admin(chat_id: int) -> bool:
    return chat_id == ADMIN_ID

def uptime() -> str:
    delta = datetime.utcnow() - START_TIME
    return str(delta).split(".")[0]

def tail(filepath: str, lines: int = 200) -> str:
    """Read last N lines of a file."""
    if not os.path.isfile(filepath):
        return "Log file not found."
    try:
        with open(filepath, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            size = 1024
            data = b""
            while lines > 0 and end > 0:
                step = min(size, end)
                f.seek(end - step)
                chunk = f.read(step)
                data = chunk + data
                end -= step
                lines -= chunk.count(b"\n")
            text = data.decode("utf-8", errors="ignore")
            lines_list = text.strip().splitlines()[-200:]
            return "\n".join(lines_list)
    except Exception as e:
        return f"Error reading log: {e}"

# ----------------------------
# Market functions (simple REST fallback + optional WS)
# ----------------------------
def fetch_binance_prices(symbols):
    """Fetch prices from Binance REST API (simple)."""
    out = {}
    for s in symbols:
        try:
            pair = s.replace("-", "").lower()
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair.upper()}"
            r = requests.get(url, timeout=6)
            if r.status_code == 200:
                data = r.json()
                out[s] = float(data.get("price", 0.0))
            else:
                out[s] = None
        except Exception as e:
            logger.warning("fetch_binance_prices error for %s: %s", s, e)
            out[s] = None
    return out

def format_market_message(market_dict):
    lines = []
    for s in SYMBOLS:
        price = market_dict.get(s)
        if price is None:
            lines.append(f"{s}: N/A")
        else:
            # show a bit prettier
            if price >= 1:
                lines.append(f"{s}: {price}")
            else:
                lines.append(f"{s}: {price:.6f}")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return "```\n" + "\n".join(lines) + f"\n``` \n_Timestamp: {ts}_"

# ----------------------------
# OKX WS (lightweight subscriber) - attempt but fallback to REST loop
# ----------------------------
def okx_ws_loop():
    """
    Minimal OKX WebSocket connector to demonstrate (public).
    If blocked (geolocation), we'll fallback gracefully.
    """
    try:
        STATE["okx_ws"] = False
        url = "wss://ws.okx.com:8443/ws/v5/public"
        def on_open(ws):
            try:
                logger.info("OKX WS opened, subscribing...")
                # subscribe ticker for requested instruments
                insts = [{"instId": s} for s in SYMBOLS]
                params = {"op": "subscribe", "args": [{"channel": "tickers", "instId": s} for s in SYMBOLS]}
                ws.send(json.dumps(params))
                STATE["okx_ws"] = True
                logger.info("OKX WS subscription sent.")
            except Exception as e:
                logger.exception("OKX on_open error: %s", e)

        def on_message(ws, message):
            try:
                obj = json.loads(message)
                # parse tickers updates
                if "data" in obj and isinstance(obj["data"], list):
                    for item in obj["data"]:
                        instId = item.get("instId")
                        last = item.get("last")
                        if instId and last:
                            key = instId.replace("-", "-").upper()
                            STATE["last_market"][key] = float(last)
            except Exception:
                logger.debug("OKX message parse fail: %s", traceback.format_exc())

        def on_error(ws, err):
            logger.warning("OKX WS error: %s", err)

        def on_close(ws, code, reason):
            STATE["okx_ws"] = False
            logger.info("OKX WS closed - %s %s", code, reason)

        ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        ws.run_forever(ping_interval=20, ping_timeout=10)
    except Exception:
        logger.exception("OKX WS loop crashed.")
    finally:
        STATE["okx_ws"] = False
        logger.info("OKX WS loop exiting, will retry after delay.")
        time.sleep(WS_RECONNECT_DELAY)

# ----------------------------
# Background threads
# ----------------------------
def heartbeat_thread():
    while True:
        try:
            STATE["heartbeat"] = datetime.utcnow().isoformat()
            logger.info("Heartbeat: alive; uptime=%s", uptime())
        except Exception:
            logger.exception("Heartbeat exception")
        time.sleep(HEARTBEAT_INTERVAL)

def market_push_thread():
    """Periodically fetch market and push to configured chat."""
    while True:
        try:
            # Prefer REST (simple)
            prices = fetch_binance_prices(SYMBOLS)
            STATE["last_market"].update(prices)
            msg = format_market_message(STATE["last_market"])
            # send to target
            try:
                bot.send_message(MARKET_PUSH_CHAT_ID, f"📈 *Market Snapshot*\n{msg}")
            except Exception as e:
                logger.warning("Failed to send market snapshot: %s", e)
            STATE["last_push"] = datetime.utcnow().isoformat()
        except Exception:
            logger.exception("market_push_thread error")
        time.sleep(MARKET_PUSH_INTERVAL)

def okx_ws_runner():
    """Run OKX WS loop with auto-reconnect."""
    while True:
        try:
            okx_ws_loop()
        except Exception:
            logger.exception("okx_ws_runner exception")
        time.sleep(WS_RECONNECT_DELAY)

# ----------------------------
# Admin commands & common commands (preserve original replies)
# ----------------------------

# Keep original text blocks as in user's earlier file (translated where necessary)
START_TEXT = """
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

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    bot.reply_to(msg, START_TEXT)

@bot.message_handler(commands=["market"])
def cmd_market(msg):
    text = "📊 **Real-time Market Data**\n\n"
    for s in SYMBOLS:
        p = STATE["last_market"].get(s)
        if p is None:
            text += f"{s}: Retrieving...\n"
        else:
            text += f"{s}: {p}\n"
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

@bot.message_handler(commands=["Register", "register"])
def cmd_register(msg):
    bot.reply_to(msg, "📝 **Registration Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Deposit", "deposit"])
def cmd_deposit(msg):
    bot.reply_to(msg, "💰 **Deposit Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Withdraw", "withdraw"])
def cmd_withdraw(msg):
    bot.reply_to(msg, "💵 **Withdraw Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Alert", "alert"])
def cmd_alert(msg):
    bot.reply_to(msg, "⏳ **Price alert feature coming soon...**")

@bot.message_handler(commands=["Bind", "bind"])
def cmd_bind(msg):
    bot.reply_to(msg, "⏳ **Bind feature coming soon...**")

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
# ADMIN commands
# ----------------------------
@bot.message_handler(commands=["status"])
def cmd_status(msg):
    chat_id = msg.from_user.id
    if not is_admin(chat_id):
        bot.reply_to(msg, "You are not authorized to use this command.")
        logger.warning("Unauthorized /status attempt by %s", chat_id)
        return
    info = {
        "uptime": uptime(),
        "start_time": START_TIME.isoformat(),
        "heartbeat": STATE.get("heartbeat"),
        "last_push": STATE.get("last_push"),
        "okx_ws": STATE.get("okx_ws"),
        "market_symbols": SYMBOLS,
    }
    txt = "*Bot Status*\n"
    for k, v in info.items():
        txt += f"{k}: `{v}`\n"
    bot.reply_to(msg, txt)

@bot.message_handler(commands=["log"])
def cmd_log(msg):
    chat_id = msg.from_user.id
    if not is_admin(chat_id):
        bot.reply_to(msg, "You are not authorized to use this command.")
        logger.warning("Unauthorized /log attempt by %s", chat_id)
        return
    try:
        parts = msg.text.strip().split()
        n = 200
        if len(parts) >= 2:
            try:
                n = int(parts[1])
            except:
                n = 200
        content = tail(LOG_FILE, lines=n)
        if len(content) > 3900:
            # send as file
            with open(LOG_FILE, "rb") as f:
                bot.send_document(chat_id, f)
        else:
            bot.reply_to(msg, f"```\n{content}\n```")
    except Exception:
        bot.reply_to(msg, "Failed to read log.")
        logger.exception("Failed to deliver logs")

@bot.message_handler(commands=["restart"])
def cmd_restart(msg):
    chat_id = msg.from_user.id
    if not is_admin(chat_id):
        bot.reply_to(msg, "You are not authorized to use this command.")
        logger.warning("Unauthorized /restart attempt by %s", chat_id)
        return
    bot.reply_to(msg, "Restarting bot... goodbye 👋")
    logger.info("Admin requested restart; exiting.")
    # allow reply to be sent
    time.sleep(1.0)
    os._exit(0)

# ----------------------------
# Fallback - unknown messages -> can implement autoresponder
# ----------------------------
@bot.message_handler(func=lambda m: True)
def catch_all(msg):
    # Example: auto reply simple keywords or pass
    text = msg.text.strip() if msg.text else ""
    # simple auto-responses:
    if "price" in text.lower():
        cmd_market(msg)
        return
    # else ignore or respond with menu
    # to avoid spam, do not reply to all messages
    return

# ----------------------------
# Start background threads
# ----------------------------
def start_background_services():
    logger.info("Starting background services...")
    # Heartbeat
    t_hb = threading.Thread(target=heartbeat_thread, daemon=True, name="heartbeat")
    t_hb.start()
    # Market push
    t_mp = threading.Thread(target=market_push_thread, daemon=True, name="market_push")
    t_mp.start()
    # OKX WS runner
    t_ws = threading.Thread(target=okx_ws_runner, daemon=True, name="okx_ws")
    t_ws.start()
    logger.info("Background threads started.")

# ----------------------------
# Main polling loop with auto-reconnect
# ----------------------------
def main_loop():
    logger.info("Program starting...")
    start_background_services()
    while True:
        try:
            logger.info("Starting bot polling...")
            bot.polling(non_stop=True, interval=0, timeout=20)
        except Exception as e:
            logger.exception("Polling exception, will restart polling in 3s: %s", e)
            time.sleep(3)

# ----------------------------
# Entry point
# ----------------------------
if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt - exiting")
    except SystemExit:
        logger.info("SystemExit - exiting")
    except Exception:
        logger.exception("Fatal error - exiting")
    finally:
        logger.info("Bot stopped.")
