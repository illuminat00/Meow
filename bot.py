import asyncio
import html
import logging
import math
import os
import random
import re
import time
import traceback
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ErrorEvent, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from dotenv import load_dotenv
from yarl import URL

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PROVIDER_URL = os.getenv("PROVIDER_URL", "")
PROVIDER_KEY = os.getenv("PROVIDER_KEY", "")
SERVICE_ID = os.getenv("SERVICE_ID", "")
PROVIDER_METHOD = os.getenv("PROVIDER_METHOD", "GET").upper()  # GET یا POST (فقط برای نوع smm)
PROVIDER_TYPE = os.getenv("PROVIDER_TYPE", "smm").lower()  # smm (استاندارد پنل‌های SMM) یا actionseen
COIN_TOMAN = float(os.getenv("COIN_TOMAN", "0.38"))  # ارزش هر سکه (فقط برای نمایش موجودی actionseen)
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "")  # مثلا @mychannel ، خالی = غیرفعال
DATABASE_URL = os.environ["DATABASE_URL"]
MIN_TOPUP = int(os.getenv("MIN_TOPUP", "10000"))
TOPUP_TTL = int(os.getenv("TOPUP_TTL_MIN", "30")) * 60
CREDIT_FULL = os.getenv("CREDIT_FULL", "0") == "1"  # 1 = کل مبلغ واریزی (با عدد رندوم) شارژ شود
RAND_MIN, RAND_MAX = 1, 999
MAX_LINKS = 10
LOW_PROVIDER_BALANCE = float(os.getenv("LOW_PROVIDER_BALANCE", "0"))  # 0 = غیرفعال

DEFAULTS = {
    "price_per_1000": os.getenv("DEFAULT_PRICE", "1500"),
    "card": f"{os.getenv('CARD_NUMBER', '0000-0000-0000-0000')}\nبه نام: {os.getenv('CARD_HOLDER', '---')}",
    "min_qty": "100",
    "max_qty": "100000",
    "brand": os.getenv("BRAND_NAME", "Nexor"),
    "support": os.getenv("SUPPORT_USERNAME", ""),
}

LINK_RE = re.compile(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/(\d+)")


def extract_links(text):
    out = []
    for ch, pid in LINK_RE.findall(text or ""):
        link = f"https://t.me/{ch}/{pid}"
        if link not in out:
            out.append(link)
    return out[:MAX_LINKS]
STATUS_FA = {
    "Unknown": "⏳ در حال بررسی", "Pending": "⏳ در صف", "Processing": "⚙️ در حال پردازش", "In progress": "⚙️ در حال انجام",
    "Completed": "✅ تکمیل", "Partial": "◐ ناقص (مابقی برگشت خورد)",
    "Canceled": "❌ لغو (برگشت پول)", "Cancelled": "❌ لغو (برگشت پول)",
    "Refunded": "❌ برگشت پول", "Fail": "❌ ناموفق", "Failed": "❌ ناموفق",
}
STATUS_ALIASES = {"done": "Completed", "completed": "Completed", "canceled": "Canceled", "cancelled": "Canceled",
                  "pending": "Pending", "in progress": "In progress", "processing": "Processing", "partial": "Partial"}
REFUND_STATUSES = {"Canceled", "Cancelled", "Refunded", "Fail", "Failed"}

BTN_ORDER, BTN_CHARGE, BTN_ACC = "🛒 ثبت سفارش سین", "💰 شارژ کیف پول", "👤 حساب من"
BTN_ORDERS, BTN_SUPPORT, BTN_HELP = "📦 سفارش‌های من", "💬 پشتیبانی", "📖 راهنما و قوانین"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_ORDER)],
              [KeyboardButton(text=BTN_CHARGE), KeyboardButton(text=BTN_ACC)],
              [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SUPPORT)],
              [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
    input_field_placeholder="از منوی پایین انتخاب کن 👇",
)

MENU_TEXTS = {BTN_ORDER, BTN_CHARGE, BTN_ACC, BTN_ORDERS, BTN_SUPPORT, BTN_HELP}

router = Router()
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)
db = None  # در init_db ساخته می‌شه


def clean_dsn(url):
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit(p._replace(query=urlencode(q)))


class Result:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


class PG:
    """لایه‌ی کوچیک روی asyncpg تا بقیه‌ی کد با ? و rowcount کار کنه."""

    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def q(sql):
        n, out = 0, []
        for ch in sql:
            if ch == "?":
                n += 1
                out.append(f"${n}")
            else:
                out.append(ch)
        return "".join(out)

    async def execute(self, sql, args=()):
        status = await self.pool.execute(self.q(sql), *args)
        try:
            return Result(int(status.split()[-1]))
        except (ValueError, IndexError):
            return Result(0)

    async def insert(self, sql, args=()):
        return await self.pool.fetchval(self.q(sql) + " RETURNING id", *args)

    async def commit(self):  # autocommit
        pass


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, username TEXT, balance BIGINT DEFAULT 0, banned INTEGER DEFAULT 0, joined BIGINT)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS topups(id BIGSERIAL PRIMARY KEY, user_id BIGINT, base BIGINT, unique_amount BIGINT, credited BIGINT DEFAULT 0, status TEXT, created BIGINT, expires BIGINT)",
    "CREATE TABLE IF NOT EXISTS orders(id BIGSERIAL PRIMARY KEY, user_id BIGINT, link TEXT, quantity BIGINT, cost BIGINT, provider_order TEXT, status TEXT, settled INTEGER DEFAULT 0, created BIGINT)",
    "ALTER TABLE topups ADD COLUMN IF NOT EXISTS receipt_uid TEXT",
    "CREATE TABLE IF NOT EXISTS ledger(id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, reason TEXT, ts BIGINT)",
]


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    global db
    pool = await asyncpg.create_pool(clean_dsn(DATABASE_URL), min_size=1, max_size=5,
                                     statement_cache_size=0, command_timeout=30)
    db = PG(pool)
    for stmt in SCHEMA:
        await db.execute(stmt)
    for k, v in DEFAULTS.items():
        await db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT (key) DO NOTHING", (k, v))


async def one(sql, args=()):
    return await db.pool.fetchrow(PG.q(sql), *args)


async def many(sql, args=()):
    return await db.pool.fetch(PG.q(sql), *args)


async def get_setting(key):
    return (await one("SELECT value FROM settings WHERE key=?", (key,)))["value"]


async def set_setting(key, value):
    await db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                     (key, str(value)))


async def balance_change(uid, delta, reason):
    await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (delta, uid))
    await db.execute("INSERT INTO ledger(user_id,amount,reason,ts) VALUES (?,?,?,?)",
                     (uid, delta, reason, int(time.time())))
    await db.commit()


async def try_spend(uid, amount, reason):
    cur = await db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount))
    ok = cur.rowcount == 1
    if ok:
        await db.execute("INSERT INTO ledger(user_id,amount,reason,ts) VALUES (?,?,?,?)",
                         (uid, -amount, reason, int(time.time())))
    await db.commit()
    return ok


