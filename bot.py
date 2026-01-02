# -*- coding: utf-8 -*-
import os
import time
import threading
import logging
import requests
from collections import defaultdict
from flask import Flask, request
import telebot

# ===== ENV =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY")
SERVICE_URL = os.getenv("SERVICE_URL")  # https://xxx.onrender.com
BASE_URL = "https://365otp.com/apiv1"

if not BOT_TOKEN or not API_KEY or not SERVICE_URL:
    raise RuntimeError("❌ Thiếu BOT_TOKEN / API_KEY / SERVICE_URL")

# ===== LOG =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OTP-BOT")

# ===== BOT + FLASK =====
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

# ===== STORAGE =====
user_orders = defaultdict(int)

# ===== HTTP SESSION =====
session = requests.Session()
session.headers.update({
    "User-Agent": "365OTP-TelegramBot/1.0"
})

# ===== API =====
def api_get(endpoint, params=None):
    try:
        params = params or {}
        params["apikey"] = API_KEY
        r = session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(e)
        return {"status": -1, "message": str(e)}

def get_balance():
    return api_get("getbalance")

def get_services():
    return api_get("availableservice")

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

# ===== AUTO CHECK OTP =====
def auto_check(chat_id, order_id):
    for _ in range(30):  # ~150s
        time.sleep(5)
        r = check_order(order_id)
        if r.get("status") == 1:
            data = r.get("data", {})
            if data.get("code"):
                bot.send_message(
                    chat_id,
                    f"🎉 OTP về!\n\n🔑 {data['code']}"
                )
                return

# ===== BOT COMMAND =====
@bot.message_handler(commands=["start"])
def start(message):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💰 Số dư", "📋 Dịch vụ")
    kb.add("📱 Tạo đơn", "🔍 Kiểm tra")
    kb.add("📞 Zalo SMS", "🔄 Tiếp tục")
    bot.send_message(
        message.chat.id,
        "🤖 BOT THUÊ SỐ 365OTP\n\nChọn chức năng:",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: m.text == "💰 Số dư")
def balance(message):
    r = get_balance()
    bot.reply_to(
        message,
        f"Số dư: ${r.get('balance', 0)}"
        if r.get("status") == 1 else r.get("message")
    )

@bot.message_handler(func=lambda m: m.text == "📋 Dịch vụ")
def services(message):
    r = get_services()
    if isinstance(r, list):
        text = "📋 Dịch vụ:\n\n"
        for s in r[:15]:
            text += f"{s['serviceId']} - {s['name']} (${s['price']})\n"
        bot.reply_to(message, text)
    else:
        bot.reply_to(message, "❌ Không lấy được dịch vụ")

@bot.message_handler(func=lambda m: m.text == "📱 Tạo đơn")
def create(message):
    msg = bot.reply_to(
        message,
        "Nhập:\nserviceId [countryId] [networkId] [prefix] [true]\nVD: 267 10 viettel !099 true"
    )
    bot.register_next_step_handler(msg, process_create)

def process_create(message):
    try:
        parts = message.text.split()
        service_id = int(parts[0])
        country_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        network_id = None
        prefix = None
        send_sms = "true" in parts

        for p in parts[2:]:
            if p.isdigit():
                continue
            if p == "true":
                continue
            if not network_id:
                network_id = p
            else:
                prefix = p

        r = create_order(service_id, country_id, network_id, prefix, send_sms)
        if r.get("status") == 1:
            order_id = r["id"]
            user_orders[message.chat.id] = order_id
            bot.reply_to(
                message,
                f"✅ Tạo đơn thành công\n📱 SĐT: {r['phone']}\n🧾 Mã đơn: {order_id}"
            )
            threading.Thread(
                target=auto_check,
                args=(message.chat.id, order_id),
                daemon=True
            ).start()
        else:
            bot.reply_to(message, r.get("message"))
    except:
        bot.reply_to(message, "❌ Sai định dạng")

@bot.message_handler(func=lambda m: m.text == "🔍 Kiểm tra")
def check(message):
    if message.chat.id in user_orders:
        do_check(message, user_orders[message.chat.id])
    else:
        msg = bot.reply_to(message, "Nhập mã đơn:")
        bot.register_next_step_handler(msg, lambda m: do_check(m, int(m.text)))

def do_check(message, order_id):
    r = check_order(order_id)
    if r.get("status") == 1:
        d = r["data"]
        bot.reply_to(
            message,
            f"📱 SĐT: {d['phone']}\n🔑 OTP: {d.get('code','Đang chờ')}"
        )
    else:
        bot.reply_to(message, r.get("message"))

@bot.message_handler(func=lambda m: m.text == "📞 Zalo SMS")
def zalo(message):
    if message.chat.id in user_orders:
        r = send_zalo_sms(user_orders[message.chat.id])
        bot.reply_to(message, "✅ Đã gửi" if r.get("status") == 1 else r.get("message"))

@bot.message_handler(func=lambda m: m.text == "🔄 Tiếp tục")
def cont(message):
    if message.chat.id in user_orders:
        r = continue_order(user_orders[message.chat.id])
        bot.reply_to(message, "✅ Đã tiếp tục" if r.get("status") == 1 else r.get("message"))

# ===== WEBHOOK =====
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_json())
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def home():
    return "BOT OTP đang chạy!", 200

@app.route("/health")
def health():
    return "OK", 200

SERVICE_URL = os.getenv("SERVICE_URL")

if __name__ == "__main__":
    bot.remove_webhook()
    time.sleep(2)
    bot.set_webhook(
        url=f"{SERVICE_URL}/{BOT_TOKEN}"
    )
    print(f"✅ Webhook: {SERVICE_URL}/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

