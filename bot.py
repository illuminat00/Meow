import asyncio
import gzip
import html
import json
import logging
import math
import os
import random
import re
import time
import traceback
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent, InlineKeyboardButton,
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
PROVIDER_TYPE = os.getenv("PROVIDER_TYPE", "smm").lower()  # فقط برای سازگاری با تنظیمات قدیمی: smm یا actionseen


def _build_providers():
    """دو provider: as = اکشن‌سین (ارزون‌تر، امکانات بیشتر) و pt = پاورتل (فوق‌سریع، فرمت استاندارد SMM).
    تنظیم‌های جدید: AS_KEY / PT_KEY (و اختیاری AS_URL, PT_URL, PT_SERVICE_ID, PT_METHOD).
    اگه فقط تنظیم‌های قدیمی (PROVIDER_*) باشن، همون‌ها به provider مربوطه تعلق می‌گیرن."""
    legacy_as = PROVIDER_TYPE == "actionseen"
    return {
        "as": {"type": "actionseen",
               "url": os.getenv("AS_URL") or (PROVIDER_URL if legacy_as else "") or "https://actionseen.ir/api/",
               "key": os.getenv("AS_KEY") or (PROVIDER_KEY if legacy_as else "")},
        "pt": {"type": "smm",
               "url": os.getenv("PT_URL") or ("" if legacy_as else PROVIDER_URL) or "https://api.power-tel.ir/v2/",
               "key": os.getenv("PT_KEY") or ("" if legacy_as else PROVIDER_KEY),
               "service": os.getenv("PT_SERVICE_ID") or SERVICE_ID,
               "method": (os.getenv("PT_METHOD") or PROVIDER_METHOD).upper()},
    }


PROVIDERS = _build_providers()
ENABLED_PROVS = [p for p in ("pt", "as") if PROVIDERS[p]["key"]]
DEFAULT_PROV = "as" if PROVIDERS["as"]["key"] else "pt"
LEGACY_PROV = "as" if PROVIDER_TYPE == "actionseen" else "pt"  # سفارش‌های قدیمی (بدون ستون provider) مال این بودن
PROV_NAMES = {"as": "اکشن‌سین (اقتصادی)", "pt": "پاورتل (فوق‌سریع)"}
# سرویس‌های سین که مشتری انتخاب می‌کنه
SERVICES = {
    "fast": {"name": "⚡ فوق‌سریع", "provider": "pt", "desc": "معمولاً زیر ۲ دقیقه می‌رسه", "max": 45000},
    "eco": {"name": "🟢 اقتصادی", "provider": "as", "desc": "حدود ۱۵ دقیقه طول می‌کشه؛ ارزون‌تره"},
}
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
    "react_price_per_100": os.getenv("DEFAULT_REACT_PRICE", "4000"),
    "like_price_per_100": os.getenv("DEFAULT_LIKE_PRICE", "4000"),
    "maintenance": "0",
}

REACT_ENABLED = bool(PROVIDERS["as"]["key"])  # ریکشن، رأی، چند پست آخر، ارسال تدریجی و لغو فقط با API اکشن‌سین
REACT_MIN, REACT_MAX = 10, 10000
LIKE_MIN, LIKE_MAX = 10, 10000
EXTRAS = REACT_ENABLED  # قابلیت‌های اضافه‌ی API اکشن‌سین (ریکشن، رأی، چند پست آخر، ارسال تدریجی، لغو)
COINS_PER_REACTION = 50
IR_OFFSET = 12600  # UTC+3:30
# هزینه‌ی خرید از provider (تومان) برای گزارش سود؛ برای اکشن‌سین از روی سکه حساب می‌شه
COST_VIEW_PER_1000 = float(os.getenv("COST_VIEW_PER_1000", str(1000 * COIN_TOMAN) if PROVIDER_TYPE == "actionseen" else "0"))
# هزینه‌ی خرید هر ۱۰۰۰ سین (تومان) از هر provider برای گزارش سود
COST_PER_1000 = {"as": float(os.getenv("COST_ECO_PER_1000", str(1000 * COIN_TOMAN))),
                 "pt": float(os.getenv("COST_FAST_PER_1000", "850"))}
COST_REACT_PER_100 = float(os.getenv("COST_REACT_PER_100", str(100 * COINS_PER_REACTION * COIN_TOMAN)))

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
BTN_REACT = "👍 ثبت ریکشن"
BTN_LAST, BTN_LIKE = "📚 سین چند پست آخر", "🗳 رأی نظرسنجی"
BTN_AUTO = "⚡ سین خودکار"
AUTO_MENU = os.getenv("AUTO_MENU", "0") == "1"  # نمایش دکمه‌ی سین خودکار توی منو (وقتی آماده‌ی عرضه‌ست روشنش کن)
_menu_rows = [[KeyboardButton(text=BTN_ORDER)]]
if EXTRAS:
    _menu_rows = [[KeyboardButton(text=BTN_ORDER), KeyboardButton(text=BTN_LAST)],
                  [KeyboardButton(text=BTN_REACT), KeyboardButton(text=BTN_LIKE)]]
if AUTO_MENU:
    _menu_rows.append([KeyboardButton(text=BTN_AUTO)])
