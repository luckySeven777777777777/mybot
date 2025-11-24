#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced NEXBIT Bot - stable production-ready main script
Features:
- Multiple admins via ADMIN_IDS env var (comma separated)
- Market snapshot via OKX REST (periodic push)
- ASCII kline / sparkline style Market Snapshot
- Commands preserved from original (kept replies)
- Admin commands: /status, /restart, /log
- Crash protection, thread supervision, self-recovering tasks
- Rotating logs to logs/bot.log
- Uses environment variables for secrets (Railway)
"""

import os
import sys
import time
import json
import math
import logging
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import List, Dict

import requests
import telebot
from telebot import apihelper
from telebot.apihelper import ApiTelegramException

# -------------------------
# CONFIG (use environment variables in production)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()  # set in Railway variables
# ADMIN_IDS: comma separated list of chat IDs, e.g. "6062973135,6163182909"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
# If a single admin id used previously, keep fallback compatibility
if not ADMIN_IDS:
    try:
        fallback = int(os.getenv("ADMIN_ID", "0") or 0)
        if fallback:
            ADMIN_IDS = [fallback]
    except Exception:
        ADMIN_IDS = []

# Chat ID to push market snapshot to (if not set, default to first admin)
MARKET_PUSH_CHAT_ID = os.getenv("MARKET_PUSH_CHAT_ID", "")
try:
    MARKET_PUSH_CHAT_ID = int(MARKET_PUSH_CHAT_ID) if MARKET_PUSH_CHAT_ID else (ADMIN_IDS[0] if ADMIN_IDS else None)
except Exception:
    MARKET_PUSH_CHAT_ID = ADMIN_IDS[0] if ADMIN_IDS else None

# Symbols to monitor (user requested)
SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "DOGE-USDT", "SOL-USDT",
    "BNB-USDT", "XRP-USDT", "TRX-USDT", "USDC-USDT"
]

# Logging
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
os.makedirs(LOG_DIR, exist_ok=True)

# Market push interval (seconds)
MARKET_PUSH_INTERVAL = int(os.getenv("MARKET_PUSH_INTERVAL", "600"))  # default 600s -> 10min

# HTTP timeout
HTTP_TIMEOUT = 10

# OKX API endpoint (public market ticker)
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"

# Telebot config
TELEBOT_POLLING_TIMEOUT = 60
TELEBOT_LONG_POLL = True  # use long polling

# -------------------------
# Logging setup
# -------------------------
logger = logging.getLogger("nexbit_bot")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

logger.info("Logger initialized. LOG_FILE=%s", LOG_FILE)

# -------------------------
# Bot init
# -------------------------
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN is not set. Exiting.")
    raise SystemExit("BOT_TOKEN is required in environment variables.")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
apihelper.SESSION = requests.Session()

# -------------------------
# Helpers
# -------------------------
def admin_check(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def safe_send(chat_id, text, parse_mode=None, **kwargs):
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.exception("Failed to send message to %s: %s", chat_id, e)

def format_price(p):
    try:
        if isinstance(p, str):
            p = float(p)
        if p >= 1:
            return f"${p:,.2f}"
        else:
            # up to 6 decimals for tiny coins
            return f"${p:,.6f}".rstrip("0").rstrip(".")
    except Exception:
        return str(p)

# -------------------------
# Market snapshot (OKX REST)
# -------------------------
def fetch_okx_tickers(symbols: List[str]) -> Dict[str, dict]:
    """
    Returns a dict mapping symbol -> { 'last': price(float), 'ts': timestamp, 'instId': symbol }
    Uses OKX REST endpoints individually or in batch
    """
    out = {}
    for s in symbols:
        try:
            params = {"instId": s}
            r = requests.get(OKX_TICKER_URL, params=params, timeout=HTTP_TIMEOUT)
            j = r.json()
            # OKX returns data in j["data"][0] typically
            if j.get("code") == "0" and "data" in j and j["data"]:
                d = j["data"][0]
                last = float(d.get("last", d.get("lastPrice", 0)))
                t = int(d.get("ts", int(time.time() * 1000)))
                out[s] = {"last": last, "ts": t, "instId": s}
            else:
                # fall back to parse if different shape
                data = j.get("data")
                if isinstance(data, list) and len(data) > 0:
                    d = data[0]
                    last = float(d.get("last", 0))
                    out[s] = {"last": last, "ts": int(d.get("ts", time.time() * 1000)), "instId": s}
                else:
                    out[s] = {"last": None, "ts": int(time.time() * 1000), "instId": s}
        except Exception as e:
            logger.exception("Failed to fetch OKX ticker for %s: %s", s, e)
            out[s] = {"last": None, "ts": int(time.time() * 1000), "instId": s}
    return out

def build_sparkbars(values: Dict[str, float]) -> Dict[str, str]:
    """
    Build a simple bar graph mapping each symbol to "█▇▆▅▄▃▂▁" style sparkbars.
    Bars are scaled relative to current min/max in the set.
    """
    blocks = "█▇▆▅▄▃▂▁"
    names = list(values.keys())
    nums = [v for v in values.values() if v is not None]
    if not nums:
        return {k: "N/A" for k in names}
    mn = min(nums)
    mx = max(nums)
    rng = mx - mn if mx != mn else 1.0
    bars = {}
    for k, v in values.items():
        if v is None:
            bars[k] = "--------"
            continue
        # normalize 0..1
        norm = (v - mn) / rng
        # build 8-char bar from blocks based on norm
        length = int(round(norm * (len(blocks) * 2 - 1)))  # more granularity
        # convert to string of 8 characters selecting block by groups
        bar_chars = []
        for i in range(8):
            # position of this char in 0..(len(blocks)-1)
            pos = (length - (i * len(blocks) // 8))
            idx = max(0, min(len(blocks)-1, pos))
            # choose lighter block if idx small
            bar_chars.append(blocks[idx])
        bars[k] = "".join(bar_chars)
    return bars

def format_market_snapshot(tickers: Dict[str, dict]) -> str:
    # prepare values
    values = {s: (tickers.get(s, {}).get("last")) for s in SYMBOLS}
    bars = build_sparkbars(values)
    # prepare output lines
    lines = []
    for s in SYMBOLS:
        short = s.split("-")[0].ljust(6)
        price = values.get(s)
        price_str = "N/A" if price is None else format_price(price)
        bar = bars.get(s, "--------")
        lines.append(f"{short} │ {bar}  {price_str}")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    output = "📈 Market Snapshot (OKX REST)\n\n" + "\n".join(lines) + f"\n\n⏱ {ts}"
    return output

# -------------------------
# Background threads and supervisors
# -------------------------
stop_event = threading.Event()

def market_push_worker():
    logger.info("Market push worker started, interval=%ds", MARKET_PUSH_INTERVAL)
    while not stop_event.is_set():
        try:
            tickers = fetch_okx_tickers(SYMBOLS)
            msg = format_market_snapshot(tickers)
            if MARKET_PUSH_CHAT_ID:
                safe_send(MARKET_PUSH_CHAT_ID, f"```\n{msg}\n```", parse_mode="Markdown")
                logger.info("Pushed market snapshot to %s", MARKET_PUSH_CHAT_ID)
            else:
                logger.warning("MARKET_PUSH_CHAT_ID not configured, skipping push.")
        except Exception as e:
            logger.exception("Exception in market_push_worker: %s", e)
        # wait with early exit
        for _ in range(int(max(1, MARKET_PUSH_INTERVAL))):
            if stop_event.is_set():
                break
            time.sleep(1)
    logger.info("Market push worker stopped.")

def heartbeat_worker():
    idx = 0
    while not stop_event.is_set():
        logger.info("Heartbeat: alive; uptime=%s", time.strftime("%H:%M:%S", time.gmtime(time.time())))
        # rotate log small message; nothing else
        time.sleep(60)

# Thread supervisor to restart threads if they die
class ThreadSupervisor(threading.Thread):
    def __init__(self, target, name):
        super().__init__(daemon=True)
        self.target = target
        self.name = name
        self._worker = None

    def run(self):
        while not stop_event.is_set():
            try:
                logger.info("Starting supervised thread: %s", self.name)
                self.target()
            except Exception:
                logger.exception("Supervised thread %s crashed, restarting in 5s...", self.name)
                time.sleep(5)
        logger.info("Supervisor stopping for %s", self.name)

# -------------------------
# Bot command handlers
# -------------------------
# Preserve original replies content when possible
WELCOME_TEXT = """🤖 **Welcome to NEXBIT-BOT** 🤖

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
    try:
        bot.reply_to(msg, WELCOME_TEXT, parse_mode="Markdown")
    except Exception:
        logger.exception("cmd_start error")

