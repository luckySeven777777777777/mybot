#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Final merged enhanced bot.py
- Old commands preserved (original texts kept)
- Enhanced menu (方案 B)
- OKX REST market snapshot with ASCII bars + 24h % change
- Auto show group ID on /start (group only)
- /bindgroup to auto-bind current group as push target (admin only)
- Auto market push to multiple chat IDs every 10 minutes
- Multi-admin via ADMIN_IDS env var (comma separated)
- Admin commands: /status /admins /addadmin /deladmin /bindgroup /push /restart /logs
- File logging, rotating logs, heartbeat, and self-recovery
"""

import os, sys, time, json, math, threading, traceback, requests
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging
import telebot
from telebot.apihelper import ApiTelegramException

# ------------- CONFIG (ENV) -------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# ADMIN_IDS: comma separated, e.g. "6062973135,6163182909"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
# Backwards compat: ADMIN_ID
if not ADMIN_IDS:
    try:
        single = int(os.getenv("ADMIN_ID", "0") or 0)
        if single:
            ADMIN_IDS = [single]
    except:
        ADMIN_IDS = []

# MARKET_PUSH_CHAT_IDS: comma separated target chat ids (groups or users)
MARKET_PUSH_CHAT_IDS_RAW = os.getenv("MARKET_PUSH_CHAT_IDS", os.getenv("MARKET_PUSH_CHAT_ID", ""))
MARKET_PUSH_CHAT_IDS = [int(x) for x in MARKET_PUSH_CHAT_IDS_RAW.split(",") if x.strip().lstrip("-").isdigit()]

# If no target configured, default to first admin (will be private chat)
if not MARKET_PUSH_CHAT_IDS and ADMIN_IDS:
    MARKET_PUSH_CHAT_IDS = [ADMIN_IDS[0]]

# Symbols (as requested)
SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "DOGE-USDT", "SOL-USDT",
    "BNB-USDT", "XRP-USDT", "TRX-USDT", "USDC-USDT"
]

# OKX REST endpoints
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"

# Intervals
MARKET_PUSH_INTERVAL = int(os.getenv("MARKET_PUSH_INTERVAL", "600"))  # 10 min default
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))
POLLING_TIMEOUT = int(os.getenv("TELEBOT_POLLING_TIMEOUT", "60"))

# Logging
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("nexbit_bot")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
fh = RotatingFileHandler(LOG_FILE, maxBytes=3*1024*1024, backupCount=5, encoding="utf-8")
fh.setFormatter(fmt)
logger.addHandler(fh)
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(fmt)
logger.addHandler(ch)
logger.info("Logger ready. Log file: %s", LOG_FILE)

# Telebot
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN not set in env - exiting")
    raise SystemExit("BOT_TOKEN required")
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# Safety / state
stop_event = threading.Event()
requests_session = requests.Session()

# ----------------- UTIL -----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def safe_send(chat_id, text, parse_mode=None, **kwargs):
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, **kwargs)
    except Exception:
        logger.exception("safe_send failed for %s", chat_id)

def format_price(v):
    try:
        if v is None:
            return "N/A"
        v = float(v)
        if v >= 1:
            return f"${v:,.2f}"
        else:
            return f"${v:,.6f}".rstrip("0").rstrip(".")
    except:
        return str(v)

# ----------------- OKX functions -----------------
def fetch_okx_ticker(instId):
    try:
        r = requests_session.get(OKX_TICKER_URL, params={"instId": instId}, timeout=HTTP_TIMEOUT)
        j = r.json()
        # OKX returns code "0" for success
        if isinstance(j, dict) and (j.get("code") == "0" or j.get("code") == 0) and "data" in j and j["data"]:
            d = j["data"][0]
            last = float(d.get("last") or d.get("lastPrice") or 0)
            # OKX provides 24h change fields sometimes: "open_24h" or "open"
            open24 = None
            if d.get("open_24h"):
                try: open24 = float(d.get("open_24h"))
                except: open24 = None
            elif d.get("open"):
                try: open24 = float(d.get("open"))
                except: open24 = None
            return {"last": last, "open24": open24}
        # fallback
        if isinstance(j, dict) and "data" in j and isinstance(j["data"], list) and j["data"]:
            d = j["data"][0]
            last = float(d.get("last", 0))
            return {"last": last, "open24": None}
    except Exception:
        logger.exception("fetch_okx_ticker error for %s", instId)
    return {"last": None, "open24": None}

def fetch_okx_tickers(symbols):
    out = {}
    for s in symbols:
        out[s] = fetch_okx_ticker(s)
    return out

# ----------------- ASCII bar builder -----------------
BLOCKS = ["▁","▂","▃","▄","▅","▆","▇","█"]
def build_bars(values_dict):
    vals = [v for v in values_dict.values() if v is not None]
    if not vals:
        return {k: BLOCKS[0]*8 for k in values_dict}
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx != mn else 1.0
    bars = {}
    for k, v in values_dict.items():
        if v is None:
            bars[k] = BLOCKS[0]*8
            continue
        norm = (v - mn) / rng  # 0..1
        # produce 8-length bar, selecting block based on norm
        bar = []
        for i in range(8):
            idx = int(round(norm * (len(BLOCKS)-1)))
            # small stagger for variety
            idx = max(0, min(len(BLOCKS)-1, idx - (i//4)))
            bar.append(BLOCKS[idx])
        bars[k] = "".join(bar)
    return bars

def format_market_snapshot_with_pct(tickers):
    # tickers: {sym: {"last": float, "open24": float or None}}
    values = {s: tickers.get(s, {}).get("last") for s in SYMBOLS}
    bars = build_bars(values)
    lines = []
    for s in SYMBOLS:
        short = s.split("-")[0].ljust(6)
        last = tickers.get(s, {}).get("last")
        open24 = tickers.get(s, {}).get("open24")
        price_str = "N/A" if last is None else format_price(last)
        pct_str = ""
        if last is not None and open24:
            try:
                pct = (last - float(open24)) / float(open24) * 100
                pct_str = f" ({pct:+.2f}%)"
            except:
                pct_str = ""
        bar = bars.get(s, BLOCKS[0]*8)
        lines.append(f"{short} │ {bar}  {price_str}{pct_str}")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    header = "📈 Market Snapshot (OKX REST)\n\n"
    return header + "\n".join(lines) + f"\n\n⏱ {ts}"

# ----------------- Background: market push (to multiple chat ids) -----------------
def market_push_loop():
    logger.info("Market push loop started (interval=%s)", MARKET_PUSH_INTERVAL)
    while not stop_event.is_set():
        try:
            tickers = fetch_okx_tickers(SYMBOLS)
            msg = format_market_snapshot_with_pct(tickers)
            # wrap in code block for monospace
            payload = "```\n" + msg + "\n```"
            for cid in MARKET_PUSH_CHAT_IDS:
                try:
                    bot.send_message(cid, payload, parse_mode="Markdown")
                except Exception:
                    logger.exception("Failed to push to %s", cid)
            logger.info("Market snapshot pushed to %s", MARKET_PUSH_CHAT_IDS)
        except Exception:
            logger.exception("market_push_loop exception")
        # sleep with early abort
        for _ in range(max(1, int(MARKET_PUSH_INTERVAL))):
            if stop_event.is_set():
                break
            time.sleep(1)
    logger.info("market_push_loop exiting")

# ----------------- Admin/Command handlers -----------------

# --- Original/old command texts are preserved below and used in /start ---
ORIGINAL_MENU_TEXT = """
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

