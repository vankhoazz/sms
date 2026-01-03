# -*- coding: utf-8 -*-
import os
import time
import threading
import logging
import requests
from collections import defaultdict
from flask import Flask, request
import telebot
from functools import lru_cache

# ================== ENV ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_URL = os.getenv("SERVICE_URL")
BASE_URL = "https://365otp.com/apiv1"
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN or not API_KEY or not SERVICE_URL:
    raise RuntimeError("❌ Thiếu BOT_TOKEN / API_KEY / SERVICE_URL")

# ================== LOG ==================
logging.basicConfig(
    level=logging.WARNING,  # Giảm log → nhanh hơn
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_errors.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("OTP-BOT")

# ================== BOT + FLASK ==========
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8)  # Tăng threads
app = Flask(__name__)

# ================== STORAGE ==============
user_orders = defaultdict(int)

# ================== HTTP SESSION =========
# Connection pooling → giảm latency
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=2
)
session.mount('https://', adapter)
session.mount('http://', adapter)
session.headers.update({
    "User-Agent": "365OTP-TelegramBot/1.0",
    "Connection": "keep-alive"
})

# ================== ERROR MESSAGES =======
ERROR_MESSAGES = {
    'timeout': '⏱️ Kết nối chậm, vui lòng thử lại!',
    'connection': '🔌 Không thể kết nối. Kiểm tra mạng!',
    'http_error': '⚠️ Dịch vụ đang bận. Thử lại sau!',
    'server_error': '❌ Lỗi hệ thống. Thử lại!',
    'unknown': '❌ Có lỗi. Liên hệ admin!',
    'invalid_response': '⚠️ Phản hồi không hợp lệ!',
    'service_unavailable': '🔧 Đang bảo trì!'
}

# ================== HELPER ===============
def send_admin_alert(error_msg, user_id=None, error_type="ERROR"):
    """Gửi alert cho admin - ASYNC để không block"""
    if ADMIN_ID:
        def _send():
            try:
                alert = f"🔴 {error_type}\n"
                if user_id:
                    alert += f"👤 {user_id}\n"
                alert += f"📝 {error_msg}\n⏰ {time.strftime('%H:%M:%S')}"
                bot.send_message(ADMIN_ID, alert)
            except:
                pass
        
        # Chạy async để không chặn response
        threading.Thread(target=_send, daemon=True).start()

def sanitize_error_message(error_str):
    """Loại bỏ thông tin nhạy cảm"""
    import re
    error_str = re.sub(r'https?://[^\s]+', '[URL]', str(error_str))
    error_str = re.sub(r'apikey=[^&\s]+', 'apikey=[***]', error_str)
    error_str = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '[IP]', error_str)
    return error_str[:200]  # Giới hạn độ dài