@bot.message_handler(commands=["market"])
def cmd_market(msg):
    try:
        tickers = fetch_okx_tickers(SYMBOLS)
        content = "📊 **Real-time Market Data**\n\n"
        for s in SYMBOLS:
            last = tickers.get(s, {}).get("last")
            content += f"{s}: {format_price(last) if last is not None else 'N/A'}\n"
        bot.reply_to(msg, content, parse_mode="Markdown")
    except Exception:
        logger.exception("cmd_market error")
        bot.reply_to(msg, "Failed to fetch market data.")

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

@bot.message_handler(commands=["register"])
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
    bot.reply_to(msg, "⏳ **Bind/wallet feature coming soon...**")

@bot.message_handler(commands=["support", "Support"])
def cmd_support(msg):
    text = """
💬 **Customer Support**
• 24-hour online customer service
• Telegram: https://t.me/monsterman197  
• Email: lucky077779999@gmail.com
"""
    bot.reply_to(msg, text)

# -------------------------
# Admin commands
# -------------------------
@bot.message_handler(commands=["status"])
def cmd_status(msg):
    user_id = msg.from_user.id
    if not admin_check(user_id):
        return
    try:
        uptime = "N/A"
        # create a simple status
        status = {
            "uptime": uptime,
            "admins": ADMIN_IDS,
            "market_push_target": MARKET_PUSH_CHAT_ID,
            "symbols": SYMBOLS,
        }
        bot.reply_to(msg, "Status:\n" + json.dumps(status, indent=2), parse_mode="Markdown")
    except Exception:
        logger.exception("cmd_status error")