def to_int(s):
    s = (s or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    s = s.replace(",", "").replace("،", "").strip()
    return int(s) if s.isdigit() else None


# ───────────────────────── provider ─────────────────────────
async def provider(**params):
    """کلاینت provider. خروجی همیشه JSON ـه.
    نوع smm: فرمت استاندارد (action=add/status/balance).
    نوع actionseen: همون سه عمل به فرمت وبسرویس اکشن‌سین ترجمه می‌شه (لینک باید خام و بدون کدگذاری برود)."""
    if PROVIDER_TYPE == "actionseen":
        act = params["action"]
        if act == "add":
            q = f"action=view&link={params['link']}&quantity={params['quantity']}"
        elif act == "status":
            q = f"action=view&order={params['order']}"
        else:
            q = f"action={act}"
        url = URL(f"{PROVIDER_URL.rstrip('/')}/?key={PROVIDER_KEY}&{q}", encoded=True)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url) as r:
                return await r.json(content_type=None)
    data = {"key": PROVIDER_KEY, **params}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        req = s.post(PROVIDER_URL, data=data) if PROVIDER_METHOD == "POST" else s.get(PROVIDER_URL, params=data)
        async with req as r:
            return await r.json(content_type=None)


async def notify_admins(bot, text):
    for a in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(a, text)


_last_err = {}


async def report_error(bot, where, exc):
    """خطا رو برای ادمین می‌فرسته (هر خطای تکراری حداکثر هر ۵ دقیقه یک بار)."""
    key = f"{where}:{type(exc).__name__}:{str(exc)[:60]}"
    now = time.time()
    if now - _last_err.get(key, 0) < 300:
        return
    _last_err[key] = now
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
    await notify_admins(bot, f"🚨 خطا در ربات ({where})\n<pre>{html.escape(tb)}</pre>")


async def place_order(uid, link, qty, cost):
    """نتیجه: ("ok", شماره‌ی provider) | ("failed", متن خطا) | ("unknown", شماره‌ی سفارش داخلی).
    unknown یعنی معلوم نیست provider سفارش رو ثبت کرده یا نه؛ پول برنمی‌گرده تا ادمین بررسی کنه."""
    res, err = None, ""
    try:
        res = await provider(action="add", service=SERVICE_ID, link=link, quantity=qty)
    except aiohttp.ClientConnectorError as e:  # اصلاً به provider وصل نشده → مطمئنیم ثبت نشده
        return "failed", str(e)
    except Exception as e:
        err = str(e) or type(e).__name__
    now = int(time.time())
    if isinstance(res, dict) and "order" in res:
        await db.execute(
            "INSERT INTO orders(user_id,link,quantity,cost,provider_order,status,created) VALUES (?,?,?,?,?,?,?)",
            (uid, link, qty, cost, str(res["order"]), "Pending", now))
        return "ok", res["order"]
    if isinstance(res, dict) and res.get("error"):
        return "failed", str(res["error"])
    oid = await db.insert(
        "INSERT INTO orders(user_id,link,quantity,cost,provider_order,status,created) VALUES (?,?,?,?,?,?,?)",
        (uid, link, qty, cost, "", "Unknown", now))
    return "unknown", oid


async def sync_order(o):
    """وضعیت سفارش رو از provider می‌گیره؛ در صورت لغو/ناقص، پول رو برمی‌گردونه."""
    if not o["provider_order"]:
        return None
    try:
        res = await provider(action="status", order=o["provider_order"])
    except Exception:
        return None
    st = res.get("status") if isinstance(res, dict) else None
    if not st:
        return None
    st = STATUS_ALIASES.get(str(st).lower(), st)
    refund, final = 0, True
    if st == "Completed":
        pass
    elif st in REFUND_STATUSES:
        refund = o["cost"]
    elif st == "Partial":
        remains = int(float(res.get("remains") or 0))
        refund = o["cost"] * min(remains, o["quantity"]) // o["quantity"]
    else:
        final = False
    if final:
        cur = await db.execute("UPDATE orders SET status=?, settled=1 WHERE id=? AND settled=0", (st, o["id"]))
        await db.commit()
        if cur.rowcount != 1:
            return None
        if refund:
            await balance_change(o["user_id"], refund, f"refund-order-{o['id']}")
    else:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (st, o["id"]))
        await db.commit()
    return st, refund, final


async def poll_orders(bot):
    while True:
        await asyncio.sleep(600)
        try:
            for o in await many("SELECT * FROM orders WHERE settled=0 AND provider_order<>'' LIMIT 200"):
                r = await sync_order(o)
                if r and r[2]:
                    msg = f"📦 سفارش #{o['id']}: {STATUS_FA.get(r[0], r[0])}"
                    if r[1]:
                        msg += f"\n💰 {r[1]:,} تومان به کیف پولت برگشت."
                    with suppress(Exception):
                        await bot.send_message(o["user_id"], msg)
            if LOW_PROVIDER_BALANCE:
                res = await provider(action="balance")
                if float(res.get("balance", 0)) < LOW_PROVIDER_BALANCE:
                    await notify_admins(bot, f"⚠️ موجودی provider کمه: {res.get('balance')} {res.get('currency', '')}")
        except Exception as e:
            logging.exception("poll_orders")
            await report_error(bot, "poll_orders", e)


# ───────────────────────── Middleware (بن + عضویت اجباری) ─────────────────────────
class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        await db.execute("INSERT INTO users(id,username,joined) VALUES (?,?,?) ON CONFLICT (id) DO NOTHING",
                         (user.id, user.username, int(time.time())))
        await db.execute("UPDATE users SET username=? WHERE id=?", (user.username, user.id))
        await db.commit()
        # زدن هر دکمه‌ی منو، فرایند نیمه‌کاره (شارژ، سفارش، رسید...) رو ریست می‌کنه
        if isinstance(event, Message) and event.text in MENU_TEXTS and data.get("state") is not None:
            await data["state"].clear()
            data["raw_state"] = None
        if user.id in ADMIN_IDS:
            return await handler(event, data)
        u = await one("SELECT banned FROM users WHERE id=?", (user.id,))
        if u["banned"]:
            return
        if FORCE_CHANNEL:
            try:
                m = await data["bot"].get_chat_member(FORCE_CHANNEL, user.id)
                joined = m.status in ("member", "administrator", "creator")
            except Exception:
                joined = True  # اگه ربات نتونست چک کنه، کاربر رو بلاک نکن
            if not joined:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📢 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.lstrip('@')}")],
                    [InlineKeyboardButton(text="✅ عضو شدم", callback_data="chk_join")]])
                text = "برای استفاده از ربات اول عضو کانال ما شو 👇"
                if isinstance(event, CallbackQuery):
                    await event.answer("هنوز عضو نشدی!", show_alert=True)
                else:
                    await event.answer(text, reply_markup=kb)
                return
        return await handler(event, data)


# ───────────────────────── منو و حساب ─────────────────────────
def fmt(n):
    return f"{int(n):,}"


