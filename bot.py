#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Final enhanced NEXBIT bot.py
Features:
 - Multiple admins (ADMIN_IDS env var, comma separated)
 - Auto show group ID in /start
 - Auto bind group for market push: use /bindgroup in group (admin only)
 - Market snapshot via OKX REST (ASCII sparkbars)
 - Periodic push to configured MARKET_PUSH_CHAT_ID
 - Admin commands: /status, /restart, /log, /addadmin, /deladmin, /admins, /bindgroup
 - Safe handling of 409 errors and auto-restart behavior
 - Rotating file logs
"""

import os
import sys
import time
import json
import logging
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import List, Dict

import requests
import telebot
from telebot.apihelper import ApiTelegramException

# -------------------------
# CONFIG (env)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")  # e.g. "6062973135,6163182909"
ADMIN_IDS: List[int] = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
# fallback single var compatibility
if not ADMIN_IDS:
    try:
        single = int(os.getenv("ADMIN_ID", "0") or 0)
        if single:
            ADMIN_IDS = [single]
    except Exception:
        ADMIN_IDS = []

MARKET_PUSH_CHAT_ID_ENV = os.getenv("MARKET_PUSH_CHAT_ID", "").strip()
try:
    MARKET_PUSH_CHAT_ID = int(MARKET_PUSH_CHAT_ID_ENV) if MARKET_PUSH_CHAT_ID_ENV else (ADMIN_IDS[0] if ADMIN_IDS else None)
except Exception:
    MARKET_PUSH_CHAT_ID = ADMIN_IDS[0] if ADMIN_IDS else None

# Symbols to monitor
SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "DOGE-USDT", "SOL-USDT",
    "BNB-USDT", "XRP-USDT", "TRX-USDT", "USDC-USDT"
]

# OKX REST
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))

# push interval seconds (default 600s)
MARKET_PUSH_INTERVAL = int(os.getenv("MARKET_PUSH_INTERVAL", "600"))

# Logging
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
os.makedirs(LOG_DIR, exist_ok=True)

# Telebot config
TELEBOT_POLLING_TIMEOUT = int(os.getenv("TELEBOT_POLLING_TIMEOUT", "60"))

# -------------------------
# Logging setup
# -------------------------
logger = logging.getLogger("nexbit_bot")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

fh = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
fh.setFormatter(fmt)
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(fmt)
logger.addHandler(ch)

logger.info("Logger initialized. LOG_FILE=%s", LOG_FILE)

# -------------------------
# Bot init
# -------------------------
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN not set - exiting")
    raise SystemExit("BOT_TOKEN required")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
# use requests session for OKX and telebot internals
requests_session = requests.Session()

# -------------------------
# Helpers
# -------------------------
stop_event = threading.Event()

def admin_check(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def safe_send(chat_id, text, parse_mode=None, **kwargs):
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.exception("safe_send failed to %s: %s", chat_id, e)

def format_price(p):
    try:
        if p is None:
            return "N/A"
        p = float(p)
        if p >= 1:
            return f"${p:,.2f}"
        else:
            # small coins
            return f"${p:,.6f}".rstrip("0").rstrip(".")
    except Exception:
        return str(p)

# -------------------------
# OKX fetch + ASCII sparkbars
# -------------------------
def fetch_okx_ticker(instId: str):
    try:
        r = requests_session.get(OKX_TICKER_URL, params={"instId": instId}, timeout=HTTP_TIMEOUT)
        j = r.json()
        if isinstance(j, dict) and j.get("code") == "0" and "data" in j and j["data"]:
            d = j["data"][0]
            last = float(d.get("last", d.get("lastPrice", 0)))
            ts = int(d.get("ts", int(time.time()*1000)))
            return {"last": last, "ts": ts}
        # fallback parse
        data = j.get("data") if isinstance(j, dict) else None
        if data and isinstance(data, list) and len(data) > 0:
            d = data[0]
            last = float(d.get("last", 0))
            return {"last": last, "ts": int(d.get("ts", int(time.time()*1000)))}
    except Exception as e:
        logger.exception("fetch_okx_ticker error for %s: %s", instId, e)
    return {"last": None, "ts": int(time.time()*1000)}

def fetch_okx_tickers(symbols: List[str]):
    out = {}
    for s in symbols:
        out[s] = fetch_okx_ticker(s)
    return out

def build_sparkbars(values: Dict[str, float]):
    blocks = "█▇▆▅▄▃▂▁"
    nums = [v for v in values.values() if v is not None]
    if not nums:
        return {k: "--------" for k in values.keys()}
    mn = min(nums); mx = max(nums); rng = mx - mn if mx!=mn else 1.0
    bars = {}
    for k,v in values.items():
        if v is None:
            bars[k] = "--------"
            continue
        norm = (v - mn) / rng  # 0..1
        # 8-char bar choosing block by position
        bar = []
        for i in range(8):
            # choose index based on position from left
            pos = norm * (len(blocks)-1)
            idx = int(round(pos))
            # small variation along bar
            idx = max(0, min(len(blocks)-1, idx - (i//3)))
            bar.append(blocks[idx])
        bars[k] = "".join(bar)
    return bars

def format_market_snapshot(tickers: Dict[str, dict]) -> str:
    values = {s: tickers.get(s, {}).get("last") for s in SYMBOLS}
    bars = build_sparkbars(values)
    lines = []
    for s in SYMBOLS:
        short = s.split("-")[0].ljust(6)
        price = values.get(s)
        price_str = "N/A" if price is None else format_price(price)
        bar = bars.get(s, "--------")
        lines.append(f"{short} │ {bar}  {price_str}")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return "📈 Market Snapshot (OKX REST)\n\n" + "\n".join(lines) + f"\n\n⏱ {ts}"

# -------------------------
# Background workers
# -------------------------
def market_push_worker():
    logger.info("market_push_worker started (interval=%s)", MARKET_PUSH_INTERVAL)
    while not stop_event.is_set():
        try:
            tickers = fetch_okx_tickers(SYMBOLS)
            msg = format_market_snapshot(tickers)
            if MARKET_PUSH_CHAT_ID:
                # send as code block so formatting monospace
                safe_send(MARKET_PUSH_CHAT_ID, f"```\n{msg}\n```", parse_mode="Markdown")
                logger.info("market pushed to %s", MARKET_PUSH_CHAT_ID)
            else:
                logger.warning("MARKET_PUSH_CHAT_ID not set; skipping push")
        except Exception:
            logger.exception("market_push_worker exception")
        # sleep with early abort
        for _ in range(max(1, int(MARKET_PUSH_INTERVAL))):
            if stop_event.is_set(): break
            time.sleep(1)
    logger.info("market_push_worker stopped")

def heartbeat_worker():
    while not stop_event.is_set():
        logger.info("Heartbeat alive")
        time.sleep(60)

# Supervisor thread wrapper
class ThreadSupervisor(threading.Thread):
    def __init__(self, target, name):
        super().__init__(daemon=True)
        self.target = target
        self.name = name
    def run(self):
        while not stop_event.is_set():
            try:
                logger.info("supervisor starting %s", self.name)
                self.target()
            except Exception:
                logger.exception("supervisor caught crash for %s; sleeping 5s", self.name)
                time.sleep(5)
        logger.info("supervisor exiting %s", self.name)

# -------------------------
# Bot command handlers
# -------------------------
WELCOME_MENU = """🤖 **Welcome to NEXBIT-BOT** 🤖

