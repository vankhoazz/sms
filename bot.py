# -*- coding: utf-8 -*-
"""
BOT THUÊ SỐ OTP - sms-verification-number.com
Viết theo phong cách & cấu trúc của bot 365otp (Webhook + Flask + auto check + retry)
"""
import os
import sys
import time
import logging
import threading
from collections import defaultdict
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify

import telebot
from telebot import types

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_URL = os.getenv("SERVICE_URL")          # domain của bạn, ví dụ: https://your-bot-domain.com
ADMIN_ID = os.getenv("ADMIN_ID")

BASE_URL = "https://sms-verification-number.com/stubs/handler_api"

if not all([BOT_TOKEN, API_KEY, SERVICE_URL]):
    print("Thiếu BOT_TOKEN, API_KEY hoặc SERVICE_URL")
    sys.exit(1)

# Proxy (nếu cần)
USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL")

COUNTRY_VN = 10
REQUEST_TIMEOUT = 12
OTP_CHECK_INTERVAL_BASE = 5

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('smsverif_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== FLASK & BOT ====================
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)

# ==================== STORAGE ====================
user_orders = defaultdict(lambda: None)  # user_id -> order info
active_checks = {}  # theo dõi thread check OTP

# ==================== HTTP SESSION + RETRY ====================
session = requests.Session()

retry_strategy = Retry(
    total=4,
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

if USE_PROXY and PROXY_URL:
    session.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
    logger.info("Proxy đã bật")

session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml"
})

# ==================== ERROR MESSAGES ====================
ERRORS = {
    'timeout': '⏱️ Kết nối chậm, vui lòng thử lại!',
    'connection': '🔌 Không kết nối được với dịch vụ!',
    'http_error': '⚠️ Dịch vụ đang bận, thử lại sau vài giây',
    'server_error': '❌ Lỗi từ phía server dịch vụ',
    'unknown': '❌ Có lỗi xảy ra. Vui lòng thử lại hoặc liên hệ admin!'
}

# ==================== UTILITIES ====================
def notify_admin(message, user_id=None):
    if not ADMIN_ID:
        return
    try:
        text = f"🚨 ALERT SMS-VERIF\n\n{message}"
        if user_id:
            text += f"\nUser: {user_id}"
        text += f"\n🕒 {datetime.now().strftime('%H:%M:%S')}"
        bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logger.error(f"Không gửi được thông báo admin: {e}")

def safe_api_call(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.Timeout:
            return {"status": "error", "message": ERRORS['timeout']}
        except requests.exceptions.ConnectionError:
            return {"status": "error", "message": ERRORS['connection']}
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 429:
                return {"status": "error", "message": "Quá nhiều yêu cầu, chờ 1 phút"}
            elif code >= 500:
                return {"status": "error", "message": ERRORS['server_error']}
            else:
                return {"status": "error", "message": ERRORS['http_error']}
        except Exception as e:
            logger.error(f"Lỗi trong {func.__name__}: {e}")
            notify_admin(f"Lỗi {func.__name__}: {str(e)[:120]}")
            return {"status": "error", "message": ERRORS['unknown']}
    return wrapper

# ==================== API FUNCTIONS ====================
@safe_api_call
def api_call(params):
    params = params.copy()
    params.update({"api_key": API_KEY, "lang": "en"})
    r = session.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    text = r.text.strip()
    return {"status": "ok", "data": text}

def get_balance():
    res = api_call({"action": "getBalance"})
    if res["status"] == "ok" and res["data"].startswith("ACCESS_BALANCE"):
        return float(res["data"].split(":",1)[1])
    return None

def get_services_vn():
    res = api_call({
        "action": "getServices",
        "country": COUNTRY_VN
    })
    if res["status"] != "ok":
        return []

    services = {}
    for line in res["data"].splitlines():
        if ':' in line:
            code, name = line.split(":", 1)
            services[code.strip()] = name.strip()
    return services

def get_number(service, operator="any"):
    res = api_call({
        "action": "getNumberV2",
        "service": service,
        "country": COUNTRY_VN,
        "operator": operator
    })
    if res["status"] == "ok" and res["data"].startswith("ACCESS_NUMBER"):
        parts = res["data"].split(":")
        if len(parts) >= 3:
            return {
                "activation_id": parts[1],
                "phone": parts[2],
                "price": parts[3] if len(parts) > 3 else "?"
            }
    return None

def get_status(activation_id):
    res = api_call({"action": "getStatus", "id": activation_id})
    if res["status"] == "ok":
        return res["data"]
    return "ERROR"

# ==================== AUTO CHECK OTP ====================
def auto_check_otp(chat_id, activation_id):
    check_key = f"{chat_id}_{activation_id}"
    if check_key in active_checks:
        return

    active_checks[check_key] = True

    try:
        intervals = [5, 5, 7, 7, 10, 10, 15, 15, 20]  # backoff
        for wait in intervals:
            time.sleep(wait)

            status = get_status(activation_id)

            if status.startswith("STATUS_OK"):
                code = status.split(":", 1)[1]
                bot.send_message(
                    chat_id,
                    f"🎉 <b>OTP ĐÃ VỀ!</b>\n\n"
                    f"🔑 Mã: <code>{code}</code>\n"
                    f"📱 Số: <code>{user_orders[chat_id]['phone']}</code>",
                    parse_mode="HTML"
                )
                break

            elif any(x in status for x in ["STATUS_CANCEL", "STATUS_FINISH", "NO_ACTIVATION"]):
                bot.send_message(chat_id, "Đơn đã kết thúc hoặc bị hủy.")
                break

        else:
            bot.send_message(
                chat_id,
                "⏰ Không nhận được OTP sau ~90s.\n"
                "Bạn có thể kiểm tra thủ công bằng nút 🔍 Kiểm tra"
            )

    except Exception as e:
        logger.error(f"Auto check lỗi: {e}")
    finally:
        active_checks.pop(check_key, None)

# ==================== KEYBOARDS ====================
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("💰 Số dư", "📋 Dịch vụ")
    kb.add("📱 Thuê số", "🔍 Kiểm tra")
    kb.add("ℹ️ Trợ giúp")
    return kb

# ==================== BOT HANDLERS ====================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    text = (
        "<b>🤖 BOT THUÊ SỐ SMS-VERIFICATION</b>\n\n"
        "Dịch vụ số ảo Việt Nam\n\n"
        "Các nút chính:\n"
        "📱 Thuê số → bắt đầu thuê\n"
        "🔍 Kiểm tra → check OTP thủ công\n"
        "💰 Số dư → xem tài khoản\n"
        "📋 Dịch vụ → danh sách dịch vụ"
    )
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text == "💰 Số dư")
def cmd_balance(message):
    balance = get_balance()
    if balance is not None:
        text = f"💰 <b>Số dư hiện tại</b>: {balance:.2f} RUB"
    else:
        text = "❌ Không lấy được số dư. API có vấn đề?"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📋 Dịch vụ")