MENU = ReplyKeyboardMarkup(
    keyboard=_menu_rows + [[KeyboardButton(text=BTN_CHARGE), KeyboardButton(text=BTN_ACC)],
                           [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SUPPORT)],
                           [KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True,
    input_field_placeholder="از منوی پایین انتخاب کن 👇",
)

MENU_TEXTS = {BTN_AUTO, BTN_ORDER, BTN_LAST, BTN_REACT, BTN_LIKE, BTN_CHARGE, BTN_ACC, BTN_ORDERS, BTN_SUPPORT, BTN_HELP}

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

    @asynccontextmanager
    async def tx(self):
        """چند دستور SQL در یک تراکنش: یا همه اعمال می‌شن یا هیچ‌کدوم."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield Tx(conn)


class Tx:
    def __init__(self, conn):
        self.conn = conn

    async def execute(self, sql, args=()):
        status = await self.conn.execute(PG.q(sql), *args)
        try:
            return Result(int(status.split()[-1]))
        except (ValueError, IndexError):
            return Result(0)

    async def one(self, sql, args=()):
        return await self.conn.fetchrow(PG.q(sql), *args)


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, username TEXT, balance BIGINT DEFAULT 0, banned INTEGER DEFAULT 0, joined BIGINT)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS topups(id BIGSERIAL PRIMARY KEY, user_id BIGINT, base BIGINT, unique_amount BIGINT, credited BIGINT DEFAULT 0, status TEXT, created BIGINT, expires BIGINT)",
    "CREATE TABLE IF NOT EXISTS orders(id BIGSERIAL PRIMARY KEY, user_id BIGINT, link TEXT, quantity BIGINT, cost BIGINT, provider_order TEXT, status TEXT, settled INTEGER DEFAULT 0, created BIGINT)",
    "ALTER TABLE topups ADD COLUMN IF NOT EXISTS receipt_uid TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'view'",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS emoji TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS posts INTEGER DEFAULT 1",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS opt TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider TEXT",
    "CREATE TABLE IF NOT EXISTS auto_channels(id BIGSERIAL PRIMARY KEY, chat_id BIGINT UNIQUE, username TEXT, user_id BIGINT, qty BIGINT, service TEXT, daily_cap INTEGER, enabled INTEGER DEFAULT 1, paused_reason TEXT, created BIGINT)",
    "CREATE TABLE IF NOT EXISTS auto_posts(id BIGSERIAL PRIMARY KEY, chat_id BIGINT, post_id BIGINT, media_group_id TEXT, order_id BIGINT, status TEXT, created BIGINT, UNIQUE(chat_id, post_id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS auto_posts_group ON auto_posts(chat_id, media_group_id) WHERE media_group_id IS NOT NULL",
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


async def _ledger(tx, uid, delta, reason):
    await tx.execute("INSERT INTO ledger(user_id,amount,reason,ts) VALUES (?,?,?,?)", (uid, delta, reason, int(time.time())))


async def _credit(tx, uid, delta, reason):
    """تغییر موجودی + ثبت در دفتر تراکنش‌ها، داخل یک تراکنش بیرونی."""
    await tx.execute("UPDATE users SET balance=balance+? WHERE id=?", (delta, uid))
    await _ledger(tx, uid, delta, reason)


async def balance_change(uid, delta, reason):
    async with db.tx() as tx:
        await _credit(tx, uid, delta, reason)


async def try_spend(uid, amount, reason):
    async with db.tx() as tx:
        cur = await tx.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount))
        if cur.rowcount != 1:
            return False
        await _ledger(tx, uid, -amount, reason)
        return True


# جدول تبدیل رقم‌ها با کد یونیکد نوشته شده (نه خود نویسه‌ها)، تا با کپی‌پیست و ویرایشگر موبایل خراب نشه
_TO_EN_DIGITS = {**{0x6F0 + i: 48 + i for i in range(10)}, **{0x660 + i: 48 + i for i in range(10)}}
_TO_FA_DIGITS = {48 + i: chr(0x6F0 + i) for i in range(10)}


def to_int(s):
    s = (s or "").translate(_TO_EN_DIGITS)
    s = s.replace(",", "").replace("،", "").strip()
    return int(s) if s.isdigit() else None


# ───────────────────────── provider ─────────────────────────
class ProviderError(Exception):
    pass


async def _json(r):
    """پاسخ provider رو می‌خونه؛ اگه JSON نبود (مثلاً صفحه‌ی HTML خطای ۵۰۲) خطای واضح می‌ده."""
    text = await r.text()
    try:
        return json.loads(text)
    except ValueError:
        raise ProviderError(f"پاسخ نامعتبر از provider (HTTP {r.status}): {text[:80]!r}")


async def provider(**params):
    """کلاینت provider. خروجی همیشه JSON ـه. پارامتر prov مشخص می‌کنه کدوم provider (as یا pt).
    نوع actionseen: عمل‌ها به فرمت وبسرویس اکشن‌سین ترجمه می‌شن (لینک باید خام و بدون کدگذاری برود).
    نوع smm: فرمت استاندارد (action=add/status/balance).
    kind=view|reaction|like ، emoji و opts فقط برای اکشن‌سین‌ان."""
    prov = params.pop("prov", None) or DEFAULT_PROV
    cfg = PROVIDERS[prov]
    kind = params.pop("kind", "view") or "view"
    emoji = params.pop("emoji", None)
    opts = params.pop("opts", None) or {}
    if cfg["type"] == "actionseen":
        act = params["action"]
        svc = kind if kind in ("view", "reaction", "like") else "view"
        extra = "".join(f"&{k}={int(v)}" for k, v in opts.items())
        if act == "add" and svc == "reaction":
            q = f"action=reaction&link={params['link']}&quantity={params['quantity']}&text={quote(emoji or '👍')}{extra}"
        elif act == "add":
            q = f"action={svc}&link={params['link']}&quantity={params['quantity']}{extra}"
        elif act in ("status", "cancel"):
            q = f"action={svc if act == 'status' else 'cancel'}&order={params['order']}"
        else:
            q = f"action={act}"
        url = URL(f"{cfg['url'].rstrip('/')}/?key={cfg['key']}&{q}", encoded=True)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url) as r:
                return await _json(r)
    data = {"key": cfg["key"], **params}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        req = s.post(cfg["url"], data=data) if cfg.get("method") == "POST" else s.get(cfg["url"], params=data)
        async with req as r:
            return await _json(r)


async def notify_admins(bot, text):
    for a in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_message(a, text)


_last_err = {}
_low_sent = {}  # آخرین زمان ارسال هشدار موجودی کم برای هر provider


async def report_error(bot, where, exc):
    """خطا رو برای ادمین می‌فرسته (هر خطای تکراری حداکثر هر ۵ دقیقه یک بار)."""
    key = f"{where}:{type(exc).__name__}:{str(exc)[:60]}"
    now = time.time()
    if now - _last_err.get(key, 0) < 300:
        return
    _last_err[key] = now
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
    await notify_admins(bot, f"🚨 خطا در ربات ({where})\n<pre>{html.escape(tb)}</pre>")


async def cancel_and_refund(oid, reason):
    """سفارش «نامشخص» رو لغو می‌کنه و پولش رو برمی‌گردونه (اتمیک)."""
    async with db.tx() as tx:
        cur = await tx.execute("UPDATE orders SET status='Canceled', settled=1 WHERE id=? AND status='Unknown'", (oid,))
        if cur.rowcount != 1:
            return False
        o = await tx.one("SELECT user_id, cost FROM orders WHERE id=?", (oid,))
        await _credit(tx, o["user_id"], o["cost"], reason)
    return True


async def reserve_order(uid, link, qty, cost, kind, emoji, posts, opt, prov):
    """کسر پول و ثبت ردیف سفارش با وضعیت «Unknown» در یک تراکنش؛ اگه موجودی کافی نباشه None.
    اینجوری اگه ربات وسط کار ری‌استارت بشه، سفارش توی /unknown می‌مونه و پول بی‌اثر گم نمی‌شه."""
    reason = {"reaction": "order-reaction", "like": "order-like"}.get(kind, "order")
    async with db.tx() as tx:
        cur = await tx.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (cost, uid, cost))
        if cur.rowcount != 1:
            return None
        await _ledger(tx, uid, -cost, reason)
        row = await tx.one(
            "INSERT INTO orders(user_id,link,quantity,cost,provider_order,status,created,kind,emoji,posts,opt,provider) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (uid, link, qty, cost, "", "Unknown", int(time.time()), kind, emoji, posts, opt, prov))
        return row["id"]


async def place_order(oid, link, qty, kind="view", emoji=None, opts=None, prov=None):
    """سفارش رزروشده رو به provider می‌فرسته و ردیفش رو به‌روز می‌کنه.
    نتیجه: ("ok", شماره‌ی provider) | ("failed", متن خطا؛ پول برگشته) | ("unknown", شماره‌ی سفارش؛ پول برنگشته، ادمین بررسی کنه)."""
    prov = prov or DEFAULT_PROV
    res = None
    try:
        for e_try in (emoji_variants(emoji) if kind == "reaction" and emoji else [emoji]):
            res = await provider(action="add", prov=prov, service=PROVIDERS[prov].get("service", ""), link=link, quantity=qty,
                                 kind=kind, emoji=e_try, opts=opts)
            bad_emoji = isinstance(res, dict) and "Invalid reaction emoji" in str(res.get("error", ""))
            if not (kind == "reaction" and bad_emoji):
                emoji = e_try
                break
    except aiohttp.ClientConnectorError as e:  # اصلاً به provider وصل نشده → مطمئنیم ثبت نشده
        await cancel_and_refund(oid, "refund-failed-order")
        return "failed", str(e)
    except Exception:
        res = None  # جواب نامعتبر یا تایم‌اوت → معلوم نیست ثبت شده یا نه
    if isinstance(res, dict) and "order" in res:
        await db.execute("UPDATE orders SET provider_order=?, status='Pending', emoji=? WHERE id=?", (str(res["order"]), emoji, oid))
        return "ok", res["order"]
    if isinstance(res, dict) and res.get("error"):
        await cancel_and_refund(oid, "refund-failed-order")
        return "failed", str(res["error"])
    return "unknown", oid


def calc_refund(o, res, st):
    """مبلغ برگشتی (تومان). remains در اکشن‌سین سکه‌ست: هر سین ۱ سکه، هر ریکشن/رأی ۵۰ سکه."""
    total = max(1, o["quantity"] * (o.get("posts") or 1))
    try:
        remains = int(float(res.get("remains") or 0))
    except (TypeError, ValueError):
        remains = 0
    is_as = (o.get("provider") or LEGACY_PROV) == "as"
    if is_as and (o["kind"] or "view") in ("reaction", "like"):
        remains //= COINS_PER_REACTION
    if st == "Partial" or (st == "Canceled" and is_as and remains > 0):
        return o["cost"] * min(remains, total) // total
    return o["cost"]


async def sync_order(o):
    """وضعیت سفارش رو از provider می‌گیره؛ در صورت لغو/ناقص، پول رو برمی‌گردونه."""
    if not o["provider_order"]:
        return None
    try:
        res = await provider(action="status", order=o["provider_order"], kind=o["kind"] or "view",
                             prov=o.get("provider") or LEGACY_PROV)
    except Exception:
        return None
    st = res.get("status") if isinstance(res, dict) else None
    if not st:
        return None
    st = STATUS_ALIASES.get(str(st).lower(), st)
    refund, final = 0, True
    if st == "Completed":
        pass
    elif st in REFUND_STATUSES or st == "Partial":
        refund = calc_refund(o, res, st)
    else:
        final = False
    if final:
        async with db.tx() as tx:
            cur = await tx.execute("UPDATE orders SET status=?, settled=1 WHERE id=? AND settled=0", (st, o["id"]))
            if cur.rowcount != 1:
                return None
            if refund:
                await _credit(tx, o["user_id"], refund, f"refund-order-{o['id']}")
    else:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (st, o["id"]))
    return st, refund, final


async def notify_order_result(bot, o, r):
    """بعد از نهایی شدن سفارش، به مشتری خبر می‌ده. اگه کامل شده باشه یه متن و بعدش جدا یه 😜 می‌فرسته."""
    st, refund, _final = r
    if st == "Completed":
        brand = html.escape(await get_setting("brand"))
        text = (f"🎉 <b>سفارشت کامل شد!</b>\n\n"
                f"📦 سفارش #{o['id']} • {order_label(o)}\n"
                f"🔗 {o['link']}\n\n"
                f"مرسی که {brand} رو انتخاب کردی 💙\n"
                "اگه راضی بودی، سفارش بعدی رو هم همین‌جا ثبت کن؛ و اگه سوالی داشتی، پشتیبانی کنارته.")
        with suppress(Exception):
            await bot.send_message(o["user_id"], text, disable_web_page_preview=True)
            await bot.send_message(o["user_id"], "😜")
        return
    msg = f"📦 سفارش #{o['id']}: {STATUS_FA.get(st, st)}"
    if refund:
        msg += f"\n💰 {refund:,} تومان به کیف پولت برگشت."
    with suppress(Exception):
        await bot.send_message(o["user_id"], msg)


async def poll_orders(bot):
    while True:
        try:
            for o in await many("SELECT * FROM orders WHERE settled=0 AND provider_order<>'' LIMIT 200"):
                r = await sync_order(o)
                if r and r[2]:
                    await notify_order_result(bot, o, r)
            if LOW_PROVIDER_BALANCE:
                for p in ENABLED_PROVS:
                    with suppress(Exception):
                        res = await provider(action="balance", prov=p)
                        bal = float(res.get("balance", 0))
                        toman = bal * COIN_TOMAN if p == "as" else bal
                        if toman < LOW_PROVIDER_BALANCE and time.time() - _low_sent.get(p, 0) > 6 * 3600:
                            _low_sent[p] = time.time()
                            await notify_admins(bot, f"⚠️ موجودی {PROV_NAMES[p]} کمه: حدود {fmt(toman)} تومان")
        except Exception as e:
            logging.exception("poll_orders")
            await report_error(bot, "poll_orders", e)
        await asyncio.sleep(600)


# ───────────────────────── ابزارهای کمکی: تعمیر، تاریخ، گزارش، بکاپ ─────────────────────────
async def get_opt(key, default=""):
    r = await one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


async def is_maintenance():
    return (await get_opt("maintenance", "0")) == "1"


MAINT_TEXT = ("🛠 <b>سفارش‌گیری موقتاً متوقفه</b>\n\n"
              "داریم سرویس رو بروزرسانی می‌کنیم. شارژ کیف پول فعاله و خیلی زود برمی‌گردیم 🙏")


def g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jdate(ts, with_time=True):
    t = time.gmtime(ts + IR_OFFSET)
    jy, jm, jd = g2j(t.tm_year, t.tm_mon, t.tm_mday)
    return f"{jy}/{jm:02d}/{jd:02d}" + (f" {t.tm_hour:02d}:{t.tm_min:02d}" if with_time else "")


def ir_day(ts=None):
    return int(((time.time() if ts is None else ts) + IR_OFFSET) // 86400)


def day_range(day):
    start = day * 86400 - IR_OFFSET
    return start, start + 86400


def order_label(o):
    k = o["kind"] or "view"
    if k == "reaction":
        return f"{o['emoji'] or '👍'} {fmt(o['quantity'])} ریکشن"
    if k == "like":
        return f"🗳 {fmt(o['quantity'])} رأی"
    if (o.get("posts") or 1) > 1:
        return f"{fmt(o['quantity'])} سین × {o['posts']} پست"
    return f"{fmt(o['quantity'])} سین"


def ledger_label(reason):
    if reason.startswith("topup"):
        return "💰 شارژ"
    if reason.startswith("order"):
        return "🛒 خرید"
    if reason.startswith("refund"):
        return "↩️ برگشت پول"
    if reason.startswith("admin-add"):
        return "➕ شارژ توسط پشتیبانی"
    if reason.startswith("admin-sub"):
        return "➖ کسر توسط پشتیبانی"
    return reason


async def svc_enabled(key):
    return bool(PROVIDERS[SERVICES[key]["provider"]]["key"]) and (await get_opt(f"svc_{key}_on", "1")) == "1"


async def enabled_services():
    return [k for k in SERVICES if await svc_enabled(k)]


async def default_service():
    svcs = await enabled_services()
    return svcs[0] if svcs else "eco"


async def svc_price(key):
    v = await get_opt(f"price_{key}", "")
    return int(v) if v else int(await get_setting("price_per_1000"))


async def qty_limits(service):
    lo, hi = int(await get_setting("min_qty")), int(await get_setting("max_qty"))
    return lo, min(hi, SERVICES.get(service, {}).get("max", hi))


async def price_line():
    svcs = await enabled_services()
    if len(svcs) > 1:
        lines = []
        for k in svcs:
            lines.append(f"{SERVICES[k]['name']}: <b>{fmt(await svc_price(k))}</b> تومان")
        return "💵 قیمت هر ۱۰۰۰ سین:\n" + "\n".join(lines)
    return f"💵 قیمت هر ۱۰۰۰ سین: <b>{fmt(await svc_price(svcs[0] if svcs else 'eco'))}</b> تومان"


async def build_report(start, end, title):
    rows = await many(
        "SELECT COALESCE(kind,'view') AS kind, COALESCE(provider, ?) AS prov, COUNT(*) AS n, "
        "COALESCE(SUM(quantity*COALESCE(posts,1)),0) AS q, COALESCE(SUM(cost),0) AS c "
        "FROM orders WHERE created>=? AND created<? AND status NOT IN ('Canceled','Refunded','Fail','Failed') GROUP BY 1,2",
        (LEGACY_PROV, start, end))
    n_orders = sum(int(r["n"]) for r in rows)
    revenue = sum(int(r["c"]) for r in rows)
    views_by = {}
    for r in rows:
        if r["kind"] == "view":
            views_by[r["prov"]] = views_by.get(r["prov"], 0) + int(r["q"])
    views = sum(views_by.values())
    reacts = sum(int(r["q"]) for r in rows if r["kind"] != "view")
    cost = sum(v / 1000 * COST_PER_1000.get(p, 0) for p, v in views_by.items()) + reacts / 100 * COST_REACT_PER_100
    cost_known = True
    parts = [f"{SERVICES[k]['name']} {fmt(views_by[SERVICES[k]['provider']])}" for k in SERVICES if views_by.get(SERVICES[k]["provider"])]
    split = f" [{' • '.join(parts)}]" if len(parts) > 1 else ""
    top = await one("SELECT COALESCE(SUM(amount),0) AS s, COUNT(*) AS n FROM ledger WHERE reason LIKE 'topup-%' AND ts>=? AND ts<?", (start, end))
    ref = await one("SELECT COALESCE(SUM(amount),0) AS s FROM ledger WHERE reason LIKE 'refund%' AND ts>=? AND ts<?", (start, end))
    newu = await one("SELECT COUNT(*) AS n FROM users WHERE joined>=? AND joined<?", (start, end))
    autos = await one("SELECT COUNT(*) AS n FROM auto_posts WHERE status='ordered' AND created>=? AND created<?", (start, end))
    debt = await one("SELECT COALESCE(SUM(balance),0) AS s FROM users")
    pend = await one("SELECT COUNT(*) AS n FROM topups WHERE status='claimed'")
    unk = await one("SELECT COUNT(*) AS n FROM orders WHERE status='Unknown'")
    prov = await provider_balance_text()
    profit = (f"✅ سود تقریبی: <b>{fmt(revenue - cost)}</b> تومان" if cost_known
              else "✅ سود: نامشخص (هزینه‌ی provider ها رو تنظیم کن)")
    return (f"📊 <b>گزارش {title}</b> ({jdate(start, False)})\n\n"
            f"💰 شارژ کیف‌پول‌ها: <b>{fmt(top['s'])}</b> تومان ({top['n']} بار)\n"
            f"🛒 سفارش‌ها: {n_orders} (👁 {fmt(views)} سین{split} • 👍 {fmt(reacts)} ریکشن)\n"
            f"💵 فروش: <b>{fmt(revenue)}</b> تومان\n"
            f"🏭 هزینه‌ی تقریبی: {fmt(cost)} تومان\n"
            f"{profit}\n"
            f"↩️ برگشتی به کیف‌پول‌ها: {fmt(ref['s'])} تومان\n"
            f"👥 کاربر جدید: {newu['n']} | ⚡ سفارش خودکار: {autos['n']}\n\n"
            f"👛 مجموع موجودی کیف‌پول مشتری‌ها (بدهی تو): <b>{fmt(debt['s'])}</b> تومان\n"
            f"{prov}\n"
            f"⏳ رسید در انتظار: {pend['n']} | ⚠️ سفارش نامشخص: {unk['n']}")


async def make_backup():
    data = {}
    for t in ("users", "settings", "topups", "orders", "ledger", "auto_channels", "auto_posts"):
        data[t] = [dict(r) for r in await many(f"SELECT * FROM {t}")]
    raw = json.dumps({"version": 1, "created": int(time.time()), "tables": data}, ensure_ascii=False, default=str)
    return gzip.compress(raw.encode("utf-8"))


async def send_backup(bot, caption="💾 بکاپ دیتابیس"):
    blob = await make_backup()
    name = f"backup-{jdate(time.time(), False).replace('/', '-')}.json.gz"
    for a in ADMIN_IDS:
        with suppress(Exception):
            await bot.send_document(a, BufferedInputFile(blob, filename=name), caption=caption)


async def daily_jobs(bot):
    """گزارش روزانه (بعد از نیمه‌شب به وقت ایران) و بکاپ روزانه؛ با ری‌استارت دوبار ارسال نمی‌شن."""
    while True:
        try:
            today = ir_day()
            raw = await get_opt("last_report_day", "")
            if not raw:
                await set_setting("last_report_day", today - 1)
                raw = str(today - 1)
            if int(raw) < today - 1:
                start, end = day_range(today - 1)
                await notify_admins(bot, await build_report(start, end, "دیروز"))
                await set_setting("last_report_day", today - 1)
            if int(await get_opt("last_backup_day", "0") or 0) < today:
                await send_backup(bot, "💾 بکاپ روزانه‌ی دیتابیس")
                await set_setting("last_backup_day", today)
        except Exception as e:
            logging.exception("daily_jobs")
            await report_error(bot, "daily_jobs", e)
        await asyncio.sleep(600)


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


def fa(n):
    return str(n).translate(_TO_FA_DIGITS)


def cancel_row():
    return [InlineKeyboardButton(text="❌ انصراف", callback_data="flow_cancel")]


async def welcome_text(user):
    brand = html.escape(await get_setting("brand"))
    bal = (await one("SELECT balance FROM users WHERE id=?", (user.id,)))["balance"]
    what = "<b>سین (بازدید)</b>، <b>ریکشن</b> و <b>رأی نظرسنجی</b>" if EXTRAS else "<b>سین (بازدید)</b>"
    return (f"سلام {html.escape(user.first_name or 'دوست عزیز')} 👋\n"
            f"به <b>{brand}</b> خوش اومدی!\n\n"
            f"اینجا می‌تونی برای پست‌های کانال تلگرامت {what} سفارش بدی؛ خودکار و بدون منتظر موندن برای پشتیبان.\n\n"
            "🚀 <b>شروع سریع:</b>\n"
            "۱) از «💰 شارژ کیف پول» حسابت رو شارژ کن\n"
            "۲) «🛒 ثبت سفارش سین» رو بزن و لینک پست + تعداد رو بفرست\n"
            "۳) سفارش رو تایید کن؛ تمام!\n\n"
            f"{await price_line()}\n"
            f"👛 موجودی تو: <b>{fmt(bal)}</b> تومان\n\n"
            "سوالی داشتی؟ «📖 راهنما و قوانین» رو بزن.")


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
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="go_charge"),
        InlineKeyboardButton(text="🧾 تاریخچه", callback_data="history")]])
    await m.answer("👤 <b>حساب من</b>\n\n"
                   f"🆔 شناسه: <code>{u['id']}</code>\n"
                   f"💰 موجودی: <b>{fmt(u['balance'])}</b> تومان\n"
                   f"📦 تعداد سفارش‌ها: {n}", reply_markup=kb)


@router.callback_query(F.data == "history")
async def history(c: CallbackQuery):
    await c.answer()
    rows = await many("SELECT amount, reason, ts FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 15", (c.from_user.id,))
    if not rows:
        return await c.message.answer("هنوز تراکنشی نداری 🙂")
    lines = [f"{ledger_label(r['reason'])}  <b>{r['amount']:+,}</b> تومان\n🕓 {jdate(r['ts'])}" for r in rows]
    await c.message.answer("🧾 <b>تاریخچه‌ی تراکنش‌ها</b> (۱۵ مورد آخر)\n\n" + "\n\n".join(lines))


HELP_TEXT = (
    "📖 <b>راهنمای کامل</b>\n\n"
    "<b>سین</b> یعنی بازدید زیر پست کانال."
    + (" <b>ریکشن</b> ایموجی‌ایه که زیر پست می‌خوره (مثل 👍 ❤️ 🔥) و <b>رأی</b> یعنی انتخاب یه گزینه‌ی نظرسنجی." if EXTRAS else "")
    + "\n\n🚀 <b>چطور سفارش بدم؟</b>\n"
    "1️⃣ «💰 شارژ کیف پول» رو بزن، مبلغ رو انتخاب کن و کارت‌به‌کارت کن. بعدش «پرداخت کردم» رو بزن و عکس رسید رو بفرست. بعد از تایید ادمین، کیف پولت شارژ می‌شه.\n"
    "2️⃣ «🛒 ثبت سفارش سین» رو بزن، لینک پست و بعد تعداد رو بفرست.\n"
    "3️⃣ خلاصه‌ی سفارش رو ببین و «تایید» رو بزن.\n"
    "4️⃣ پیشرفت کار رو از «📦 سفارش‌های من» ببین؛ بعد از کامل شدن هم بهت پیام می‌دم.\n\n"
    "📜 <b>قوانین مهم</b>\n"
    "• کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه) و تا پایان سفارش خصوصی نشه.\n"
    "• تا کامل شدن سفارش، برای همون پست سفارش دوم ثبت نکن.\n"
    "• لینک رو دقیق بفرست؛ لینک اشتباه ممکنه باعث انجام نشدن سفارش بشه.\n"
    "• اگه سفارشی لغو بشه یا ناقص بمونه، هزینه‌ی بخش انجام‌نشده خودکار به کیف پولت برمی‌گرده.\n"
    "• برای شارژ، دقیقاً همون مبلغی که ربات نشون می‌ده رو واریز کن تا سریع‌تر تایید بشه.\n"
    "• زمان رسیدن سفارش بسته به شرایط تلگرام ممکنه کمی متفاوت باشه.\n\n"
    "💬 هر سوال دیگه‌ای داشتی، «💬 پشتیبانی» رو بزن."
    + ("\n\n⚡ <b>سین خودکار:</b> ربات رو ادمین کانالت کن تا برای هر پست جدید، خودکار سین ثبت بشه." if AUTO_MENU else "")
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


PRESETS = [10000, 20000, 50000, 100000]


async def topup_text(t):
    card = (await get_setting("card")).strip().split("\n", 1)
    card_html = f"<code>{html.escape(card[0])}</code>" + (f"\n{html.escape(card[1])}" if len(card) > 1 else "")
    left = max(0, (t["expires"] - int(time.time())) // 60)
    note = "" if CREDIT_FULL else (
        f"\n📌 رقم‌های آخرِ مبلغ فقط برای شناسایی پرداخته؛ <b>{fmt(t['base'])} تومان</b> به کیف پولت اضافه می‌شه.\n")
    return (
        "💳 <b>پرداخت کارت‌به‌کارت</b>\n\n"
        f"1️⃣ دقیقاً این مبلغ رو به کارت زیر واریز کن:\n💰 <code>{t['unique_amount']}</code> تومان\n\n"
        f"🏦 شماره کارت:\n{card_html}\n\n"
        "⚠️ مبلغ باید <b>دقیقاً همین عدد</b> باشه (حتی رقم‌های آخرش)؛ نه کمتر و نه بیشتر. "
        "این‌جوری پرداختت سریع‌تر شناسایی و تایید می‌شه.\n"
        f"{note}\n"
        "2️⃣ بعد از واریز، دکمه‌ی «✅ پرداخت کردم» رو بزن و عکس رسید رو بفرست.\n"
        f"⏳ مهلت پرداخت: {left} دقیقه"
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
        return await msg.answer("⏳ رسید قبلی‌ت در انتظار تایید ادمینه.\nبعد از تایید می‌تونی دوباره شارژ کنی.")
    if t["status"] == "awaiting":
        return await msg.answer("🧾 منتظر رسید پرداختت هستم.\nعکس رسید (یا شماره‌ی پیگیری) رو همین‌جا بفرست.")
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
                     "برای سفارش دادن، اول باید کیف پولت پول داشته باشه.\n\n"
                     "👇 یکی از مبلغ‌ها رو بزن، یا مبلغ دلخواهت رو (به تومان، فقط عدد) تایپ کن و بفرست.\n"
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
        return await m.answer("مبلغ معتبر نیست 🤔\n"
                              "فقط عدد (به تومان) بفرست؛ مثلاً: <code>50000</code>\n"
                              f"📌 حداقل شارژ: <b>{fmt(MIN_TOPUP)}</b> تومان",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await create_topup(m, state, m.from_user.id, base)


@router.callback_query(F.data.startswith("cancel:"))
async def topup_cancel(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    cur = await db.execute("UPDATE topups SET status='canceled' WHERE id=? AND user_id=? AND status IN ('pending','awaiting')",
                           (tid, c.from_user.id))
    await db.commit()
    await c.message.edit_text("درخواست شارژ لغو شد.\nاگه قبلاً واریز کردی، رسیدت رو برای پشتیبانی بفرست. هر وقت خواستی دوباره از «💰 شارژ کیف پول» شروع کن."
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
    credit = t["unique_amount"] if CREDIT_FULL else t["base"]
    await c.message.edit_text(
        "🧾 <b>مرحله‌ی آخر: رسید رو بفرست</b>\n\n"
        "عکس رسید (یا شماره‌ی پیگیری) رو همین‌جا بفرست تا برای تایید به ادمین برسه.\n\n"
        f"✅ بعد از تایید، <b>{fmt(credit)} تومان</b> به کیف پولت اضافه می‌شه و بهت پیام می‌دم.\n"
        f"💰 مبلغ واریزی تو: <code>{t['unique_amount']}</code> تومان")
    await c.answer()


async def approve_topup(tid, amount=None):
    async with db.tx() as tx:
        cur = await tx.execute("UPDATE topups SET status='approved' WHERE id=? AND status IN ('pending','awaiting','claimed')", (tid,))
        if cur.rowcount != 1:
            return None
        t = await tx.one("SELECT * FROM topups WHERE id=?", (tid,))
        credit = amount or (t["unique_amount"] if CREDIT_FULL else t["base"])
        await tx.execute("UPDATE topups SET credited=? WHERE id=?", (credit, tid))
        await _credit(tx, t["user_id"], credit, f"topup-{tid}")
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
    service = State()
    links = State()
    qty = State()


QTY_PRESETS = [1000, 5000, 10000, 20000]


@router.message(F.text == BTN_ORDER)
async def order_start(m: Message, state: FSMContext):
    await state.clear()
    if await is_maintenance() and m.from_user.id not in ADMIN_IDS:
        return await m.answer(MAINT_TEXT)
    svcs = await enabled_services()
    if not svcs:
        return await m.answer("🛠 سرویس سین موقتاً در دسترس نیست. کمی بعد دوباره امتحان کن 🙏")
    if len(svcs) == 1:
        return await begin_links(m, state, svcs[0])
    await state.set_state(Order.service)
    lines, rows = [], []
    for k in svcs:
        sv, price = SERVICES[k], await svc_price(k)
        lines.append(f"{sv['name']} — <b>{fmt(price)}</b> تومان برای هر ۱۰۰۰ سین\n{sv['desc']}")
        rows.append([InlineKeyboardButton(text=f"{sv['name']} • {fmt(price)} تومان", callback_data=f"svc:{k}")])
    rows.append(cancel_row())
    await m.answer("🛒 <b>ثبت سفارش سین (بازدید)</b>\n\n"
                   "<b>مرحله ۱ از ۳:</b> سرعت ارسال رو انتخاب کن 👇\n\n" + "\n\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def begin_links(msg: Message, state: FSMContext, key: str):
    multi = len(await enabled_services()) > 1
    steps, cur = (3, 2) if multi else (2, 1)
    price = await svc_price(key)
    await state.set_state(Order.links)
    await state.update_data(service=key, multi=multi)
    await msg.answer("🛒 <b>ثبت سفارش سین (بازدید)</b>\n\n"
                     + (f"⚙️ سرویس: {SERVICES[key]['name']}\n" if multi else "")
                     + f"💵 قیمت: هر ۱۰۰۰ سین = <b>{fmt(price)}</b> تومان\n\n"
                     f"🔗 <b>مرحله {fa(cur)} از {fa(steps)}:</b> لینک پستی که می‌خوای سین بخوره رو بفرست.\n"
                     f"• اگه چند پست داری، هر لینک رو توی یه خط بنویس (حداکثر {MAX_LINKS} تا).\n"
                     "• مثال: <code>https://t.me/channel/123</code>\n\n"
                     "⚠️ کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه).",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.callback_query(F.data.startswith("svc:"), Order.service)
async def order_service(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in SERVICES or not await svc_enabled(key):
        return await c.answer("این سرویس فعلاً فعال نیست.", show_alert=True)
    await c.answer()
    await begin_links(c.message, state, key)


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


async def validate_links(m: Message, bot: Bot, kind: str):
    """(لینک‌های معتبر، یادداشت) یا None (در این حالت خودش پیام خطا داده)."""
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row()])
    links = extract_links(m.text)
    if not links:
        if "t.me/c/" in m.text:
            await m.answer("این لینک مربوط به کانال <b>خصوصی</b> 🔒 هست.\n"
                           "این سرویس فقط برای پست‌های کانال‌های <b>عمومی</b> ثبت می‌شه.", reply_markup=cancel_kb)
        else:
            await m.answer("لینکی پیدا نکردم 🤔\nلینک باید شبیه این باشه:\n<code>https://t.me/channel/123</code>",
                           reply_markup=cancel_kb)
        return None
    results = await asyncio.gather(*(check_link(bot, l) for l in links))
    bad = [(l, r) for l, r in zip(links, results) if r]
    if bad:
        txt = "❌ این لینک‌ها مشکل دارن:\n\n" + "\n".join(f"{l}\n↳ {r}" for l, r in bad) + "\n\nلینک‌های درست رو دوباره بفرست."
        await m.answer(txt, reply_markup=cancel_kb, disable_web_page_preview=True)
        return None
    note = ""
    if kind == "view":
        busy = [r["link"] for r in await many(
            "SELECT DISTINCT link FROM orders WHERE settled=0 AND COALESCE(kind,'view')='view' AND link = ANY(?)", (links,))]
        if busy:
            links = [l for l in links if l not in busy]
            note = "⚠️ برای این پست‌ها هنوز یه سفارش در حال انجامه، پس حذف شدن:\n" + "\n".join(busy) + "\n\n"
            if not links:
                await m.answer(note + "بعد از تکمیل سفارش قبلی دوباره امتحان کن، یا لینک دیگه‌ای بفرست.",
                               reply_markup=cancel_kb, disable_web_page_preview=True)
                return None
    return links, note


@router.message(Order.links, F.text)
async def order_links(m: Message, state: FSMContext, bot: Bot):
    r = await validate_links(m, bot, "view")
    if not r:
        return
    links, note = r
    d = await state.get_data()
    service = d.get("service") or await default_service()
    steps = 3 if d.get("multi") else 2
    lo, hi = await qty_limits(service)
    price = await svc_price(service)
    await state.update_data(links=links, service=service)
    await state.set_state(Order.qty)
    await m.answer(note + f"✅ {len(links)} پست دریافت شد.\n\n"
                   f"👁 <b>مرحله {fa(steps)} از {fa(steps)}:</b> تعداد سین (بازدید) برای <b>هر پست</b> رو به‌صورت عدد بنویس و بفرست.\n"
                   "مثال: <code>1000</code>\n\n"
                   f"📌 حداقل {fmt(lo)} و حداکثر {fmt(hi)}\n"
                   f"💡 هر ۱۰۰۰ سین = {fmt(price)} تومان",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]), disable_web_page_preview=True)


# ───────────────────────── خلاصه‌ی سفارش، سرعت و ارسال تدریجی ─────────────────────────
DRIP_CHOICES = [(0, "⚡ سریع (پیش‌فرض)"), (180, "🐢 تدریجی: طی حدود ۳ ساعت"), (360, "🐢 تدریجی: طی حدود ۶ ساعت"),
                (720, "🐢 تدریجی: طی حدود ۱۲ ساعت"), (1440, "🐢 تدریجی: طی حدود ۲۴ ساعت")]
SPEED_CHOICES = [(1, "⚡ سریع (پیش‌فرض)"), (5, "🐢 آرام: هر ۵ دقیقه یک بخش"), (15, "🐢 آرام: هر ۱۵ دقیقه یک بخش"),
                 (30, "🐢 آرام: هر ۳۰ دقیقه یک بخش")]


def drip_params(qty, minutes):
    """«طی N دقیقه» رو به interval/vpi اکشن‌سین تبدیل می‌کنه (vpi حداقل ۱۰ و interval بین ۱ تا ۱۴۴۰ دقیقه)."""
    if not minutes:
        return {}
    interval = 15 if minutes >= 90 else 5
    steps = max(1, minutes // interval)
    return {"interval": interval, "vpi": max(10, math.ceil(qty / steps))}


def drip_actual_minutes(qty, minutes):
    p = drip_params(qty, minutes)
    return math.ceil(qty / p["vpi"]) * p["interval"] if p else 0


def fmt_minutes(m):
    return f"حدود {m} دقیقه" if m < 90 else f"حدود {round(m / 60)} ساعت"


def view_prov(d):
    sv = SERVICES.get(d.get("service"))
    return sv["provider"] if sv else DEFAULT_PROV


def build_opts(d, kind):
    if not EXTRAS or (kind == "view" and view_prov(d) != "as"):
        return {}
    o = {}
    if kind == "view":
        if d.get("posts", 1) > 1:
            o["lastxpost"] = d["posts"]
        o.update(drip_params(d["qty"], d.get("drip", 0)))
    else:
        if d.get("speed", 1) != 1:
            o["speed"] = d["speed"]
        if kind == "like":
            o["row"], o["column"] = d["row"], d["col"]
    return o


async def show_summary(msg: Message, state: FSMContext, uid: int, edit=False):
    d = await state.get_data()
    kind, links, posts = d.get("kind", "view"), d.get("links") or [], d.get("posts", 1)
    qty, link_cost, total = d["qty"], d["link_cost"], d["total"]
    bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
    if kind == "reaction":
        head = "🧾 <b>خلاصه‌ی سفارش ریکشن</b>\n\n"
        lines = [f"📌 تعداد پست: {len(links)}", f"{d['emoji']} تعداد ریکشن هر پست: {fmt(qty)}"]
    elif kind == "like":
        head = "🧾 <b>خلاصه‌ی سفارش رأی</b>\n\n"
        lines = [f"📌 پست: {links[0]}", f"🗳 گزینه: ردیف {d['row']}، ستون {d['col']}", f"👍 تعداد رأی: {fmt(qty)}"]
    elif posts > 1:
        head = "🧾 <b>خلاصه‌ی سفارش سین</b>\n\n"
        lines = [f"📚 {posts} پست آخر کانال: {links[0]}", f"👁 سین هر پست: {fmt(qty)}"]
    else:
        head = "🧾 <b>خلاصه‌ی سفارش سین</b>\n\n"
        lines = [f"📌 تعداد پست: {len(links)}", f"👁 سین هر پست: {fmt(qty)}"]
    lines.append(f"💵 هزینه‌ی هر پست: {fmt(link_cost // posts)} تومان" if kind != "like" else f"💵 هزینه: {fmt(link_cost)} تومان")
    can_speed = EXTRAS and (kind != "view" or view_prov(d) == "as")
    if kind == "view" and d.get("service") in SERVICES and len(await enabled_services()) > 1:
        lines.insert(0, f"⚙️ سرویس: {SERVICES[d['service']]['name']}")
    if can_speed:
        if kind == "view":
            dm = d.get("drip", 0)
            lines.append("⏱ سرعت ارسال: حداکثر (پیش‌فرض)" if not dm
                         else f"⏱ سرعت ارسال: تدریجی، {fmt_minutes(drip_actual_minutes(qty, dm))}")
        else:
            sp = d.get("speed", 1)
            lines.append("⏱ سرعت ارسال: حداکثر (پیش‌فرض)" if sp == 1 else f"⏱ سرعت ارسال: آرام، هر {sp} دقیقه یک بخش")
    text = (head + "\n".join(lines) +
            f"\n\n💰 <b>مبلغ قابل پرداخت: {fmt(total)} تومان</b>\n👛 موجودی کیف پولت: {fmt(bal)} تومان")
    if bal < total:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="go_charge")]])
        return await msg.answer(text + f"\n\n❌ موجودی کافی نیست. برای این سفارش <b>{fmt(total - bal)}</b> تومان دیگه لازمه.",
                                reply_markup=kb, disable_web_page_preview=True)
    text += (f"\n👛 موجودی بعد از سفارش: {fmt(bal - total)} تومان\n\n"
             "با زدن «تایید»، مبلغ از کیف پولت کم می‌شه و سفارش ثبت می‌شه 👇")
    rows = [[InlineKeyboardButton(text="✅ تایید و ثبت سفارش", callback_data="ord_ok"),
             InlineKeyboardButton(text="❌ انصراف", callback_data="ord_no")]]
    if can_speed:
        rows.append([InlineKeyboardButton(text="⏱ تغییر سرعت ارسال (اختیاری)", callback_data="spd_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        with suppress(Exception):
            await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "spd_menu")
async def speed_menu(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "qty" not in d:
        return await c.answer("این سفارش منقضی شده؛ دوباره ثبت کن.", show_alert=True)
    if d.get("kind", "view") == "view":
        body = ("⏱ <b>سرعت ارسال</b>\n\n"
                "پیش‌فرض، سین‌ها <b>با حداکثر سرعت</b> می‌رسن و معمولاً نیازی به تغییر نیست.\n\n"
                "اگه می‌خوای طبیعی‌تر دیده بشه، ارسال رو <b>تدریجی</b> کن؛ یعنی سین‌ها طی چند ساعت پخش می‌شن.\n\n"
                "یکی رو انتخاب کن 👇")
        cur, items = d.get("drip", 0), [(f"drip:{m}", t, m) for m, t in DRIP_CHOICES]
    else:
        body = ("⏱ <b>سرعت ارسال</b>\n\n"
                "پیش‌فرض، سفارش <b>با حداکثر سرعت</b> ثبت می‌شه و معمولاً نیازی به تغییر نیست.\n\n"
                "اگه می‌خوای طبیعی‌تر دیده بشه، «آرام» رو انتخاب کن؛ یعنی هر چند دقیقه فقط یه بخش از سفارش ارسال می‌شه.\n\n"
                "یکی رو انتخاب کن 👇")
        cur, items = d.get("speed", 1), [(f"spd:{n}", t, n) for n, t in SPEED_CHOICES]
    rows = [[InlineKeyboardButton(text=("✅ " if v == cur else "") + t, callback_data=cb)] for cb, t, v in items]
    rows.append([InlineKeyboardButton(text="↩️ بازگشت به سفارش", callback_data="spd_back")])
    await c.answer()
    with suppress(Exception):
        await c.message.edit_text(body, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "spd_back")
async def speed_back(c: CallbackQuery, state: FSMContext):
    if "qty" not in await state.get_data():
        return await c.answer("این سفارش منقضی شده؛ دوباره ثبت کن.", show_alert=True)
    await c.answer()
    await show_summary(c.message, state, c.from_user.id, edit=True)


@router.callback_query(F.data.startswith("drip:"))
async def set_drip(c: CallbackQuery, state: FSMContext):
    if "qty" not in await state.get_data():
        return await c.answer("این سفارش منقضی شده؛ دوباره ثبت کن.", show_alert=True)
    await state.update_data(drip=int(c.data.split(":")[1]))
    await c.answer("ثبت شد ✅")
    await show_summary(c.message, state, c.from_user.id, edit=True)


@router.callback_query(F.data.startswith("spd:"))
async def set_speed(c: CallbackQuery, state: FSMContext):
    if "qty" not in await state.get_data():
        return await c.answer("این سفارش منقضی شده؛ دوباره ثبت کن.", show_alert=True)
    await state.update_data(speed=int(c.data.split(":")[1]))
    await c.answer("ثبت شد ✅")
    await show_summary(c.message, state, c.from_user.id, edit=True)


async def process_qty(msg: Message, state: FSMContext, uid: int, qty):
    d = await state.get_data()
    service = d.get("service") or await default_service()
    lo, hi = await qty_limits(service)
    if qty is None or not lo <= qty <= hi:
        return await msg.answer(f"تعداد باید یه <b>عدد</b> بین <b>{fmt(lo)}</b> و <b>{fmt(hi)}</b> باشه 🙏\nدوباره بنویس و بفرست:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    links, posts = d.get("links"), d.get("posts", 1)
    if not links:
        await state.clear()
        return await msg.answer("این سفارش منقضی شده؛ دوباره از «🛒 ثبت سفارش سین» شروع کن.")
    link_cost = math.ceil(qty * await svc_price(service) / 1000) * posts
    await state.update_data(qty=qty, link_cost=link_cost, total=link_cost * len(links), kind="view", posts=posts, service=service)
    await show_summary(msg, state, uid)


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


_locks = {}  # قفل هر کاربر؛ جلوی دوبار زدنِ سریعِ «تایید» رو می‌گیره


@router.callback_query(F.data == "ord_ok")
async def order_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    async with _locks.setdefault(c.from_user.id, asyncio.Lock()):
        await _order_confirm(c, state, bot)


async def _order_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if "links" not in d or "total" not in d:
        return await c.answer("این سفارش منقضی شده، دوباره ثبت کن.", show_alert=True)
    uid = c.from_user.id
    if await is_maintenance() and uid not in ADMIN_IDS:
        await c.message.edit_text(MAINT_TEXT)
        return await c.answer()
    kind, emoji, posts = d.get("kind", "view"), d.get("emoji"), d.get("posts", 1)
    unit = {"reaction": "ریکشن", "like": "رأی"}.get(kind, "سین")
    opts = build_opts(d, kind)
    prov = view_prov(d) if kind == "view" else "as"
    opt = f"{d['row']},{d['col']}" if kind == "like" else None
    bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
    if bal < d["total"]:
        await c.message.edit_text("❌ موجودی کافی نیست.")
        return await c.answer()
    await c.message.edit_text("⏳ در حال ثبت سفارش...")
    done, errors, unknown, nofunds = 0, [], [], 0
    for i, link in enumerate(d["links"]):
        if i:
            await asyncio.sleep(0.5)
        oid = await reserve_order(uid, link, d["qty"], d["link_cost"], kind, emoji, posts, opt, prov)
        if oid is None:
            nofunds += 1
            continue
        st, info = await place_order(oid, link, d["qty"], kind, emoji, opts, prov)
        if st == "ok":
            done += 1
        elif st == "unknown":
            unknown.append(info)
            await notify_admins(bot, f"🚨 <b>سفارش نامشخص #{info}</b> (کاربر <code>{uid}</code>)\n{link} | {fmt(d['qty'])} {unit}\n"
                                     "جواب provider نامعتبر بود یا دیر رسید؛ ممکنه ثبت شده باشه.\n"
                                     f"اگه توی پنل provider ثبت شده: /resolve {info} شماره_سفارش_provider\n"
                                     f"اگه ثبت نشده: /refund {info}")
        else:
            errors.append(info)  # پول داخل place_order برگشته
    parts = []
    if done:
        parts.append(f"✅ <b>{done} سفارش با موفقیت ثبت شد!</b>\n"
                     f"🚀 {unit}‌ها به‌تدریج ارسال می‌شن. پیشرفت رو از «📦 سفارش‌های من» ببین.")
    if unknown:
        parts.append(f"⏳ <b>{len(unknown)} سفارش در حال بررسیه.</b>\n"
                     "پاسخ سرویس‌دهنده دیر رسید. نتیجه رو بهت خبر می‌دم؛ اگه ثبت نشده باشه، پولش به کیف پولت برمی‌گرده.")
    if errors:
        parts.append(f"⚠️ {len(errors)} سفارش ثبت نشد و {fmt(len(errors) * d['link_cost'])} تومان به کیف پولت برگشت. "
                     "لطفاً کمی بعد دوباره تلاش کن یا به پشتیبانی پیام بده.")
        await notify_admins(bot, f"⚠️ خطا در ثبت سفارش (کاربر {uid}):\n{html.escape(errors[0][:300])}")
    if nofunds:
        parts.append(f"⚠️ {nofunds} سفارش به‌خاطر کافی نبودن موجودی ثبت نشد.")
    text = "\n\n".join(parts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📦 سفارش‌های من", callback_data="my_orders")]])
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


# ───────────────────────── ریکشن ─────────────────────────
class React(StatesGroup):
    links = State()
    emoji = State()
    qty = State()


# همه‌ی ۷۳ ریکشن مجاز؛ پرکاربردها اول. ایموجی‌های ترکیبی با کد یونیکد نوشته شدن تا هیچ نویسه‌ی نامرئی‌ای گم نشه.
EMOJIS = [
    "👍", "\u2764\ufe0f", "🔥", "🎉", "😁", "🤩", "👏", "😍", "🙏", "🥰", "💯", "🤣", "\u26a1\ufe0f", "🏆", "👌", "🤝",
    "👎", "🤔", "🤯", "😱", "🤬", "😢", "🤮", "💩", "\U0001F54A", "🤡", "🥱", "🥴", "🐳", "\u2764\ufe0f\u200d\U0001F525",
    "🌚", "🌭", "🍌", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "\U0001F468\u200d\U0001F4BB",
    "👀", "🎃", "🙈", "😇", "😨", "\u270d\ufe0f", "🤗", "🫡", "🎅", "🎄", "\u2603\ufe0f", "💅", "🤪", "🗿", "🆒", "💘",
    "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "\U0001F937\u200d\u2642\ufe0f", "\U0001F937", "\U0001F937\u200d\u2640\ufe0f", "😡",
]


def emoji_variants(e):
    """بعضی ایموجی‌ها با/بدون کاراکتر نامرئی FE0F نوشته می‌شن؛ اگه provider یکی رو رد کرد، حالت دیگه رو امتحان می‌کنیم."""
    out = [e]
    stripped = e.replace("\ufe0f", "")
    if stripped != e:
        out.append(stripped)
    elif len(e) == 1:
        out.append(e + "\ufe0f")
    return out


REACT_QTY_PRESETS = [10, 50, 100, 500]


async def react_price():
    return int(await get_setting("react_price_per_100"))


@router.message(F.text == BTN_REACT)
async def react_start(m: Message, state: FSMContext):
    await state.clear()
    if not REACT_ENABLED:
        return await m.answer("این سرویس فعلاً فعال نیست.")
    if await is_maintenance() and m.from_user.id not in ADMIN_IDS:
        return await m.answer(MAINT_TEXT)
    price = await react_price()
    await state.set_state(React.links)
    await m.answer("👍 <b>ثبت ریکشن</b>\n\n"
                   "ریکشن یعنی ایموجی‌ای که زیر پست کانال می‌خوره (مثل 👍 یا ❤️ یا 🔥).\n\n"
                   f"💵 قیمت: هر ۱۰۰ ریکشن = <b>{fmt(price)}</b> تومان\n\n"
                   "🔗 <b>مرحله ۱ از ۳:</b> لینک پست(ها) رو بفرست.\n"
                   f"• اگه چند پست داری، هر لینک رو توی یه خط بنویس (حداکثر {MAX_LINKS} تا).\n"
                   "• مثال: <code>https://t.me/channel/123</code>\n\n"
                   "⚠️ کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه).",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(React.links, F.text)
async def react_links(m: Message, state: FSMContext, bot: Bot):
    r = await validate_links(m, bot, "reaction")
    if not r:
        return
    links, note = r
    await state.update_data(links=links)
    await state.set_state(React.emoji)
    rows = [[InlineKeyboardButton(text=e, callback_data=f"emo:{i}") for i, e in enumerate(EMOJIS) if k <= i < k + 6]
            for k in range(0, len(EMOJIS), 6)]
    rows.append(cancel_row())
    await m.answer(note + f"✅ {len(links)} پست دریافت شد.\n\n"
                   "😀 <b>مرحله ۲ از ۳:</b> کدوم ریکشن زده بشه؟ یکی از ایموجی‌ها رو بزن 👇",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("emo:"), React.emoji)
async def react_emoji(c: CallbackQuery, state: FSMContext):
    await c.answer()
    i = int(c.data.split(":")[1])
    if not 0 <= i < len(EMOJIS):
        return
    emoji = EMOJIS[i]
    links = (await state.get_data()).get("links") or []
    busy = [r["link"] for r in await many(
        "SELECT DISTINCT link FROM orders WHERE settled=0 AND kind='reaction' AND emoji=? AND link = ANY(?)", (emoji, links))]
    note = ""
    if busy:
        links = [l for l in links if l not in busy]
        note = f"⚠️ برای این پست‌ها هنوز یه سفارش {emoji} در حال انجامه، پس حذف شدن:\n" + "\n".join(busy) + "\n\n"
        if not links:
            return await c.message.answer(note + "یه ریکشن دیگه انتخاب کن یا لینک جدید بفرست.", disable_web_page_preview=True)
    await state.update_data(links=links, emoji=emoji)
    await state.set_state(React.qty)
    price = await react_price()
    await c.message.answer(note + f"{emoji} انتخاب شد.\n\n"
                           "🔢 <b>مرحله ۳ از ۳:</b> تعداد ریکشن برای <b>هر پست</b> رو به‌صورت عدد بنویس و بفرست.\n"
                           "مثال: <code>50</code>\n\n"
                           f"📌 حداقل {fmt(REACT_MIN)} و حداکثر {fmt(REACT_MAX)}\n"
                           f"💡 هر ۱۰۰ ریکشن = {fmt(price)} تومان",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]), disable_web_page_preview=True)


async def process_react_qty(msg: Message, state: FSMContext, uid: int, qty):
    if qty is None or not REACT_MIN <= qty <= REACT_MAX:
        return await msg.answer(f"تعداد باید یه <b>عدد</b> بین <b>{fmt(REACT_MIN)}</b> و <b>{fmt(REACT_MAX)}</b> باشه 🙏\nدوباره بنویس و بفرست:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    d = await state.get_data()
    links, emoji = d.get("links"), d.get("emoji")
    if not links or not emoji:
        await state.clear()
        return await msg.answer("این سفارش منقضی شده؛ دوباره از «👍 ثبت ریکشن» شروع کن.")
    link_cost = math.ceil(qty * await react_price() / 100)
    await state.update_data(qty=qty, link_cost=link_cost, total=link_cost * len(links), kind="reaction", posts=1)
    await show_summary(msg, state, uid)


@router.message(React.qty, F.text)
async def react_qty(m: Message, state: FSMContext):
    await process_react_qty(m, state, m.from_user.id, to_int(m.text))


@router.callback_query(F.data.startswith("rqty:"), React.qty)
async def react_qty_preset(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await process_react_qty(c.message, state, c.from_user.id, int(c.data.split(":")[1]))


# ───────────────────────── سین برای چند پست آخر کانال ─────────────────────────
CH_RE = re.compile(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})")
LASTX_PRESETS = [3, 5, 10, 20, 50, 100]


class LastX(StatesGroup):
    link = State()
    count = State()
    qty = State()


async def check_channel(bot, ch):
    try:
        chat = await bot.get_chat(f"@{ch}")
    except TelegramBadRequest:
        return "کانال پیدا نشد یا عمومی نیست"
    except Exception:
        return None
    return None if chat.type == "channel" else "این لینک مربوط به یه کانال نیست"


def qty_keyboard(lo, hi, prefix, presets):
    pres = [q for q in presets if lo <= q <= hi]
    rows = [[InlineKeyboardButton(text=fmt(q), callback_data=f"{prefix}:{q}") for q in pres[i:i + 2]] for i in range(0, len(pres), 2)]
    rows.append(cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == BTN_LAST)
async def last_start(m: Message, state: FSMContext):
    await state.clear()
    if not EXTRAS:
        return await m.answer("این سرویس فعلاً فعال نیست.")
    if await is_maintenance() and m.from_user.id not in ADMIN_IDS:
        return await m.answer(MAINT_TEXT)
    price = await svc_price("eco")
    await state.set_state(LastX.link)
    await m.answer("📚 <b>سین برای چند پست آخر کانال</b>\n\n"
                   "وقتی می‌خوای روی چند تا از آخرین پست‌های کانالت <b>یکجا</b> سین بخوره، از این بخش استفاده کن. "
                   "لازم نیست لینک تک‌تک پست‌ها رو بفرستی.\n\n"
                   f"💵 قیمت: هر ۱۰۰۰ سین = <b>{fmt(price)}</b> تومان\n"
                   "💰 هزینه = تعداد پست × سین هر پست\n\n"
                   "🔗 <b>مرحله ۱ از ۳:</b> لینک کانالت رو بفرست.\n"
                   "مثال: <code>https://t.me/channel</code>\n\n"
                   "⚠️ کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه).",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(LastX.link, F.text)
async def last_link(m: Message, state: FSMContext, bot: Bot):
    kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row()])
    mm = CH_RE.search(m.text or "")
    if not mm:
        return await m.answer("لینک کانال معتبر نیست 🤔\nمثل این بفرست:\n<code>https://t.me/channel</code>", reply_markup=kb)
    err = await check_channel(bot, mm.group(1))
    if err:
        return await m.answer(f"❌ {err}\nلینک یه کانال عمومی دیگه بفرست.", reply_markup=kb)
    await state.update_data(links=[f"https://t.me/{mm.group(1)}"], service="eco")
    await state.set_state(LastX.count)
    await m.answer("✅ کانال تایید شد.\n\n"
                   "🔢 <b>مرحله ۲ از ۳:</b> سین روی <b>چند پست آخر</b> کانال ثبت بشه؟\n"
                   "یه عدد بین ۱ تا ۱۰۰ بنویس و بفرست، یا یکی از دکمه‌ها رو بزن 👇",
                   reply_markup=qty_keyboard(1, 100, "lx", LASTX_PRESETS))


async def process_lastx_count(msg: Message, state: FSMContext, n):
    if n is None or not 1 <= n <= 100:
        return await msg.answer("یه عدد بین ۱ تا ۱۰۰ بفرست 🙏", reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    lo, hi = await qty_limits("eco")
    price = await svc_price("eco")
    await state.update_data(posts=n)
    await state.set_state(LastX.qty)
    await msg.answer(f"✅ {n} پست آخر.\n\n"
                     "👁 <b>مرحله ۳ از ۳:</b> تعداد سین برای <b>هر پست</b> رو به‌صورت عدد بنویس و بفرست.\n"
                     "مثال: <code>1000</code>\n\n"
                     f"📌 حداقل {fmt(lo)} و حداکثر {fmt(hi)}\n"
                     f"💡 هر ۱۰۰۰ سین = {fmt(price)} تومان",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(LastX.count, F.text)
async def last_count(m: Message, state: FSMContext):
    await process_lastx_count(m, state, to_int(m.text))


@router.callback_query(F.data.startswith("lx:"), LastX.count)
async def last_count_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await process_lastx_count(c.message, state, int(c.data.split(":")[1]))


@router.message(LastX.qty, F.text)
async def last_qty(m: Message, state: FSMContext):
    await process_qty(m, state, m.from_user.id, to_int(m.text))


@router.callback_query(F.data.startswith("qty:"), LastX.qty)
async def last_qty_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await process_qty(c.message, state, c.from_user.id, int(c.data.split(":")[1]))


# ───────────────────────── رأی نظرسنجی / لایک ─────────────────────────
class Like(StatesGroup):
    link = State()
    row = State()
    col = State()
    qty = State()


async def like_price():
    return int(await get_setting("like_price_per_100"))


@router.message(F.text == BTN_LIKE)
async def like_start(m: Message, state: FSMContext):
    await state.clear()
    if not EXTRAS:
        return await m.answer("این سرویس فعلاً فعال نیست.")
    if await is_maintenance() and m.from_user.id not in ADMIN_IDS:
        return await m.answer(MAINT_TEXT)
    await state.set_state(Like.link)
    await m.answer("🗳 <b>رأی نظرسنجی</b>\n\n"
                   "وقتی زیر پستت نظرسنجی (یا دکمه‌ی انتخاب) هست و می‌خوای به یکی از گزینه‌ها رأی داده بشه، از این بخش استفاده کن.\n\n"
                   f"💵 قیمت: هر ۱۰۰ رأی = <b>{fmt(await like_price())}</b> تومان\n\n"
                   "🔗 <b>مرحله ۱ از ۴:</b> لینک پست رو بفرست.\n"
                   "مثال: <code>https://t.me/channel/123</code>\n\n"
                   "⚠️ کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه).",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(Like.link, F.text)
async def like_link(m: Message, state: FSMContext, bot: Bot):
    r = await validate_links(m, bot, "like")
    if not r:
        return
    await state.update_data(links=[r[0][0]])
    await state.set_state(Like.row)
    await m.answer("📍 <b>مرحله ۲ از ۴:</b> گزینه‌ی مورد نظرت توی <b>کدوم ردیف</b>ـه؟\n\n"
                   "ردیف‌ها رو از بالا بشمار؛ ردیف اول = ۱.\n"
                   "مثلاً اگه می‌خوای به گزینه‌ی دوم رأی بدی، ۲ رو بزن. عدد رو بنویس یا از دکمه‌ها انتخاب کن 👇",
                   reply_markup=qty_keyboard(1, 100, "lrow", [1, 2, 3, 4, 5, 6]))


async def like_set_row(msg: Message, state: FSMContext, n):
    if n is None or not 1 <= n <= 100:
        return await msg.answer("یه عدد بین ۱ تا ۱۰۰ بفرست 🙏", reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await state.update_data(row=n)
    await state.set_state(Like.col)
    await msg.answer("↔️ <b>مرحله ۳ از ۴:</b> توی اون ردیف، گزینه <b>چندمین ستون</b>ـه؟\n\n"
                     "اگه هر ردیف فقط یه گزینه داره (حالت معمولِ نظرسنجی)، ۱ رو بزن 👇",
                     reply_markup=qty_keyboard(1, 100, "lcol", [1, 2, 3, 4]))


@router.message(Like.row, F.text)
async def like_row(m: Message, state: FSMContext):
    await like_set_row(m, state, to_int(m.text))


@router.callback_query(F.data.startswith("lrow:"), Like.row)
async def like_row_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await like_set_row(c.message, state, int(c.data.split(":")[1]))


async def like_set_col(msg: Message, state: FSMContext, n):
    if n is None or not 1 <= n <= 100:
        return await msg.answer("یه عدد بین ۱ تا ۱۰۰ بفرست 🙏", reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await state.update_data(col=n)
    await state.set_state(Like.qty)
    await msg.answer("🔢 <b>مرحله ۴ از ۴:</b> چند تا رأی ثبت بشه؟ به‌صورت عدد بنویس و بفرست.\n"
                     "مثال: <code>50</code>\n\n"
                     f"📌 حداقل {fmt(LIKE_MIN)} و حداکثر {fmt(LIKE_MAX)}\n"
                     f"💡 هر ۱۰۰ رأی = {fmt(await like_price())} تومان",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(Like.col, F.text)
async def like_col(m: Message, state: FSMContext):
    await like_set_col(m, state, to_int(m.text))


@router.callback_query(F.data.startswith("lcol:"), Like.col)
async def like_col_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await like_set_col(c.message, state, int(c.data.split(":")[1]))


async def process_like_qty(msg: Message, state: FSMContext, uid: int, qty):
    if qty is None or not LIKE_MIN <= qty <= LIKE_MAX:
        return await msg.answer(f"تعداد باید یه <b>عدد</b> بین <b>{fmt(LIKE_MIN)}</b> و <b>{fmt(LIKE_MAX)}</b> باشه 🙏\nدوباره بنویس و بفرست:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    d = await state.get_data()
    if not d.get("links") or not d.get("row") or not d.get("col"):
        await state.clear()
        return await msg.answer("این سفارش منقضی شده؛ دوباره از «🗳 رأی نظرسنجی» شروع کن.")
    link_cost = math.ceil(qty * await like_price() / 100)
    await state.update_data(qty=qty, link_cost=link_cost, total=link_cost, kind="like", posts=1)
    await show_summary(msg, state, uid)


@router.message(Like.qty, F.text)
async def like_qty(m: Message, state: FSMContext):
    await process_like_qty(m, state, m.from_user.id, to_int(m.text))


@router.callback_query(F.data.startswith("lqty:"), Like.qty)
async def like_qty_cb(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await process_like_qty(c.message, state, c.from_user.id, int(c.data.split(":")[1]))


async def show_orders(msg: Message, uid: int, edit=False):
    for o in await many("SELECT * FROM orders WHERE user_id=? AND settled=0 AND provider_order<>'' ORDER BY id DESC LIMIT 10", (uid,)):
        r = await sync_order(o)
        if r and r[2]:
            await notify_order_result(msg.bot, o, r)
    rows = await many("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
    if not rows:
        return await msg.answer("هنوز سفارشی ثبت نکردی 🙂\nاز «🛒 ثبت سفارش سین» شروع کن 🚀")
    lines = [f"<b>#{o['id']}</b> • {order_label(o)} • {STATUS_FA.get(o['status'], o['status'])}\n🔗 {o['link']}"
             for o in rows]
    text = "📦 <b>سفارش‌های اخیر</b>\n\n" + "\n\n".join(lines)
    cancel_btns = [InlineKeyboardButton(text=f"❌ لغو #{o['id']}", callback_data=f"co:{o['id']}")
                   for o in rows if EXTRAS and not o["settled"] and o["provider_order"] and (o.get("provider") or LEGACY_PROV) == "as"]
    kb_rows = [cancel_btns[i:i + 3] for i in range(0, len(cancel_btns), 3)]
    kb_rows.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="orders_refresh")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if edit:
        with suppress(Exception):
            return await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("co:"))
async def cancel_ask(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await one("SELECT * FROM orders WHERE id=? AND user_id=? AND settled=0 AND provider_order<>''", (oid, c.from_user.id))
    if not o or (o.get("provider") or LEGACY_PROV) != "as":
        return await c.answer("این سفارش دیگه قابل لغو نیست.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله، لغو کن", callback_data=f"co_yes:{oid}"),
        InlineKeyboardButton(text="↩️ نه", callback_data="co_no")]])
    await c.answer()
    await c.message.answer(f"❓ سفارش <b>#{oid}</b> ({order_label(o)}) لغو بشه؟\n"
                           "بخش انجام‌نشده‌ش به کیف پولت برمی‌گرده؛ بخشی که قبلاً ارسال شده برنمی‌گرده.", reply_markup=kb)


@router.callback_query(F.data == "co_no")
async def cancel_no(c: CallbackQuery):
    await c.answer()
    with suppress(Exception):
        await c.message.edit_text("باشه، سفارش ادامه پیدا می‌کنه 👍")


@router.callback_query(F.data.startswith("co_yes:"))
async def cancel_do(c: CallbackQuery, bot: Bot):
    await c.answer()
    oid = int(c.data.split(":")[1])
    o = await one("SELECT * FROM orders WHERE id=? AND user_id=? AND settled=0 AND provider_order<>''", (oid, c.from_user.id))
    if not o:
        return await c.message.edit_text("این سفارش دیگه قابل لغو نیست.")
    try:
        res = await provider(action="cancel", order=o["provider_order"], kind=o["kind"] or "view", prov="as")
    except Exception:
        return await c.message.edit_text("ارتباط با سرویس برقرار نشد؛ کمی بعد دوباره امتحان کن.")
    err = str(res.get("error", "")) if isinstance(res, dict) else "bad response"
    if "Already cancelled" in err:
        text = "این سفارش قبلاً لغو شده."
    elif "Not possible" in err:
        text = "این سفارش دیگه قابل لغو نیست (احتمالاً کامل شده یا تقریباً تموم شده)."
    elif err:
        text = "لغو انجام نشد. اگه مشکل ادامه داشت به پشتیبانی پیام بده."
    else:
        text = "✅ درخواست لغو ارسال شد. هزینه‌ی بخش انجام‌نشده بعد از تایید به کیف پولت برمی‌گرده."
        r = await sync_order(o)
        if r and r[2]:
            await notify_order_result(bot, o, r)
    await c.message.edit_text(text)


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
def admin_kb(maint):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 مدیریت و شارژ کاربر", callback_data="adm_user")],
        [InlineKeyboardButton(text="📋 رسیدهای در انتظار", callback_data="adm_pending"),
         InlineKeyboardButton(text="💼 موجودی provider", callback_data="adm_provider")],
        [InlineKeyboardButton(text="📊 گزارش امروز", callback_data="adm_report"),
         InlineKeyboardButton(text="💾 بکاپ الان", callback_data="adm_backup")],
        [InlineKeyboardButton(text="🛠 حالت تعمیر: روشن ✅" if maint else "🛠 حالت تعمیر: خاموش", callback_data="adm_maint")]])


@router.message(Command("admin"), IS_ADMIN)
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    u = await one("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users")
    o = await one("SELECT COUNT(*) n, COALESCE(SUM(cost),0) s FROM orders")
    p = await one("SELECT COUNT(*) n FROM topups WHERE status='claimed'")
    unk = (await one("SELECT COUNT(*) AS n FROM orders WHERE status='Unknown'"))["n"]
    price = await get_setting("price_per_1000")
    on = await enabled_services()
    plines = []
    for k in SERVICES:
        if PROVIDERS[SERVICES[k]["provider"]]["key"]:
            plines.append(f"💵 {SERVICES[k]['name']} هر ۱۰۰۰ سین: {fmt(await svc_price(k))}" + ("" if k in on else " (خاموش)"))
    pline = "\n".join(plines) or f"💵 قیمت هر ۱۰۰۰ سین: {fmt(price)}"
    maint = await is_maintenance()
    amode = {"off": "خاموش", "beta": "آزمایشی", "on": "روشن"}.get(await get_opt("auto_mode", "off"), "خاموش")
    nauto = (await one("SELECT COUNT(*) AS n FROM auto_channels WHERE enabled=1"))["n"]
    rline = (f"\n👍 قیمت هر ۱۰۰ ریکشن: {fmt(await get_setting('react_price_per_100'))}"
             f"\n🗳 قیمت هر ۱۰۰ رأی: {fmt(await get_setting('like_price_per_100'))}") if REACT_ENABLED else ""
    await m.answer(
        "🛠 <b>پنل ادمین</b>\n\n"
        f"📊 کاربران: {u['n']} | مجموع موجودی کیف‌پول‌ها: {fmt(u['b'])}\n"
        f"🛒 سفارش‌ها: {o['n']} | فروش: {fmt(o['s'])}\n"
        f"🕓 رسید در انتظار: {p['n']}\n⚠️ سفارش نامشخص: {unk}\n{pline}{rline}\n"
        f"🛠 حالت تعمیر: {'روشن' if maint else 'خاموش'}\n⚡ سین خودکار: {amode} | کانال فعال: {nauto}\n\n"
        "<b>دستورات:</b>\n/user آیدی یا @یوزرنیم\n/pending رسیدهای در انتظار\n/add id مبلغ\n/sub id مبلغ\n"
        "/ban id\n/unban id\n/price مبلغ یا /price fast|eco مبلغ\n/service fast|eco on|off\n/auto off|beta|on\n/autobeta آیدی‌ها\n/autocap تعداد\n/autolist\n/autooff id\n/card متن کارت\n/limits حداقل حداکثر\n"
        "/support @آیدی\n/brand نام ربات\n/provider موجودی provider\n/unknown سفارش‌های نامشخص\n/resolve id شماره\n/refund id\n/rprice مبلغ (قیمت هر ۱۰۰ ریکشن)\n/lprice مبلغ (قیمت هر ۱۰۰ رأی)\n"
        "/maintenance on|off\n/report [روز_قبل]\n/backup\n/broadcast متن",
        reply_markup=admin_kb(maint))


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
    parts = (command.args or "").split()
    if len(parts) == 1 and to_int(parts[0]):
        v = to_int(parts[0])
        await set_setting("price_per_1000", v)
        for k in SERVICES:
            await set_setting(f"price_{k}", v)
        return await m.answer(f"✅ قیمت هر ۱۰۰۰ سین برای همه‌ی سرویس‌ها: {fmt(v)} تومان")
    if len(parts) == 2 and parts[0] in SERVICES and to_int(parts[1]):
        await set_setting(f"price_{parts[0]}", to_int(parts[1]))
        return await m.answer(f"✅ قیمت هر ۱۰۰۰ سین ({SERVICES[parts[0]]['name']}): {fmt(to_int(parts[1]))} تومان")
    await m.answer("فرمت:\n/price 5000  (قیمت همه‌ی سرویس‌ها)\n/price fast 6000\n/price eco 4000")


@router.message(Command("service"), IS_ADMIN)
async def cmd_service(m: Message, command: CommandObject):
    parts = (command.args or "").lower().split()
    if len(parts) != 2 or parts[0] not in SERVICES or parts[1] not in ("on", "off"):
        on = await enabled_services()
        status = "\n".join(f"{SERVICES[k]['name']} ({k}): {'روشن' if k in on else 'خاموش/تنظیم‌نشده'}" for k in SERVICES)
        return await m.answer(f"فرمت: /service fast off  یا  /service eco on\n\nوضعیت فعلی:\n{status}")
    await set_setting(f"svc_{parts[0]}_on", "1" if parts[1] == "on" else "0")
    await m.answer(f"✅ {SERVICES[parts[0]]['name']}: {'روشن' if parts[1] == 'on' else 'خاموش'} شد.")


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
    lines = []
    for p in ENABLED_PROVS:
        try:
            res = await provider(action="balance", prov=p)
            bal, cur = res.get("balance"), str(res.get("currency", ""))
            extra = f" ≈ {fmt(float(bal) * COIN_TOMAN)} تومان" if cur.lower() == "coin" and bal is not None else ""
            lines.append(f"💼 {PROV_NAMES[p]}: {bal} {cur}{extra}")
        except Exception as e:
            lines.append(f"💼 {PROV_NAMES[p]}: خطا ({html.escape(str(e))[:80]})")
    return "\n".join(lines) or "هیچ provider‌ای تنظیم نشده."


@router.message(Command("provider"), IS_ADMIN)
async def cmd_provider(m: Message):
    await m.answer(await provider_balance_text())


@router.callback_query(F.data == "adm_provider", IS_ADMIN)
async def adm_provider(c: CallbackQuery):
    await c.answer()
    await c.message.answer(await provider_balance_text())


@router.message(Command("lprice"), IS_ADMIN)
async def cmd_lprice(m: Message, command: CommandObject):
    v = to_int(command.args)
    if not v:
        return await m.answer("فرمت: /lprice 4000 (قیمت فروش هر ۱۰۰ رأی)")
    await set_setting("like_price_per_100", v)
    await m.answer(f"✅ قیمت هر ۱۰۰ رأی: {fmt(v)} تومان")


@router.message(Command("rprice"), IS_ADMIN)
async def cmd_rprice(m: Message, command: CommandObject):
    v = to_int(command.args)
    if not v:
        return await m.answer("فرمت: /rprice 4000 (قیمت فروش هر ۱۰۰ ریکشن)")
    await set_setting("react_price_per_100", v)
    await m.answer(f"✅ قیمت هر ۱۰۰ ریکشن: {fmt(v)} تومان")


@router.message(Command("maintenance"), IS_ADMIN)
async def cmd_maint(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        return await m.answer(f"فرمت: /maintenance on یا /maintenance off\nوضعیت فعلی: {'روشن' if await is_maintenance() else 'خاموش'}")
    await set_setting("maintenance", "1" if arg == "on" else "0")
    await m.answer("🛠 حالت تعمیر <b>روشن</b> شد؛ سفارش‌گیری بسته‌ست (شارژ فعاله)." if arg == "on"
                   else "✅ حالت تعمیر خاموش شد؛ سفارش‌گیری باز شد.")


@router.callback_query(F.data == "adm_maint", IS_ADMIN)
async def adm_maint(c: CallbackQuery):
    new = not await is_maintenance()
    await set_setting("maintenance", "1" if new else "0")
    with suppress(Exception):
        await c.message.edit_reply_markup(reply_markup=admin_kb(new))
    await c.answer("حالت تعمیر روشن شد 🛠" if new else "حالت تعمیر خاموش شد ✅", show_alert=True)


@router.message(Command("report"), IS_ADMIN)
async def cmd_report(m: Message, command: CommandObject):
    back = to_int(command.args) or 0
    start, end = day_range(ir_day() - back)
    await m.answer(await build_report(start, end, "امروز" if back == 0 else f"{back} روز قبل"))


@router.callback_query(F.data == "adm_report", IS_ADMIN)
async def adm_report(c: CallbackQuery):
    await c.answer()
    start, end = day_range(ir_day())
    await c.message.answer(await build_report(start, end, "امروز"))


@router.message(Command("backup"), IS_ADMIN)
async def cmd_backup(m: Message, bot: Bot):
    await send_backup(bot)
    await m.answer("✅ بکاپ ارسال شد.")


@router.callback_query(F.data == "adm_backup", IS_ADMIN)
async def adm_backup(c: CallbackQuery, bot: Bot):
    await c.answer("در حال ساخت بکاپ...")
    await send_backup(bot)


@router.message(Command("unknown"), IS_ADMIN)
async def cmd_unknown(m: Message):
    rows = await many("SELECT * FROM orders WHERE status='Unknown' ORDER BY id")
    if not rows:
        return await m.answer("سفارش نامشخصی نیست ✅")
    for o in rows:
        await m.answer(f"#{o['id']} • کاربر <code>{o['user_id']}</code> • {order_label(o)}\n{o['link']}\n"
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
    async with db.tx() as tx:
        cur = await tx.execute("UPDATE orders SET status='Canceled', settled=1 WHERE id=? AND status='Unknown'", (oid,))
        o = None
        if cur.rowcount == 1:
            o = await tx.one("SELECT * FROM orders WHERE id=?", (oid,))
            await _credit(tx, o["user_id"], o["cost"], f"refund-order-{oid}")
    if not o:
        return await m.answer("این سفارش پیدا نشد یا دیگه نامشخص نیست.")
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


# ───────────────────────── سین خودکار (ربات ادمین کانال مشتری) ─────────────────────────
class Auto(StatesGroup):
    channel = State()
    service = State()
    qty = State()


_chan_locks = {}
PAUSE_TEXT = {"balance": "موجودی کافی نبود", "removed": "ربات از ادمینی کانال برداشته شد",
              "private": "کانال دیگه عمومی نیست", "admin": "توسط مدیریت متوقف شد"}


async def auto_allowed(uid):
    mode = await get_opt("auto_mode", "off")
    if mode == "on":
        return True
    return mode == "beta" and (uid in ADMIN_IDS or str(uid) in (await get_opt("auto_beta", "")).replace(" ", "").split(","))


async def notice_once(key, ttl):
    """true برمی‌گردونه اگه از آخرین بار بیشتر از ttl ثانیه گذشته باشه (برای جلوگیری از پیام تکراری)."""
    if time.time() - int(await get_opt(key, "0") or 0) < ttl:
        return False
    await set_setting(key, int(time.time()))
    return True


async def auto_cost(qty, key):
    return math.ceil(qty * await svc_price(key) / 1000)


async def auto_hub(msg: Message, uid: int):
    rows = await many("SELECT * FROM auto_channels WHERE user_id=? ORDER BY id", (uid,))
    text = ("⚡ <b>سین خودکار</b>\n\n"
            "هر پست جدیدی که توی کانالت بذاری، <b>خودکار</b> سین می‌خوره؛ لازم نیست هر بار لینک بفرستی.\n\n"
            "📌 فقط پست‌های <b>جدید</b> حساب می‌شن، نه پست‌های قبلی.\n"
            "💰 هزینه‌ی هر پست از کیف پولت کم می‌شه. اگه موجودی تموم بشه، سین خودکار متوقف می‌شه و بهت خبر می‌دم.\n")
    kb = []
    if rows:
        text += "\n<b>کانال‌های تو:</b>\n"
        for r in rows:
            state = "🟢 فعال" if r["enabled"] else f"⏸ متوقف ({PAUSE_TEXT.get(r['paused_reason'], 'خاموش')})"
            text += f"• @{html.escape(r['username'])} — {fmt(r['qty'])} سین برای هر پست ({SERVICES.get(r['service'], {}).get('name', '')}) — {state}\n"
            kb.append([InlineKeyboardButton(text=("⏸ توقف" if r["enabled"] else "▶️ روشن") + f" @{r['username']}", callback_data=f"auto_t:{r['id']}"),
                       InlineKeyboardButton(text="🗑 حذف", callback_data=f"auto_d:{r['id']}")])
    kb.append([InlineKeyboardButton(text="➕ افزودن کانال", callback_data="auto_add")])
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.message(F.text == BTN_AUTO)
async def auto_start(m: Message, state: FSMContext):
    await state.clear()
    if not await auto_allowed(m.from_user.id):
        return await m.answer("⚡ <b>سین خودکار</b>\n\nاین قابلیت به‌زودی فعال می‌شه 🔜")
    await auto_hub(m, m.from_user.id)


@router.callback_query(F.data == "auto_add")
async def auto_add(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    if not await auto_allowed(c.from_user.id):
        return await c.message.answer("این قابلیت فعلاً فعال نیست.")
    me = await bot.me()
    await state.set_state(Auto.channel)
    await c.message.answer("➕ <b>افزودن کانال به سین خودکار</b>\n\n"
                           "1️⃣ ربات رو توی کانالت <b>ادمین</b> کن:\n"
                           f"تنظیمات کانال ← Administrators ← Add Admin ← <code>@{me.username}</code>\n"
                           "(به هیچ دسترسی خاصی نیاز نداره)\n\n"
                           "2️⃣ بعد آیدی کانال رو همین‌جا بفرست؛ مثل <code>@mychannel</code>\n\n"
                           "⚠️ کانال باید <b>عمومی</b> باشه (آیدی @ داشته باشه) و خودت ادمین یا مالکش باشی.",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.message(Auto.channel, F.text)
async def auto_channel(m: Message, state: FSMContext, bot: Bot):
    kb = InlineKeyboardMarkup(inline_keyboard=[cancel_row()])
    mm = re.search(r"(?:@|https?://t\.me/)([A-Za-z][A-Za-z0-9_]{3,})", m.text or "")
    if not mm:
        return await m.answer("آیدی کانال معتبر نیست 🤔\nمثل این بفرست: <code>@mychannel</code>", reply_markup=kb)
    try:
        chat = await bot.get_chat(f"@{mm.group(1)}")
    except TelegramBadRequest:
        return await m.answer("❌ کانال پیدا نشد یا عمومی نیست. آیدی درست رو بفرست.", reply_markup=kb)
    except Exception:
        return await m.answer("الان نتونستم کانال رو چک کنم؛ کمی بعد دوباره امتحان کن 🙏", reply_markup=kb)
    if chat.type != "channel":
        return await m.answer("❌ این آیدی مربوط به یه کانال نیست.", reply_markup=kb)
    me = await bot.me()
    try:
        bm = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        bm = None
    if not bm or bm.status not in ("administrator", "creator"):
        return await m.answer("❌ ربات هنوز توی کانال <b>ادمین</b> نیست.\n\n"
                              f"تنظیمات کانال ← Administrators ← Add Admin ← <code>@{me.username}</code>\n"
                              "بعدش دوباره آیدی کانال رو بفرست.", reply_markup=kb)
    try:
        um = await bot.get_chat_member(chat.id, m.from_user.id)
    except Exception:
        um = None
    if not um or um.status not in ("administrator", "creator"):
        return await m.answer("❌ فقط ادمین یا مالک کانال می‌تونه سین خودکار رو فعال کنه.", reply_markup=kb)
    ex = await one("SELECT user_id FROM auto_channels WHERE chat_id=?", (chat.id,))
    if ex:
        return await m.answer("این کانال قبلاً ثبت شده." + (" از همین منوی «سین خودکار» مدیریتش کن." if ex["user_id"] == m.from_user.id else ""),
                              reply_markup=kb)
    await state.update_data(chat_id=chat.id, username=chat.username or mm.group(1))
    svcs = await enabled_services()
    if not svcs:
        await state.clear()
        return await m.answer("🛠 سرویس سین موقتاً در دسترس نیست. کمی بعد دوباره امتحان کن 🙏")
    if len(svcs) == 1:
        return await auto_ask_qty(m, state, svcs[0])
    await state.set_state(Auto.service)
    lines, rows = [], []
    for k in svcs:
        sv, price = SERVICES[k], await svc_price(k)
        lines.append(f"{sv['name']} — <b>{fmt(price)}</b> تومان برای هر ۱۰۰۰ سین\n{sv['desc']}")
        rows.append([InlineKeyboardButton(text=f"{sv['name']} • {fmt(price)} تومان", callback_data=f"autosvc:{k}")])
    rows.append(cancel_row())
    await m.answer("✅ کانال تایید شد.\n\n<b>سرعت ارسال سین‌ها رو انتخاب کن</b> 👇\n\n" + "\n\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def auto_ask_qty(msg: Message, state: FSMContext, key: str):
    lo, hi = await qty_limits(key)
    await state.update_data(service=key)
    await state.set_state(Auto.qty)
    await msg.answer(f"✅ سرویس: {SERVICES[key]['name']}\n\n"
                     "👁 تعداد سین برای <b>هر پست جدید</b> رو به‌صورت عدد بنویس و بفرست.\nمثال: <code>1000</code>\n\n"
                     f"📌 حداقل {fmt(lo)} و حداکثر {fmt(hi)}\n💡 هر ۱۰۰۰ سین = {fmt(await svc_price(key))} تومان",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))


@router.callback_query(F.data.startswith("autosvc:"), Auto.service)
async def auto_service(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in SERVICES or not await svc_enabled(key):
        return await c.answer("این سرویس فعلاً فعال نیست.", show_alert=True)
    await c.answer()
    await auto_ask_qty(c.message, state, key)


@router.message(Auto.qty, F.text)
async def auto_qty(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d.get("service")
    lo, hi = await qty_limits(key)
    qty = to_int(m.text)
    if qty is None or not lo <= qty <= hi:
        return await m.answer(f"تعداد باید یه <b>عدد</b> بین <b>{fmt(lo)}</b> و <b>{fmt(hi)}</b> باشه 🙏\nدوباره بنویس و بفرست:",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[cancel_row()]))
    await state.update_data(qty=qty)
    cap = int(await get_opt("auto_daily_cap", "10"))
    cost = await auto_cost(qty, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ فعال کن", callback_data="auto_ok"),
                                                InlineKeyboardButton(text="❌ انصراف", callback_data="flow_cancel")]])
    await m.answer("🧾 <b>خلاصه‌ی سین خودکار</b>\n\n"
                   f"📣 کانال: @{html.escape(d['username'])}\n"
                   f"⚙️ سرویس: {SERVICES[key]['name']}\n"
                   f"👁 سین هر پست جدید: {fmt(qty)}\n"
                   f"💵 هزینه‌ی هر پست: {fmt(cost)} تومان\n"
                   f"📅 حداکثر {cap} پست در روز (حداکثر {fmt(cost * cap)} تومان در روز)\n\n"
                   "📌 فقط پست‌های <b>بعد از فعال شدن</b> حساب می‌شن. هر وقت خواستی می‌تونی متوقفش کنی.", reply_markup=kb)


@router.callback_query(F.data == "auto_ok")
async def auto_confirm(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    if not all(k in d for k in ("chat_id", "username", "service", "qty")):
        return await c.answer("این درخواست منقضی شده؛ دوباره امتحان کن.", show_alert=True)
    cap = int(await get_opt("auto_daily_cap", "10"))
    cur = await db.execute(
        "INSERT INTO auto_channels(chat_id,username,user_id,qty,service,daily_cap,enabled,created) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (chat_id) DO NOTHING",
        (d["chat_id"], d["username"], c.from_user.id, d["qty"], d["service"], cap, 1, int(time.time())))
    await c.answer()
    if cur.rowcount != 1:
        return await c.message.edit_text("این کانال قبلاً ثبت شده.")
    await c.message.edit_text("✅ <b>سین خودکار فعال شد!</b>\n\n"
                              f"از حالا برای هر پست جدید @{html.escape(d['username'])}، {fmt(d['qty'])} سین خودکار ثبت می‌شه و بهت پیام می‌دم.")


async def _own_channel(c: CallbackQuery, cid: int):
    return await one("SELECT * FROM auto_channels WHERE id=? AND user_id=?", (cid, c.from_user.id))


@router.callback_query(F.data.startswith("auto_t:"))
async def auto_toggle(c: CallbackQuery):
    r = await _own_channel(c, int(c.data.split(":")[1]))
    if not r:
        return await c.answer("پیدا نشد.", show_alert=True)
    if r["enabled"]:
        await db.execute("UPDATE auto_channels SET enabled=0, paused_reason=NULL WHERE id=?", (r["id"],))
        await c.answer("متوقف شد ⏸")
    else:
        await db.execute("UPDATE auto_channels SET enabled=1, paused_reason=NULL WHERE id=?", (r["id"],))
        await c.answer("روشن شد ▶️")
    await auto_hub(c.message, c.from_user.id)


@router.callback_query(F.data.startswith("auto_d:"))
async def auto_delete(c: CallbackQuery):
    r = await _own_channel(c, int(c.data.split(":")[1]))
    if not r:
        return await c.answer("پیدا نشد.", show_alert=True)
    await db.execute("DELETE FROM auto_channels WHERE id=?", (r["id"],))
    await c.answer("حذف شد 🗑")
    await auto_hub(c.message, c.from_user.id)


async def pause_channel(bot, row, reason, text, kb=None):
    await db.execute("UPDATE auto_channels SET enabled=0, paused_reason=? WHERE id=?", (reason, row["id"]))
    with suppress(Exception):
        await bot.send_message(row["user_id"], text, reply_markup=kb)


async def process_auto_post(bot, m):
    """برای هر پست جدید کانال‌های ثبت‌شده سفارش می‌ده (پول و ردیف سفارش اتمیک رزرو می‌شن)."""
    if (await get_opt("auto_mode", "off")) == "off":
        return
    row = await one("SELECT * FROM auto_channels WHERE chat_id=?", (m.chat.id,))
    if not row or not row["enabled"] or not await auto_allowed(row["user_id"]):
        return
    uid, chat_id = row["user_id"], m.chat.id
    async with _chan_locks.setdefault(chat_id, asyncio.Lock()):
        now = int(time.time())
        # ادعای پست: هر پست (و هر آلبوم) فقط یک‌بار پردازش می‌شه
        cur = await db.execute("INSERT INTO auto_posts(chat_id,post_id,media_group_id,status,created) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                               (chat_id, m.message_id, m.media_group_id, "new", now))
        if cur.rowcount != 1:
            return

        async def done(status, order_id=None):
            await db.execute("UPDATE auto_posts SET status=?, order_id=? WHERE chat_id=? AND post_id=?", (status, order_id, chat_id, m.message_id))

        uname = m.chat.username
        if not uname:
            await done("paused")
            return await pause_channel(bot, row, "private", f"⏸ سین خودکار کانالت متوقف شد: کانال دیگه عمومی نیست (آیدی @ نداره).")
        if now - m.date.timestamp() > int(await get_opt("auto_max_age_min", "180")) * 60:
            return await done("old")
        if await is_maintenance():
            await done("maint")
            if await notice_once(f"auto_maint_{chat_id}", 86400):
                with suppress(Exception):
                    await bot.send_message(uid, "🛠 سفارش‌گیری موقتاً متوقفه؛ سین خودکار این پست ثبت نشد.")
            return
        key = row["service"]
        if key not in SERVICES or not await svc_enabled(key):
            await done("svc_off")
            if await notice_once(f"auto_svc_{chat_id}", 3600):
                with suppress(Exception):
                    await bot.send_message(uid, "⚠️ سرویس انتخابی سین خودکارت موقتاً در دسترس نیست؛ این پست ثبت نشد.")
            return
        cap = min(row["daily_cap"] or 10 ** 9, int(await get_opt("auto_daily_cap", "10")))
        start, _end = day_range(ir_day())
        used = (await one("SELECT COUNT(*) AS n FROM auto_posts WHERE chat_id=? AND status IN ('ordered','unknown') AND created>=?",
                          (chat_id, start)))["n"]
        if used >= cap:
            await done("capped")
            if await notice_once(f"auto_cap_{chat_id}", 43200):
                with suppress(Exception):
                    await bot.send_message(uid, f"📅 امروز به سقف {cap} پست رسیدی؛ پست‌های بعدیِ امروز سفارش داده نمی‌شن و از فردا خودکار ادامه پیدا می‌کنه.")
            return
        qty, prov = row["qty"], SERVICES[key]["provider"]
        cost = await auto_cost(qty, key)
        link = f"https://t.me/{uname}/{m.message_id}"
        oid = await reserve_order(uid, link, qty, cost, "view", None, 1, None, prov)
        if oid is None:
            await done("paused")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 شارژ کیف پول", callback_data="go_charge")]])
            return await pause_channel(bot, row, "balance",
                                       f"⏸ <b>سین خودکار متوقف شد</b>\nموجودی کیف پولت برای پست جدید @{html.escape(uname)} ({fmt(cost)} تومان) کافی نبود.\n"
                                       "بعد از شارژ، از منوی «⚡ سین خودکار» دوباره روشنش کن.", kb)
        st, info = await place_order(oid, link, qty, "view", None, None, prov)
        if st == "ok":
            await done("ordered", oid)
            with suppress(Exception):
                await bot.send_message(uid, f"⚡ <b>سین خودکار</b>\nبرای پست جدید @{html.escape(uname)} تعداد <b>{fmt(qty)} سین</b> ثبت شد 💙\n"
                                            f"🔗 {link}\n💵 {fmt(cost)} تومان از کیف پولت کم شد.", disable_web_page_preview=True)
        elif st == "unknown":
            await done("unknown", oid)
            await notify_admins(bot, f"🚨 <b>سفارش نامشخص #{oid}</b> (سین خودکار، کاربر <code>{uid}</code>)\n{link} | {fmt(qty)} سین\n"
                                     f"اگه توی پنل provider ثبت شده: /resolve {oid} شماره_سفارش_provider\nاگه ثبت نشده: /refund {oid}")
            with suppress(Exception):
                await bot.send_message(uid, f"⏳ سفارش خودکار پست جدید @{html.escape(uname)} در حال بررسیه؛ نتیجه رو بهت خبر می‌دم.")
        else:
            await done("failed", oid)
            if await notice_once(f"auto_fail_{chat_id}", 3600):
                await notify_admins(bot, f"⚠️ سفارش خودکار ثبت نشد (کاربر {uid}, @{uname}):\n{html.escape(str(info)[:300])}")
                with suppress(Exception):
                    await bot.send_message(uid, f"⚠️ سفارش خودکار پست جدیدت ثبت نشد و {fmt(cost)} تومان به کیف پولت برگشت.")
        await asyncio.sleep(0.5)  # فاصله بین سفارش‌ها؛ نرخ provider رعایت بشه


@router.channel_post()
async def on_channel_post(m: Message, bot: Bot):
    await process_auto_post(bot, m)


@router.my_chat_member()
async def on_my_chat_member(ev, bot: Bot):
    """اگه ربات از ادمینی کانالی برداشته شد، سین خودکار اون کانال متوقف می‌شه."""
    if ev.chat.type != "channel" or ev.new_chat_member.status in ("administrator", "creator"):
        return
    rows = await many("SELECT * FROM auto_channels WHERE chat_id=? AND enabled=1", (ev.chat.id,))
    for r in rows:
        await pause_channel(bot, r, "removed", f"⏸ سین خودکار @{html.escape(r['username'])} متوقف شد: ربات دیگه ادمین کانال نیست.\n"
                                               "دوباره ادمینش کن و از منوی «⚡ سین خودکار» روشنش کن.")


# ── دستورهای ادمین سین خودکار
@router.message(Command("auto"), IS_ADMIN)
async def cmd_auto(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    names = {"off": "خاموش", "beta": "آزمایشی (فقط ادمین‌ها و لیست /autobeta)", "on": "روشن برای همه"}
    if arg not in names:
        cur = await get_opt("auto_mode", "off")
        return await m.answer(f"فرمت: /auto off | beta | on\nوضعیت فعلی: {names.get(cur, cur)}")
    await set_setting("auto_mode", arg)
    await m.answer(f"⚡ سین خودکار: <b>{names[arg]}</b>")


@router.message(Command("autobeta"), IS_ADMIN)
async def cmd_autobeta(m: Message, command: CommandObject):
    ids = [x for x in (command.args or "").replace(",", " ").split() if x.isdigit()]
    if not ids:
        return await m.answer(f"فرمت: /autobeta 111 222\nلیست فعلی: {await get_opt('auto_beta', '') or '—'}")
    await set_setting("auto_beta", ",".join(ids))
    await m.answer(f"✅ لیست آزمایشی: {', '.join(ids)}")


@router.message(Command("autocap"), IS_ADMIN)
async def cmd_autocap(m: Message, command: CommandObject):
    v = to_int(command.args)
    if not v:
        return await m.answer(f"فرمت: /autocap 10\nسقف فعلی: {await get_opt('auto_daily_cap', '10')} پست در روز برای هر کانال")
    await set_setting("auto_daily_cap", v)
    await m.answer(f"✅ سقف روزانه برای همه‌ی کانال‌ها: {v} پست")


@router.message(Command("autolist"), IS_ADMIN)
async def cmd_autolist(m: Message):
    start, _e = day_range(ir_day())
    rows = await many("SELECT * FROM auto_channels ORDER BY id DESC LIMIT 30")
    if not rows:
        return await m.answer("کانالی ثبت نشده.")
    lines = []
    for r in rows:
        n = (await one("SELECT COUNT(*) AS n FROM auto_posts WHERE chat_id=? AND status='ordered' AND created>=?", (r["chat_id"], start)))["n"]
        lines.append(f"#{r['id']} @{r['username']} • کاربر <code>{r['user_id']}</code> • {fmt(r['qty'])} سین • "
                     f"{'🟢' if r['enabled'] else '⏸ ' + PAUSE_TEXT.get(r['paused_reason'], '')} • امروز: {n}")
    await m.answer("⚡ <b>کانال‌های سین خودکار</b>\n\n" + "\n".join(lines))


@router.message(Command("autooff"), IS_ADMIN)
async def cmd_autooff(m: Message, command: CommandObject, bot: Bot):
    cid = to_int(command.args)
    r = await one("SELECT * FROM auto_channels WHERE id=?", (cid,)) if cid else None
    if not r:
        return await m.answer("فرمت: /autooff شماره_کانال (از /autolist)")
    await pause_channel(bot, r, "admin", f"⏸ سین خودکار @{html.escape(r['username'])} توسط مدیریت متوقف شد. برای اطلاعات بیشتر به پشتیبانی پیام بده.")
    await m.answer("✅ متوقف شد.")


# ───────────────────────── دریافت رسید (باید آخرین هندلر باشه) ─────────────────────────
@router.message(StateFilter(None), F.photo | F.document | F.text)
async def receipt_in(m: Message, bot: Bot):
    if m.text and m.text.startswith("/"):
        return
    uid = m.from_user.id
    is_media = bool(m.photo or m.document)
    t = await one("SELECT * FROM topups WHERE user_id=? AND status='awaiting' ORDER BY id DESC LIMIT 1", (uid,))
    if not t:
        # رسید اضافه (مثلاً چند عکس پشت‌سرهم) برای درخواستی که تازه ثبت شده
        if is_media:
            t2 = await one("SELECT * FROM topups WHERE user_id=? AND status='claimed' AND expires>? ORDER BY id DESC LIMIT 1",
                           (uid, int(time.time()) - 7200))
            if t2:
                for a in ADMIN_IDS:
                    with suppress(Exception):
                        cp = await bot.copy_message(chat_id=a, from_chat_id=m.chat.id, message_id=m.message_id)
                        await bot.send_message(a, f"🧾 رسید اضافه برای درخواست #{t2['id']}", reply_to_message_id=cp.message_id,
                                               reply_markup=admin_topup_kb(t2["id"]))
                await m.answer("✅ این رسید هم برای ادمین ارسال شد.")
        return
    digits = sum(ch.isdigit() for ch in (m.text or ""))
    if not is_media and digits < 6:  # متن معمولی، نه شماره‌ی پیگیری
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ لغو درخواست شارژ", callback_data=f"cancel:{t['id']}")]])
        return await m.answer("🧾 منتظر رسید پرداختت هستم.\nعکس رسید یا شماره‌ی پیگیری (حداقل ۶ رقم) رو بفرست.", reply_markup=kb)
    key = None
    if m.photo:
        key = m.photo[-1].file_unique_id
    elif m.document:
        key = m.document.file_unique_id
    elif m.text:
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
    try:
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    except OSError as e:
        logging.warning("health server could not bind: %s", e)


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
    asyncio.create_task(daily_jobs(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