Available Commands:
/start - show this message
/market - View Real-Time Market Data
/analysis - Market analysis
/mobile - Mobile Version
/feature - Platform Features
/support - Customer Support
"""

@bot.message_handler(commands=["start"])
def start_cmd(message):
    chat_id = message.chat.id
    chat_type = message.chat.type  # private / group / supergroup
    welcome = "🤖 Welcome to NEXBIT-BOT 🤖\n\n"
    if chat_type in ["group", "supergroup"]:
        welcome += f"📌 **Group Chat ID:** `{chat_id}`\n"
        welcome += "（请管理员在 Railway 环境变量把此 Chat ID 填入 MARKET_PUSH_CHAT_ID 或在群里以管理员身份发送 /bindgroup 来自动绑定）\n\n"
    welcome += "\nAvailable Commands:\n/market /analysis /mobile /feature /support\n"
    bot.reply_to(message, welcome, parse_mode="Markdown")

@bot.message_handler(commands=["market"])
def cmd_market(msg):
    try:
        tickers = fetch_okx_tickers(SYMBOLS)
        lines = ["📊 **Real-time Market Data**\n"]
        for s in SYMBOLS:
            last = tickers.get(s, {}).get("last")
            lines.append(f"{s}: {format_price(last) if last is not None else 'N/A'}")
        bot.reply_to(msg, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("cmd_market")
        bot.reply_to(msg, "Failed to fetch market data.")

@bot.message_handler(commands=["analysis"])
def cmd_analysis(msg):
    bot.reply_to(msg, "📈 Market analysis (placeholder).")

@bot.message_handler(commands=["mobile","feature","support"])
def simple_info(msg):
    text = {
        "mobile": "📱 Mobile info",
        "feature": "✨ Platform features",
        "support": "💬 Support: https://t.me/nexbitonlineservice"
    }
    cmd = msg.text.lstrip("/").split()[0].lower()
    bot.reply_to(msg, text.get(cmd, "Info"))

# ---------- admin-only commands ----------
@bot.message_handler(commands=["status"])
def cmd_status(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        bot.reply_to(msg, "You are not authorized.")
        return
    status = {
        "admins": ADMIN_IDS,
        "market_push_target": MARKET_PUSH_CHAT_ID,
        "symbols": SYMBOLS,
        "uptime": "N/A"
    }
    bot.reply_to(msg, "Status:\n```{}```".format(json.dumps(status, indent=2)), parse_mode="Markdown")

@bot.message_handler(commands=["log","logs"])
def cmd_logs(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        return
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "rb") as f:
                data = f.read()[-100*1024:].decode(errors="ignore")
            bot.reply_to(msg, "Last logs:\n```{}```".format(data), parse_mode="Markdown")
        else:
            bot.reply_to(msg, "No log file found.")
    except Exception:
        logger.exception("cmd_logs")
        bot.reply_to(msg, "Failed to read logs.")

@bot.message_handler(commands=["restart"])
def cmd_restart(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        bot.reply_to(msg, "You are not authorized.")
        return
    bot.reply_to(msg, "Restarting...")
    logger.warning("Restart requested by admin %s", uid)
    def _exit_later():
        time.sleep(1.5)
        os._exit(0)
    threading.Thread(target=_exit_later, daemon=True).start()

@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        bot.reply_to(msg, "Not authorized.")
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /addadmin <telegram_user_id>")
        return
    try:
        new = int(parts[1])
        if new not in ADMIN_IDS:
            ADMIN_IDS.append(new)
            bot.reply_to(msg, f"Added admin {new}. Current admins: {ADMIN_IDS}")
            logger.info("Admin %s added new admin %s", uid, new)
        else:
            bot.reply_to(msg, f"{new} already an admin.")
    except Exception:
        bot.reply_to(msg, "Invalid id.")

@bot.message_handler(commands=["deladmin","removeadmin"])
def cmd_deladmin(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        bot.reply_to(msg, "Not authorized.")
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(msg, "Usage: /deladmin <telegram_user_id>")
        return
    try:
        rem = int(parts[1])
        if rem in ADMIN_IDS:
            ADMIN_IDS.remove(rem)
            bot.reply_to(msg, f"Removed admin {rem}. Current admins: {ADMIN_IDS}")
            logger.info("Admin %s removed admin %s", uid, rem)
        else:
            bot.reply_to(msg, f"{rem} not in admin list.")
    except Exception:
        bot.reply_to(msg, "Invalid id.")

@bot.message_handler(commands=["admins"])
def cmd_admins(msg):
    uid = msg.from_user.id
    if not admin_check(uid):
        bot.reply_to(msg, "Not authorized.")
        return
    bot.reply_to(msg, f"Admins: {ADMIN_IDS}")

@bot.message_handler(commands=["bindgroup","setpush"])
def cmd_bindgroup(msg):
    """
    When used inside a group by an authorized admin, bind this group as MARKET_PUSH_CHAT_ID.
    """
    global MARKET_PUSH_CHAT_ID
    uid = msg.from_user.id
    chat = msg.chat
    # only allow admin users
    if not admin_check(uid):
        bot.reply_to(msg, "You are not authorized to bind a group.")
        return
    if chat.type not in ["group","supergroup"]:
        bot.reply_to(msg, "Please run this command inside the target group.")
        return
    MARKET_PUSH_CHAT_ID = chat.id
    bot.reply_to(msg, f"Bound this group for market pushes. Chat ID: `{chat.id}`", parse_mode="Markdown")
    logger.info("Admin %s bound MARKET_PUSH_CHAT_ID -> %s", uid, chat.id)
    # Note: this is in-memory only. To persist across restarts, set MARKET_PUSH_CHAT_ID in Railway env.

# generic unknown handler
@bot.message_handler(func=lambda m: True)
def fallback(msg):
    text = (msg.text or "").strip()
    if text.startswith("/"):
        # unknown command
        if not admin_check(msg.from_user.id):
            bot.reply_to(msg, "Command not recognized or you are not authorized.")
            logger.info("Blocked unknown command from %s: %s", msg.from_user.id, text)
        else:
            bot.reply_to(msg, "Unknown admin command.")
    else:
        # optionally other auto-replies
        pass

# -------------------------
# Polling wrapper (handle 409)
# -------------------------
def start_bot_polling():
    logger.info("Starting bot polling loop")
    while not stop_event.is_set():
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            bot.infinity_polling(timeout=TELEBOT_POLLING_TIMEOUT, long_polling_timeout=TELEBOT_POLLING_TIMEOUT)
        except ApiTelegramException as e:
            # handle 409 conflicts etc
            logger.exception("ApiTelegramException in polling: %s", e)
            time.sleep(10)
        except Exception:
            logger.exception("Unhandled exception in polling loop")
            time.sleep(5)
    logger.info("Polling loop exiting")

# -------------------------
# Main
# -------------------------
def main():
    logger.info("Program starting")
    # supervisors
    market_sup = ThreadSupervisor(target=market_push_worker, name="market_push_worker")
    hb_sup = ThreadSupervisor(target=heartbeat_worker, name="heartbeat_worker")
    market_sup.start()
    hb_sup.start()
    polling_thread = threading.Thread(target=start_bot_polling, name="polling_thread", daemon=True)
    polling_thread.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    finally:
        stop_event.set()
        logger.info("Shutting down")
        time.sleep(1)
        os._exit(0)

if __name__ == "__main__":
    main()
