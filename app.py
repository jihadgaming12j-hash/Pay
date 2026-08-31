"""
Telegram Bot + Mr Ai Pay (mraipay.top) Payment Gateway Integration
-------------------------------------------------------------------
Flow:
  1. User sends /pay <amount> in Telegram
  2. Bot calls Mr Ai Pay "create" API -> gets payment_url
  3. Bot sends payment_url as inline button to user
  4. User completes payment on Mr Ai Pay checkout page
  5. Mr Ai Pay redirects browser to our /success or /cancel route
     with transactionId, paymentMethod, paymentAmount, status as query params
  6. Our /success route calls Mr Ai Pay "verify" API to confirm the
     transaction server-side, then sends a Telegram message to the user
     confirming payment and delivering the product / adding balance.

Deploy target: Vercel (serverless) -> uses Telegram WEBHOOK, not polling.
"""

import os
import json
import logging
import requests
from flask import Flask, request, jsonify, redirect

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mraipay-bot")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG — set these as environment variables (never hardcode secrets)
# ---------------------------------------------------------------------------
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
MRAIPAY_API_KEY    = os.environ.get("MRAIPAY_API_KEY", "")
MRAIPAY_SECRET_KEY = os.environ.get("MRAIPAY_SECRET_KEY", "")
MRAIPAY_BRAND_KEY  = os.environ.get("MRAIPAY_BRAND_KEY", "")

# Your own public base URL (Vercel deployment URL), used to build success/cancel links
# e.g. "https://your-bot-name.vercel.app"
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

MRAIPAY_CREATE_URL = "https://pay.mraipay.top/api/payment/create"
MRAIPAY_VERIFY_URL = "https://pay.mraipay.top/api/payment/verify"

MRAIPAY_HEADERS = {
    "Content-Type": "application/json",
    "API-KEY": MRAIPAY_API_KEY,
    "SECRET-KEY": MRAIPAY_SECRET_KEY,
    "BRAND-KEY": MRAIPAY_BRAND_KEY,
}

# In-memory store: transaction_id -> telegram chat_id
# NOTE: on serverless (Vercel) this dict resets between cold starts.
# For production, replace with a real database (e.g. Redis, Postgres, Supabase).
PENDING_TX = {}


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------
def tg_send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)
        if not r.ok:
            log.error("Telegram sendMessage failed: %s", r.text)
    except Exception as e:
        log.exception("Telegram sendMessage error: %s", e)


# ---------------------------------------------------------------------------
# Mr Ai Pay helpers
# ---------------------------------------------------------------------------
def mraipay_create_payment(cus_name, cus_email, amount, chat_id):
    """Create a payment intent and return the payment_url (or None on failure)."""
    payload = {
        "cus_name": cus_name,
        "cus_email": cus_email,
        "amount": str(amount),
        "success_url": f"{BASE_URL}/success",
        "cancel_url": f"{BASE_URL}/cancel",
        "meta_data": {"chat_id": str(chat_id)},
    }
    try:
        res = requests.post(
            MRAIPAY_CREATE_URL, headers=MRAIPAY_HEADERS, json=payload, timeout=20
        )
        data = res.json()
    except Exception as e:
        log.exception("mraipay_create_payment error: %s", e)
        return None

    if data.get("status") is True and data.get("payment_url"):
        return data["payment_url"]

    log.error("mraipay create failed: %s", data)
    return None


def mraipay_verify_payment(transaction_id):
    """Verify a transaction server-side. Returns the verify API response dict."""
    payload = {"transaction_id": transaction_id}
    try:
        res = requests.post(
            MRAIPAY_VERIFY_URL, headers=MRAIPAY_HEADERS, json=payload, timeout=20
        )
        return res.json()
    except Exception as e:
        log.exception("mraipay_verify_payment error: %s", e)
        return {"status": "ERROR", "message": str(e)}


# ---------------------------------------------------------------------------
# Telegram Webhook — receives all bot updates
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message")

    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    first_name = message["chat"].get("first_name", "Customer")

    if text.startswith("/start"):
        tg_send_message(
            chat_id,
            "স্বাগতম! পেমেন্ট করতে লিখুন:\n<code>/pay 100</code>\n(পরিমাণ টাকার অংকে দিন)",
        )

    elif text.startswith("/pay"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().replace(".", "", 1).isdigit():
            tg_send_message(chat_id, "সঠিক অ্যামাউন্ট দিন। উদাহরণ:\n<code>/pay 100</code>")
            return jsonify({"ok": True})

        amount = parts[1].strip()
        cus_email = f"user{chat_id}@telegram.user"  # placeholder if you don't collect email

        payment_url = mraipay_create_payment(
            cus_name=first_name, cus_email=cus_email, amount=amount, chat_id=chat_id
        )

        if payment_url:
            reply_markup = {
                "inline_keyboard": [[{"text": f"Pay {amount} Taka", "url": payment_url}]]
            }
            tg_send_message(chat_id, "পেমেন্ট লিংক তৈরি হয়েছে 👇", reply_markup)
        else:
            tg_send_message(chat_id, "দুঃখিত, পেমেন্ট লিংক তৈরি করা যায়নি। আবার চেষ্টা করুন।")

    else:
        tg_send_message(chat_id, "কমান্ড বুঝতে পারিনি। /pay 100 লিখে চেষ্টা করুন।")

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Success route — Mr Ai Pay redirects the user's browser here after payment
# ---------------------------------------------------------------------------
@app.route("/success", methods=["GET"])
def payment_success():
    transaction_id = request.args.get("transactionId")
    status = request.args.get("status")
    payment_amount = request.args.get("paymentAmount")
    payment_method = request.args.get("paymentMethod")

    if not transaction_id:
        return "Missing transaction id", 400

    # Always verify server-side — never trust the redirect status alone
    result = mraipay_verify_payment(transaction_id)

    verified_status = result.get("status")
    meta = result.get("metadata") or {}
    chat_id = meta.get("chat_id")

    if verified_status == "COMPLETED":
        if chat_id:
            tg_send_message(
                chat_id,
                f"✅ <b>পেমেন্ট সফল হয়েছে!</b>\n\n"
                f"পরিমাণ: {result.get('amount', payment_amount)} টাকা\n"
                f"মাধ্যম: {payment_method or 'N/A'}\n"
                f"ট্রানজেকশন আইডি: <code>{transaction_id}</code>\n\n"
                f"ধন্যবাদ! আপনার balance/order আপডেট করা হয়েছে।",
            )
        return "<h2>Payment Successful ✅ You can return to Telegram now.</h2>"

    else:
        if chat_id:
            tg_send_message(
                chat_id,
                f"⏳ পেমেন্ট এখনও '{verified_status}' অবস্থায় আছে। "
                f"ট্রানজেকশন আইডি: <code>{transaction_id}</code>",
            )
        return f"<h2>Payment status: {verified_status}</h2>"


# ---------------------------------------------------------------------------
# Cancel route — user cancelled the payment
# ---------------------------------------------------------------------------
@app.route("/cancel", methods=["GET"])
def payment_cancel():
    transaction_id = request.args.get("transactionId", "")
    return "<h2>Payment Cancelled ❌ You can return to Telegram and try again.</h2>"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "service": "mraipay-telegram-bot"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