ENHANCED_MENU_TEXT = """
🛠 Admin Features:
/status - Bot status
/restart - Restart bot
/logs - View logs
/admins - View admin list
/addadmin <id> - Add admin (runtime)
/deladmin <id> - Remove admin (runtime)
/bindgroup - Bind this group as market push target
/push - Force push snapshot now
"""

@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    welcome = "🤖 **Welcome to NEXBIT-BOT** 🤖\n\n"
    # Auto display group id if in group
    if chat_type in ["group", "supergroup"]:
        welcome += f"📌 **Group Chat ID:** `{chat_id}`\n"
        welcome += "（管理员可在本群发送 /bindgroup 自动绑定为行情推送目标）\n\n"
    # combine enhanced menu + original commands
    welcome += "📋 *Basic & Legacy Commands:*\n"
    welcome += ORIGINAL_MENU_TEXT + "\n"
    welcome += "📈 *Market Tools & Admin:*\n"
    welcome += ENHANCED_MENU_TEXT
    bot.reply_to(message, welcome, parse_mode="Markdown")

@bot.message_handler(commands=["market"])
def cmd_market(message):
    try:
        tickers = fetch_okx_tickers(SYMBOLS)
        lines = ["📊 **Real-time Market Data**\n"]
        for s in SYMBOLS:
            last = tickers.get(s, {}).get("last")
            lines.append(f"{s}: {format_price(last) if last is not None else 'N/A'}")
        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        logger.exception("cmd_market")
        bot.reply_to(message, "Failed to fetch market data.")