def cancel_row():
    return [InlineKeyboardButton(text="❌ انصراف", callback_data="flow_cancel")]


async def welcome_text(user):
    brand = html.escape(await get_setting("brand"))
    price = int(await get_setting("price_per_1000"))
    bal = (await one("SELECT balance FROM users WHERE id=?", (user.id,)))["balance"]
    return (f"سلام {html.escape(user.first_name or 'دوست عزیز')} 👋\n"
            f"به <b>{brand}</b> خوش اومدی!\n\n"
            "اینجا می‌تونی خیلی سریع و خودکار برای پست‌های کانالت <b>سین (بازدید)</b> سفارش بدی؛ "
            "بدون معطلی و بدون پیام دادن به پشتیبان.\n\n"
            f"💵 قیمت هر ۱۰۰۰ سین: <b>{fmt(price)}</b> تومان\n"
            f"👛 موجودی تو: <b>{fmt(bal)}</b> تومان\n\n"
            "از منوی پایین شروع کن 👇")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await welcome_text(m.from_user), reply_markup=MENU)


@router.callback_query(F.data == "chk_join")
async def chk_join(c: CallbackQuery):
    await c.message.answer(await welcome_text(c.from_user), reply_markup=MENU)
    await c.answer()


@router.callback_query(F.data == "flow_cancel")
async def flow_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    with suppress(Exception):
        await c.message.edit_text("انصراف داده شد 🌿\nهر وقت خواستی از منوی پایین ادامه بده.")
    await c.answer()


@router.message(F.text == BTN_ACC)
async def account(m: Message, state: FSMContext):
    await state.clear()
    u = await one("SELECT * FROM users WHERE id=?", (m.from_user.id,))
    n = (await one("SELECT COUNT(*) AS n FROM orders WHERE user_id=?", (m.from_user.id,)))["n"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="go_charge")]])
    await m.answer("👤 <b>حساب من</b>\n\n"
                   f"🆔 شناسه: <code>{u['id']}</code>\n"
                   f"💰 موجودی: <b>{fmt(u['balance'])}</b> تومان\n"
                   f"📦 تعداد سفارش‌ها: {n}", reply_markup=kb)


HELP_TEXT = (
    "📖 <b>راهنمای استفاده</b>\n\n"
    "1️⃣ از «💰 شارژ کیف پول» حسابت رو شارژ کن.\n"
    "2️⃣ «🛒 ثبت سفارش سین» رو بزن و لینک پست‌ها رو بفرست.\n"
    "3️⃣ تعداد سین هر پست رو انتخاب کن و سفارش رو تایید کن.\n"
    "4️⃣ پیشرفت کار رو از «📦 سفارش‌های من» ببین.\n\n"
    "📜 <b>قوانین مهم</b>\n"
    "• کانال باید <b>عمومی (Public)</b> باشه و تا پایان سفارش خصوصی نشه.\n"
    "• تا کامل شدن سفارش، برای همون پست سفارش دوم ثبت نکن.\n"
    "• لینک رو دقیق بفرست؛ لینک اشتباه ممکنه باعث انجام نشدن سفارش بشه.\n"
    "• اگه سفارشی لغو بشه یا ناقص بمونه، هزینه‌ی بخش انجام‌نشده خودکار به کیف پولت برمی‌گرده.\n"
    "• برای تایید سریع‌تر شارژ، دقیقاً همون مبلغی که ربات نشون می‌ده رو واریز کن.\n"
    "• زمان رسیدن سین‌ها بسته به سرویس و شرایط تلگرام ممکنه کمی متفاوت باشه."
)


@router.message(F.text == BTN_HELP)
async def help_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP_TEXT)


@router.message(F.text == BTN_SUPPORT)
async def support(m: Message, state: FSMContext):
    await state.clear()
    sup = (await get_setting("support")).strip().lstrip("@")
    if not sup:
        return await m.answer("💬 پشتیبانی هنوز تنظیم نشده. لطفاً کمی بعد دوباره سر بزن 🙏")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 پیام به پشتیبانی", url=f"https://t.me/{sup}")]])
    await m.answer("💬 <b>پشتیبانی</b>\n\n"
                   "هر سوال یا مشکلی داشتی، با پشتیبانی در ارتباط باش.\n"
                   "موقع پیام دادن، شناسه‌ی حسابت رو هم بفرست تا سریع‌تر پیگیری کنیم.", reply_markup=kb)


# ───────────────────────── شارژ کیف پول ─────────────────────────
class Charge(StatesGroup):
    amount = State()


class AdminEdit(StatesGroup):
    amount = State()


class AdminUser(StatesGroup):
    find = State()
    amount = State()


PRESETS = [50000, 100000, 200000, 500000]


