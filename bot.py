import asyncio
import html
import logging
import math
import os
import random
import re
import time
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
PROVIDER_URL = os.getenv("PROVIDER_URL", "")
PROVIDER_KEY = os.getenv("PROVIDER_KEY", "")
SERVICE_ID = os.getenv("SERVICE_ID", "")
PROVIDER_METHOD = os.getenv("PROVIDER_METHOD", "GET").upper()  # GET یا POST
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
}

LINK_RE = re.compile(r"https?://t\.me/(?:c/)?[A-Za-z0-9_]+/\d+")
STATUS_FA = {
    "Pending": "⏳ در صف", "Processing": "⚙️ در حال پردازش", "In progress": "⚙️ در حال انجام",
    "Completed": "✅ تکمیل", "Partial": "◐ ناقص (مابقی برگشت خورد)",
    "Canceled": "❌ لغو (برگشت پول)", "Cancelled": "❌ لغو (برگشت پول)",
    "Refunded": "❌ برگشت پول", "Fail": "❌ ناموفق", "Failed": "❌ ناموفق",
}
REFUND_STATUSES = {"Canceled", "Cancelled", "Refunded", "Fail", "Failed"}

BTN_ORDER, BTN_CHARGE, BTN_ACC, BTN_ORDERS = "🛒 ثبت سفارش سین", "💰 شارژ کیف پول", "👤 حساب من", "📦 سفارش‌های من"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_ORDER)],
              [KeyboardButton(text=BTN_CHARGE), KeyboardButton(text=BTN_ACC)],
              [KeyboardButton(text=BTN_ORDERS)]],
    resize_keyboard=True,
)

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
    data = {"key": PROVIDER_KEY, **params}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        req = s.post(PROVIDER_URL, data=data) if PROVIDER_METHOD == "POST" else s.get(PROVIDER_URL, params=data)
        async with req as r:
            return await r.json(content_type=None)


async def notify_admins(bot, text):
    for a in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(a, text)


async def place_order(uid, link, qty, cost):
    try:
        res = await provider(action="add", service=SERVICE_ID, link=link, quantity=qty)
    except Exception as e:
        res = {"error": str(e)}
    if isinstance(res, dict) and "order" in res:
        await db.execute(
            "INSERT INTO orders(user_id,link,quantity,cost,provider_order,status,created) VALUES (?,?,?,?,?,?,?)",
            (uid, link, qty, cost, str(res["order"]), "Pending", int(time.time())))
        await db.commit()
        return True, res["order"]
    return False, str(res.get("error", res) if isinstance(res, dict) else res)


async def sync_order(o):
    """وضعیت سفارش رو از provider می‌گیره؛ در صورت لغو/ناقص، پول رو برمی‌گردونه."""
    try:
        res = await provider(action="status", order=o["provider_order"])
    except Exception:
        return None
    st = res.get("status") if isinstance(res, dict) else None
    if not st:
        return None
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
            for o in await many("SELECT * FROM orders WHERE settled=0 LIMIT 200"):
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
        except Exception:
            logging.exception("poll_orders")


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
WELCOME = "سلام 👋 به ربات فروش سین خوش اومدی.\nاز منوی پایین انتخاب کن:"


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=MENU)


@router.callback_query(F.data == "chk_join")
async def chk_join(c: CallbackQuery):
    await c.message.answer(WELCOME, reply_markup=MENU)
    await c.answer()


@router.message(F.text == BTN_ACC)
async def account(m: Message, state: FSMContext):
    await state.clear()
    u = await one("SELECT * FROM users WHERE id=?", (m.from_user.id,))
    await m.answer(f"🆔 شناسه: <code>{u['id']}</code>\n💰 موجودی: <b>{u['balance']:,}</b> تومان")


# ───────────────────────── شارژ کیف پول ─────────────────────────
class Charge(StatesGroup):
    amount = State()


class AdminEdit(StatesGroup):
    amount = State()


