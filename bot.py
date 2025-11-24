import telebot
import json
import time
import threading
import websocket
import traceback

# ===========================
# Telegram Bot Token
# ===========================
BOT_TOKEN = "8344095901:AAEZUTB0FZQooWVsIK1p-cTg_3lu6ARR4Ec"
bot = telebot.TeleBot(BOT_TOKEN)

# ===========================
# 支持的币种（OKX 格式）
# ===========================
SYMBOLS = [
   "BTC-USDT",
    "ETH-USDT",
    "DOGE-USDT",
    "SOL-USDT",
    "BNB-USDT",
    "XRP-USDT",
    "TRX-USDT",
    "USDC-USDT"
]

# 保存行情
MARKET = {s: {"price": "Retrieving..."} for s in SYMBOLS}

# 全局 WebSocket
ws_global = None


# ===========================
# OKX WebSocket 回调
# ===========================
def on_message(ws, message):
    try:
        data = json.loads(message)

        # 非行情数据
        if "data" not in data:
            return

        tick = data["data"][0]
        symbol = tick["instId"]
        price = tick["last"]

        MARKET[symbol]["price"] = price

        print(f"{symbol} : {price}")

    except Exception as e:
        print("处理行情时发生错误：", e)
        print(traceback.format_exc())


def on_error(ws, error):
    print("WebSocket 错误：", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket 已关闭，5 秒后重连...")
    time.sleep(5)
    start_ws()


def on_open(ws):
    print("WebSocket 已连接！")

    subs = [{"channel": "tickers", "instId": s} for s in SYMBOLS]

    msg = {
        "op": "subscribe",
        "args": subs
    }

    ws.send(json.dumps(msg))
    print("已订阅：", SYMBOLS)


# ===========================
# 启动 OKX WebSocket
# ===========================
def start_ws():
    global ws_global
    url = "wss://ws.okx.com:8443/ws/v5/public"

    websocket.enableTrace(False)

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error
    )

    ws_global = ws

    while True:
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            print("WebSocket 运行错误：", e)
            time.sleep(5)


# ===========================
# Telegram 指令
# ===========================
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
    for s in SYMBOLS:
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
    bot.reply_to(msg, "📝 **Registration Guide**:\nhttps://Price alert feature coming soon.." )


@bot.message_handler(commands=["Deposit"])
def cmd_deposit(msg):
    bot.reply_to(msg, "💰 **Deposit Guide**:\nhttps://Price alert feature coming soon.." )


@bot.message_handler(commands=["Withdraw"])
def cmd_withdraw(msg):
    bot.reply_to(msg, "💵 **Withdraw Guide**:\nhttps://Price alert feature coming soon.." )


@bot.message_handler(commands=["Alert"])
def cmd_alert(msg):
    bot.reply_to(msg, "⏳ **Price alert feature coming soon...**")


@bot.message_handler(commands=["Bind"])
def cmd_bind(msg):
    bot.reply_to(msg, "⏳ **Wallet binding feature coming soon...**")


@bot.message_handler(commands=["support"])
def cmd_support(msg):
    text = """
💬 **Customer Support**
• 24-hour online customer service
• Telegram: https://t.me/monsterman197  
• Email: lucky077779999@gmail.com
"""
    bot.reply_to(msg, text)


# ===========================
# 启动后台线程 + Telegram 机器人
# ===========================
def start_threads():
    t = threading.Thread(target=start_ws)
    t.daemon = True
    t.start()


if __name__ == "__main__":
    print("Program starting...")
    start_threads()

    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            print("机器人错误：", e)
            time.sleep(3)