async def topup_text(t):
    card = (await get_setting("card")).strip().split("\n", 1)
    card_html = f"<code>{html.escape(card[0])}</code>" + (f"\n{html.escape(card[1])}" if len(card) > 1 else "")
    left = max(0, (t["expires"] - int(time.time())) // 60)
    note = "" if CREDIT_FULL else (
        f"\n📌 مبلغ <b>{fmt(t['base'])}</b> تومان به کیف پولت اضافه می‌شه؛ عدد اضافه‌ی آخر فقط برای شناسایی پرداخته.\n")
    return (
        "💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
        f"💰 مبلغ دقیق واریز:\n<code>{t['unique_amount']}</code> تومان ({fmt(t['unique_amount'])})\n\n"
        f"🏦 شماره کارت:\n{card_html}\n"
        f"{note}\n"
        "⚠️ <b>دقیقاً همین مبلغ رو واریز کن</b>، نه کمتر و نه بیشتر. با مبلغ متفاوت، تایید دیرتر انجام می‌شه.\n"
        f"⏳ مهلت پرداخت: {left} دقیقه\n\n"
        "بعد از واریز، دکمه‌ی «✅ پرداخت کردم» رو بزن."
    )


def topup_kb(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ پرداخت کردم", callback_data=f"paid:{tid}"),
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel:{tid}")]])


async def make_unique_amount(base):
    now = int(time.time())
    used = {r[0] for r in await many(
        "SELECT unique_amount FROM topups WHERE status='claimed' OR (status IN ('pending','awaiting') AND expires>?)", (now,))}
    nums = list(range(RAND_MIN, RAND_MAX + 1))
    random.shuffle(nums)
    for n in nums:
        if base + n not in used:
            return base + n
    return None


async def get_open_topup(uid):
    return await one("SELECT * FROM topups WHERE user_id=? AND (status='claimed' OR (status IN ('pending','awaiting') AND expires>?))",
                     (uid, int(time.time())))


async def show_open_topup(msg: Message, t):
    if t["status"] == "claimed":
        return await msg.answer("⏳ رسید قبلی‌ت در انتظار تاییده. بعد از تایید می‌تونی دوباره شارژ کنی.")
    if t["status"] == "awaiting":
        return await msg.answer("🧾 منتظر رسید پرداختت هستم!\nعکس رسید یا شماره پیگیری رو همین‌جا بفرست.")
    return await msg.answer(await topup_text(t), reply_markup=topup_kb(t["id"]))


async def start_charge(msg: Message, state: FSMContext, uid: int):
    await state.clear()
    t = await get_open_topup(uid)
    if t:
        return await show_open_topup(msg, t)
    await state.set_state(Charge.amount)
    pres = [p for p in PRESETS if p >= MIN_TOPUP]
    rows = [[InlineKeyboardButton(text=f"{fmt(p)} تومان", callback_data=f"amt:{p}") for p in pres[i:i + 2]]
            for i in range(0, len(pres), 2)]
    rows.append(cancel_row())
    await msg.answer("💰 <b>شارژ کیف پول</b>\n\n"
                     "یکی از مبلغ‌ها رو انتخاب کن، یا مبلغ دلخواهت رو (به تومان) تایپ کن و بفرست.\n"
                     f"📌 حداقل شارژ: <b>{fmt(MIN_TOPUP)}</b> تومان",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def create_topup(msg: Message, state: FSMContext, uid: int, base: int):
    t = await get_open_topup(uid)
    if t:
        await state.clear()
        return await show_open_topup(msg, t)
    unique = await make_unique_amount(base)
    if unique is None:
        return await msg.answer("الان ظرفیت پر شده، چند دقیقه دیگه دوباره امتحان کن 🙏")
    now = int(time.time())
    tid = await db.insert(
        "INSERT INTO topups(user_id,base,unique_amount,status,created,expires) VALUES (?,?,?,?,?,?)",
        (uid, base, unique, "pending", now, now + TOPUP_TTL))
    await state.clear()
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await msg.answer(await topup_text(t), reply_markup=topup_kb(t["id"]))


@router.message(F.text == BTN_CHARGE)
async def charge(m: Message, state: FSMContext):
    await start_charge(m, state, m.from_user.id)


@router.callback_query(F.data == "go_charge")
async def go_charge(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await start_charge(c.message, state, c.from_user.id)


@router.callback_query(F.data.startswith("amt:"))
async def charge_preset(c: CallbackQuery, state: FSMContext):
    base = int(c.data.split(":")[1])
    await c.answer()
    if base < MIN_TOPUP:
        return
    await create_topup(c.message, state, c.from_user.id, base)


@router.message(Charge.amount, F.text)
async def charge_amount(m: Message, state: FSMContext):
    base = to_int(m.text)
    if base is None or base < MIN_TOPUP:
        return await m.answer(f"مبلغ معتبر نیست 🤔\nیه عدد (به تومان) و حداقل <b>{fmt(MIN_TOPUP)}</b> بفرست:",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await create_topup(m, state, m.from_user.id, base)


@router.callback_query(F.data.startswith("cancel:"))
async def topup_cancel(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='canceled' WHERE id=? AND user_id=? AND status='pending'",
                           (tid, c.from_user.id))
    await db.commit()
    await c.message.edit_text("درخواست شارژ لغو شد.\nهر وقت خواستی دوباره از «💰 شارژ کیف پول» شروع کن."
                              if cur.rowcount else "این درخواست دیگه قابل لغو نیست.")
    await c.answer()


def admin_topup_kb(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data=f"tc_ok:{tid}"),
        InlineKeyboardButton(text="❌ رد", callback_data=f"tc_no:{tid}"),
        InlineKeyboardButton(text="✏️ مبلغ دیگر", callback_data=f"tc_edit:{tid}")]])


def admin_topup_text(t, username):
    credit = t["unique_amount"] if CREDIT_FULL else t["base"]
    return (f"💳 <b>رسید #{t['id']}</b>\n"
            f"👤 {html.escape(username or '-')} | <code>{t['user_id']}</code>\n"
            f"💰 مبلغ واریزی: <b>{t['unique_amount']:,}</b>\n"
            f"📥 اعتبار پیش‌فرض: {credit:,}")


@router.callback_query(F.data.startswith("paid:"))
async def topup_paid(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='awaiting', expires=? WHERE id=? AND user_id=? AND status='pending'",
                           (int(time.time()) + TOPUP_TTL, tid, c.from_user.id))
    await db.commit()
    if cur.rowcount != 1:
        return await c.answer("این درخواست دیگه فعال نیست.", show_alert=True)
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await c.message.edit_text(
        "🧾 <b>حالا رسید پرداخت رو بفرست</b>\n\n"
        "عکس رسید (یا شماره پیگیری) رو همین‌جا بفرست تا برای ادمین ارسال بشه. "
        "بعد از تایید، کیف پولت شارژ می‌شه و بهت خبر می‌دم.\n\n"
        f"💰 مبلغ واریزی: <code>{t['unique_amount']}</code> تومان")
    await c.answer()


async def approve_topup(tid, amount=None):
    cur = await db.execute("UPDATE topups SET status='approved' WHERE id=? AND status IN ('pending','awaiting','claimed')", (tid,))
    await db.commit()
    if cur.rowcount != 1:
        return None
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    credit = amount or (t["unique_amount"] if CREDIT_FULL else t["base"])
    await db.execute("UPDATE topups SET credited=? WHERE id=?", (credit, tid))
    await db.commit()
    await balance_change(t["user_id"], credit, f"topup-{tid}")
    return t, credit


@router.callback_query(F.data.startswith("tc_ok:"), IS_ADMIN)
async def tc_ok(c: CallbackQuery, bot: Bot):
    r = await approve_topup(int(c.data.split(":")[1]))
    if not r:
        return await c.answer("قبلاً بررسی شده.", show_alert=True)
    t, credit = r
    await c.message.edit_text(c.message.html_text + f"\n\n✅ تایید شد ({credit:,})")
    with suppress(Exception):
        await bot.send_message(t["user_id"], f"✅ <b>پرداختت تایید شد!</b>\n💰 {credit:,} تومان به کیف پولت اضافه شد.\nحالا می‌تونی سفارش بدی 🛒")
    await c.answer()


@router.callback_query(F.data.startswith("tc_no:"), IS_ADMIN)
async def tc_no(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='rejected' WHERE id=? AND status IN ('pending','awaiting','claimed')", (tid,))
    await db.commit()
    if cur.rowcount != 1:
        return await c.answer("قبلاً بررسی شده.", show_alert=True)
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await c.message.edit_text(c.message.html_text + "\n\n❌ رد شد")
    with suppress(Exception):
        await bot.send_message(t["user_id"], "❌ متأسفانه پرداختت تایید نشد.\nاگه واریز کردی، رسید رو برای پشتیبانی بفرست تا بررسی کنیم.")
    await c.answer()


@router.callback_query(F.data.startswith("tc_edit:"), IS_ADMIN)
async def tc_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminEdit.amount)
    await state.update_data(tid=int(c.data.split(":")[1]))
    await c.message.answer("مبلغ واقعی که باید به کیف پول اضافه بشه رو بفرست:")
    await c.answer()


@router.message(AdminEdit.amount, IS_ADMIN, F.text)
async def tc_edit_amount(m: Message, state: FSMContext, bot: Bot):
    amount = to_int(m.text)
    if not amount:
        return await m.answer("عدد معتبر بفرست.")
    tid = (await state.get_data())["tid"]
    await state.clear()
    r = await approve_topup(tid, amount)
    if not r:
        return await m.answer("این رسید قبلاً بررسی شده.")
    await m.answer(f"✅ {amount:,} تومان شارژ شد.")
    with suppress(Exception):
        await bot.send_message(r[0]["user_id"], f"✅ <b>پرداختت تایید شد!</b>\n💰 {amount:,} تومان به کیف پولت اضافه شد.\nحالا می‌تونی سفارش بدی 🛒")


# ───────────────────────── ثبت سفارش ─────────────────────────
class Order(StatesGroup):
    links = State()
    qty = State()


QTY_PRESETS = [1000, 5000, 10000, 20000]


@router.message(F.text == BTN_ORDER)
async def order_start(m: Message, state: FSMContext):
    await state.clear()
    price = int(await get_setting("price_per_1000"))
    await state.set_state(Order.links)
    await m.answer("🛒 <b>ثبت سفارش سین</b>\n\n"
                   f"💵 قیمت هر ۱۰۰۰ سین: <b>{fmt(price)}</b> تومان\n\n"
                   f"🔗 لینک پست(ها) رو بفرست؛ هر لینک توی یه خط (حداکثر {MAX_LINKS} تا).\n"
                   "مثال:\n<code>https://t.me/channel/123</code>\n\n"
                   "⚠️ کانال باید <b>عمومی (Public)</b> باشه.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


async def check_link(bot, link):
    """None یعنی سالمه؛ در غیر این صورت متن مشکل."""
    ch, pid = link.split("/")[3], link.split("/")[4]
    try:
        chat = await bot.get_chat(f"@{ch}")
    except TelegramBadRequest:
        return "کانال پیدا نشد یا عمومی نیست"
    except Exception:
        return None  # نتونستیم چک کنیم، بلاک نمی‌کنیم
    if chat.type != "channel":
        return "این لینک مربوط به یه کانال نیست"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(f"https://t.me/{ch}/{pid}", params={"embed": "1", "mode": "tme"}) as r:
                if "tgme_widget_message_error" in await r.text():
                    return "پست پیدا نشد (شماره‌ی پست رو چک کن)"
    except Exception:
        pass
    return None


@router.message(Order.links, F.text)
async def order_links(m: Message, state: FSMContext, bot: Bot):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row()])
    links = extract_links(m.text)
    if not links:
        if "t.me/c/" in m.text:
            return await m.answer("این لینک مربوط به کانال <b>خصوصی</b> 🔒 هست.\n"
                                  "سین فقط برای پست‌های کانال‌های <b>عمومی</b> ثبت می‌شه.", reply_markup=cancel_kb)
        return await m.answer("لینکی پیدا نکردم 🤔\nلینک باید شبیه این باشه:\n<code>https://t.me/channel/123</code>",
                              reply_markup=cancel_kb)
    results = await asyncio.gather(*(check_link(bot, l) for l in links))
    bad = [(l, r) for l, r in zip(links, results) if r]
    if bad:
        txt = "❌ این لینک‌ها مشکل دارن:\n\n" + "\n".join(f"{l}\n↳ {r}" for l, r in bad) + "\n\nلینک‌های درست رو دوباره بفرست."
        return await m.answer(txt, reply_markup=cancel_kb, disable_web_page_preview=True)
    busy = [r["link"] for r in await many("SELECT DISTINCT link FROM orders WHERE settled=0 AND link = ANY(?)", (links,))]
    note = ""
    if busy:
        links = [l for l in links if l not in busy]
        note = ("⚠️ برای این پست‌ها هنوز یه سفارش در حال انجامه، پس حذف شدن:\n" + "\n".join(busy) + "\n\n")
        if not links:
            return await m.answer(note + "بعد از تکمیل سفارش قبلی دوباره امتحان کن، یا لینک دیگه‌ای بفرست.",
                                  reply_markup=cancel_kb, disable_web_page_preview=True)
    await state.update_data(links=links)
    await state.set_state(Order.qty)
    lo, hi = int(await get_setting("min_qty")), int(await get_setting("max_qty"))
    pres = [q for q in QTY_PRESETS if lo <= q <= hi]
    rows = [[InlineKeyboardButton(text=fmt(q), callback_data=f"qty:{q}") for q in pres[i:i + 2]]
            for i in range(0, len(pres), 2)]
    rows.append(cancel_row())
    await m.answer(note + f"✅ {len(links)} پست دریافت شد.\n\n"
                   f"👁 تعداد سین <b>هر پست</b> رو انتخاب کن یا تایپ کن ({fmt(lo)} تا {fmt(hi)}):",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def process_qty(msg: Message, state: FSMContext, uid: int, qty):
    lo, hi = int(await get_setting("min_qty")), int(await get_setting("max_qty"))
    if qty is None or not lo <= qty <= hi:
        return await msg.answer(f"تعداد باید بین <b>{fmt(lo)}</b> و <b>{fmt(hi)}</b> باشه 🙏\nدوباره بفرست:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    links = (await state.get_data()).get("links")
    if not links:
        await state.clear()
        return await msg.answer("این سفارش منقضی شده؛ دوباره از «🛒 ثبت سفارش سین» شروع کن.")
    price = int(await get_setting("price_per_1000"))
    link_cost = math.ceil(qty * price / 1000)
    total = link_cost * len(links)
    bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
    await state.update_data(qty=qty, link_cost=link_cost, total=total)
    text = ("🧾 <b>خلاصه‌ی سفارش</b>\n\n"
            f"📌 تعداد پست: {len(links)}\n"
            f"👁 سین هر پست: {fmt(qty)}\n"
            f"💵 هزینه‌ی هر پست: {fmt(link_cost)} تومان\n"
            "━━━━━━━━━━\n"
            f"💰 <b>جمع کل: {fmt(total)} تومان</b>\n"
            f"👛 موجودی تو: {fmt(bal)} تومان")
    if bal < total:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="go_charge")]])
        return await msg.answer(text + f"\n\n❌ موجودی کافی نیست. برای این سفارش <b>{fmt(total - bal)}</b> تومان دیگه لازمه.",
                                reply_markup=kb)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید و ثبت سفارش", callback_data="ord_ok"),
        InlineKeyboardButton(text="❌ انصراف", callback_data="ord_no")]])
    await msg.answer(text + "\n\nاگه همه‌چی درسته، تایید رو بزن 👇", reply_markup=kb)


