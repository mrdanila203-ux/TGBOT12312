"""Telegram Spy Bot for Telegram Business API."""

import urllib.request
import urllib.parse
import mimetypes
import json
import time
import logging
import sys
import os
import threading
import base64
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import database as db
from config import BOT_TOKEN, ADMIN_ID, PORT
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL
from config import RUB_PRICE_WEEKLY, RUB_PRICE_MONTHLY, RUB_PRICE_YEARLY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
INSTRUCTION_IMAGE_PATHS = [
    os.path.join(os.path.dirname(__file__), "instruction.jpg"),
    os.path.join(os.path.dirname(__file__), "instruction.png"),
]
PRIVACY_IMAGE_PATHS = [
    os.path.join(os.path.dirname(__file__), "privacy.jpg"),
    os.path.join(os.path.dirname(__file__), "privacy.png"),
]

# Prices in Telegram Stars
PRICE_WEEKLY = 45
PRICE_MONTHLY = 100
PRICE_YEARLY = 550
PAYMENT_PLANS = {
    "weekly": {"days": 7, "stars": PRICE_WEEKLY, "title": "Подписка 7 дней"},
    "monthly": {"days": 30, "stars": PRICE_MONTHLY, "title": "Подписка 30 дней"},
    "yearly": {"days": 365, "stars": PRICE_YEARLY, "title": "Подписка 365 дней"},
}
RUB_PAYMENT_PLANS = {
    "weekly": {"rub": RUB_PRICE_WEEKLY, "title": "Подписка 7 дней"},
    "monthly": {"rub": RUB_PRICE_MONTHLY, "title": "Подписка 30 дней"},
    "yearly": {"rub": RUB_PRICE_YEARLY, "title": "Подписка 365 дней"},
}
MENU_ACTION_TEXTS = {
    "📊 Статус",
    "Статус",
    "⚙️ Настройки",
    "Настройки",
    "📖 Инструкция",
    "🔒 Приватность",
    "💳 Купить подписку",
    "👥 Пригласить друга",
    "💬 Поддержка",
    "◀️ Назад",
}

ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "pre_checkout_query",
]

BOT_USERNAME = ""
MSK = ZoneInfo("Europe/Moscow")
BUSINESS_RATE_LIMIT_PER_MINUTE = int(os.getenv("BUSINESS_RATE_LIMIT_PER_MINUTE", "120"))
_BUSINESS_RATE_BUCKETS = {}


def allow_business_event(owner_id: int, connection_id: str) -> bool:
    now = int(time.time())
    window = now // 60
    key = (owner_id, connection_id, window)
    current = _BUSINESS_RATE_BUCKETS.get(key, 0)
    if current >= BUSINESS_RATE_LIMIT_PER_MINUTE:
        if current == BUSINESS_RATE_LIMIT_PER_MINUTE:
            logging.warning("Business event rate limited owner_id=%s connection_id=%s", owner_id, connection_id)
        _BUSINESS_RATE_BUCKETS[key] = current + 1
        return False
    _BUSINESS_RATE_BUCKETS[key] = current + 1
    if len(_BUSINESS_RATE_BUCKETS) > 5000:
        stale_windows = {bucket_key for bucket_key in _BUSINESS_RATE_BUCKETS if bucket_key[2] < window - 2}
        for bucket_key in stale_windows:
            _BUSINESS_RATE_BUCKETS.pop(bucket_key, None)
    return True


def format_ts_msk(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, MSK).strftime("%d.%m.%Y %H:%M")


def format_db_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value[:10]
    return value.strftime("%d.%m.%Y")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path not in ("/payment-webhook", "/external-payment-webhook", "/yookassa-webhook"):
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8") if length else ""
            data = json.loads(payload or "{}")
            if parsed_path == "/yookassa-webhook":
                if data.get("event") != "payment.succeeded":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                payment = data.get("object", {})
                payment_id = payment.get("id", "")
                if not payment_id:
                    raise ValueError("missing payment id")
                payment_info = get_yookassa_payment(payment_id)
                if payment_info.get("status") != "succeeded":
                    raise ValueError("payment not succeeded")
                metadata = payment_info.get("metadata", {}) or payment.get("metadata", {})
                user_id = int(metadata.get("user_id", 0))
                plan = str(metadata.get("plan", "")).strip()
            else:
                user_id = int(data.get("user_id", 0))
                plan = str(data.get("plan", "")).strip()
                if str(data.get("status", "")).lower() not in ("paid", "success", "succeeded", "ok"):
                    raise ValueError("payment not successful")
            payment_id = data.get("payment_id", "") or data.get("id", "")
            plan_info = PAYMENT_PLANS.get(plan)
            if not user_id or not plan_info:
                raise ValueError("bad payload")
            db.set_subscription(user_id, plan, plan_info["days"])
            send(user_id, f"✅ <b>Оплата прошла!</b>\nПодписка на <b>{plan_info['days']} дней</b> активирована.", keyboard=main_keyboard())
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as exc:
            logging.error("Payment webhook error: %s", exc)
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"bad request")

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info(f"Healthcheck server started on port {PORT}")


def start_cleanup_worker():
    def worker():
        while True:
            try:
                db.cleanup_temp_tables()
            except Exception as exc:
                logging.error("Cleanup error: %s", exc)
            time.sleep(24 * 60 * 60)

    threading.Thread(target=worker, daemon=True).start()


def get_settings(user_id: int) -> dict:
    return db.get_user_settings(user_id)


def api(method, **params):
    url = f"{BASE}/{method}"
    data = json.dumps(params).encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            response = json.loads(r.read())
            if not response.get("ok"):
                logging.error("Telegram API returned error for %s: %s", method, response)
            return response
    except Exception as e:
        logging.error(f"API error {method}: {e}")
        return {"ok": False}


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        params["reply_markup"] = keyboard
    result = api("sendMessage", **params)
    if not result.get("ok"):
        logging.error("sendMessage failed for chat_id=%s", chat_id)
    return result


