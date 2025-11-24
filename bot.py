import telebot
import json
import time
import threading
import websocket

# ===========================
# 配置
# ===========================
BOT_TOKEN = "8344095901:AAEZUTB0FZQooWVsIK1p-cTg_3lu6ARR4Ec"   # ⚠️ 请务必替换 BotFather 重置后的新 Token
bot = telebot.TeleBot(BOT_TOKEN)

# 监控币种
SYMBOLS = ["btcusdt", "ethusdt", "bnbusdt", "solusdt", "dogeusdt"]
MARKET = {s: {"price": "Retrieving..."} for s in SYMBOLS}


# ===========================
# WebSocket URL 生成
# ===========================
def build_ws_url():
    streams = []
    streams += [f"{s}@ticker" for s in SYMBOLS]
    streams += [f"{s}@kline_1m" for s in SYMBOLS]
    streams += [f"{s}@depth5" for s in SYMBOLS]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)


# ===========================
# WebSocket 数据处理
# ===========================
def on_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get("stream", "")
        payload = data.get("data", {})

        # ticker 更新最新价格
        if "@ticker" in stream:
            symbol = payload["s"].lower()
            MARKET[symbol]["price"] = payload["c"]

    except Exception as e:
        print("解析错误：", e)


def on_error(ws, error):
    print("WebSocket 错误：", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket 已关闭，5 秒后重连...")
    time.sleep(5)
    start_websocket()


def on_open(ws):
    print("WebSocket 已连接!")


# ===========================
# 启动 WebSocket
# ===========================
def start_websocket():
    url = build_ws_url()
    print("订阅：", url)

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=10)  # 更稳定


# ===========================
# /start
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


# ===========================
# /market 指令
# ===========================
@bot.message_handler(commands=["market"])
def cmd_market(msg):
    text = "📊 **Real-time Market Data**\n\n"
    for s in SYMBOLS:
        text += f"{s.upper()}: {MARKET[s]['price']}\n"
    bot.reply_to(msg, text)


# ===========================
# /analysis
# ===========================
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


# ===========================
# /safe
# ===========================
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


# ===========================
# /mobile
# ===========================
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


# ===========================
# /feature
# ===========================
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


# ===========================
# 注册 / 充值 / 提现 / 提醒 / 绑定
# ===========================
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
def cmd_alert(msg):
    bot.reply_to(msg, "⏳ **Price alert feature coming soon...**")

# ===========================
# /support
# ===========================
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
# 启动 WS 后台线程
# ===========================
def run_ws_thread():
    t = threading.Thread(target=start_websocket)
    t.daemon = True
    t.start()


# ===========================
# 主程序
# ===========================
if __name__ == "__main__":
    print("Program starting...")
    run_ws_thread()

    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            print("机器人错误：", e)
            time.sleep(3)