def safe_api_call(func):
    """Decorator xử lý lỗi nhanh"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout: {func.__name__}")
            send_admin_alert(f"Timeout: {func.__name__}", error_type="TIMEOUT")
            return {"status": -1, "message": ERROR_MESSAGES['timeout']}
        
        except requests.exceptions.ConnectionError:
            logger.warning(f"Connection: {func.__name__}")
            send_admin_alert(f"Connection: {func.__name__}", error_type="CONNECT")
            return {"status": -1, "message": ERROR_MESSAGES['connection']}
        
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if hasattr(e, 'response') else 0
            logger.warning(f"HTTP {code}: {func.__name__}")
            
            if code == 500:
                msg = ERROR_MESSAGES['server_error']
            elif code == 503:
                msg = ERROR_MESSAGES['service_unavailable']
            else:
                msg = ERROR_MESSAGES['http_error']
            
            send_admin_alert(f"HTTP {code}: {func.__name__}", error_type=f"HTTP{code}")
            return {"status": -1, "message": msg}
        
        except ValueError:
            logger.warning(f"JSON: {func.__name__}")
            send_admin_alert(f"JSON: {func.__name__}", error_type="JSON")
            return {"status": -1, "message": ERROR_MESSAGES['invalid_response']}
        
        except Exception as e:
            sanitized = sanitize_error_message(str(e))
            logger.error(f"Unknown: {sanitized}")
            send_admin_alert(f"Error: {sanitized}", error_type="UNKNOWN")
            return {"status": -1, "message": ERROR_MESSAGES['unknown']}
    
    return wrapper

# ================== API ==================
@safe_api_call
def api_get(endpoint, params=None):
    """API call tối ưu tốc độ"""
    params = params or {}
    params["apikey"] = API_KEY
    
    r = session.get(
        f"{BASE_URL}/{endpoint}", 
        params=params, 
        timeout=10  # Giảm từ 15s → 10s
    )
    r.raise_for_status()
    return r.json()

def get_balance():
    return api_get("getbalance")

# Cache services 30s để giảm API calls
@lru_cache(maxsize=1)
def _get_services_cached(timestamp):
    return api_get("availableservice")

def get_services():
    # Cache 30 giây
    current_time = int(time.time() / 30)
    return _get_services_cached(current_time)

def create_order(service_id, country_id=10, network_id=None, prefix=None, send_sms=False):
    params = {"serviceId": service_id, "countryId": country_id}
    if network_id:
        params["networkId"] = network_id
    if prefix:
        params["prefix"] = prefix
    if send_sms:
        params["sendSms"] = "true"
    return api_get("orderv2", params)

def check_order(order_id):
    return api_get("ordercheck", {"id": order_id})

def send_zalo_sms(order_id):
    return api_get("sendsmszalo", {"id": order_id})

def continue_order(order_id):
    return api_get("continueorder", {"orderId": order_id})

# ================== AUTO CHECK OTP =======
def auto_check(chat_id, order_id):
    """Auto check với backoff thông minh"""
    try:
        error_count = 0
        notified = False
        
        # Intervals: 5s → 7s → 10s
        intervals = [5, 5, 5, 7, 7, 10, 10, 10]
        
        for i in range(len(intervals) * 3):  # ~200s
            time.sleep(intervals[min(i, len(intervals)-1)])
            
            r = check_order(order_id)
            
            if r.get("status") == -1:
                error_count += 1
                if error_count == 1 and not notified:
                    bot.send_message(chat_id, "⏳ Kết nối chậm, đang thử lại...")
                    notified = True
                
                if error_count >= 3:
                    bot.send_message(
                        chat_id,
                        f"⚠️ {r.get('message')}\n💡 Dùng 🔍 Kiểm tra!"
                    )
                    return
                continue
            
            error_count = 0
            
            if r.get("status") == 1:
                data = r.get("data", {})
                if data.get("code"):
                    bot.send_message(
                        chat_id,
                        f"🎉 <b>OTP ĐÃ VỀ!</b>\n\n"
                        f"🔑 <code>{data['code']}</code>\n"
                        f"📱 <code>{data.get('phone', '')}</code>",
                        parse_mode="HTML"
                    )
                    return
        
        bot.send_message(chat_id, "⏰ Hết thời gian. Dùng 🔍 Kiểm tra!")
    except:
        pass  # Silent fail, không làm gián đoạn user

# ================== BOT HANDLER ==========
# Response nhanh - keyboard có sẵn
MAIN_KEYBOARD = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
MAIN_KEYBOARD.add("💰 Số dư", "📋 Dịch vụ")
MAIN_KEYBOARD.add("📱 Tạo đơn", "🔍 Kiểm tra")
MAIN_KEYBOARD.add("📞 Zalo SMS", "🔄 Tiếp tục")

@bot.message_handler(commands=["start"])
def start(message):
    # Reply ngay lập tức
    bot.send_message(
        message.chat.id,
        "🤖 <b>BOT THUÊ SỐ 365OTP</b>\n\n"
        "✨ Chọn chức năng:\n"
        "💡 <i>Auto check OTP sau khi tạo đơn</i>",
        reply_markup=MAIN_KEYBOARD,
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: m.text == "💰 Số dư")
def balance(message):
    # Typing action để user biết đang xử lý
    bot.send_chat_action(message.chat.id, 'typing')
    
    r = get_balance()
    if r.get("status") == 1:
        bot.reply_to(message, f"💰 ${r.get('balance', 0):.2f}")
    elif r.get("status") == -1:
        bot.reply_to(message, r.get("message"))
    else:
        bot.reply_to(message, "❌ Lỗi lấy số dư")

@bot.message_handler(func=lambda m: m.text == "📋 Dịch vụ")
def services(message):
    bot.send_chat_action(message.chat.id, 'typing')
    
    r = get_services()
    
    if isinstance(r, dict) and r.get("status") == -1:
        bot.reply_to(message, r.get("message"))
        return
    
    if isinstance(r, list) and len(r) > 0:
        # Format ngắn gọn hơn
        text = "📋 <b>DỊCH VỤ:</b>\n\n"
        for s in r[:50]:  # Giảm từ 15 → 12
            text += f"<code>{s['serviceId']}</code> {s['name']} ${s['price']}\n"
        text += "\n💡 Dùng 📱 Tạo đơn"
        bot.reply_to(message, text, parse_mode="HTML")
    else:
        bot.reply_to(message, "❌ Không có dịch vụ")

@bot.message_handler(func=lambda m: m.text == "📱 Tạo đơn")
def create(message):
    # Reply ngay không cần API
    msg = bot.reply_to(
        message,
        "📝 <b>TẠO ĐƠN:</b>\n\n"
        "Cú pháp: <code>serviceId [country] [network] [prefix] [true]</code>\n\n"
        "VD: <code>267 10 viettel !099 true</code>",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, process_create)

def process_create(message):
    processing_msg = None
    try:
        parts = message.text.split()
        service_id = int(parts[0])
        country_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        network_id = None
        prefix = None
        send_sms = "true" in parts

        for p in parts[2:]:
            if p == "true":
                continue
            if not network_id:
                network_id = p
            else:
                prefix = p

        # Typing action
        bot.send_chat_action(message.chat.id, 'typing')
        processing_msg = bot.reply_to(message, "⏳ Đang tạo...")

        r = create_order(service_id, country_id, network_id, prefix, send_sms)
        
        # Xóa message loading
        try:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass

        if r.get("status") == 1:
            order_id = r["id"]
            user_orders[message.chat.id] = order_id
            
            bot.reply_to(
                message,
                f"✅ <b>THÀNH CÔNG!</b>\n\n"
                f"📱 <code>{r['phone']}</code>\n"
                f"🧾 <code>{order_id}</code>\n\n"
                f"⏳ <i>Đang chờ OTP...</i>",
                parse_mode="HTML"
            )
            
            # Start auto check ASYNC
            threading.Thread(
                target=auto_check,
                args=(message.chat.id, order_id),
                daemon=True
            ).start()
        elif r.get("status") == -1:
            bot.reply_to(message, r.get("message"))
        else:
            bot.reply_to(message, f"❌ {r.get('message', 'Thất bại')}")
            
    except ValueError:
        if processing_msg:
            try:
                bot.delete_message(message.chat.id, processing_msg.message_id)
            except:
                pass
        bot.reply_to(message, "❌ Sai định dạng!")
    except Exception as e:
        if processing_msg:
            try:
                bot.delete_message(message.chat.id, processing_msg.message_id)
            except:
                pass
        logger.error(f"Create: {sanitize_error_message(str(e))}")
        bot.reply_to(message, ERROR_MESSAGES['unknown'])

@bot.message_handler(func=lambda m: m.text == "🔍 Kiểm tra")
def check(message):
    if message.chat.id in user_orders:
        do_check(message, user_orders[message.chat.id])
    else:
        msg = bot.reply_to(message, "🔍 Nhập mã đơn:")
        bot.register_next_step_handler(msg, lambda m: do_check(m, int(m.text)))

def do_check(message, order_id):
    bot.send_chat_action(message.chat.id, 'typing')
    
    r = check_order(order_id)
    
    if r.get("status") == 1:
        d = r["data"]
        otp = d.get('code', '⏳ Chờ...')
        bot.reply_to(
            message,
            f"📋 <b>ĐƠN HÀNG:</b>\n\n"
            f"📱 <code>{d['phone']}</code>\n"
            f"🔑 <code>{otp}</code>",
            parse_mode="HTML"
        )
    elif r.get("status") == -1:
        bot.reply_to(message, r.get("message"))
    else:
        bot.reply_to(message, f"❌ {r.get('message', 'Thất bại')}")

@bot.message_handler(func=lambda m: m.text == "📞 Zalo SMS")
def zalo(message):
    if message.chat.id in user_orders:
        bot.send_chat_action(message.chat.id, 'typing')
        r = send_zalo_sms(user_orders[message.chat.id])
        
        if r.get("status") == 1:
            bot.reply_to(message, "✅ Đã gửi!")
        elif r.get("status") == -1:
            bot.reply_to(message, r.get("message"))
        else:
            bot.reply_to(message, f"❌ {r.get('message', 'Thất bại')}")
    else:
        bot.reply_to(message, "❌ Chưa có đơn!")

@bot.message_handler(func=lambda m: m.text == "🔄 Tiếp tục")
def cont(message):
    if message.chat.id in user_orders:
        bot.send_chat_action(message.chat.id, 'typing')
        r = continue_order(user_orders[message.chat.id])
        
        if r.get("status") == 1:
            bot.reply_to(message, "✅ Đã tiếp tục!")
        elif r.get("status") == -1:
            bot.reply_to(message, r.get("message"))
        else:
            bot.reply_to(message, f"❌ {r.get('message', 'Thất bại')}")
    else:
        bot.reply_to(message, "❌ Chưa có đơn!")

# ================== WEBHOOK ==============
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = telebot.types.Update.de_json(request.get_json(force=True))
        bot.process_new_updates([update])
        return "OK", 200
    except:
        return "ERROR", 500

@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# Disable Flask logging để nhanh hơn
import logging as flask_logging
flask_log = flask_logging.getLogger('werkzeug')
flask_log.setLevel(flask_logging.ERROR)

# ================== RUN ==================
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(1)  # Giảm từ 2s → 1s

        webhook_url = f"{SERVICE_URL}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook: {webhook_url}")
        
        # Tắt debug mode → nhanh hơn
        app.run(
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            debug=False,
            threaded=True
        )
    except Exception as e:
        logger.error(f"Startup: {sanitize_error_message(str(e))}")
        raise