def cmd_services(message):
    services = get_services_vn()
    if not services:
        bot.reply_to(message, "❌ Không tải được danh sách dịch vụ")
        return

    text = "<b>📋 Danh sách dịch vụ phổ biến</b>\n\n"
    for code, name in list(services.items())[:15]:
        text += f"<code>{code}</code> • {name}\n"
    text += "\nDùng <b>📱 Thuê số</b> và nhập mã dịch vụ"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "📱 Thuê số")
def cmd_create_order(message):
    text = (
        "<b>📝 Cách thuê số</b>\n\n"
        "Gửi mã dịch vụ + (tùy chọn) nhà mạng\n\n"
        "<b>Ví dụ:</b>\n"
        "<code>ot</code>           → Telegram bất kỳ\n"
        "<code>wa viettel</code>   → WhatsApp Viettel\n"
        "<code>fb mobifone</code>  → Facebook MobiFone\n\n"
        "Nhà mạng hỗ trợ: any, viettel, vinaphone, mobifone"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    uid = message.chat.id
    text = message.text.strip().lower().split()

    if len(text) == 0:
        return

    service = text[0]
    operator = text[1] if len(text) > 1 else "any"

    if user_orders[uid] is not None:
        bot.reply_to(message, "⚠️ Bạn đang có đơn đang chờ OTP. Vui lòng chờ hoặc hủy trước.")
        return

    loading = bot.reply_to(message, "⏳ Đang thuê số...")

    number_info = get_number(service, operator)

    try:
        bot.delete_message(uid, loading.message_id)
    except:
        pass

    if number_info:
        user_orders[uid] = {
            "activation_id": number_info["activation_id"],
            "phone": number_info["phone"],
            "price": number_info["price"],
            "operator": operator,
            "service": service,
            "time": datetime.now()
        }

        success = (
            "✅ <b>THUÊ SỐ THÀNH CÔNG</b>\n\n"
            f"📱 Số: <code>{number_info['phone']}</code>\n"
            f"🆔 ID: <code>{number_info['activation_id']}</code>\n"
            f"💰 Giá: {number_info['price']} RUB\n\n"
            "⏳ Đang tự động chờ OTP..."
        )
        bot.reply_to(message, success, parse_mode="HTML")

        threading.Thread(
            target=auto_check_otp,
            args=(uid, number_info["activation_id"]),
            daemon=True
        ).start()

    else:
        bot.reply_to(message, f"❌ Không thuê được số cho dịch vụ <code>{service}</code> - {operator}")

@bot.message_handler(func=lambda m: m.text == "🔍 Kiểm tra")
def cmd_check(message):
    order = user_orders.get(message.chat.id)
    if not order:
        bot.reply_to(message, "Bạn chưa có đơn hàng nào đang hoạt động.")
        return

    status = get_status(order["activation_id"])

    if status.startswith("STATUS_OK"):
        code = status.split(":",1)[1]
        reply = f"🎉 <b>ĐÃ CÓ OTP!</b>\n\n🔑 <code>{code}</code>"
    elif "NO_ACTIVATION" in status:
        reply = "Đơn không tồn tại hoặc đã hết hạn"
    else:
        reply = "⏳ Vẫn chưa có OTP..."

    bot.reply_to(message, reply, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "ℹ️ Trợ giúp")
def cmd_help(message):
    cmd_start(message)

# ==================== WEBHOOK ====================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ERROR", 500

@app.route("/")
def home():
    return f"""
    <h1>SMS-Verification Telegram Bot</h1>
    <p>Status: <b>Online</b></p>
    <p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p>Proxy: {'ON' if USE_PROXY else 'OFF'}</p>
    """

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ==================== MAIN ====================
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(0.5)

        webhook_url = f"{SERVICE_URL.rstrip('/')}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_url)

        logger.info(f"Webhook set: {webhook_url}")

        port = int(os.environ.get("PORT", 8443))
        app.run(host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        logger.critical(f"Startup failed: {e}")
        sys.exit(1)