async def topup_text(t):
    card = await get_setting("card")
    left = max(0, (t["expires"] - int(time.time())) // 60)
    return (
        "💳 برای شارژ، <b>دقیقاً</b> مبلغ زیر رو کارت‌به‌کارت کن:\n\n"
        f"💰 مبلغ: <code>{t['unique_amount']}</code> تومان ({t['unique_amount']:,})\n\n"
        f"🏦 کارت:\n<code>{html.escape(card)}</code>\n\n"
        "⚠️ <b>دقیقاً همین مبلغ رو واریز کن، نه کمتر و نه بیشتر.</b> "
        "با مبلغ متفاوت، تایید دیرتر انجام می‌شه.\n"
        f"⏳ مهلت پرداخت: {left} دقیقه\n\n"
        "بعد از واریز دکمه‌ی «پرداخت کردم» رو بزن."
    )


def topup_kb(tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ پرداخت کردم", callback_data=f"paid:{tid}"),
        InlineKeyboardButton(text="❌ لغو", callback_data=f"cancel:{tid}")]])


async def make_unique_amount(base):
    now = int(time.time())
    used = {r[0] for r in await many(
        "SELECT unique_amount FROM topups WHERE status='claimed' OR (status='pending' AND expires>?)", (now,))}
    nums = list(range(RAND_MIN, RAND_MAX + 1))
    random.shuffle(nums)
    for n in nums:
        if base + n not in used:
            return base + n
    return None


@router.message(F.text == BTN_CHARGE)
async def charge(m: Message, state: FSMContext):
    await state.clear()
    t = await one("SELECT * FROM topups WHERE user_id=? AND (status='claimed' OR (status='pending' AND expires>?))",
                  (m.from_user.id, int(time.time())))
    if t:
        if t["status"] == "claimed":
            return await m.answer("⏳ رسید قبلی‌ت در انتظار تاییده. بعد از تایید می‌تونی دوباره شارژ کنی.")
        return await m.answer(await topup_text(t), reply_markup=topup_kb(t["id"]))
    await state.set_state(Charge.amount)
    await m.answer(f"مبلغ شارژ رو به تومان بفرست (حداقل {MIN_TOPUP:,}):")


@router.message(Charge.amount, F.text)
async def charge_amount(m: Message, state: FSMContext):
    base = to_int(m.text)
    if base is None or base < MIN_TOPUP:
        return await m.answer(f"مبلغ معتبر نیست. یه عدد حداقل {MIN_TOPUP:,} بفرست.")
    unique = await make_unique_amount(base)
    if unique is None:
        return await m.answer("الان ظرفیت پر شده، چند دقیقه دیگه امتحان کن.")
    now = int(time.time())
    tid = await db.insert(
        "INSERT INTO topups(user_id,base,unique_amount,status,created,expires) VALUES (?,?,?,?,?,?)",
        (m.from_user.id, base, unique, "pending", now, now + TOPUP_TTL))
    await state.clear()
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await m.answer(await topup_text(t), reply_markup=topup_kb(t["id"]))


@router.callback_query(F.data.startswith("cancel:"))
async def topup_cancel(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='canceled' WHERE id=? AND user_id=? AND status='pending'",
                           (tid, c.from_user.id))
    await db.commit()
    await c.message.edit_text("درخواست شارژ لغو شد." if cur.rowcount else "این درخواست قابل لغو نیست.")
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
async def topup_paid(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='claimed' WHERE id=? AND user_id=? AND status='pending'",
                           (tid, c.from_user.id))
    await db.commit()
    if cur.rowcount != 1:
        return await c.answer("این درخواست دیگه فعال نیست.", show_alert=True)
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await c.message.edit_text("✅ ثبت شد. بعد از تایید ادمین، کیف پولت شارژ می‌شه و بهت خبر می‌دم.")
    for a in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(a, admin_topup_text(t, c.from_user.username), reply_markup=admin_topup_kb(tid))
    await c.answer()