@bot.message_handler(commands=["analysis"])
def cmd_analysis(msg):
    bot.reply_to(msg, "📈 **Market Analysis**\n• Increased volatility observed\n• BTC is testing a key support level\n• Monitor major coins closely\n• Market sentiment: Neutral → Bullish")

@bot.message_handler(commands=["safe"])
def cmd_safe(msg):
    bot.reply_to(msg, "🛡 **Security Tips**\n• Do NOT click unknown links\n• Never disclose your seed phrase/private key\n• Beware of phishing websites\n• Official support will NEVER ask for your password")

@bot.message_handler(commands=["mobile"])
def cmd_mobile(msg):
    bot.reply_to(msg, "📱 **Mobile App Guide**\n• Web version recommended\n• Supports Android & iOS\n• Enable Face ID / Fingerprint for safety\n• Keep app up to date")

@bot.message_handler(commands=["feature"])
def cmd_feature(msg):
    bot.reply_to(msg, "✨ **Platform Features**\n• Real-time market data\n• Automatic analysis\n• Advanced alerts\n• In-depth data push")

@bot.message_handler(commands=["register"])
def cmd_register(msg):
    # Map old register behavior to start/help text
    bot.reply_to(msg, "📝 **Registration Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["Bind","bind"])
def cmd_bind(msg):
    bot.reply_to(msg, "⏳ **Bind feature coming soon...**")

@bot.message_handler(commands=["deposit","Deposit"])
def cmd_deposit(msg):
    bot.reply_to(msg, "💰 **Deposit Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["withdraw","Withdraw"])
def cmd_withdraw(msg):
    bot.reply_to(msg, "💵 **Withdraw Guide**:\nhttps://Price alert feature coming soon..")

@bot.message_handler(commands=["support"])
def cmd_support(msg):
    bot.reply_to(msg, "💬 **Customer Support**\n• 24-hour online customer service\n• Telegram: https://t.me/monsterman197\n• Email: lucky077779999@gmail.com")

# -------- admin commands -----------
@bot.message_handler(commands=["status"])
def cmd_status(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "You are not authorized.")
        return
    info = {
        "admins": ADMIN_IDS,
        "push_targets": MARKET_PUSH_CHAT_IDS,
        "symbols": SYMBOLS
    }
    bot.reply_to(msg, "Status:\n```{}```".format(json.dumps(info, indent=2)), parse_mode="Markdown")

@bot.message_handler(commands=["admins"])
def cmd_admins(msg):
    if not is_admin(msg.from_user.id):
        return
    bot.reply_to(msg, f"Admins: {ADMIN_IDS}")

@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "Not authorized.")
        return
    try:
        parts = msg.text.strip().split()
        new = int(parts[1])
        if new not in ADMIN_IDS:
            ADMIN_IDS.append(new)
            bot.reply_to(msg, f"Added admin {new}")
        else:
            bot.reply_to(msg, "Already admin.")
    except Exception:
        bot.reply_to(msg, "Usage: /addadmin <id>")

@bot.message_handler(commands=["deladmin","removeadmin"])
def cmd_deladmin(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "Not authorized.")
        return
    try:
        parts = msg.text.strip().split()
        rem = int(parts[1])
        if rem in ADMIN_IDS:
            ADMIN_IDS.remove(rem)
            bot.reply_to(msg, f"Removed admin {rem}")
        else:
            bot.reply_to(msg, "Not found.")
    except Exception:
        bot.reply_to(msg, "Usage: /deladmin <id>")

@bot.message_handler(commands=["bindgroup","setpush"])
def cmd_bindgroup(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "Not authorized.")
        return
    chat = msg.chat
    if chat.type not in ["group","supergroup"]:
        bot.reply_to(msg, "Please run this command inside the target group.")
        return
    # Add group id to MARKET_PUSH_CHAT_IDS if not present
    gid = chat.id
    if gid not in MARKET_PUSH_CHAT_IDS:
        MARKET_PUSH_CHAT_IDS.append(gid)
    bot.reply_to(msg, f"Bound this group for market pushes. Chat ID: `{gid}`", parse_mode="Markdown")
    logger.info("Bound group %s to push list by admin %s", gid, msg.from_user.id)
    # NOTE: this is in-memory only; persist by setting env VAR in Railway if needed.

@bot.message_handler(commands=["push"])
def cmd_push(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "Not authorized.")
        return
    try:
        tickers = fetch_okx_tickers(SYMBOLS)
        payload = "```\n" + format_market_snapshot_with_pct(tickers) + "\n```"
        for cid in MARKET_PUSH_CHAT_IDS:
            try:
                bot.send_message(cid, payload, parse_mode="Markdown")
            except Exception:
                logger.exception("push failed for %s", cid)
        bot.reply_to(msg, "Snapshot pushed.")
    except Exception:
        logger.exception("cmd_push")
        bot.reply_to(msg, "Failed to push snapshot.")

@bot.message_handler(commands=["logs","log"])
def cmd_logs(msg):
    if not is_admin(msg.from_user.id):
        return
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "rb") as f:
                data = f.read()[-120*1024:].decode(errors="ignore")
            bot.reply_to(msg, "Last logs:\n```{}```".format(data), parse_mode="Markdown")
        else:
            bot.reply_to(msg, "No logs found.")
    except Exception:
        logger.exception("cmd_logs")
        bot.reply_to(msg, "Failed to read logs.")