@router.message(Order.qty, F.text)
async def order_qty(m: Message, state: FSMContext):
    await process_qty(m, state, m.from_user.id, to_int(m.text))


@router.callback_query(F.data.startswith("qty:"), Order.qty)
async def order_qty_preset(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await process_qty(c.message, state, c.from_user.id, int(c.data.split(":")[1]))


@router.callback_query(F.data == "ord_no")
async def order_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("سفارش لغو شد. پولی از حسابت کم نشد ✅")
    await c.answer()


@router.callback_query(F.data == "ord_ok")
async def order_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if "links" not in d or "total" not in d:
        return await c.answer("این سفارش منقضی شده، دوباره ثبت کن.", show_alert=True)
    uid = c.from_user.id
    if not await try_spend(uid, d["total"], "order"):
        await c.message.edit_text("❌ موجودی کافی نیست.")
        return await c.answer()
    await c.message.edit_text("⏳ در حال ثبت سفارش...")
    done, errors, unknown = 0, [], []
    for i, link in enumerate(d["links"]):
        if i:
            await asyncio.sleep(0.5)
        st, info = await place_order(uid, link, d["qty"], d["link_cost"])
        if st == "ok":
            done += 1
        elif st == "unknown":
            unknown.append(info)
            await notify_admins(bot, f"🚨 <b>سفارش نامشخص #{info}</b> (کاربر <code>{uid}</code>)\n{link} | {fmt(d['qty'])} سین\n"
                                     "جواب provider نامعتبر بود یا دیر رسید؛ ممکنه ثبت شده باشه.\n"
                                     f"اگه توی پنل provider ثبت شده: /resolve {info} شماره_سفارش_provider\n"
                                     f"اگه ثبت نشده: /refund {info}")
        else:
            errors.append(info)
            await balance_change(uid, d["link_cost"], "refund-failed-order")
    parts = []
    if done:
        parts.append(f"✅ <b>{done} سفارش با موفقیت ثبت شد!</b>\n"
                     "🚀 سین‌ها به‌تدریج ارسال می‌شن. پیشرفت رو از «📦 سفارش‌های من» ببین.")
    if unknown:
        parts.append(f"⏳ <b>{len(unknown)} سفارش در حال بررسیه.</b>\n"
                     "پاسخ سرویس‌دهنده دیر رسید. نتیجه رو بهت خبر می‌دم؛ اگه ثبت نشده باشه، پولش به کیف پولت برمی‌گرده.")
    if errors:
        parts.append(f"⚠️ {len(errors)} سفارش ثبت نشد و {fmt(len(errors) * d['link_cost'])} تومان به کیف پولت برگشت. "
                     "لطفاً کمی بعد دوباره تلاش کن یا به پشتیبانی پیام بده.")
        await notify_admins(bot, f"⚠️ خطا در ثبت سفارش (کاربر {uid}):\n{html.escape(errors[0][:300])}")
    text = "\n\n".join(parts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="my_orders")]])
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