async def approve_topup(tid, amount=None):
    cur = await db.execute("UPDATE topups SET status='approved' WHERE id=? AND status IN ('pending','claimed')", (tid,))
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
        await bot.send_message(t["user_id"], f"✅ پرداختت تایید شد و <b>{credit:,}</b> تومان به کیف پولت اضافه شد.")
    await c.answer()


@router.callback_query(F.data.startswith("tc_no:"), IS_ADMIN)
async def tc_no(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='rejected' WHERE id=? AND status IN ('pending','claimed')", (tid,))
    await db.commit()
    if cur.rowcount != 1:
        return await c.answer("قبلاً بررسی شده.", show_alert=True)
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    await c.message.edit_text(c.message.html_text + "\n\n❌ رد شد")
    with suppress(Exception):
        await bot.send_message(t["user_id"], "❌ پرداختت تایید نشد. اگه واریز کردی با پشتیبانی تماس بگیر.")
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
        await bot.send_message(r[0]["user_id"], f"✅ پرداختت تایید شد و <b>{amount:,}</b> تومان به کیف پولت اضافه شد.")


# ───────────────────────── ثبت سفارش ─────────────────────────
class Order(StatesGroup):
    links = State()
    qty = State()


@router.message(F.text == BTN_ORDER)
async def order_start(m: Message, state: FSMContext):
    await state.clear()
    price = int(await get_setting("price_per_1000"))
    await state.set_state(Order.links)
    await m.answer(f"💵 قیمت هر ۱۰۰۰ سین: <b>{price:,}</b> تومان\n\n"
                   f"لینک پست‌ها رو بفرست (هر لینک توی یه خط، حداکثر {MAX_LINKS} تا).\n"
                   "مثال: <code>https://t.me/channel/123</code>")


@router.message(Order.links, F.text)
async def order_links(m: Message, state: FSMContext):
    links = list(dict.fromkeys(LINK_RE.findall(m.text)))[:MAX_LINKS]
    if not links:
        return await m.answer("لینک معتبر پیدا نشد. لینک پست کانال رو بفرست (مثل https://t.me/channel/123).")
    await state.update_data(links=links)
    await state.set_state(Order.qty)
    lo, hi = await get_setting("min_qty"), await get_setting("max_qty")
    await m.answer(f"✅ {len(links)} پست دریافت شد.\nتعداد سین برای <b>هر پست</b> رو بفرست ({int(lo):,} تا {int(hi):,}):")


@router.message(Order.qty, F.text)
async def order_qty(m: Message, state: FSMContext):
    qty = to_int(m.text)
    lo, hi = int(await get_setting("min_qty")), int(await get_setting("max_qty"))
    if qty is None or not lo <= qty <= hi:
        return await m.answer(f"تعداد باید بین {lo:,} و {hi:,} باشه.")
    price = int(await get_setting("price_per_1000"))
    links = (await state.get_data())["links"]
    link_cost = math.ceil(qty * price / 1000)
    total = link_cost * len(links)
    bal = (await one("SELECT balance FROM users WHERE id=?", (m.from_user.id,)))["balance"]
    await state.update_data(qty=qty, link_cost=link_cost, total=total)
    text = (f"🧾 <b>خلاصه سفارش</b>\n\n📌 تعداد پست: {len(links)}\n👁 سین هر پست: {qty:,}\n"
            f"💵 هزینه هر پست: {link_cost:,}\n💰 <b>جمع کل: {total:,} تومان</b>\n"
            f"👛 موجودی تو: {bal:,} تومان")
    if bal < total:
        await state.clear()
        return await m.answer(text + f"\n\n❌ موجودی کافی نیست. {total - bal:,} تومان دیگه لازمه، از «{BTN_CHARGE}» شارژ کن.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ تایید و ثبت", callback_data="ord_ok"),
        InlineKeyboardButton(text="❌ لغو", callback_data="ord_no")]])
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ord_no")
async def order_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("سفارش لغو شد.")
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
    done, errors = 0, []
    for link in d["links"]:
        ok, info = await place_order(uid, link, d["qty"], d["link_cost"])
        if ok:
            done += 1
        else:
            errors.append(info)
            await balance_change(uid, d["link_cost"], "refund-failed-order")
    text = f"✅ {done} سفارش ثبت شد."
    if errors:
        text += f"\n❌ {len(errors)} سفارش ثبت نشد و {len(errors) * d['link_cost']:,} تومان به کیف پولت برگشت."
        await notify_admins(bot, f"⚠️ خطا در ثبت سفارش (کاربر {uid}):\n{html.escape(errors[0][:300])}")
    await c.message.edit_text(text + "\nوضعیت رو از «📦 سفارش‌های من» ببین.")
    await c.answer()


