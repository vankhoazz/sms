# -*- coding: utf-8 -*-
import os
import time
import threading
import logging
import requests
from collections import defaultdict
from flask import Flask, request, jsonify
import telebot
from functools import lru_cache
from datetime import datetime

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_URL = os.getenv("SERVICE_URL")
ADMIN_ID = os.getenv("ADMIN_ID", "5617674327")
BASE_URL = "https://365otp.com/apiv1"

# Proxy config (optional)
USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL")

if not all([BOT_TOKEN, API_KEY, SERVICE_URL]):
    raise RuntimeError("❌ Missing: BOT_TOKEN, API_KEY, or SERVICE_URL")

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== FLASK & BOT ====================
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=10)

# ==================== STORAGE ====================
user_orders = defaultdict(lambda: None)
active_checks = {}  # Track active auto-check threads

# ==================== HTTP SESSION ====================
session = requests.Session()

# Retry strategy
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=40,
    max_retries=retry_strategy
)
session.mount('https://', adapter)
session.mount('http://', adapter)

# Setup proxy if enabled
if USE_PROXY and PROXY_URL:
    session.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
    logger.info(f"✅ Proxy enabled")

session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Connection": "keep-alive"
})

# ==================== ERROR MESSAGES ====================
ERRORS = {
    'timeout': '⏱️ Kết nối chậm. Vui lòng thử lại!',
    'connection': '🔌 Không thể kết nối. Kiểm tra mạng!',
    'http_error': '⚠️ Dịch vụ đang bận. Thử lại sau!',
    'server_error': '❌ Lỗi hệ thống. Vui lòng thử lại!',
    'unknown': '❌ Có lỗi xảy ra. Liên hệ admin!',
    'rate_limit': '⏰ Quá nhiều request. Đợi 1 phút!',
    'invalid': '⚠️ Dữ liệu không hợp lệ!'
}

# ==================== UTILITIES ====================
def notify_admin(message, user_id=None):
    """Send alert to admin (async)"""
    if not ADMIN_ID:
        return

    def _send():
        try:
            text = f"🔴 ALERT\n\n{message}"
            if user_id:
                text += f"\n👤 User: {user_id}"
            text += f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
            bot.send_message(ADMIN_ID, text)
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")

    threading.Thread(target=_send, daemon=True).start()