@bot.message_handler(commands=["restart"])
def cmd_restart(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "Not authorized.")
        return
    bot.reply_to(msg, "Restarting bot...")
    def _exit():
        time.sleep(1.2)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()

# fallback
@bot.message_handler(func=lambda m: True)
def fallback_handler(m):
    txt = (m.text or "").strip()
    if txt.startswith("/"):
        if not is_admin(m.from_user.id):
            bot.reply_to(m, "Command not recognized or you are not authorized.")
            logger.info("Blocked unknown command from %s: %s", m.from_user.id, txt)
        else:
            bot.reply_to(m, "Unknown admin command.")
    else:
        # leave non-command messages alone
        pass

# ----------------- Polling wrapper with 409 handling -----------------
def start_polling():
    logger.info("Starting polling loop")
    while not stop_event.is_set():
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            bot.infinity_polling(timeout=POLLING_TIMEOUT, long_polling_timeout=POLLING_TIMEOUT)
        except ApiTelegramException as e:
            logger.exception("ApiTelegramException during polling: %s", e)
            time.sleep(10)
        except Exception:
            logger.exception("Unhandled exception in polling, sleeping 5s")
            time.sleep(5)
    logger.info("Polling loop ended")

# --------------- Supervisors / main ---------------
class ThreadSupervisor(threading.Thread):
    def __init__(self, target, name):
        super().__init__(daemon=True)
        self.target = target
        self.name = name
    def run(self):
        while not stop_event.is_set():
            try:
                logger.info("Supervisor starting %s", self.name)
                self.target()
            except Exception:
                logger.exception("Supervisor %s crashed - retrying in 5s", self.name)
                time.sleep(5)
        logger.info("Supervisor %s exiting", self.name)

def main():
    logger.info("Bot starting")
    # threads
    market_sup = ThreadSupervisor(target=market_push_loop, name="market_push_loop")
    hb_sup = ThreadSupervisor(target=lambda: (logger.info("Heartbeat alive"), time.sleep(60)), name="heartbeat")
    market_sup.start()
    hb_sup.start()
    polling_thread = threading.Thread(target=start_polling, daemon=True)
    polling_thread.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt - shutting down")
    finally:
        stop_event.set()
        time.sleep(1)
        os._exit(0)

if __name__ == "__main__":
    main()