async def show_orders(msg: Message, uid: int, edit=False):
    for o in await many("SELECT * FROM orders WHERE user_id=? AND settled=0 AND provider_order<>'' ORDER BY id DESC LIMIT 10", (uid,)):
        await sync_order(o)
    rows = await many("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
    if not rows:
        return await msg.answer("هنوز سفارشی ثبت نکردی 🙂\nاز «🛒 ثبت سفارش سین» شروع کن 🚀")
    lines = [f"<b>#{o['id']}</b> • {fmt(o['quantity'])} سین • {STATUS_FA.get(o['status'], o['status'])}\n🔗 {o['link']}"
             for o in rows]
    text = "📦 <b>سفارش‌های اخیر</b>\n\n" + "\n\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="orders_refresh")]])
    if edit:
        with suppress(Exception):
            return await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(F.text == BTN_ORDERS)
async def my_orders(m: Message, state: FSMContext):
    await state.clear()
    await show_orders(m, m.from_user.id)


@router.callback_query(F.data == "my_orders")
async def my_orders_cb(c: CallbackQuery):
    await c.answer()
    await show_orders(c.message, c.from_user.id)


@router.callback_query(F.data == "orders_refresh")
async def orders_refresh(c: CallbackQuery):
    await c.answer("بروز شد ✅")
    await show_orders(c.message, c.from_user.id, edit=True)


# ───────────────────────── پنل ادمین ─────────────────────────
ADMIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 مدیریت و شارژ کاربر", callback_data="adm_user")],
    [InlineKeyboardButton(text="📋 رسیدهای در انتظار", callback_data="adm_pending"),
     InlineKeyboardButton(text="💼 موجودی provider", callback_data="adm_provider")],
])


@router.message(Command("admin"), IS_ADMIN)
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    u = await one("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users")
    o = await one("SELECT COUNT(*) n, COALESCE(SUM(cost),0) s FROM orders")
    p = await one("SELECT COUNT(*) n FROM topups WHERE status='claimed'")
    unk = (await one("SELECT COUNT(*) AS n FROM orders WHERE status='Unknown'"))["n"]
    price = await get_setting("price_per_1000")
    await m.answer(
        "🛠 <b>پنل ادمین</b>\n\n"
        f"📊 کاربران: {u['n']} | مجموع موجودی کیف‌پول‌ها: {fmt(u['b'])}\n"
        f"🛒 سفارش‌ها: {o['n']} | فروش: {fmt(o['s'])}\n"
        f"🕓 رسید در انتظار: {p['n']}\n⚠️ سفارش نامشخص: {unk}\n💵 قیمت هر ۱۰۰۰: {fmt(price)}\n\n"
        "<b>دستورات:</b>\n/user آیدی یا @یوزرنیم\n/pending رسیدهای در انتظار\n/add id مبلغ\n/sub id مبلغ\n"
        "/ban id\n/unban id\n/price مبلغ\n/card متن کارت\n/limits حداقل حداکثر\n"
        "/support @آیدی\n/brand نام ربات\n/provider موجودی provider\n/unknown سفارش‌های نامشخص\n/resolve id شماره\n/refund id\n/broadcast متن",
        reply_markup=ADMIN_KB)


# ── مدیریت کاربر و شارژ دستی کیف پول
async def find_user(q):
    q = (q or "").strip()
    n = to_int(q)
    if n:
        return await one("SELECT * FROM users WHERE id=?", (n,))
    return await one("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (q.lstrip("@"),))


async def user_card(u):
    st = await one("SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS s FROM orders WHERE user_id=?", (u["id"],))
    last = await many("SELECT amount, reason, ts FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["id"],))
    hist = "\n".join(
        f"{r['amount']:+,} • {html.escape(r['reason'])} • {time.strftime('%m/%d %H:%M', time.gmtime(r['ts'] + 12600))}"
        for r in last) or "—"
    return ("👤 <b>اطلاعات کاربر</b>\n\n"
            f"🆔 <code>{u['id']}</code>\n"
            f"🔗 {('@' + html.escape(u['username'])) if u['username'] else '-'}\n"
            f"💰 موجودی: <b>{fmt(u['balance'])}</b> تومان\n"
            f"📦 سفارش‌ها: {st['n']} | مجموع خرید: {fmt(st['s'])} تومان\n"
            f"🚦 وضعیت: {'🚫 بن‌شده' if u['banned'] else '✅ فعال'}\n\n"
            f"🕓 <b>آخرین تراکنش‌ها:</b>\n{hist}")


def user_kb(u):
    uid = u["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data=f"au_add:{uid}"),
         InlineKeyboardButton(text="➖ کسر", callback_data=f"au_sub:{uid}")],
        [InlineKeyboardButton(text="✅ رفع بن" if u["banned"] else "🚫 بن کاربر", callback_data=f"au_ban:{uid}")]])