def safe_api_call(func):
    """Decorator for safe API calls"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout: {func.__name__}")
            return {"status": 0, "message": ERRORS['timeout']}
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection error: {func.__name__}")
            return {"status": 0, "message": ERRORS['connection']}
        except requests.exceptions.HTTPError as e:
            code = getattr(e.response, 'status_code', 0)
            logger.warning(f"HTTP {code}: {func.__name__}")

            if code == 429:
                return {"status": 0, "message": ERRORS['rate_limit']}
            elif code >= 500:
                return {"status": 0, "message": ERRORS['server_error']}
            else:
                return {"status": 0, "message": ERRORS['http_error']}
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            notify_admin(f"Error in {func.__name__}: {str(e)[:100]}")
            return {"status": 0, "message": ERRORS['unknown']}
    return wrapper

# ==================== API FUNCTIONS ====================
@safe_api_call
def api_call(endpoint, params=None):
    """Make API call to 365otp"""
    params = params or {}
    params["apikey"] = API_KEY

    response = session.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        timeout=10
    )
    response.raise_for_status()
    return response.json()

def get_balance():
    return api_call("getbalance")

@lru_cache(maxsize=1)
def _cached_services(timestamp):
    """Cache services for 60 seconds"""
    return api_call("availableservice")

def get_services():
    current_time = int(time.time() / 60)
    return _cached_services(current_time)

def create_order(service_id, country_id=10, network=None, prefix=None, send_sms=False):
    params = {
        "serviceId": service_id,
        "countryId": country_id
    }
    if network:
        params["networkId"] = network
    if prefix:
        params["prefix"] = prefix
    if send_sms:
        params["sendSms"] = "true"

    return api_call("orderv2", params)

def check_order(order_id):
    return api_call("ordercheck", {"id": order_id})

def send_zalo_sms(order_id):
    return api_call("sendsmszalo", {"id": order_id})

def continue_order(order_id):
    return api_call("continueorder", {"orderId": order_id})

# ==================== AUTO CHECK OTP ====================
def auto_check_otp(chat_id, order_id):
    """Auto check OTP with smart intervals"""
    check_key = f"{chat_id}_{order_id}"

    if check_key in active_checks:
        logger.info(f"Already checking {check_key}")
        return

    active_checks[check_key] = True

    try:
        intervals = [5, 5, 7, 7, 10, 10, 15, 15]  # Smart backoff
        error_count = 0
        notified = False

        for idx, wait in enumerate(intervals):
            time.sleep(wait)

            result = check_order(order_id)

            # Handle errors
            if result.get("status") == 0:
                error_count += 1

                if error_count == 2 and not notified:
                    bot.send_message(chat_id, "⏳ Kết nối chậm, đang thử lại...")
                    notified = True

                if error_count >= 4:
                    bot.send_message(
                        chat_id,
                        "⚠️ Kết nối không ổn định.\n\n"
                        "💡 Dùng 🔍 <b>Kiểm tra</b> để check thủ công!",
                        parse_mode="HTML"
                    )
                    break
                continue

            error_count = 0

            # Check for OTP
            if result.get("status") == 1:
                data = result.get("data", {})
                otp = data.get("code")

                if otp:
                    bot.send_message(
                        chat_id,
                        f"🎉 <b>OTP ĐÃ VỀ!</b>\n\n"
                        f"🔑 Mã OTP: <code>{otp}</code>\n"
                        f"📱 Số: <code>{data.get('phone', 'N/A')}</code>\n\n"
                        f"✨ Sử dụng ngay nhé!",
                        parse_mode="HTML"
                    )
                    break
        else:
            # Timeout
            bot.send_message(
                chat_id,
                "⏰ Chưa có OTP sau 90s.\n\n"
                "💡 Dùng 🔍 <b>Kiểm tra</b> để xem lại!",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Auto check error: {e}")
    finally:
        active_checks.pop(check_key, None)

# ==================== BOT KEYBOARDS ====================
def get_main_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("💰 Số dư", "📋 Dịch vụ")
    kb.add("📱 Tạo đơn", "🔍 Kiểm tra")
    kb.add("📞 Zalo SMS", "🔄 Tiếp tục")
    kb.add("ℹ️ Trợ giúp")
    return kb

# ==================== BOT HANDLERS ====================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    welcome = (
        "🤖 <b>BOT THUÊ SỐ 365OTP</b>\n\n"
        "✨ <b>Tính năng:</b>\n"
        "• Tự động check OTP\n"
        "• Hỗ trợ nhiều quốc gia\n"
        "• Giao diện thân thiện\n\n"
        "💡 <i>Chọn chức năng bên dưới để bắt đầu!</i>"
    )
    bot.send_message(
        message.chat.id,
        welcome,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text == "💰 Số dư")
def cmd_balance(message):
    bot.send_chat_action(message.chat.id, 'typing')

    result = get_balance()

    if result.get("status") == 1:
        balance = result.get("balance", 0)
        bot.reply_to(
            message,
            f"💰 <b>Số dư tài khoản</b>\n\n"
            f"💵 ${balance:.2f}\n\n"
            f"📊 Cập nhật: {datetime.now().strftime('%H:%M:%S')}",
            parse_mode="HTML"
        )
    else:
        bot.reply_to(message, result.get("message", "❌ Không lấy được số dư"))

@bot.message_handler(func=lambda m: m.text == "📋 Dịch vụ")
def cmd_services(message):
    bot.send_chat_action(message.chat.id, 'typing')

    result = get_services()

    if isinstance(result, dict) and result.get("status") == 0:
        bot.reply_to(message, result.get("message"))
        return

    if isinstance(result, list) and len(result) > 0:
        # Paginate services
        text = "📋 <b>DỊCH VỤ PHỔ BIẾN</b>\n\n"

        for service in result[:20]:
            sid = service.get('serviceId')
            name = service.get('name', 'Unknown')
            price = service.get('price', 0)
            text += f"🔹 <code>{sid}</code> • {name}\n 💵 ${price}\n\n"

        text += "💡 <i>Dùng 📱 Tạo đơn để thuê số</i>"

        bot.reply_to(message, text, parse_mode="HTML")
    else:
        bot.reply_to(message, "❌ Không có dịch vụ nào")

@bot.message_handler(func=lambda m: m.text == "📱 Tạo đơn")
def cmd_create_order(message):
    instructions = (
        "📝 <b>HƯỚNG DẪN TẠO ĐƠN</b>\n\n"
        "<b>Cú pháp:</b>\n"
        "<code>serviceId [country] [network] [prefix] [true]</code>\n\n"
        "<b>Ví dụ:</b>\n"
        "• <code>656</code> (chỉ service ID)\n"
        "• <code>656 251</code> (service + country)\n"
        "• <code>656 251 1</code> (+ network)\n"
        "• <code>656 251 1 !099</code> (+ prefix)\n"
        "• <code>656 251 1 !099 true</code> (+ SMS Zalo)\n\n"
        "📌 <b>Ghi chú:</b>\n"
        "• Country: 10=VN, 251=US, v.v.\n"
        "• Network: 1=Viettel, 2=Vinaphone, v.v.\n"
        "• Prefix: !099, !088, v.v.\n"
        "• true = gửi SMS Zalo tự động"
    )

    msg = bot.reply_to(message, instructions, parse_mode="HTML")
    bot.register_next_step_handler(msg, process_create_order)

def process_create_order(message):
    loading = None

    try:
        # Parse input
        parts = message.text.strip().split()

        if not parts:
            bot.reply_to(message, "❌ Vui lòng nhập service ID!")
            return

        service_id = int(parts[0])
        country_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        network = None
        prefix = None
        send_sms = "true" in [p.lower() for p in parts]

        # Parse network and prefix
        for part in parts[2:]:
            if part.lower() == "true":
                continue
            if part.startswith("!"):
                prefix = part
            elif not network:
                network = part

        # Show loading
        bot.send_chat_action(message.chat.id, 'typing')
        loading = bot.reply_to(message, "⏳ Đang tạo đơn hàng...")

        # Create order
        result = create_order(service_id, country_id, network, prefix, send_sms)

        # Delete loading message
        try:
            bot.delete_message(message.chat.id, loading.message_id)
        except:
            pass

        if result.get("status") == 1:
            order_id = result.get("id")
            phone = result.get("phone")

            # Save order
            user_orders[message.chat.id] = order_id

            # Send success message
            success_msg = (
                "✅ <b>TẠO ĐƠN THÀNH CÔNG!</b>\n\n"
                f"📱 Số điện thoại: <code>{phone}</code>\n"
                f"🆔 Mã đơn: <code>{order_id}</code>\n"
                f"🌐 Dịch vụ: {service_id}\n\n"
                f"⏳ <i>Đang tự động chờ OTP...</i>\n\n"
                f"💡 Bạn cũng có thể dùng 🔍 <b>Kiểm tra</b> để xem thủ công"
            )

            bot.reply_to(message, success_msg, parse_mode="HTML")

            # Start auto check
            threading.Thread(
                target=auto_check_otp,
                args=(message.chat.id, order_id),
                daemon=True
            ).start()
        else:
            error_msg = result.get("message", "Tạo đơn thất bại")
            bot.reply_to(message, f"❌ {error_msg}")

    except ValueError:
        bot.reply_to(message, "❌ Service ID phải là số!")
    except Exception as e:
        logger.error(f"Create order error: {e}")
        bot.reply_to(message, ERRORS['unknown'])
    finally:
        if loading:
            try:
                bot.delete_message(message.chat.id, loading.message_id)
            except:
                pass

@bot.message_handler(func=lambda m: m.text == "🔍 Kiểm tra")
def cmd_check_order(message):
    order_id = user_orders.get(message.chat.id)

    if order_id:
        do_check_order(message, order_id)
    else:
        msg = bot.reply_to(
            message,
            "🔍 <b>Kiểm tra đơn hàng</b>\n\n"
            "Nhập mã đơn hàng cần kiểm tra:",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, lambda m: do_check_order(m, m.text))

def do_check_order(message, order_id):
    try:
        order_id = int(order_id)
    except:
        bot.reply_to(message, "❌ Mã đơn không hợp lệ!")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    result = check_order(order_id)

    if result.get("status") == 1:
        data = result.get("data", {})
        phone = data.get("phone", "N/A")
        otp = data.get("code")

        if otp:
            status = "✅ Đã có OTP"
            otp_text = f"🔑 <code>{otp}</code>"
        else:
            status = "⏳ Đang chờ OTP"
            otp_text = "⏳ <i>Chưa có</i>"

        response = (
            f"📋 <b>THÔNG TIN ĐƠN HÀNG</b>\n\n"
            f"🆔 Mã đơn: <code>{order_id}</code>\n"
            f"📱 Số điện thoại: <code>{phone}</code>\n"
            f"🔐 Trạng thái: {status}\n"
            f"💬 OTP: {otp_text}\n\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

        bot.reply_to(message, response, parse_mode="HTML")
    else:
        bot.reply_to(message, result.get("message", "❌ Kiểm tra thất bại"))

@bot.message_handler(func=lambda m: m.text == "📞 Zalo SMS")
def cmd_zalo_sms(message):
    order_id = user_orders.get(message.chat.id)

    if not order_id:
        bot.reply_to(message, "❌ Bạn chưa có đơn hàng nào!")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    result = send_zalo_sms(order_id)

    if result.get("status") == 1:
        bot.reply_to(message, "✅ Đã gửi SMS Zalo thành công!")
    else:
        bot.reply_to(message, result.get("message", "❌ Gửi SMS thất bại"))

@bot.message_handler(func=lambda m: m.text == "🔄 Tiếp tục")
def cmd_continue_order(message):
    order_id = user_orders.get(message.chat.id)

    if not order_id:
        bot.reply_to(message, "❌ Bạn chưa có đơn hàng nào!")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    result = continue_order(order_id)

    if result.get("status") == 1:
        bot.reply_to(message, "✅ Đã tiếp tục đơn hàng thành công!")
    else:
        bot.reply_to(message, result.get("message", "❌ Tiếp tục thất bại"))

@bot.message_handler(func=lambda m: m.text == "ℹ️ Trợ giúp")
def cmd_help(message):
    help_text = (
        "ℹ️ <b>HƯỚNG DẪN SỬ DỤNG</b>\n\n"
        "📱 <b>Tạo đơn:</b>\n"
        "Nhập service ID và các tham số tùy chọn\n\n"
        "🔍 <b>Kiểm tra:</b>\n"
        "Xem trạng thái và OTP của đơn hàng\n\n"
        "💰 <b>Số dư:</b>\n"
        "Kiểm tra số dư tài khoản\n\n"
        "📋 <b>Dịch vụ:</b>\n"
        "Xem danh sách dịch vụ có sẵn\n\n"
        "📞 <b>Zalo SMS:</b>\n"
        "Gửi tin nhắn Zalo test\n\n"
        "🔄 <b>Tiếp tục:</b>\n"
        "Gia hạn thời gian đơn hàng\n\n"
        "💡 <i>Bot tự động check OTP sau khi tạo đơn!</i>"
    )
    bot.reply_to(message, help_text, parse_mode="HTML")

# ==================== WEB ROUTES ====================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_data = request.get_json()
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ERROR", 500

@app.route("/")
def home():
    html = f"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OTP Bot Status</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Tahoma, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 40px;
            max-width: 600px;
            width: 100%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }}
        h1 {{
            color: #667eea;
            margin-bottom: 10px;
            font-size: 32px;
        }}
        .status {{
            display: inline-flex;
            align-items: center;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
            margin: 10px 5px;
        }}
        .status.online {{
            background: #10b981;
            color: white;
        }}
        .status.proxy {{
            background: #3b82f6;
            color: white;
        }}
        .card {{
            background: #f8fafc;
            border-radius: 12px;
            padding: 20px;
            margin: 20px 0;
            border-left: 4px solid #667eea;
        }}
        .card h3 {{
            color: #334155;
            margin-bottom: 15px;
            font-size: 18px;
        }}
        .link {{
            display: inline-block;
            color: #667eea;
            text-decoration: none;
            padding: 10px 20px;
            background: #ede9fe;
            border-radius: 8px;
            margin: 5px;
            transition: all 0.3s;
        }}
        .link:hover {{
            background: #667eea;
            color: white;
            transform: translateY(-2px);
        }}
        .time {{
            color: #64748b;
            font-size: 14px;
            margin-top: 10px;
        }}
        ul {{
            list-style: none;
            padding-left: 0;
        }}
        li {{
            padding: 8px 0;
            color: #475569;
            display: flex;
            align-items: center;
        }}
        li:before {{
            content: "•";
            color: #667eea;
            font-weight: bold;
            margin-right: 10px;
            font-size: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 OTP Bot</h1>
        <p style="color: #64748b; margin-bottom: 20px;">365OTP Telegram Bot</p>

        <div>
            <span class="status online">🟢 Online</span>
            <span class="status proxy">{'🔐 Proxy ON' if USE_PROXY else '⚠️ Proxy OFF'}</span>
        </div>

        <div class="card">
            <h3>📊 System Status</h3>
            <ul>
                <li>Bot running normally</li>
                <li>Auto OTP check enabled</li>
                <li>API connection stable</li>
            </ul>
            <p class="time">⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>

        <div class="card">
            <h3>🔧 Quick Actions</h3>
            <a href="/api/test" class="link">🧪 Test API</a>
            <a href="/health" class="link">💚 Health Check</a>
        </div>

        <div class="card">
            <h3>💡 Tips</h3>
            <ul>
                <li>Use /start in Telegram to begin</li>
                <li>Bot auto-checks OTP after order</li>
                <li>Enable proxy if connection fails</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""
    return html, 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()}), 200

@app.route("/api/test")
def test_api():
    try:
        start = time.time()
        result = get_balance()
        latency = (time.time() - start) * 1000

        if result.get("status") == 1:
            return jsonify({
                "status": "✅ Online",
                "balance": result.get("balance"),
                "latency_ms": round(latency, 2),
                "proxy": "enabled" if USE_PROXY else "disabled",
                "timestamp": datetime.now().isoformat()
            }), 200
        else:
            return jsonify({
                "status": "❌ Error",
                "message": result.get("message"),
                "latency_ms": round(latency, 2),
                "timestamp": datetime.now().isoformat()
            }), 500
    except Exception as e:
        return jsonify({
            "status": "❌ Failed",
            "error": str(e)[:100],
            "timestamp": datetime.now().isoformat()
        }), 500

# ==================== STARTUP ====================
if __name__ == "__main__":
    try:
        # Setup webhook
        try:
            bot.remove_webhook()
        except Exception as e:
            logger.warning(f"Remove webhook warning: {e}")

        time.sleep(0.5)

        webhook_url = f"{SERVICE_URL}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_url)

        logger.info(f"✅ Bot started successfully")
        logger.info(f"📡 Webhook: {webhook_url}")

        # Disable Flask logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        # Run Flask app
        port = int(os.environ.get("PORT", 10000))
        app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            threaded=True
        )
    except Exception as e:
        logger.error(f"Startup failed: {e}")
        raise