@bot.message_handler(commands=["log", "logs"])
def cmd_log(msg):
    user_id = msg.from_user.id
    if not admin_check(user_id):
        return
    try:
        # send last part of log file
        if os.path.exists(LOG_FILE):
            size = os.path.getsize(LOG_FILE)
            tail_bytes = 100 * 1024  # last 100KB
            with open(LOG_FILE, "rb") as f:
                if size > tail_bytes:
                    f.seek(-tail_bytes, os.SEEK_END)
                data = f.read().decode(errors="ignore")
            bot.reply_to(msg, f"Last logs (truncated):\n```\n{data}\n```", parse_mode="Markdown")
        else:
            bot.reply_to(msg, "No log file found.")
    except Exception:
        logger.exception("cmd_log error")
        bot.reply_to(msg, "Failed to read logs.")

@bot.message_handler(commands=["restart"])
def cmd_restart(msg):
    user_id = msg.from_user.id
    if not admin_check(user_id):
        bot.reply_to(msg, "You are not authorized.")
        return
    try:
        bot.reply_to(msg, "Restarting bot (graceful exit)...")
        logger.warning("Admin requested restart by %s", user_id)
        # give some time for message to send
        def _delayed_exit():
            time.sleep(1.5)
            # exit so Railway restarts the container
            os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()
    except Exception:
        logger.exception("cmd_restart error")


# -------------------------
# Misc handlers
# -------------------------
@bot.message_handler(func=lambda m: True)
def echo_all(msg):
    # Basic unknown command intercept; optionally block strangers use of certain commands
    text = msg.text or ""
    if text.startswith("/"):
        # unknown command
        if not admin_check(msg.from_user.id):
            # auto-ignore certain sensitive commands from strangers
            (bot.reply_to(msg, "Command not recognized or you are not authorized."), logger.info("Blocked unknown command from %s: %s", msg.from_user.id, text))
        else:
            bot.reply_to(msg, "Command not recognized (admin).")
    else:
        # optional auto-reply patterns
        pass


# -------------------------
# Polling wrapper (handles 409 conflict)
# -------------------------
def start_bot_polling():
    logger.info("Starting bot polling loop...")
    while not stop_event.is_set():
        try:
            # Remove webhook if any and start polling
            try:
                bot.remove_webhook()
            except Exception:
                pass
            logger.info("Bot polling starting (long_poll=%s timeout=%s)", TELEBOT_LONG_POLL, TELEBOT_POLLING_TIMEOUT)
            bot.infinity_polling(timeout=TELEBOT_POLLING_TIMEOUT, long_polling_timeout=TELEBOT_POLLING_TIMEOUT)
        except ApiTelegramException as e:
            # 409 means another getUpdates in progress -> wait
            try:
                status_code = getattr(e, "result_json", {}).get("error_code", None)
            except Exception:
                status_code = None
            logger.exception("ApiTelegramException while polling: %s. Sleeping 10s and retrying...", e)
            time.sleep(10)
        except Exception as e:
            logger.exception("Unhandled exception in polling loop: %s. Sleeping 5s and retrying...", e)
            time.sleep(5)
    logger.info("Bot polling stopped.")

# -------------------------
# Start background services
# -------------------------
def main():
    logger.info("Program starting...")
    # start supervised threads
    market_supervisor = ThreadSupervisor(target=market_push_worker, name="market_push_worker")
    market_supervisor.start()
    heartbeat_supervisor = ThreadSupervisor(target=heartbeat_worker, name="heartbeat_worker")
    heartbeat_supervisor.start()

    # start bot polling in main thread or another thread
    polling_thread = threading.Thread(target=start_bot_polling, name="bot_polling_thread", daemon=True)
    polling_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
    except Exception:
        logger.exception("Main loop exception")
    finally:
        stop_event.set()
        logger.info("Waiting briefly for threads to stop...")
        time.sleep(2)
        logger.info("Exiting.")
        os._exit(0)

if __name__ == "__main__":
    main()