@router.callback_query(F.data == "adm_user", IS_ADMIN)
async def adm_user(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminUser.find)
    await c.message.answer("🔎 آیدی عددی یا @یوزرنیم کاربر رو بفرست.\n(کاربر باید قبلاً ربات رو استارت کرده باشه)",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await c.answer()


@router.message(AdminUser.find, IS_ADMIN, F.text)
async def adm_find(m: Message, state: FSMContext):
    u = await find_user(m.text)
    if not u:
        return await m.answer("کاربری پیدا نشد 🤔 دوباره آیدی یا یوزرنیم رو بفرست:",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await state.clear()
    await m.answer(await user_card(u), reply_markup=user_kb(u))


@router.message(Command("user"), IS_ADMIN)
async def cmd_user(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    u = await find_user(command.args)
    if not u:
        return await m.answer("فرمت: /user آیدی یا @یوزرنیم (کاربر باید ربات رو استارت کرده باشه)")
    await m.answer(await user_card(u), reply_markup=user_kb(u))


@router.callback_query(F.data.regexp(r"^au_(add|sub):\d+$"), IS_ADMIN)
async def au_start(c: CallbackQuery, state: FSMContext):
    mode, uid = c.data[3:].split(":")
    await state.set_state(AdminUser.amount)
    await state.update_data(mode=mode, uid=int(uid))
    title = "➕ شارژ" if mode == "add" else "➖ کسر"
    await c.message.answer(f"{title} کیف پول <code>{uid}</code>\nمبلغ (تومان) رو بفرست:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await c.answer()


@router.message(AdminUser.amount, IS_ADMIN, F.text)
async def au_amount(m: Message, state: FSMContext):
    amt = to_int(m.text)
    if not amt:
        return await m.answer("عدد معتبر بفرست:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    d = await state.get_data()
    await state.update_data(amt=amt)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید", callback_data="au_ok"),
        InlineKeyboardButton(text="❌ لغو", callback_data="au_no")]])
    verb = "شارژ" if d["mode"] == "add" else "کسر"
    await m.answer(f"{verb} <b>{fmt(amt)}</b> تومان برای کاربر <code>{d['uid']}</code>؟", reply_markup=kb)


@router.callback_query(F.data == "au_no", IS_ADMIN)
async def au_no(c: CallbackQuery, state: FSMContext):
    await state.clear()
    with suppress(Exception):
        await c.message.edit_text("لغو شد.")
    await c.answer()


@router.callback_query(F.data == "au_ok", IS_ADMIN)
async def au_ok(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d.get("uid") or not d.get("amt"):
        return await c.answer("این درخواست منقضی شده.", show_alert=True)
    uid, amt = d["uid"], d["amt"]
    if d["mode"] == "add":
        await balance_change(uid, amt, f"admin-add-{c.from_user.id}")
        with suppress(Exception):
            await bot.send_message(uid, f"💰 <b>{fmt(amt)}</b> تومان به کیف پولت اضافه شد.")
    elif not await try_spend(uid, amt, f"admin-sub-{c.from_user.id}"):
        await c.message.edit_text("❌ موجودی کاربر کمتر از این مبلغه.")
        return await c.answer()
    u = await one("SELECT * FROM users WHERE id=?", (uid,))
    await c.message.edit_text("✅ انجام شد.\n\n" + await user_card(u), reply_markup=user_kb(u))
    await c.answer()


@router.callback_query(F.data.startswith("au_ban:"), IS_ADMIN)
async def au_ban(c: CallbackQuery):
    uid = int(c.data.split(":")[1])
    u = await one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return await c.answer("کاربر پیدا نشد.", show_alert=True)
    await db.execute("UPDATE users SET banned=? WHERE id=?", (0 if u["banned"] else 1, uid))
    u = await one("SELECT * FROM users WHERE id=?", (uid,))
    await c.message.edit_text(await user_card(u), reply_markup=user_kb(u))
    await c.answer("انجام شد ✅")


async def send_pending(msg: Message):
    rows = await many("SELECT t.*, u.username FROM topups t LEFT JOIN users u ON u.id=t.user_id WHERE t.status='claimed'")
    if not rows:
        return await msg.answer("رسید در انتظاری نیست ✅")
    for t in rows:
        await msg.answer(admin_topup_text(t, t["username"]), reply_markup=admin_topup_kb(t["id"]))


@router.message(Command("pending"), IS_ADMIN)
async def pending(m: Message):
    await send_pending(m)


@router.callback_query(F.data == "adm_pending", IS_ADMIN)
async def adm_pending(c: CallbackQuery):
    await c.answer()
    await send_pending(c.message)


async def _id_amount(command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) < 2:
        return None, None
    return to_int(parts[0]), to_int(parts[1])


@router.message(Command("add"), IS_ADMIN)
async def cmd_add(m: Message, command: CommandObject):
    uid, amt = await _id_amount(command)
    if not uid or not amt or not await one("SELECT 1 FROM users WHERE id=?", (uid,)):
        return await m.answer("فرمت: /add user_id مبلغ (کاربر باید قبلاً ربات رو استارت کرده باشه)")
    await balance_change(uid, amt, f"admin-add-{m.from_user.id}")
    await m.answer(f"✅ {amt:,} تومان به {uid} اضافه شد.")


@router.message(Command("sub"), IS_ADMIN)
async def cmd_sub(m: Message, command: CommandObject):
    uid, amt = await _id_amount(command)
    if not uid or not amt:
        return await m.answer("فرمت: /sub user_id مبلغ")
    ok = await try_spend(uid, amt, f"admin-sub-{m.from_user.id}")
    await m.answer("✅ کسر شد." if ok else "❌ موجودی کاربر کمتر از این مبلغه.")


@router.message(Command("ban", "unban"), IS_ADMIN)
async def cmd_ban(m: Message, command: CommandObject):
    uid = to_int(command.args)
    if not uid:
        return await m.answer("فرمت: /ban user_id")
    await db.execute("UPDATE users SET banned=? WHERE id=?", (1 if command.command == "ban" else 0, uid))
    await db.commit()
    await m.answer("✅ انجام شد.")


@router.message(Command("price"), IS_ADMIN)
async def cmd_price(m: Message, command: CommandObject):
    v = to_int(command.args)
    if not v:
        return await m.answer("فرمت: /price 1500")
    await set_setting("price_per_1000", v)
    await m.answer(f"✅ قیمت هر ۱۰۰۰ سین: {v:,}")


@router.message(Command("card"), IS_ADMIN)
async def cmd_card(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("فرمت: /card 6037... به نام ...")
    await set_setting("card", command.args)
    await m.answer("✅ اطلاعات کارت عوض شد.")


@router.message(Command("support"), IS_ADMIN)
async def cmd_support(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("فرمت: /support @username")
    await set_setting("support", command.args.strip())
    await m.answer("✅ آیدی پشتیبانی عوض شد.")


@router.message(Command("brand"), IS_ADMIN)
async def cmd_brand(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("فرمت: /brand نام ربات")
    await set_setting("brand", command.args.strip())
    await m.answer("✅ نام ربات عوض شد.")


@router.message(Command("limits"), IS_ADMIN)
async def cmd_limits(m: Message, command: CommandObject):
    lo, hi = await _id_amount(command)
    if not lo or not hi or lo > hi:
        return await m.answer("فرمت: /limits 100 100000")
    await set_setting("min_qty", lo)
    await set_setting("max_qty", hi)
    await m.answer("✅ انجام شد.")


async def provider_balance_text():
    try:
        res = await provider(action="balance")
        bal, cur = res.get("balance"), str(res.get("currency", ""))
        extra = f" ≈ {fmt(float(bal) * COIN_TOMAN)} تومان" if cur.lower() == "coin" and bal is not None else ""
        return f"💼 موجودی provider: {bal} {cur}{extra}"
    except Exception as e:
        return f"خطا: {html.escape(str(e))}"


@router.message(Command("provider"), IS_ADMIN)
async def cmd_provider(m: Message):
    await m.answer(await provider_balance_text())


@router.callback_query(F.data == "adm_provider", IS_ADMIN)
async def adm_provider(c: CallbackQuery):
    await c.answer()
    await c.message.answer(await provider_balance_text())


@router.message(Command("unknown"), IS_ADMIN)
async def cmd_unknown(m: Message):
    rows = await many("SELECT * FROM orders WHERE status='Unknown' ORDER BY id")
    if not rows:
        return await m.answer("سفارش نامشخصی نیست ✅")
    for o in rows:
        await m.answer(f"#{o['id']} • کاربر <code>{o['user_id']}</code> • {fmt(o['quantity'])} سین\n{o['link']}\n"
                       f"/resolve {o['id']} شماره_provider\n/refund {o['id']}", disable_web_page_preview=True)


@router.message(Command("resolve"), IS_ADMIN)
async def cmd_resolve(m: Message, command: CommandObject, bot: Bot):
    parts = (command.args or "").split()
    oid = to_int(parts[0]) if parts else None
    if len(parts) < 2 or not oid:
        return await m.answer("فرمت: /resolve شماره_سفارش شماره_سفارش_provider")
    cur = await db.execute("UPDATE orders SET provider_order=?, status='Pending' WHERE id=? AND status='Unknown'", (parts[1], oid))
    if cur.rowcount != 1:
        return await m.answer("این سفارش پیدا نشد یا دیگه نامشخص نیست.")
    o = await one("SELECT * FROM orders WHERE id=?", (oid,))
    await m.answer("✅ ثبت شد و از حالا وضعیتش پیگیری می‌شه.")
    with suppress(Exception):
        await bot.send_message(o["user_id"], f"✅ سفارش #{oid} تایید شد و در حال انجامه.")


@router.message(Command("refund"), IS_ADMIN)
async def cmd_refund(m: Message, command: CommandObject, bot: Bot):
    oid = to_int(command.args)
    if not oid:
        return await m.answer("فرمت: /refund شماره_سفارش")
    cur = await db.execute("UPDATE orders SET status='Canceled', settled=1 WHERE id=? AND status='Unknown'", (oid,))
    if cur.rowcount != 1:
        return await m.answer("این سفارش پیدا نشد یا دیگه نامشخص نیست.")
    o = await one("SELECT * FROM orders WHERE id=?", (oid,))
    await balance_change(o["user_id"], o["cost"], f"refund-order-{oid}")
    await m.answer(f"✅ {fmt(o['cost'])} تومان به کاربر برگشت.")
    with suppress(Exception):
        await bot.send_message(o["user_id"], f"ℹ️ سفارش #{oid} ثبت نشد و {fmt(o['cost'])} تومان به کیف پولت برگشت.")


@router.message(Command("broadcast"), IS_ADMIN)
async def cmd_broadcast(m: Message, command: CommandObject, bot: Bot):
    if not command.args:
        return await m.answer("فرمت: /broadcast متن پیام")
    sent = 0
    for u in await many("SELECT id FROM users WHERE banned=0"):
        try:
            await bot.send_message(u["id"], command.args)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ برای {sent} نفر ارسال شد.")


# ───────────────────────── دریافت رسید (باید آخرین هندلر باشه) ─────────────────────────
@router.message(StateFilter(None), F.photo | F.document | F.text)
async def receipt_in(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return
    t = await one("SELECT * FROM topups WHERE user_id=? AND status='awaiting' ORDER BY id DESC LIMIT 1", (m.from_user.id,))
    if not t:
        return
    key = None
    if m.photo:
        key = m.photo[-1].file_unique_id
    elif m.document:
        key = m.document.file_unique_id
    elif m.text and len(m.text.strip()) >= 6:
        key = "t:" + m.text.strip().lower()
    dup = await one("SELECT id, status FROM topups WHERE receipt_uid=? AND id<>? LIMIT 1", (key, t["id"])) if key else None
    cur = await db.execute("UPDATE topups SET status='claimed', receipt_uid=? WHERE id=? AND status='awaiting'", (key, t["id"]))
    if cur.rowcount != 1:
        return
    warn = (f"🚨 <b>هشدار: این رسید قبلاً برای درخواست #{dup['id']} ({dup['status']}) ثبت شده!</b>\n\n" if dup else "")
    for a in ADMIN_IDS:
        with suppress(Exception):
            cp = await bot.copy_message(chat_id=a, from_chat_id=m.chat.id, message_id=m.message_id)
            await bot.send_message(a, warn + "🧾 <b>رسید بالا</b> مربوط به این پرداخته:\n\n" + admin_topup_text(t, m.from_user.username),
                                   reply_markup=admin_topup_kb(t["id"]), reply_to_message_id=cp.message_id)
    await m.answer("✅ <b>رسیدت ثبت و برای ادمین ارسال شد.</b>\nبعد از تایید، کیف پولت شارژ می‌شه و همین‌جا بهت خبر می‌دم 🙏")


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot):
    logging.error("handler error", exc_info=event.exception)
    await report_error(bot, "handler", event.exception)
    upd = event.update
    with suppress(Exception):
        if upd.message:
            await upd.message.answer("⚠️ یه خطای موقت پیش اومد. لطفاً دوباره امتحان کن.")
        elif upd.callback_query:
            await upd.callback_query.answer("⚠️ خطای موقت؛ دوباره امتحان کن", show_alert=True)
    return True


# ───────────────────────── اجرا ─────────────────────────
async def health_server():
    """سرور کوچیک برای Render تا سرویس رو «زنده» تشخیص بده."""
    from aiohttp import web

    async def ok(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await health_server()
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Guard())
    dp.callback_query.outer_middleware(Guard())
    dp.include_router(router)
    asyncio.create_task(poll_orders(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