def send_photo(chat_id, photo_path, caption="", keyboard=None):
    if not os.path.exists(photo_path):
        logging.error("sendPhoto file not found: %s", photo_path)
        return {"ok": False}

    boundary = f"----CodexBoundary{int(time.time() * 1000)}"
    body = bytearray()

    def add_field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
        add_field("parse_mode", "HTML")
    if keyboard:
        add_field("reply_markup", json.dumps(keyboard, ensure_ascii=False))

    mime_type = mimetypes.guess_type(photo_path)[0] or "image/png"
    filename = os.path.basename(photo_path)
    with open(photo_path, "rb") as photo_file:
        photo_data = photo_file.read()

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode())
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.extend(photo_data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    url = f"{BASE}/sendPhoto"
    req = urllib.request.Request(url, data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            response = json.loads(r.read())
            if not response.get("ok"):
                logging.error("Telegram API returned error for sendPhoto: %s", response)
            return response
    except Exception as exc:
        logging.error("sendPhoto failed for chat_id=%s path=%s: %s", chat_id, photo_path, exc)
        return {"ok": False}


def send_invoice(chat_id: int, title: str, description: str, payload: str, amount: int):
    result = api("sendInvoice",
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[{"label": title, "amount": amount}]
    )
    if not result.get("ok"):
        logging.error("sendInvoice failed for chat_id=%s payload=%s amount=%s", chat_id, payload, amount)
    return result


def send_file(chat_id, file_id, file_type, caption=""):
    method_map = {
        "voice": "sendVoice",
        "video_note": "sendVideoNote",
        "audio": "sendAudio",
        "photo": "sendPhoto",
        "video": "sendVideo",
        "document": "sendDocument",
        "sticker": "sendSticker",
    }
    method = method_map.get(file_type, "sendDocument")
    if file_type in ("video_note", "sticker"):
        result = api(method, **{"chat_id": chat_id, file_type: file_id})
        if caption:
            send(chat_id, caption)
    else:
        params = {"chat_id": chat_id, file_type: file_id}
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"
        result = api(method, **params)
    if not result.get("ok"):
        logging.error("send_file failed for chat_id=%s file_type=%s", chat_id, file_type)
    return result


def get_support_media(msg: dict):
    if msg.get("photo"):
        return "photo", msg["photo"][-1]["file_id"]
    if msg.get("document"):
        return "document", msg["document"]["file_id"]
    if msg.get("video"):
        return "video", msg["video"]["file_id"]
    if msg.get("voice"):
        return "voice", msg["voice"]["file_id"]
    if msg.get("audio"):
        return "audio", msg["audio"]["file_id"]
    if msg.get("video_note"):
        return "video_note", msg["video_note"]["file_id"]
    if msg.get("sticker"):
        return "sticker", msg["sticker"]["file_id"]
    return None, None


def save_support_link_from_result(result: dict, user_id: int):
    if not result or not result.get("ok"):
        return
    message = result.get("result", {})
    message_id = message.get("message_id")
    if message_id:
        db.save_support_message_link(message_id, user_id)


def resolve_user_identifier(value: str):
    value = value.strip()
    if value.isdigit():
        return int(value)
    username = value.lstrip("@").lower()
    if not username:
        return None
    found = next((user for user in db.get_all_users() if (user.get("username") or "").lower() == username), None)
    return found["user_id"] if found else None


def filter_users(query: str | None):
    users = db.get_all_users()
    if not query:
        return users
    normalized = query.strip().lower().lstrip("@")
    if not normalized:
        return users
    result = []
    for user in users:
        user_id = str(user.get("user_id", ""))
        username = (user.get("username") or "").lower()
        first_name = (user.get("first_name") or "").lower()
        if normalized in user_id or normalized in username or normalized in first_name:
            result.append(user)
    return result


def is_user_sub_active_row(user: dict) -> bool:
    if not user:
        return False
    if user.get("sub_type") == "banned":
        return False
    expires = user.get("sub_expires")
    if not expires:
        return True
    if isinstance(expires, str):
        try:
            expires = datetime.strptime(expires[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return True
    return datetime.now(MSK) < expires.replace(tzinfo=MSK) if getattr(expires, "tzinfo", None) is None else datetime.now(MSK) < expires.astimezone(MSK)


def get_broadcast_targets(scope: str):
    users = [user for user in db.get_all_users() if user.get("user_id") != ADMIN_ID]
    if scope == "all":
        return [user for user in users if user.get("sub_type") != "banned"]
    if scope == "sub":
        return [user for user in users if is_user_sub_active_row(user)]
    if scope == "conn":
        connected_ids = set(db.get_connected_owner_ids())
        return [user for user in users if user.get("user_id") in connected_ids and user.get("sub_type") != "banned"]
    return []


def render_user_line(user: dict) -> str:
    uid = user["user_id"]
    name = user.get("first_name") or user.get("username") or str(uid)
    if user.get("username"):
        link = f'<a href="https://t.me/{user["username"]}">@{user["username"]}</a>'
    else:
        link = f'<a href="tg://user?id={uid}">{name}</a>'
    sub = user.get("sub_type", "?")
    exp = format_db_date(user.get("sub_expires"))
    sub_icon = "✅" if db.is_sub_active(uid) else "❌"
    conn_count = db.get_connections_count_for_user(uid)
    ref_count = db.get_referral_count(uid)
    automation_status = "🔗 автоподкл" if conn_count else "➖ не подключен"
    return f"{sub_icon} {link} · <code>{uid}</code> · {sub} до {exp} · {automation_status} · реф: {ref_count}"


def users_page_text_and_keyboard(users: list[dict], page: int, query: str = ""):
    per_page = 10
    total = len(users)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    chunk = users[start:start + per_page]

    header = "👥 <b>Пользователи:</b>\n"
    if query:
        header += f"🔎 Поиск: <code>{query}</code>\n"
    header += f"📄 Страница {page}/{total_pages} · Всего: {total} · Подключены: {db.get_connections_count()}\n\n"

    if not chunk:
        text = header + "Ничего не найдено."
    else:
        text = header + "\n".join(render_user_line(user) for user in chunk)

    encoded_query = urllib.parse.quote(query) if query else ""
    keyboard_rows = []
    keyboard_rows.append([
        {"text": "🎁 Реферальная программа", "callback_data": f"users:ref:{page}:{encoded_query}"},
    ])

    nav_row = []
    if page > 1:
        nav_row.append({"text": "⬅️", "callback_data": f"users:{page-1}:{encoded_query}"})
    if page < total_pages:
        nav_row.append({"text": "➡️", "callback_data": f"users:{page+1}:{encoded_query}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard = {"inline_keyboard": keyboard_rows} if keyboard_rows else None
    return text, keyboard


def referrals_text(page: int = 1, query: str = "") -> tuple[str, dict | None]:
    leaderboard = db.get_referral_leaderboard(limit=15)
    total_referrals = sum(item.get("referrals_count", 0) for item in leaderboard)
    lines = ["🎁 <b>Реферальная программа</b>", "", f"Всего приглашений в топе: <b>{total_referrals}</b>", ""]
    for index, item in enumerate(leaderboard, start=1):
        user_id = item.get("referrer_id")
        username = item.get("username")
        name = item.get("first_name") or username or str(user_id)
        referred = db.get_referrals_by_referrer(user_id)
        preview = []
        for row in referred[:5]:
            referred_name = row.get("first_name") or row.get("username") or str(row.get("referred_id"))
            if row.get("username"):
                referred_name = f"@{row['username']}"
            preview.append(referred_name)
        preview_text = ", ".join(preview) if preview else "нет"
        lines.append(
            f"{index}. {name} (<code>{user_id}</code>) — <b>{item.get('referrals_count', 0)}</b>\n"
            f"   Кого привёл: {preview_text}"
        )
    encoded_query = urllib.parse.quote(query) if query else ""
    keyboard = {"inline_keyboard": [[{"text": "🔙 Назад", "callback_data": f"users:{page}:{encoded_query}"}]]}
    return "\n".join(lines), keyboard


def copy_message(chat_id: int, from_chat_id: int, message_id: int):
    result = api(
        "copyMessage",
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
    )
    if not result.get("ok"):
        logging.error("copyMessage failed for chat_id=%s from_chat_id=%s message_id=%s", chat_id, from_chat_id, message_id)
    return result


def run_broadcast(scope: str, source_chat_id: int, text: str | None = None, source_message_id: int | None = None):
    targets = get_broadcast_targets(scope)
    if not targets:
        return {"sent": 0, "failed": 0, "total": 0}

    sent = 0
    failed = 0
    for user in targets:
        target_id = user["user_id"]
        try:
            if source_message_id is not None:
                result = copy_message(target_id, source_chat_id, source_message_id)
            else:
                result = send(target_id, text or "")
            if not result.get("ok") and result.get("error_code") == 429:
                retry_after = result.get("parameters", {}).get("retry_after", 2)
                time.sleep(int(retry_after))
                if source_message_id is not None:
                    result = copy_message(target_id, source_chat_id, source_message_id)
                else:
                    result = send(target_id, text or "")
            if result.get("ok"):
                sent += 1
            else:
                failed += 1
            time.sleep(0.03)
        except Exception as exc:
            failed += 1
            logging.error("Broadcast error for %s: %s", target_id, exc)
    return {"sent": sent, "failed": failed, "total": len(targets)}


def get_ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


def yookassa_api(method: str, payload: dict | None = None):
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return {"ok": False}
    url = f"https://api.yookassa.ru/v3/{method}"
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {
        "Content-Type": "application/json",
        "Idempotence-Key": str(int(time.time() * 1000)),
    }
    token = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except Exception as exc:
        logging.error("YooKassa API error on %s: %s", method, exc)
        return {"ok": False}


def get_yookassa_payment(payment_id: str):
    if not payment_id:
        return {}
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    headers = {}
    token = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except Exception as exc:
        logging.error("YooKassa get payment error: %s", exc)
        return {}


def create_yookassa_checkout_url(user_id: int, plan: str) -> str | None:
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return None
    rub_plan = RUB_PAYMENT_PLANS.get(plan)
    plan_info = PAYMENT_PLANS.get(plan)
    if not rub_plan or not plan_info:
        return None
    return_url = YOOKASSA_RETURN_URL or "https://t.me/"
    payload = {
        "amount": {
            "value": f"{rub_plan['rub']:.2f}",
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": f"{plan_info['title']} — Dialog Spy Bot",
        "metadata": {
            "user_id": str(user_id),
            "plan": plan,
        },
    }
    response = yookassa_api("payments", payload)
    confirmation = response.get("confirmation", {}) if isinstance(response, dict) else {}
    return confirmation.get("confirmation_url")


def get_external_checkout_url(user_id: int, plan: str) -> str | None:
    return create_yookassa_checkout_url(user_id, plan)


def rub_payment_button(user_id: int, plan: str, days: int, price: int) -> dict:
    return {"text": f"💳 {days} дней — {price} ₽", "callback_data": f"rub_{plan}"}


def send_expired_message(user_id: int):
    ref_link = get_ref_link(user_id)
    ref_count = db.get_referral_count(user_id)
    send(user_id,
        "⏰ <b>Ваша подписка истекла</b>\n\n"
        "Для продолжения выберите вариант:\n\n"
        f"👥 <b>Пригласи друга</b> — получи +3 дня бесплатно\n"
        f"Приглашено: {ref_count} чел.\n\n"
        "💳 <b>Или купи подписку через YooKassa / карту / СБП:</b>\n"
        f"• 7 дней — {RUB_PRICE_WEEKLY} ₽\n"
        f"• 30 дней — {RUB_PRICE_MONTHLY} ₽\n"
        f"• 365 дней — {RUB_PRICE_YEARLY} ₽",
        keyboard={
            "inline_keyboard": [
                [{"text": f"⭐ 7 дней — {PRICE_WEEKLY} Stars", "callback_data": "buy_weekly"}],
                [{"text": f"⭐ 30 дней — {PRICE_MONTHLY} Stars", "callback_data": "buy_monthly"}],
                [{"text": f"⭐ 365 дней — {PRICE_YEARLY} Stars", "callback_data": "buy_yearly"}],
                [{"text": "👥 Пригласить друга", "url": ref_link}],
                [rub_payment_button(user_id, "weekly", 7, RUB_PRICE_WEEKLY)],
                [rub_payment_button(user_id, "monthly", 30, RUB_PRICE_MONTHLY)],
                [rub_payment_button(user_id, "yearly", 365, RUB_PRICE_YEARLY)],
            ]
        }
    )


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Статус"}, {"text": "⚙️ Настройки"}],
            [{"text": "📖 Инструкция"}, {"text": "🔒 Приватность"}],
            [{"text": "💳 Купить подписку"}, {"text": "👥 Пригласить друга"}],
            [{"text": "💬 Поддержка"}],
        ],
        "resize_keyboard": True
    }


def settings_keyboard(user_id: int):
    s = get_settings(user_id)
    del_icon = "✅" if s["track_deleted"] else "❌"
    edit_icon = "✅" if s["track_edited"] else "❌"
    return {
        "keyboard": [
            [{"text": f"{del_icon} Удалённые сообщения"}],
            [{"text": f"{edit_icon} Изменённые сообщения"}],
            [{"text": "◀️ Назад"}],
        ],
        "resize_keyboard": True
    }


def get_user_link(user: dict) -> str:
    if not user:
        return "Неизвестный"
    name = (user.get("first_name") or "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    name = name.strip() or "Неизвестный"
    username = user.get("username")
    uid = user.get("id")
    if username:
        return f'<a href="https://t.me/{username}">{name} (@{username})</a>'
    elif uid:
        return f'<a href="tg://user?id={uid}">{name}</a>'
    return name


def get_chat_link(chat: dict) -> str:
    name = (chat.get("first_name") or "")
    if chat.get("last_name"):
        name += " " + chat["last_name"]
    name = name.strip() or "Неизвестный"
    username = chat.get("username")
    cid = chat.get("id")
    if username:
        return f'<a href="https://t.me/{username}">{name} (@{username})</a>'
    elif cid:
        return f'<a href="tg://user?id={cid}">{name}</a>'
    return name


def handle_update(update: dict):
    logging.info(f"UPDATE: {list(update.keys())}")

    # ── Успешная оплата ────────────────────────────────────
    if "message" in update and update["message"].get("successful_payment"):
        msg = update["message"]
        user_id = msg["from"]["id"]
        payment = msg["successful_payment"]
        payload = payment.get("invoice_payload", "")
        plan = PAYMENT_PLANS.get(payload)
        if not plan:
            logging.error("Unknown successful payment payload: %s", payload)
            send(user_id, "❌ Не удалось определить оплаченный тариф. Напиши в поддержку.")
            return
        telegram_charge_id = payment.get("telegram_payment_charge_id", "")
        if telegram_charge_id:
            db.save_payment(
                user_id=user_id,
                invoice_payload=payload,
                total_amount=payment.get("total_amount", plan["stars"]),
                currency=payment.get("currency", "XTR"),
                telegram_payment_charge_id=telegram_charge_id,
                provider_payment_charge_id=payment.get("provider_payment_charge_id", ""),
            )
        db.set_subscription(user_id, payload, plan["days"])
        send(
            user_id,
            f"✅ <b>Оплата прошла!</b>\nПодписка на <b>{plan['days']} дней</b> активирована.",
            keyboard=main_keyboard(),
        )
        return

    # ── Обычное сообщение боту ─────────────────────────────
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        user = msg.get("from", {})
        chat_id = msg["chat"]["id"]
        user_id = user.get("id")

        db.save_user(user_id, user.get("username", ""), user.get("first_name", ""))
        s = get_settings(user_id)

        if text == "/cancel":
            if s.get("support_mode"):
                s["support_mode"] = False
                db.save_user_settings(
                    user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s.get("support_active", False)
                )
                send(chat_id, "✅ Режим обращения в поддержку выключен.", keyboard=main_keyboard())
            else:
                send(chat_id, "ℹ️ Сейчас режим поддержки не активен.", keyboard=main_keyboard())
            return

        if text.startswith("/reply ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                send(chat_id, "❌ Формат: /reply user_id|@user текст")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден.")
                return
            reply_text = parts[2].strip()
            send(target_id, f"💬 <b>Ответ поддержки</b>\n\n{reply_text}", keyboard=main_keyboard())
            send(chat_id, f"✅ Ответ отправлен пользователю {target_id}.")
            return

        if user_id == ADMIN_ID and msg.get("reply_to_message"):
            reply_to_message = msg["reply_to_message"]
            target_id = db.get_support_message_link(reply_to_message.get("message_id"))
            if target_id:
                media_type, media_file_id = get_support_media(msg)
                response_text = text or msg.get("caption") or ""
                if media_type and media_file_id:
                    caption = f"💬 <b>Ответ поддержки</b>\n\n{response_text}" if response_text else "💬 <b>Ответ поддержки</b>"
                    send_file(target_id, media_file_id, media_type, caption)
                elif response_text:
                    send(target_id, f"💬 <b>Ответ поддержки</b>\n\n{response_text}", keyboard=main_keyboard())
                else:
                    send(chat_id, "❌ В ответе нет текста или поддерживаемого файла.")
                    return
                send(chat_id, f"✅ Ответ отправлен пользователю {target_id}.")
                return

        if text.startswith("/closesupport ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send(chat_id, "❌ Формат: /closesupport user_id или /closesupport @username")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            target_settings = db.get_user_settings(target_id)
            db.save_user_settings(
                target_id,
                target_settings.get("track_deleted", True),
                target_settings.get("track_edited", True),
                False,
                False,
            )
            send(chat_id, f"✅ Диалог поддержки с пользователем {target_id} закрыт.")
            send(target_id, "✅ <b>Диалог с поддержкой закрыт.</b>\nЕсли понадобится, нажми «💬 Поддержка» снова.")
            return

        if text == "/supportlist" and user_id == ADMIN_ID:
            active_support = db.get_users_with_active_support()
            if not active_support:
                send(chat_id, "ℹ️ Сейчас нет активных диалогов поддержки.")
                return
            out = "💬 <b>Активные диалоги поддержки</b>\n\n"
            for item in active_support:
                support_user_id = item.get("user_id")
                first_name = item.get("first_name") or "Без имени"
                username = f"@{item['username']}" if item.get("username") else "без username"
                updated_at = item.get("updated_at")
                updated_text = updated_at.strftime("%d.%m.%Y %H:%M") if updated_at else "—"
                out += (
                    f"• {first_name} ({username})\n"
                    f"ID: <code>{support_user_id}</code>\n"
                    f"Последняя активность: {updated_text}\n"
                    f"Закрыть: <code>/closesupport {support_user_id}</code>\n\n"
                )
            send(chat_id, out)
            return

        if text.startswith("/payments ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send(chat_id, "❌ Формат: /payments user_id или /payments @username")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            payments = db.get_payments_by_user(target_id, limit=10)
            if not payments:
                send(chat_id, f"ℹ️ У пользователя {target_id} нет сохранённых платежей.")
                return
            out = f"💳 <b>Платежи пользователя {target_id}</b>\n\n"
            for payment in payments:
                created_at = payment.get("created_at")
                created_text = created_at.strftime("%d.%m.%Y %H:%M") if created_at else "—"
                refunded = "да" if payment.get("refunded") else "нет"
                out += (
                    f"• Тариф: {payment.get('invoice_payload')}\n"
                    f"Сумма: {payment.get('total_amount')} {payment.get('currency')}\n"
                    f"Возврат: {refunded}\n"
                    f"Дата: {created_text}\n"
                    f"Charge ID:\n<code>{payment.get('telegram_payment_charge_id')}</code>\n\n"
                )
            send(chat_id, out)
            return

        if text.startswith("/refund ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                send(chat_id, "❌ Формат: /refund user_id|@user telegram_payment_charge_id")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден.")
                return
            charge_id = parts[2].strip()
            payment_row = db.get_payment(charge_id)
            if payment_row and payment_row.get("refunded"):
                send(chat_id, "ℹ️ Этот платёж уже отмечен как возвращённый.")
                return
            if payment_row and int(payment_row.get("user_id", 0)) != target_id:
                send(chat_id, "❌ Этот charge_id не принадлежит указанному пользователю.")
                return
            result = api(
                "refundStarPayment",
                user_id=target_id,
                telegram_payment_charge_id=charge_id,
            )
            if result.get("ok"):
                db.mark_payment_refunded(charge_id)
                send(chat_id, f"✅ Возврат выполнен для user_id={target_id}.")
                send(target_id, "✅ <b>Оплата возвращена.</b>\nStars должны вернуться на твой баланс Telegram.")
            else:
                send(chat_id, "❌ Не удалось сделать возврат. Проверь charge_id и логи.")
            return

        if text.startswith("/cancelsub ") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                send(chat_id, "❌ Формат: /cancelsub user_id или /cancelsub @username")
                return
            target_id = resolve_user_identifier(parts[1])
            if not target_id:
                send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                return
            db.set_subscription(target_id, "expired", 0)
            send(chat_id, f"✅ Подписка пользователя {target_id} отменена.")
            try:
                send(target_id, "⚠️ <b>Подписка отключена администратором.</b>", keyboard=main_keyboard())
            except Exception:
                pass
            return

        if s.get("support_mode") and user_id != ADMIN_ID:
            username = user.get("username")
            first_name = user.get("first_name") or "Без имени"
            media_type, media_file_id = get_support_media(msg)
            message_body = text or msg.get("caption") or "[не текстовое сообщение]"
            header = (
                f"💬 <b>Новое обращение в поддержку</b>\n\n"
                f"👤 Пользователь: {first_name}\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"{'🔗 @' + username if username else '🔗 username не указан'}\n\n"
                f"<b>Сообщение:</b>\n{message_body}"
            )
            header_result = send(ADMIN_ID, header)
            save_support_link_from_result(header_result, user_id)
            if media_type and media_file_id:
                media_result = send_file(ADMIN_ID, media_file_id, media_type)
                save_support_link_from_result(media_result, user_id)
            s["support_mode"] = False
            s["support_active"] = True
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s["support_active"])
            send(
                chat_id,
                "✅ Сообщение отправлено в поддержку. Ответ придёт сюда.\n\n"
                "Диалог с поддержкой открыт. Можешь продолжать писать сюда, и сообщения будут пересылаться админу.",
                keyboard=main_keyboard(),
            )
            return

        if s.get("support_active") and user_id != ADMIN_ID:
            if text.startswith("/") or text in MENU_ACTION_TEXTS:
                pass
            else:
                username = user.get("username")
                first_name = user.get("first_name") or "Без имени"
                media_type, media_file_id = get_support_media(msg)
                message_body = text or msg.get("caption") or "[не текстовое сообщение]"
                header = (
                    f"💬 <b>Сообщение в открытом диалоге поддержки</b>\n\n"
                    f"👤 Пользователь: {first_name}\n"
                    f"🆔 ID: <code>{user_id}</code>\n"
                    f"{'🔗 @' + username if username else '🔗 username не указан'}\n\n"
                    f"<b>Сообщение:</b>\n{message_body}"
                )
                header_result = send(ADMIN_ID, header)
                save_support_link_from_result(header_result, user_id)
                if media_type and media_file_id:
                    media_result = send_file(ADMIN_ID, media_file_id, media_type)
                    save_support_link_from_result(media_result, user_id)
                return

        # Реферальная ссылка
        if text.startswith("/start ref_"):
            referrer_str = text.replace("/start ref_", "").strip()
            if referrer_str.isdigit():
                referrer_id = int(referrer_str)
                if referrer_id != user_id:
                    is_new = db.add_referral(referrer_id, user_id)
                    if is_new:
                        db.add_days(referrer_id, 3)
                        try:
                            send(referrer_id, "🎉 <b>Друг зарегистрировался по вашей ссылке!</b>\n+3 дня добавлено к подписке.")
                        except Exception:
                            pass

        if text.startswith("/start"):
            send(chat_id,
                "👁 <b>Dialog Spy Bot</b>\n\n"
                "Узнавай что скрывают — видь удалённые и изменённые сообщения в своих чатах.\n\n"
                "🔍 <b>Что умеет бот:</b>\n"
                "• Удалённые сообщения, фото, видео, голосовые и кружочки\n"
                "• Что было написано до редактирования\n"
                "• Работает в реальном времени\n\n"
                "🔒 Бот видит только твои чаты — никто другой не имеет доступа\n\n"
                "🎁 <b>14 дней бесплатно</b> при первом подключении!\n\n"
                "Нажми <b>📖 Инструкция</b> чтобы подключить.",
                keyboard=main_keyboard()
            )

        elif text in ("📊 Статус", "Статус"):
            is_connected = db.get_connections_count_for_user(user_id) or db.get_connections_count_for_user(chat_id)
            status = "🟢 Подключён" if is_connected else "🔴 Не подключён"
            sub_active = db.is_sub_active(user_id)
            user_data = db.get_user(user_id)
            sub_type = user_data.get("sub_type", "trial") if user_data else "trial"
            sub_expires = format_db_date(user_data.get("sub_expires")) if user_data else "—"
            remaining_seconds = int(user_data.get("sub_remaining_seconds") or 0) if user_data else 0
            if not is_connected:
                if remaining_seconds > 0:
                    sub_reason = "подписка на паузе — подключи бота, и оставшееся время возобновится"
                else:
                    sub_reason = "нет подключённых чатов"
            elif sub_active:
                sub_reason = "активна"
            elif remaining_seconds > 0:
                sub_reason = "пауза до следующего подключения"
            else:
                sub_reason = "истекла"
            del_icon = "✅" if s["track_deleted"] else "❌"
            edit_icon = "✅" if s["track_edited"] else "❌"
            send(chat_id,
                f"📊 <b>Статус</b>\n\n"
                f"Подключение: {status}\n"
                f"Подписка: {'✅ Активна' if sub_active else '❌ Не активна'} ({sub_type})\n"
                f"Причина: {sub_reason}\n"
                f"До: {sub_expires}\n"
                f"Удалённые: {del_icon}\n"
                f"Изменённые: {edit_icon}\n\n"
                + ("" if is_connected else "⏸ Подписка не сгорает, пока бот не подключён.\nДобавь бота в <b>Автоматизация чатов</b>, чтобы она продолжила идти."),
                keyboard=main_keyboard()
            )

        elif text in ("⚙️ Настройки", "Настройки"):
            send(chat_id, "⚙️ <b>Настройки</b>\n\nВыбери что отслеживать:", keyboard=settings_keyboard(user_id))

        elif "Удалённые сообщения" in text:
            s["track_deleted"] = not s["track_deleted"]
            db.save_user_settings(
                user_id,
                s["track_deleted"],
                s["track_edited"],
                s.get("support_mode", False),
                s.get("support_active", False),
            )
            send(chat_id, f"Удалённые сообщения: {'✅' if s['track_deleted'] else '❌'}", keyboard=settings_keyboard(user_id))

        elif "Изменённые сообщения" in text:
            s["track_edited"] = not s["track_edited"]
            db.save_user_settings(
                user_id,
                s["track_deleted"],
                s["track_edited"],
                s.get("support_mode", False),
                s.get("support_active", False),
            )
            send(chat_id, f"Изменённые сообщения: {'✅' if s['track_edited'] else '❌'}", keyboard=settings_keyboard(user_id))

        elif text == "◀️ Назад":
            send(chat_id, "Главное меню:", keyboard=main_keyboard())

        elif text in ("💳 Купить подписку",):
            weekly_url = monthly_url = yearly_url = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)
            send(chat_id,
                f"💳 <b>Купить подписку</b>\n\n"
                f"⭐ 7 дней — {PRICE_WEEKLY} Telegram Stars\n"
                f"⭐ 30 дней — {PRICE_MONTHLY} Telegram Stars\n"
                f"⭐ 365 дней — {PRICE_YEARLY} Telegram Stars\n\n"
                "Оплата через Telegram Stars — мгновенно и безопасно.\n"
                f"\n💳 Через YooKassa / карту / СБП:\n"
                f"• 7 дней — {RUB_PRICE_WEEKLY} ₽\n"
                f"• 30 дней — {RUB_PRICE_MONTHLY} ₽\n"
                f"• 365 дней — {RUB_PRICE_YEARLY} ₽\n"
                + ("\nЕсть внешний checkout через YooKassa." if any((weekly_url, monthly_url, yearly_url)) else "\nYooKassa появится после подключения магазина."),
                keyboard={
                    "inline_keyboard": [
                        [{"text": f"⭐ 7 дней — {PRICE_WEEKLY} Stars", "callback_data": "buy_weekly"}],
                        [{"text": f"⭐ 30 дней — {PRICE_MONTHLY} Stars", "callback_data": "buy_monthly"}],
                        [{"text": f"⭐ 365 дней — {PRICE_YEARLY} Stars", "callback_data": "buy_yearly"}],
                        [rub_payment_button(user_id, "weekly", 7, RUB_PRICE_WEEKLY)],
                        [rub_payment_button(user_id, "monthly", 30, RUB_PRICE_MONTHLY)],
                        [rub_payment_button(user_id, "yearly", 365, RUB_PRICE_YEARLY)],
                    ]
                }
            )

        elif text in ("👥 Пригласить друга",):
            ref_link = get_ref_link(user_id)
            ref_count = db.get_referral_count(user_id)
            share_url = f"https://t.me/share/url?url={ref_link}&text=Попробуй%20этого%20бота!"
            send(chat_id,
                f"👥 <b>Пригласи друга — получи +3 дня</b>\n\n"
                f"За каждого друга который зарегистрируется по твоей ссылке — "
                f"ты получаешь <b>+3 дня</b> автоматически.\n\n"
                f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
                f"Приглашено друзей: <b>{ref_count}</b>",
                keyboard={"inline_keyboard": [[{"text": "📤 Поделиться", "url": share_url}]]}
            )

        elif text in ("💬 Поддержка",):
            s["support_mode"] = True
            s["support_active"] = False
            db.save_user_settings(user_id, s["track_deleted"], s["track_edited"], s["support_mode"], s["support_active"])
            send(
                chat_id,
                "💬 <b>Поддержка</b>\n\n"
                "Напиши одним сообщением, что случилось.\n"
                "Можно отправить текст, и я перешлю его админу.\n\n"
                "Для отмены отправь <code>/cancel</code>",
            )

        elif text in ("🔒 Приватность",):
            privacy_caption = (
                "🔒 <b>Приватность и безопасность</b>\n\n"
                "✅ Бот работает через <b>официальный Telegram Bot API</b>\n"
                "✅ Уведомления об удалённых и изменённых сообщениях приходят <b>только тебе</b>\n"
                "✅ Тексты сообщений и file_id хранятся только в зашифрованном виде\n"
                "✅ В базе данных нет читаемых переписок\n"
                "✅ Ключ шифрования хранится отдельно от БД\n"
                "✅ Логи не содержат содержимое сообщений\n"
                "✅ Бота можно отключить в любой момент\n\n"
            )
            privacy_image_path = next((path for path in PRIVACY_IMAGE_PATHS if os.path.exists(path)), None)
            if privacy_image_path:
                send_photo(chat_id, privacy_image_path, caption=privacy_caption, keyboard=main_keyboard())
            else:
                send(
                    chat_id,
                    privacy_caption + "\n\n<i>Положи файл <code>privacy.jpg</code> или <code>privacy.png</code> рядом с ботом, и он будет отправляться как картинка.</i>",
                    keyboard=main_keyboard()
                )

        elif text in ("📖 Инструкция",):
            instruction_caption = (
                "⚙️ <b>Как подключить:</b>\n\n"
                "<b>Способ 1 — Новая версия Telegram:</b>\n"
                "1️⃣ Открой профиль → <b>Изм.</b>\n"
                "2️⃣ Прокрути вниз → <b>Автоматизация чатов</b>\n"
                f"3️⃣ Введи <code>@{BOT_USERNAME}</code> → <b>Добавить</b>\n\n"
                "<b>Способ 2 — Telegram Premium:</b>\n"
                "1️⃣ Настройки → <b>Telegram для бизнеса</b>\n"
                "2️⃣ <b>Чат-боты</b>\n"
                f"3️⃣ Введи <code>@{BOT_USERNAME}</code> → <b>Добавить</b>\n\n"
                "⚠️ <i>Если раздел не появляется — обнови Telegram</i>"
            )
            instruction_keyboard = {"inline_keyboard": [[{"text": "⚙️ Открыть настройки", "url": "tg://settings/edit"}]]}
            instruction_image_path = next((path for path in INSTRUCTION_IMAGE_PATHS if os.path.exists(path)), None)
            if instruction_image_path:
                send_photo(chat_id, instruction_image_path, caption=instruction_caption, keyboard=instruction_keyboard)
            else:
                send(chat_id, instruction_caption + "\n\n<i>Положи файл <code>instruction.jpg</code> или <code>instruction.png</code> рядом с ботом, и он будет отправляться как картинка.</i>", keyboard=instruction_keyboard)

        elif text == "/admin" and user_id == ADMIN_ID:
            users = db.get_all_users()
            connections = db.get_connections_count()
            trial = sum(1 for u in users if u["sub_type"] == "trial")
            paid = sum(1 for u in users if u["sub_type"] in ("monthly", "yearly"))
            banned = sum(1 for u in users if u["sub_type"] == "banned")
            recent = db.get_recent_connections(5)
            recent_text = ""
            for r in recent:
                name = r.get("first_name") or r.get("username") or str(r["owner_id"])
                uname = f"@{r['username']}" if r.get("username") else f"id:{r['owner_id']}"
                icon = "🟢" if r["is_enabled"] else "🔴"
                sub = r.get("sub_type", "?")
                raw_date = r.get("connected_at")
                date = raw_date.strftime("%d.%m.%Y %H:%M") if raw_date else ""
                recent_text += f"\n{icon} {name} ({uname}) · {sub} · {date}"
            send(chat_id,
                f"👑 <b>Админ панель</b>\n\n"
                f"👥 Пользователей: {len(users)}\n"
                f"🔗 Подключений: {connections}\n\n"
                f"🆓 Trial: {trial} | 💳 Платных: {paid} | 🚫 Бан: {banned}\n\n"
                f"🕐 <b>Последние подключения:</b>{recent_text or ' нет'}\n\n"
                f"<b>Команды:</b>\n\n"
                f"/sub @user monthly|yearly|trial — выдать подписку\n"
                f"/ban user_id|@user — забанить пользователя\n"
                f"/unban user_id|@user — разбанить и дать 14 дней trial\n"
                f"/users [user_id|@user] — список пользователей и поиск\n"
                f"Кнопка в /users: 🎁 реферальная программа\n"
                f"/reply user_id текст — ответить в поддержку вручную\n"
                f"reply на сообщение пользователя — быстрый ответ в поддержку\n"
                f"/refund user_id|@user telegram_payment_charge_id — вернуть Stars\n"
                f"/cancelsub user_id|@user — отключить подписку без возврата\n"
                f"/closesupport user_id|@user — закрыть диалог поддержки\n"
                f"/supportlist — активные диалоги поддержки\n"
                f"/payments user_id|@user — последние платежи пользователя\n"
                f"/bd [текст] — рассылка всем пользователям (alias /broadcast)\n"
                f"/bdsub [текст] — рассылка только активным подписчикам\n"
                f"/bdconn [текст] — рассылка только подключённым\n"
                f"/admin"
            )

        elif text.startswith("/sub ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 3:
                try:
                    sub_type = parts[2]
                    days = {"trial": 14, "monthly": 30, "yearly": 365}.get(sub_type, 30)
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, f"❌ {parts[1]} не найден.")
                        return
                    db.set_subscription(target_id, sub_type, days)
                    send(chat_id, f"✅ {sub_type} выдан {target_id} на {days} дней.")
                except Exception as e:
                    send(chat_id, f"❌ {e}")

        elif text.startswith("/ban ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 2:
                try:
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                        return
                    db.set_subscription(target_id, "banned", 0)
                    send(chat_id, f"🚫 {target_id} забанен.")
                    try:
                        send(target_id, "🚫 Ваш доступ заблокирован.")
                    except Exception:
                        pass
                except Exception as e:
                    send(chat_id, f"❌ {e}")

        elif text.startswith("/unban ") and user_id == ADMIN_ID:
            parts = text.split()
            if len(parts) >= 2:
                try:
                    target_id = resolve_user_identifier(parts[1])
                    if not target_id:
                        send(chat_id, "❌ Пользователь не найден. Используй user_id или @username.")
                        return
                    db.set_subscription(target_id, "trial", 14)
                    send(chat_id, f"✅ {target_id} разбанен, trial 14 дней.")
                    try:
                        send(target_id, "✅ Ваш доступ восстановлен.")
                    except Exception:
                        pass
                except Exception as e:
                    send(chat_id, f"❌ {e}")

        elif user_id == ADMIN_ID and (
            text.startswith("/bd")
            or text.startswith("/broadcast")
        ):
            command = text.split(maxsplit=1)[0].lower()
            scope_map = {
                "/bd": "all",
                "/broadcast": "all",
                "/bdsub": "sub",
                "/broadcastsub": "sub",
                "/bdconn": "conn",
                "/broadcastconn": "conn",
            }
            scope = scope_map.get(command)
            if not scope:
                send(chat_id, "❌ Используй /bd, /bdsub или /bdconn.")
                return

            parts = text.split(maxsplit=1)
            broadcast_text = parts[1].strip() if len(parts) > 1 else ""
            source_message_id = None
            if not broadcast_text:
                reply = msg.get("reply_to_message")
                if reply:
                    source_message_id = reply.get("message_id")
                    source_chat_id = msg["chat"]["id"]
                else:
                    send(chat_id, f"❌ Формат: {command} текст или {command} в ответ на сообщение")
                    return
            else:
                source_chat_id = None

            targets = get_broadcast_targets(scope)
            if not targets:
                send(chat_id, "ℹ️ Нет получателей для рассылки.")
                return

            scope_label = {"all": "всем пользователям", "sub": "активным подписчикам", "conn": "подключённым"}[scope]
            send(chat_id, f"🔄 Рассылка запущена {scope_label}. Получателей: {len(targets)}")
            result = run_broadcast(
                scope=scope,
                source_chat_id=source_chat_id or msg["chat"]["id"],
                text=broadcast_text or None,
                source_message_id=source_message_id,
            )
            send(
                chat_id,
                f"✅ Рассылка завершена.\n"
                f"Получателей: {result['total']}\n"
                f"Отправлено: {result['sent']}\n"
                f"Ошибок: {result['failed']}"
            )
            return

        elif text.startswith("/users") and user_id == ADMIN_ID:
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ""
            users = filter_users(query)
            text_out, keyboard = users_page_text_and_keyboard(users, page=1, query=query)
            send(chat_id, text_out, keyboard=keyboard)

    # ── Callback кнопки ────────────────────────────────────
    elif "callback_query" in update:
        cq = update["callback_query"]
        user_id = cq["from"]["id"]
        data = cq.get("data", "")
        api("answerCallbackQuery", callback_query_id=cq["id"])

        if data.startswith("users:ref:") and user_id == ADMIN_ID:
            _, _, page_str, encoded_query = data.split(":", 3)
            page = int(page_str)
            query = urllib.parse.unquote(encoded_query) if encoded_query else ""
            text_out, keyboard = referrals_text(page=page, query=query)
            api(
                "editMessageText",
                chat_id=cq["message"]["chat"]["id"],
                message_id=cq["message"]["message_id"],
                text=text_out,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif data.startswith("users:") and user_id == ADMIN_ID:
            _, page_str, encoded_query = data.split(":", 2)
            page = int(page_str)
            query = urllib.parse.unquote(encoded_query) if encoded_query else ""
            users = filter_users(query)
            text_out, keyboard = users_page_text_and_keyboard(users, page=page, query=query)
            api(
                "editMessageText",
                chat_id=cq["message"]["chat"]["id"],
                message_id=cq["message"]["message_id"],
                text=text_out,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        elif data.startswith("rub_"):
            plan_key = data.replace("rub_", "", 1)
            checkout_url = get_external_checkout_url(user_id, plan_key)
            if checkout_url:
                send(
                    user_id,
                    "💳 <b>Оплата через YooKassa</b>\n\nНажми кнопку ниже, чтобы оплатить картой или через СБП.",
                    keyboard={"inline_keyboard": [[{"text": "💳 Перейти к оплате", "url": checkout_url}]]},
                )
            else:
                send(
                    user_id,
                    "💳 Оплата в рублях скоро будет доступна.\n\nСейчас можно оплатить через Telegram Stars.",
                )
        elif data == "buy_weekly":
            plan = PAYMENT_PLANS["weekly"]
            send_invoice(user_id, plan["title"], "Dialog Spy Bot — 7 дней доступа", "weekly", plan["stars"])
        elif data == "buy_monthly":
            plan = PAYMENT_PLANS["monthly"]
            send_invoice(user_id, plan["title"], "Dialog Spy Bot — 30 дней доступа", "monthly", plan["stars"])
        elif data == "buy_yearly":
            plan = PAYMENT_PLANS["yearly"]
            send_invoice(user_id, plan["title"], "Dialog Spy Bot — 365 дней доступа", "yearly", plan["stars"])

    # ── Pre-checkout ───────────────────────────────────────
    elif "pre_checkout_query" in update:
        pcq = update["pre_checkout_query"]
        payload = pcq.get("invoice_payload", "")
        plan = PAYMENT_PLANS.get(payload)
        amount = pcq.get("total_amount")
        if not plan:
            logging.error("Invalid pre_checkout payload: %s", payload)
            api(
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=False,
                error_message="Неизвестный тариф. Попробуй создать счёт заново.",
            )
            return
        if amount != plan["stars"]:
            logging.error("Invalid pre_checkout amount for %s: got=%s expected=%s", payload, amount, plan["stars"])
            api(
                "answerPreCheckoutQuery",
                pre_checkout_query_id=pcq["id"],
                ok=False,
                error_message="Сумма счёта изменилась. Попробуй создать счёт заново.",
            )
            return
        api("answerPreCheckoutQuery", pre_checkout_query_id=pcq["id"], ok=True)

    # ── Подключение бизнес-аккаунта ───────────────────────
    elif "business_connection" in update:
        bc = update["business_connection"]
        owner_id = bc["user_chat_id"]
        is_enabled = bc.get("is_enabled", False)
        db.save_connection(bc["id"], owner_id, is_enabled)
        if is_enabled:
            db.resume_subscription(owner_id)
            send(owner_id, "✅ <b>Бот подключён!</b>\n\nБуду присылать уведомления об удалённых и изменённых сообщениях.", keyboard=main_keyboard())
        else:
            if db.get_connections_count_for_user(owner_id) == 0:
                db.pause_subscription(owner_id)
            send(owner_id, "❌ Бот отключён.\n\nПричина: нет подключённых чатов.", keyboard=main_keyboard())

    # ── Новое сообщение из бизнес-чата ────────────────────
    elif "business_message" in update:
        msg = update["business_message"]
        conn_id = msg.get("business_connection_id", "")
        sender = msg.get("from", {})
        owner_id = db.get_owner_by_connection(conn_id) or ADMIN_ID

        if sender.get("id") == owner_id or msg["chat"]["id"] == owner_id:
            return
        if not allow_business_event(owner_id, conn_id):
            return

        date_str = format_ts_msk(msg["date"])
        sender_link = get_user_link(sender)
        if msg.get("text"):
            db.cache_message(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, msg["text"], date_str)
        elif msg.get("voice"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "voice", msg["voice"]["file_id"], date_str)
        elif msg.get("video_note"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "video_note", msg["video_note"]["file_id"], date_str)
        elif msg.get("audio"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "audio", msg["audio"]["file_id"], date_str)
        elif msg.get("photo"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "photo", msg["photo"][-1]["file_id"], date_str)
        elif msg.get("video"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "video", msg["video"]["file_id"], date_str)
        elif msg.get("document"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "document", msg["document"]["file_id"], date_str)
        elif msg.get("sticker"):
            db.cache_media(conn_id, msg["chat"]["id"], msg["message_id"], sender_link, "sticker", msg["sticker"]["file_id"], date_str)

    # ── Изменённое сообщение ───────────────────────────────
    elif "edited_business_message" in update:
        msg = update["edited_business_message"]
        conn_id = msg.get("business_connection_id", "")
        new_text = msg.get("text", "")
        owner_id = db.get_owner_by_connection(conn_id) or ADMIN_ID

        if not db.is_sub_active(owner_id):
            send_expired_message(owner_id)
            return

        s = get_settings(owner_id)
        if not s["track_edited"]:
            return
        if not allow_business_event(owner_id, conn_id):
            return

        original = db.get_cached_message(conn_id, msg["chat"]["id"], msg["message_id"])
        if original and original["text"] != new_text:
            send(owner_id,
                f"✏️ <b>Сообщение изменено</b>\n"
                f"👤 {get_chat_link(msg['chat'])}\n"
                f"🕐 {original['date']}\n\n"
                f"<b>Было:</b>\n{original['text']}\n\n"
                f"<b>Стало:</b>\n{new_text}"
            )
            db.update_cached_text(conn_id, msg["chat"]["id"], msg["message_id"], new_text)

    # ── Удалённые сообщения ────────────────────────────────
    elif "deleted_business_messages" in update:
        event = update["deleted_business_messages"]
        conn_id = event.get("business_connection_id", "")
        owner_id = db.get_owner_by_connection(conn_id) or ADMIN_ID

        if not db.is_sub_active(owner_id):
            send_expired_message(owner_id)
            return

        s = get_settings(owner_id)
        if not s["track_deleted"]:
            return
        if not allow_business_event(owner_id, conn_id):
            return

        chat_link = get_chat_link(event["chat"])

        for msg_id in event["message_ids"]:
            original = db.get_cached_message(conn_id, event["chat"]["id"], msg_id)
            if original:
                send(owner_id,
                    f"🗑️ <b>Сообщение удалено</b>\n"
                    f"👤 {chat_link}\n"
                    f"🕐 {original['date']}\n\n"
                    f"<b>Текст:</b>\n{original['text']}"
                )
                db.delete_cached_message(conn_id, event["chat"]["id"], msg_id)
                continue

            media = db.get_cached_media(conn_id, event["chat"]["id"], msg_id)
            if media:
                caption = f"🗑️ <b>Удалено</b> · {media['file_type']}\n👤 {chat_link}\n🕐 {media['date']}"
                send_file(owner_id, media["file_id"], media["file_type"], caption)
                db.delete_cached_media(conn_id, event["chat"]["id"], msg_id)


def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")

    db.init_db()
    db.cleanup_temp_tables()
    start_health_server()
    start_cleanup_worker()

    api("deleteWebhook", drop_pending_updates=False)

    me = api("getMe")
    BOT_USERNAME = me.get("result", {}).get("username", "DialogDelBot")

    print("=" * 40)
    print(f"Бот @{BOT_USERNAME} запущен!")
    print("=" * 40)

    offset = 0
    while True:
        try:
            result = api("getUpdates", offset=offset, timeout=50, allowed_updates=ALLOWED_UPDATES)
            if not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    logging.error(f"Ошибка: {e}")

        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