@router.message(F.text == BTN_ORDERS)
async def my_orders(m: Message, state: FSMContext):
    await state.clear()
    for o in await many("SELECT * FROM orders WHERE user_id=? AND settled=0 ORDER BY id DESC LIMIT 10", (m.from_user.id,)):
        await sync_order(o)
    rows = await many("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (m.from_user.id,))
    if not rows:
        return await m.answer("هنوز سفارشی نداری.")
    lines = [f"#{o['id']} | {o['quantity']:,} سین | {STATUS_FA.get(o['status'], o['status'])}\n{o['link']}" for o in rows]
    await m.answer("📦 <b>۱۰ سفارش آخر:</b>\n\n" + "\n\n".join(lines), disable_web_page_preview=True)


# ───────────────────────── پنل ادمین ─────────────────────────
@router.message(Command("admin"), IS_ADMIN)
async def admin_panel(m: Message):
    u = await one("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users")
    o = await one("SELECT COUNT(*) n, COALESCE(SUM(cost),0) s FROM orders")
    p = await one("SELECT COUNT(*) n FROM topups WHERE status='claimed'")
    price = await get_setting("price_per_1000")
    await m.answer(
        f"📊 کاربران: {u['n']} | مجموع موجودی کیف‌پول‌ها: {u['b']:,}\n"
        f"🛒 سفارش‌ها: {o['n']} | فروش: {o['s']:,}\n"
        f"🕓 رسید در انتظار: {p['n']}\n💵 قیمت هر ۱۰۰۰: {int(price):,}\n\n"
        "<b>دستورات:</b>\n/pending رسیدهای در انتظار\n/add id مبلغ\n/sub id مبلغ\n/ban id\n/unban id\n"
        "/price مبلغ\n/card متن کارت\n/limits حداقل حداکثر\n/provider موجودی provider\n/broadcast متن")


@router.message(Command("pending"), IS_ADMIN)
async def pending(m: Message):
    rows = await many("SELECT t.*, u.username FROM topups t LEFT JOIN users u ON u.id=t.user_id WHERE t.status='claimed'")
    if not rows:
        return await m.answer("رسید در انتظاری نیست.")
    for t in rows:
        await m.answer(admin_topup_text(t, t["username"]), reply_markup=admin_topup_kb(t["id"]))


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


@router.message(Command("limits"), IS_ADMIN)
async def cmd_limits(m: Message, command: CommandObject):
    lo, hi = await _id_amount(command)
    if not lo or not hi or lo > hi:
        return await m.answer("فرمت: /limits 100 100000")
    await set_setting("min_qty", lo)
    await set_setting("max_qty", hi)
    await m.answer("✅ انجام شد.")


@router.message(Command("provider"), IS_ADMIN)
async def cmd_provider(m: Message):
    try:
        res = await provider(action="balance")
        await m.answer(f"💼 موجودی provider: {res.get('balance')} {res.get('currency', '')}")
    except Exception as e:
        await m.answer(f"خطا: {html.escape(str(e))}")


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
