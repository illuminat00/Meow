"""مینی‌اپ تلگرام: فروشگاه سین + حساب کاربری + کیف پول.
از bot.py با miniapp.register(app, globals()) صدا زده می‌شه و از همون دیتابیس و منطق سفارش ربات استفاده می‌کنه."""
import re, asyncio, base64, hashlib, hmac, json, logging, math, time
from urllib.parse import parse_qsl

from aiohttp import web

from miniapp_assets import IMGS, IMGS_PNG, BG_JPG, ANIM_JPG


LIVE = {}  # دقیقاً همون globals() ربات (زنده)
_IMG = {k: base64.b64decode(v) for k, v in IMGS.items()}
_PNG = {k: base64.b64decode(v) for k, v in IMGS_PNG.items()}
_BG = base64.b64decode(BG_JPG)
_ANIM = {k: base64.b64decode(v) for k, v in ANIM_JPG.items()}
ERR = lambda m, **k: {"error": m, **k}
_EM = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u23E9-\u23FF\u25A0-\u25FF\uFE0F\u200d]\s?")
_s = lambda x: _EM.sub("", str(x)).strip()  # ایموجی‌های متن‌های ربات حذف می‌شن


def g(name):
    return LIVE[name]


def verify(init, max_age=86400):
    """اعتبارسنجی initData تلگرام؛ کاربر یا None."""
    try:
        p = dict(parse_qsl(init, keep_blank_values=True))
        h = p.pop("hash", "")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(p.items()))
        sec = hmac.new(b"WebAppData", g("BOT_TOKEN").encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(p.get("auth_date", "0")) > max_age:
            return None
        return json.loads(p["user"])
    except Exception:
        return None


# ───────── API ─────────
async def me(uid, u, b):
    one, many = g("one"), g("many")
    bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
    n = (await one("SELECT COUNT(*) AS n FROM orders WHERE user_id=?", (uid,)))["n"]
    svcs = []
    for k in await g("enabled_services")():
        lo, hi = await g("qty_limits")(k)
        sv = g("SERVICES")[k]
        svcs.append({"key": k, "name": sv["name"], "desc": sv["desc"], "price": await g("svc_price")(k), "min": lo, "max": hi})
    return {"id": uid, "name": u.get("first_name", ""), "balance": bal, "orders": n, "services": svcs,
            "support": (await g("get_setting")("support")).strip().lstrip("@"), "brand": await g("get_setting")("brand"),
            "react": ({"price": int(await g("react_price")()), "min": g("REACT_MIN"), "max": g("REACT_MAX"), "emojis": g("EMOJIS")} if g("REACT_ENABLED") else None),
            "min_topup": g("MIN_TOPUP"), "presets": [p for p in g("PRESETS") if p >= g("MIN_TOPUP")],
            "maint": bool(await g("is_maintenance")()) and uid not in g("ADMIN_IDS")}


async def _sync_open(rows):
    """وضعیت سفارش‌های باز رو همین الان از provider می‌گیره (حداکثر هر ۲۰ ثانیه برای هر سفارش)."""
    ls = g("_last_sync")
    now = time.time()
    fresh = [o for o in rows if now - ls.get(o["id"], 0) > 20]
    sem = asyncio.Semaphore(5)

    async def one_(o):
        async with sem:
            ls[o["id"]] = time.time()
            try:
                r = await g("sync_order")(o)
                if r and r[2]:
                    await g("notify_order_result")(g("BOT"), o, r)
            except Exception:
                pass
    if fresh:
        await asyncio.gather(*(one_(o) for o in fresh))


async def orders(uid, u, b):
    await _sync_open(await g("many")("SELECT * FROM orders WHERE user_id=? AND settled=0 AND provider_order<>'' ORDER BY id DESC LIMIT 10", (uid,)))
    rows = await g("many")("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,))
    SF = g("STATUS_FA")
    return {"orders": [{"id": o["id"], "label": _s(g("order_label")(o)), "status": _s(SF.get(o["status"], o["status"])),
                        "link": o["link"], "cost": o["cost"], "date": g("jdate")(o["created"])} for o in rows]}


async def ledger(uid, u, b):
    rows = await g("many")("SELECT amount, reason, ts FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,))
    return {"items": [{"label": _s(g("ledger_label")(r["reason"])), "amount": r["amount"], "date": g("jdate")(r["ts"])} for r in rows]}


async def order(uid, u, b):
    one, many = g("one"), g("many")
    if await g("is_maintenance")() and uid not in g("ADMIN_IDS"):
        return ERR("سفارش‌گیری موقتاً متوقفه؛ کمی بعد دوباره امتحان کن.")
    key = b.get("service")
    if key not in await g("enabled_services")():
        return ERR("این سرویس الان فعال نیست.")
    links = g("extract_links")("\n".join(str(x) for x in (b.get("links") or [])))
    if not links:
        return ERR("لینک معتبری پیدا نشد. شکلش باید https://t.me/channel/123 باشه (کانال عمومی).")
    qty = g("to_int")(str(b.get("qty", "")))
    lo, hi = await g("qty_limits")(key)
    if qty is None or not lo <= qty <= hi:
        return ERR(f"تعداد باید بین {lo:,} و {hi:,} باشه.")
    res = await asyncio.gather(*(g("check_link")(g("BOT"), l) for l in links))
    bad = [(l, r) for l, r in zip(links, res) if r]
    if bad:
        return ERR(f"مشکل لینک: {bad[0][0]} ← {bad[0][1]}")
    prov = g("SERVICES")[key]["provider"]
    async with g("_locks").setdefault(uid, asyncio.Lock()):
        await _sync_open(await many("SELECT * FROM orders WHERE settled=0 AND provider_order<>'' AND link = ANY(?)", (links,)))
        busy = {r["link"] for r in await many(
            "SELECT DISTINCT link FROM orders WHERE settled=0 AND COALESCE(kind,'view')='view' AND link = ANY(?)", (links,))}
        links = [l for l in links if l not in busy]
        if not links:
            return ERR("برای این پست‌ها هنوز یه سفارش در حال انجامه؛ بعد از تکمیلش دوباره امتحان کن.")
        link_cost = math.ceil(qty * await g("svc_price")(key) / 1000)
        total = link_cost * len(links)
        bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
        if bal < total:
            return ERR("موجودی کافی نیست.", need=total - bal)
        done = fail = unk = 0
        for i, link in enumerate(links):
            if i:
                await asyncio.sleep(0.5)
            oid = await g("reserve_order")(uid, link, qty, link_cost, "view", None, 1, None, prov)
            if oid is None:
                fail += 1
                continue
            st, info = await g("place_order")(oid, link, qty, "view", None, {}, prov)
            if st == "ok":
                done += 1
            elif st == "unknown":
                unk += 1
                await g("notify_admins")(g("BOT"), f"🚨 <b>سفارش نامشخص #{info}</b> (کاربر <code>{uid}</code>، مینی‌اپ)\n{link} | {qty:,} سین\n"
                                         f"اگه توی پنل provider ثبت شده: /resolve {info} شماره_سفارش_provider\nاگه ثبت نشده: /refund {info}")
            else:
                fail += 1
                await g("notify_admins")(g("BOT"), f"⚠️ خطا در ثبت سفارش (کاربر {uid}، مینی‌اپ):\n{str(info)[:300]}")
    return {"done": done, "failed": fail, "unknown": unk, "skipped": len(busy)}


async def _t(t):
    if not t:
        return None
    card = (await g("get_setting")("card")).strip().split("\n", 1)
    return {"id": t["id"], "status": t["status"], "amount": t["unique_amount"], "base": t["base"],
            "credit": t["unique_amount"] if g("CREDIT_FULL") else t["base"], "left": max(0, (t["expires"] - int(time.time())) // 60),
            "card": card[0], "holder": card[1] if len(card) > 1 else ""}


async def topup_state(uid, u, b):
    return {"topup": await _t(await g("get_open_topup")(uid))}


async def topup_create(uid, u, b):
    base = g("to_int")(str(b.get("amount", "")))
    if base is None or base < g("MIN_TOPUP"):
        return ERR(f"حداقل شارژ {g('MIN_TOPUP'):,} تومانه.")
    t = await g("get_open_topup")(uid)
    if not t:
        tid = await g("alloc_topup")(uid, base)
        if tid is None:
            return ERR("الان ظرفیت پر شده؛ چند دقیقه دیگه دوباره امتحان کن.")
        t = await g("one")("SELECT * FROM topups WHERE id=?", (tid,))
    return {"topup": await _t(t)}


async def topup_paid(uid, u, b):
    one, db = g("one"), g("db")
    tid, now = int(b.get("id") or 0), int(time.time())
    t0 = await one("SELECT * FROM topups WHERE id=? AND user_id=? AND status='pending'", (tid, uid))
    if t0 and t0["expires"] <= now:
        clash = await one("SELECT 1 AS x FROM topups WHERE unique_amount=? AND id<>? AND (status IN ('claimed','awaiting') "
                          "OR (status='pending' AND expires>?)) LIMIT 1", (t0["unique_amount"], tid, now))
        if clash:
            await db.execute("UPDATE topups SET status='expired' WHERE id=? AND status='pending'", (tid,))
            return ERR("مهلت این درخواست تموم شده؛ یه درخواست شارژ جدید بزن.")
    cur = await db.execute("UPDATE topups SET status='awaiting', expires=? WHERE id=? AND user_id=? AND status='pending'",
                           (now + g("TOPUP_TTL"), tid, uid))
    if cur.rowcount != 1:
        return ERR("این درخواست دیگه فعال نیست.")
    t = await one("SELECT * FROM topups WHERE id=?", (tid,))
    try:
        await g("BOT").send_message(uid, "🧾 <b>مرحله‌ی آخر: رسید رو بفرست</b>\n\nعکس رسید (یا شماره‌ی پیگیری) رو همین‌جا بفرست تا برای تایید به ادمین برسه.\n"
                                    f"💰 مبلغ واریزی تو: <code>{t['unique_amount']}</code> تومان")
    except Exception:
        pass
    return {"topup": await _t(t)}


async def topup_cancel(uid, u, b):
    await g("db").execute("UPDATE topups SET status='canceled' WHERE id=? AND user_id=? AND status IN ('pending','awaiting')",
                          (int(b.get("id") or 0), uid))
    return {"topup": None}


async def auto_list(uid, u, b):
    if not await g("auto_allowed")(uid):
        return {"allowed": False}
    rows = await g("many")("SELECT * FROM auto_channels WHERE user_id=? ORDER BY id", (uid,))
    me_ = await g("BOT").me()
    cap = int(await g("get_opt")("auto_daily_cap", "10"))
    SV, PT = g("SERVICES"), g("PAUSE_TEXT")
    return {"allowed": True, "bot": me_.username, "cap": cap, "channels": [
        {"id": r["id"], "username": r["username"], "qty": r["qty"], "service": _s(SV.get(r["service"], {}).get("name", "")),
         "enabled": bool(r["enabled"]), "reason": PT.get(r["paused_reason"], "خاموش")} for r in rows]}


async def auto_add(uid, u, b):
    if not await g("auto_allowed")(uid):
        return ERR("این قابلیت فعلاً فعال نیست.")
    mm = re.search(r"(?:@|https?://t\.me/)?([A-Za-z][A-Za-z0-9_]{3,})", str(b.get("channel", "")).strip())
    if not mm:
        return ERR("آیدی کانال معتبر نیست؛ مثل @mychannel بنویس.")
    BOT = g("BOT")
    try:
        chat = await BOT.get_chat(f"@{mm.group(1)}")
    except Exception:
        return ERR("کانال پیدا نشد یا عمومی نیست.")
    if chat.type != "channel":
        return ERR("این آیدی مربوط به یه کانال نیست.")
    me_ = await BOT.me()
    try:
        bm = await BOT.get_chat_member(chat.id, me_.id)
    except Exception:
        bm = None
    if not bm or bm.status not in ("administrator", "creator"):
        return ERR(f"ربات هنوز توی کانال ادمین نیست؛ @{me_.username} رو ادمین کن و دوباره امتحان کن.")
    try:
        um = await BOT.get_chat_member(chat.id, uid)
    except Exception:
        um = None
    if not um or um.status not in ("administrator", "creator"):
        return ERR("فقط ادمین یا مالک کانال می‌تونه سین خودکار رو فعال کنه.")
    if await g("one")("SELECT 1 AS x FROM auto_channels WHERE chat_id=?", (chat.id,)):
        return ERR("این کانال قبلاً ثبت شده.")
    key = b.get("service")
    if key not in await g("enabled_services")():
        return ERR("این سرویس الان فعال نیست.")
    lo, hi = await g("qty_limits")(key)
    qty = g("to_int")(str(b.get("qty", "")))
    if qty is None or not lo <= qty <= hi:
        return ERR(f"تعداد باید بین {lo:,} و {hi:,} باشه.")
    cap = int(await g("get_opt")("auto_daily_cap", "10"))
    cur = await g("db").execute(
        "INSERT INTO auto_channels(chat_id,username,user_id,qty,service,daily_cap,enabled,created) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (chat_id) DO NOTHING",
        (chat.id, chat.username or mm.group(1), uid, qty, key, cap, 1, int(time.time())))
    if cur.rowcount != 1:
        return ERR("این کانال قبلاً ثبت شده.")
    return {"ok": True}


async def _own(uid, b):
    return await g("one")("SELECT * FROM auto_channels WHERE id=? AND user_id=?", (int(b.get("id") or 0), uid))


async def auto_toggle(uid, u, b):
    r = await _own(uid, b)
    if not r:
        return ERR("پیدا نشد.")
    await g("db").execute("UPDATE auto_channels SET enabled=?, paused_reason=NULL WHERE id=?", (0 if r["enabled"] else 1, r["id"]))
    return {"ok": True}


async def auto_delete(uid, u, b):
    r = await _own(uid, b)
    if not r:
        return ERR("پیدا نشد.")
    await g("db").execute("DELETE FROM auto_channels WHERE id=?", (r["id"],))
    return {"ok": True}


async def react_order(uid, u, b):
    one, many = g("one"), g("many")
    if not g("REACT_ENABLED"):
        return ERR("ریکشن فعلاً در دسترس نیست.")
    if await g("is_maintenance")() and uid not in g("ADMIN_IDS"):
        return ERR("سفارش‌گیری موقتاً متوقفه؛ کمی بعد دوباره امتحان کن.")
    emoji = b.get("emoji")
    if emoji not in g("EMOJIS"):
        return ERR("ریکشن انتخاب‌شده معتبر نیست.")
    links = g("extract_links")("\n".join(str(x) for x in (b.get("links") or [])))
    if not links:
        return ERR("لینک معتبری پیدا نشد. شکلش باید https://t.me/channel/123 باشه (کانال عمومی).")
    lo, hi = g("REACT_MIN"), g("REACT_MAX")
    qty = g("to_int")(str(b.get("qty", "")))
    if qty is None or not lo <= qty <= hi:
        return ERR(f"تعداد باید بین {lo:,} و {hi:,} باشه.")
    res = await asyncio.gather(*(g("check_link")(g("BOT"), l) for l in links))
    bad = [(l, r) for l, r in zip(links, res) if r]
    if bad:
        return ERR(f"مشکل لینک: {bad[0][0]} ← {bad[0][1]}")
    busy = {r["link"] for r in await many(
        "SELECT DISTINCT link FROM orders WHERE settled=0 AND kind='reaction' AND emoji=? AND link = ANY(?)", (emoji, links))}
    links = [l for l in links if l not in busy]
    if not links:
        return ERR("برای این پست‌ها هنوز یه سفارش با همین ریکشن در حال انجامه؛ بعد از تکمیلش دوباره امتحان کن.")
    link_cost = math.ceil(qty * int(await g("react_price")()) / 100)
    total = link_cost * len(links)
    async with g("_locks").setdefault(uid, asyncio.Lock()):
        bal = (await one("SELECT balance FROM users WHERE id=?", (uid,)))["balance"]
        if bal < total:
            return ERR("موجودی کافی نیست.", need=total - bal)
        done = fail = unk = 0
        for i, link in enumerate(links):
            if i:
                await asyncio.sleep(0.5)
            oid = await g("reserve_order")(uid, link, qty, link_cost, "reaction", emoji, 1, None, "as")
            if oid is None:
                fail += 1
                continue
            st, info = await g("place_order")(oid, link, qty, "reaction", emoji, {}, "as")
            if st == "ok":
                done += 1
            elif st == "unknown":
                unk += 1
                await g("notify_admins")(g("BOT"), f"🚨 <b>سفارش ریکشن نامشخص #{info}</b> (کاربر <code>{uid}</code>، مینی‌اپ)\n{link} | {qty:,} ریکشن\n"
                                         f"اگه توی پنل provider ثبت شده: /resolve {info} شماره_سفارش_provider\nاگه ثبت نشده: /refund {info}")
            else:
                fail += 1
                await g("notify_admins")(g("BOT"), f"⚠️ خطا در ثبت سفارش ریکشن (کاربر {uid}، مینی‌اپ):\n{str(info)[:300]}")
    return {"done": done, "failed": fail, "unknown": unk, "skipped": len(busy)}


ROUTES = {f.__name__: f for f in (me, orders, ledger, order, topup_state, topup_create, topup_paid, topup_cancel,
                                       auto_list, auto_add, auto_toggle, auto_delete, react_order)}


async def api(req):
    fn = ROUTES.get(req.match_info["a"])
    u = verify(req.headers.get("X-Init", ""))
    if not fn:
        return web.json_response(ERR("not_found"), status=404)
    if not u:
        return web.json_response(ERR("این صفحه فقط داخل تلگرام کار می‌کنه."), status=401)
    uid, now = u["id"], int(time.time())
    try:
        await g("db").execute("INSERT INTO users(id,username,joined,last_seen) VALUES (?,?,?,?) "
                              "ON CONFLICT (id) DO UPDATE SET username=EXCLUDED.username, last_seen=EXCLUDED.last_seen",
                              (uid, u.get("username"), now, now))
        if uid not in g("ADMIN_IDS"):
            if (await g("one")("SELECT banned FROM users WHERE id=?", (uid,)))["banned"]:
                return web.json_response(ERR("دسترسی شما محدود شده."), status=403)
            fc, jc = g("FORCE_CHANNEL"), g("_joined_cache")
            if fc and jc.get(uid, 0) <= time.time():
                try:
                    m = await g("BOT").get_chat_member(fc, uid)
                    ok = m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", False))
                except Exception:
                    ok = True
                if not ok:
                    return web.json_response(ERR(f"اول عضو کانال {fc} شو و دوباره باز کن."), status=403)
                jc[uid] = time.time() + 300
        try:
            body = await req.json()
        except Exception:
            body = {}
        return web.json_response(await fn(uid, u, body if isinstance(body, dict) else {}))
    except Exception:
        logging.exception("miniapp api error: %s", fn.__name__)
        return web.json_response(ERR("خطای موقت؛ دوباره امتحان کن."), status=500)


async def page(_):
    return web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-store"})


async def img(req):
    d = _IMG.get(req.match_info["n"])
    if not d:
        raise web.HTTPNotFound()
    return web.Response(body=d, content_type="image/webp", headers={"Cache-Control": "public, max-age=86400"})


async def img_png(req):
    d = _PNG.get(req.match_info["n"])
    if not d:
        raise web.HTTPNotFound()
    return web.Response(body=d, content_type="image/png", headers={"Cache-Control": "no-cache"})


async def bg(_):
    return web.Response(body=_BG, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


async def anim(req):
    d = _ANIM.get(req.match_info["n"])
    if not d:
        raise web.HTTPNotFound()
    return web.Response(body=d, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


def register(app, ns):
    global LIVE
    LIVE = ns
    app.router.add_get("/app", page)
    app.router.add_get("/app/img/{n}.webp", img)
    app.router.add_get("/app/img/{n}.png", img_png)
    app.router.add_get("/app/bg.jpg", bg)
    app.router.add_get("/app/anim/{n}.jpg", anim)
    app.router.add_post("/api/{a}", api)


HTML = r"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>فروشگاه</title>
<script src="https://telegram.org/js/telegram-web-app.js" async></script>
<style>
:root{--bg:#050505;--card:#111;--pk:#ff1a8c;--tx:#f4e7e1;--mu:#9a8d89;--ok:#3ddc97}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.7 Roboto,'Segoe UI',system-ui,Tahoma,sans-serif;font-variant-numeric:lining-nums;padding:0 0 120px}
header{display:flex;align-items:center;gap:10px;padding:12px 14px;position:sticky;top:0;background:#050505ee;z-index:5;border-bottom:1px solid #222}
header img{width:40px;height:40px;border-radius:50%;mix-blend-mode:screen}header b{flex:1;font-size:16px}
.pill{background:var(--card);border:1.5px solid var(--pk);border-radius:20px;padding:3px 12px;font-size:14px;color:var(--pk);font-weight:800}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:linear-gradient(#0008,#0008),url(/app/bg.jpg?v=1) center/cover no-repeat}
#ov{position:fixed;inset:0;z-index:99;display:none;align-items:center;justify-content:center;background:#000d;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
#ov.on{display:flex}#ov.out{animation:ovo .38s forwards}
.ovc{width:min(94vw,520px);animation:ovin .7s cubic-bezier(.2,1.3,.4,1) both}
.ovc img{width:100%;display:block;border-radius:18px;animation:glow 1.5s ease-in-out infinite alternate}
.ovb{height:3px;background:#222;border-radius:3px;margin:14px 8% 0;overflow:hidden}.ovb i{display:block;height:100%;background:var(--pk);transform-origin:left;animation:bar var(--d,1.2s) linear forwards}
@keyframes ovin{from{opacity:0;transform:translateX(-60px) scale(.85) rotate(-2deg)}to{opacity:1;transform:none}}
@keyframes glow{from{box-shadow:0 0 16px #ff149355}to{box-shadow:0 0 34px #ff1493cc,0 0 90px #ff149366}}
@keyframes bar{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes ovo{to{opacity:0}}
main{padding:14px}section{display:none}section.on{display:block}
.hero{mix-blend-mode:screen;width:100%;border-radius:16px;display:block;margin-bottom:14px}
h3{margin:16px 0 8px;font-size:15px;color:var(--pk)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cd{background:var(--card);border:1.5px solid #2a2a2a;border-radius:16px;padding:14px 10px;text-align:center;position:relative;overflow:hidden}
.cd.act{border-color:var(--pk);box-shadow:0 0 14px #ff1a8c44}.cd .e{font-size:30px}.cd small{color:var(--mu);display:block}
.cd img{width:100%;margin-top:6px;mix-blend-mode:screen}.cd img.ic{width:88%;display:block;margin:0 auto}.hd{display:block;width:62%;margin:0 auto 6px;mix-blend-mode:screen}.hg{display:block;width:70px;margin:4px auto;mix-blend-mode:screen}.cd img.b{width:72%;display:block;margin:6px auto 0}
.box{background:var(--card);border:1.5px solid #2a2a2a;border-radius:16px;padding:14px;margin-bottom:12px}
label{display:block;margin:8px 0 4px;color:var(--mu);font-size:13px}
input,textarea{width:100%;background:#000;color:var(--tx);border:1.5px solid #333;border-radius:12px;padding:10px;font:inherit;direction:ltr;text-align:left}
input:focus,textarea:focus{outline:0;border-color:var(--pk)}
.row b{font-size:15px;font-weight:800}.btn{display:block;width:100%;border:0;border-radius:14px;padding:12px;margin-top:12px;background:var(--pk);color:#fff;font:700 15px Tahoma,sans-serif}
.btn:disabled{opacity:.4}.btn.g{background:#222;color:var(--tx)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.chip{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border:1.5px solid #333;border-radius:18px;font-size:13px;background:#000;color:var(--tx)}
.chip.on{border-color:var(--pk);color:var(--pk)}.chip img{width:28px;height:28px;object-fit:contain;mix-blend-mode:screen}
.row{display:flex;justify-content:space-between;gap:8px;padding:8px 0;border-bottom:1px solid #1e1e1e;font-size:13px}.row:last-child{border:0}
.row s{text-decoration:none;color:var(--mu);font-size:12px;display:block;direction:ltr;text-align:right;word-break:break-all}
.pos{color:var(--ok)}.neg{color:var(--pk)}.big{font-size:32px;font-weight:800;letter-spacing:.5px;color:var(--pk)}
.cp{direction:ltr;font-family:monospace;font-size:17px;background:#000;border:1.5px dashed var(--pk);border-radius:12px;padding:8px;text-align:center;margin:6px 0}
nav{position:fixed;bottom:0;right:0;left:0;display:flex;background:#0a0a0a;border-top:1px solid #222;padding:6px 0 calc(6px + env(safe-area-inset-bottom));z-index:6}
nav a{flex:1;text-align:center;font-size:11px;color:var(--mu)}nav a img{height:38px;display:block;margin:0 auto 2px;opacity:.85;mix-blend-mode:screen}
nav a.on{color:var(--pk)}nav a.on img{opacity:1}
#toast{position:fixed;top:70px;left:50%;transform:translateX(-50%);background:var(--pk);color:#fff;padding:8px 16px;border-radius:14px;z-index:9;display:none;max-width:90%;text-align:center}
.mu{color:var(--mu);font-size:13px}.xb{display:block;width:38px;height:38px;margin:0 auto 6px 0;opacity:.9;mix-blend-mode:screen}.cth{display:block;width:84px;margin:0 auto 6px;mix-blend-mode:screen}.hs{width:24px;height:24px;vertical-align:middle;margin-left:4px;mix-blend-mode:screen}
.eg{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;margin-bottom:6px}.eg span{font-size:21px;text-align:center;padding:5px 0;border:1.5px solid #333;border-radius:10px;background:#000}.eg span.on{border-color:var(--pk);box-shadow:0 0 8px #ff1a8c88}.supb{display:flex;align-items:center;gap:12px;background:var(--card);border:1.5px solid #2a2a2a;border-radius:14px;padding:8px 14px;margin-top:12px}.supb img{width:76px;height:76px;object-fit:contain;mix-blend-mode:screen}.supb b{font-size:15px}
</style></head><body>
<header><img src="/app/img/logo.webp"><b id="brand">فروشگاه</b><span class="pill" id="bal">…</span></header>
<div id="ov" class="on" onclick="ovClose()"><div class="ovc"><img id="ovi" src="data:image/jpeg;base64,@@W@@"><div class="ovb"><i></i></div></div></div>
<div id="toast"></div>
<main>
<section id="home" class="on"><img class="hero" src="/app/img/banner.webp">
<h3>خدمات سین</h3><div class="grid">
<div class="cd act" onclick="openOrder()"><img class="ic" src="/app/img/svc_sin.webp"><small id="fromp"></small><img class="b" src="/app/img/best.webp"></div>
<div class="cd act" onclick="tab('auto')"><img class="ic" src="/app/img/svc_auto.webp?v=2"><small>برای هر پست جدید</small><img class="b" src="/app/img/hot.webp?v=2"></div>
<div class="cd act" onclick="openReact()"><img class="ic" src="/app/img/svc_react.webp"><small id="fromr"></small><img class="b" src="/app/img/cheap.webp?v=1"></div>
<div class="cd"><img class="ic" src="/app/img/svc_last.webp"><img src="/app/img/soon.webp"></div>
<div class="cd"><img class="ic" src="/app/img/svc_boost.webp"><img src="/app/img/soon.webp"></div></div>
<p class="mu">تا آماده شدن بقیه‌ی خدمات، از منوی ربات (/start ← منوی کامل ربات) استفاده کن.</p></section>

<section id="order"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><div class="box"><img class="cth" src="/app/img/cart.png?v=2"><b>ثبت سفارش سین</b>
<div class="chips" id="svcs"></div><div class="mu" id="sdesc"></div>
<label>لینک پست‌ها (هر لینک توی یه خط)</label><textarea id="links" rows="4" placeholder="https://t.me/channel/123"></textarea>
<label>تعداد سین برای هر پست</label><input id="qty" inputmode="numeric" placeholder="1000">
<div class="chips" id="qc"></div>
<div class="row" style="margin-top:10px"><span>تعداد پست</span><b id="nl">۰</b></div>
<div class="row"><span>قیمت هر ۱۰۰۰</span><b id="pp"></b></div>
<div class="row"><span>مبلغ قابل پرداخت</span><b id="tot" style="color:var(--pk)">۰</b></div>
<button class="btn" id="go" onclick="submitOrder()">ثبت سفارش</button>
<button class="btn g" onclick="tab('home')">بازگشت</button></div></section>

<section id="orders"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><h3>سفارش‌های من</h3><div class="box" id="ol"></div></section>

<section id="wallet"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><img class="hd" src="/app/img/wallet.webp"><div class="box"><span class="mu">موجودی کیف پول</span><div class="big" id="wb"></div></div>
<div id="tp"></div><div class="box" id="chg"><b>شارژ کیف پول</b>
<div class="chips" id="pre"></div><label>یا مبلغ دلخواه (تومان)</label><input id="amt" inputmode="numeric" placeholder="50000">
<button class="btn" onclick="mkTopup()">ادامه و دریافت شماره کارت</button></div>
<h3>تراکنش‌های اخیر</h3><div class="box" id="lg"></div></section>

<section id="react"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><h3>ثبت ریکشن</h3>
<div class="box"><label>ریکشن مورد نظر</label><div class="eg" id="rg"></div>
<label>لینک پست‌ها (هر لینک توی یه خط)</label><textarea id="rl" rows="4" placeholder="https://t.me/channel/123" oninput="rcalc()"></textarea>
<label>تعداد ریکشن برای هر پست</label><input id="rq" inputmode="numeric" placeholder="50" oninput="rcalc()"><div class="chips" id="rqc"></div>
<div class="row" style="margin-top:10px"><span>تعداد پست</span><b id="rn">0</b></div><div class="row"><span>قیمت هر ۱۰۰ ریکشن</span><b id="rp"></b></div>
<div class="row"><span>مبلغ قابل پرداخت</span><b id="rt" style="color:var(--pk)">0</b></div>
<button class="btn" id="rgo" onclick="submitReact()">ثبت سفارش</button></div></section>

<section id="auto"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><h3>سین خودکار</h3><div id="au"></div></section>

<section id="acct"><img class="xb" src="/app/img/x.png?v=2" onclick="tab('home')"><div class="box"><span class="mu">نام</span><div id="an"></div><span class="mu">شناسه</span><div id="ai" style="direction:ltr;text-align:right"></div>
<span class="mu">تعداد سفارش‌ها</span><div id="ac"></div></div>
<div class="supb" onclick="sup()"><img src="/app/img/support.png?v=2"><div><b>پشتیبانی</b><span class="mu" style="display:block">پیام به پشتیبان در تلگرام</span></div></div><button class="btn g" onclick="tab('wallet')">شارژ کیف پول</button></section>
</main>
<nav><a data-t="home"><img src="/app/img/nav_home.png?v=3">خانه</a><a data-t="orders"><img src="/app/img/nav_cart.png?v=3">سفارش‌ها</a>
<a data-t="wallet"><img src="/app/img/wallet_ic.png?v=2">کیف پول</a><a data-t="acct"><img src="/app/img/user.png?v=2">حساب</a></nav>
<script>
const $=s=>document.querySelector(s),fa=n=>Number(n).toLocaleString('en-US'),E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let tg=null,S={},sv=null,tmr,lastBal=null,ovq=[],ovOn=true,ovT;
function ovShow(k,ms){ovq.push([k,ms]);if(!ovOn)ovNext()}
function ovNext(){const n=ovq.shift();if(!n){ovOn=false;return}ovOn=true;const o=$('#ov');if(!o)return;o.classList.remove('out');$('#ovi').src='/app/anim/'+n[0]+'.jpg?v=1';o.style.setProperty('--d',n[1]+'ms');
[$('.ovc'),$('.ovb i')].forEach(e=>{e.style.animation='none';void e.offsetWidth;e.style.animation=''});o.classList.add('on');clearTimeout(ovT);ovT=setTimeout(ovClose,n[1])}
function ovClose(){clearTimeout(ovT);const o=$('#ov');if(!o||!o.classList.contains('on')||o.classList.contains('out'))return;o.classList.add('out');setTimeout(()=>{o.classList.remove('on','out');ovNext()},380)}
async function chkCharge(){const l=await api('ledger'),t=l.items&&l.items[0];if(t&&t.amount>0&&/شارژ/.test(t.label))ovShow('charge',3400)}
const init=()=>(window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData)||new URLSearchParams(location.hash.slice(1)).get('tgWebAppData')||'';
async function api(a,b){try{const r=await fetch('/api/'+a,{method:'POST',headers:{'X-Init':init(),'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok&&!j.error)j.error='خطا';return j}catch(e){return{error:'اتصال برقرار نشد'}}}
function toast(t){const e=$('#toast');e.textContent=t;e.style.display='block';clearTimeout(tmr);tmr=setTimeout(()=>e.style.display='none',3500)}
function tab(t){document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===t));document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.t===t));scrollTo(0,0);if(t==='orders')loadOrders();if(t==='wallet')loadWallet();if(t==='order')calc();if(t==='auto')loadAuto()}
document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>tab(a.dataset.t));
async function load(q){const j=await api('me');if(j.error){if(q)return;document.body.innerHTML='<p style="padding:30px;text-align:center">'+E(j.error)+'</p>';return}
let old=lastBal;if(old===null){try{const v=parseInt(localStorage.getItem('lb'));old=isNaN(v)?null:v}catch(e){}}
lastBal=j.balance;try{localStorage.setItem('lb',j.balance)}catch(e){}if(old!==null&&j.balance>old)chkCharge();
S=j;$('#brand').textContent=j.brand||'فروشگاه';$('#bal').textContent=fa(j.balance)+' تومان';$('#wb').textContent=fa(j.balance)+' تومان';
$('#an').textContent=j.name;$('#ai').textContent=j.id;$('#ac').textContent=fa(j.orders);
if(j.react)$('#fromr').textContent='از '+fa(j.react.price)+' تومان / ۱۰۰';
if(j.services.length)$('#fromp').textContent='از '+fa(Math.min(...j.services.map(s=>s.price)))+' تومان / ۱۰۰۰';
if(!sv||!j.services.find(s=>s.key===sv.key))sv=j.services[0]||null}
function openOrder(){if(S.maint)return toast('سفارش‌گیری موقتاً متوقفه است');if(!sv)return toast('سرویسی فعال نیست');
$('#svcs').innerHTML=S.services.map(s=>`<span class="chip ${s.key===sv.key?'on':''}" onclick="pick('${s.key}')">${['fast','eco'].includes(s.key)?`<img src="/app/img/${s.key}.png?v=2">`:''}${E(String(s.name).replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]/gu,'').trim())}</span>`).join('');
$('#qc').innerHTML=[1000,5000,10000].filter(q=>q>=sv.min&&q<=sv.max).map(q=>`<span class="chip" onclick="$('#qty').value=${q};calc()">${fa(q)}</span>`).join('');
$('#sdesc').textContent=sv.desc;tab('order')}

let rem=null;
function rlinks(){return [...new Set($('#rl').value.match(/(?:https?:\/\/)?(?:www\.)?t\.me\/[A-Za-z][A-Za-z0-9_]{3,}\/\d+/g)||[])]}
function openReact(){if(!S.react)return toast('ریکشن فعلاً در دسترس نیست');const r=S.react;
$('#rg').innerHTML=r.emojis.map(e=>`<span data-e="${e}" onclick="rpick(this)">${e}</span>`).join('');
$('#rqc').innerHTML=[10,50,100,500].filter(q=>q>=r.min&&q<=r.max).map(q=>`<span class="chip" onclick="$('#rq').value=${q};rcalc()">${fa(q)}</span>`).join('');
rem=null;tab('react');rcalc()}
function rpick(el){document.querySelectorAll('#rg span').forEach(x=>x.classList.remove('on'));el.classList.add('on');rem=el.dataset.e;rcalc()}
function rcalc(){const r=S.react;if(!r)return;const n=rlinks().length,q=parseInt(($('#rq').value||'').replace(/[^0-9]/g,''))||0,ok=rem&&n&&q>=r.min&&q<=r.max,t=Math.ceil(q*r.price/100)*n;
$('#rn').textContent=fa(n);$('#rp').textContent=fa(r.price)+' تومان';$('#rt').textContent=fa(t)+' تومان';
$('#rgo').disabled=!ok;$('#rgo').textContent=!rem?'یه ریکشن انتخاب کن':!n?'لینک پست رو بنویس':!q?'تعداد رو بنویس':!ok?'تعداد: '+fa(r.min)+' تا '+fa(r.max):t>S.balance?'موجودی کافی نیست؛ شارژ کن':'ثبت سفارش'}
async function submitReact(){const r=S.react,q=parseInt($('#rq').value.replace(/[^0-9]/g,''))||0,n=rlinks().length,t=Math.ceil(q*r.price/100)*n;
if(t>S.balance)return tab('wallet');$('#rgo').disabled=true;$('#rgo').textContent='در حال ثبت...';
const j=await api('react_order',{emoji:rem,links:rlinks(),qty:q});rcalc();if(j.error)return toast(j.error);
toast(j.done?fa(j.done)+' سفارش ثبت شد'+(j.failed?' | '+fa(j.failed)+' ناموفق (پول برگشت)':''):j.unknown?'سفارش در حال بررسیه':'ثبت نشد؛ پولی کم نشد');
$('#rl').value='';$('#rq').value='';await load();tab('orders')}
let asv=null;
const noEm=s=>String(s).replace(/[\u{1F000}-\u{1FAFF}\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d]/gu,'').trim();
function acost(){if(!asv)return;const q=parseInt(($('#aq').value||'').replace(/[^0-9]/g,''))||0;$('#ac1').textContent=fa(Math.ceil(q*asv.price/1000))+' تومان'}
function apick(k){asv=S.services.find(s=>s.key===k);asvc();acost()}
function asvc(){$('#asvc').innerHTML=S.services.length>1?S.services.map(s=>`<span class="chip ${s.key===asv.key?'on':''}" onclick="apick('${s.key}')">${['fast','eco'].includes(s.key)?`<img src="/app/img/${s.key}.png?v=2">`:''}${E(noEm(s.name))}</span>`).join(''):''}
async function loadAuto(){const el=$('#au');el.innerHTML='<span class="mu">در حال بارگذاری…</span>';const j=await api('auto_list');
if(j.error){el.innerHTML=E(j.error);return}
if(!j.allowed){el.innerHTML='<div class="box" style="text-align:center"><img class="cth" style="width:200px" src="/app/img/soon.webp"><p class="mu">این قابلیت به‌زودی فعال می‌شه</p></div>';return}
if(!asv||!S.services.find(s=>s.key===asv.key))asv=S.services[0]||null;
if(!asv){el.innerHTML='<div class="box mu">سرویس سین الان فعال نیست</div>';return}
const ch=j.channels.map(c=>`<div class="row"><div><b dir="ltr">@${E(c.username)}</b><span class="mu" style="display:block">${fa(c.qty)} سین برای هر پست | ${E(c.service)}</span><span class="mu" style="display:block">${c.enabled?'فعال':'متوقف: '+E(c.reason)}</span></div><div style="text-align:left"><span class="chip" onclick="autoTg(${c.id})">${c.enabled?'توقف':'روشن کردن'}</span> <span class="chip" onclick="autoDel(${c.id})">حذف</span></div></div>`).join('')||'<span class="mu">هنوز کانالی اضافه نکردی</span>';
el.innerHTML=`<div class="box"><span class="mu">هر پست جدیدی که توی کانالت بذاری خودکار سین می‌خوره. فقط پست‌های جدید حساب می‌شن و هزینه‌ی هر پست از کیف پولت کم می‌شه؛ اگه موجودی تموم بشه متوقف می‌شه.</span></div>
<h3>کانال‌های تو</h3><div class="box">${ch}</div>
<h3>افزودن کانال</h3><div class="box"><span class="mu">اول ربات <b dir="ltr">@${E(j.bot)}</b> رو توی کانالت ادمین کن (دسترسی خاصی لازم نیست)، بعد آیدی کانال عمومی رو بنویس. فقط ادمین یا مالک کانال می‌تونه فعال کنه.</span>
<label>آیدی کانال</label><input id="ach" placeholder="@mychannel"><div class="chips" id="asvc"></div>
<label>تعداد سین برای هر پست جدید</label><input id="aq" inputmode="numeric" placeholder="1000" oninput="acost()">
<div class="row" style="margin-top:10px"><span>هزینه‌ی هر پست</span><b id="ac1">0</b></div><div class="row"><span>حداکثر پست در روز</span><b>${fa(j.cap)}</b></div>
<button class="btn" onclick="autoAdd()">فعال‌سازی سین خودکار</button></div>`;asvc();acost()}
async function autoAdd(){const j=await api('auto_add',{channel:$('#ach').value,service:asv.key,qty:$('#aq').value});if(j.error)return toast(j.error);toast('سین خودکار فعال شد');loadAuto()}
async function autoTg(id){const j=await api('auto_toggle',{id});if(j.error)toast(j.error);loadAuto()}
async function autoDel(id){const j=await api('auto_delete',{id});if(j.error)toast(j.error);loadAuto()}
function pick(k){sv=S.services.find(s=>s.key===k);openOrder()}
function links(){return $('#links').value.match(/(?:https?:\/\/)?(?:www\.)?t\.me\/[A-Za-z][A-Za-z0-9_]{3,}\/\d+/g)||[]}
function calc(){if(!sv)return;const n=new Set(links()).size,q=parseInt(($('#qty').value||'').replace(/[^0-9]/g,''))||0,ok=q>=sv.min&&q<=sv.max&&n>0,t=Math.ceil(q*sv.price/1000)*n;
$('#nl').textContent=fa(n);$('#pp').textContent=fa(sv.price)+' تومان';$('#tot').textContent=fa(t)+' تومان';
$('#go').disabled=!ok;$('#go').textContent=!n?'لینک پست رو بنویس':!q?'تعداد رو بنویس':!ok?'تعداد: '+fa(sv.min)+' تا '+fa(sv.max):t>S.balance?'موجودی کافی نیست؛ شارژ کن':'ثبت سفارش'}
$('#links').oninput=$('#qty').oninput=calc;
async function submitOrder(){const q=parseInt($('#qty').value.replace(/[^0-9]/g,''))||0,n=new Set(links()).size,t=Math.ceil(q*sv.price/1000)*n;
if(t>S.balance)return tab('wallet');$('#go').disabled=true;$('#go').textContent='در حال ثبت...';
const j=await api('order',{service:sv.key,links:links(),qty:q});calc();if(j.error)return toast(j.error);
if(j.done)ovShow('order',3400);toast(j.done?`${fa(j.done)} سفارش ثبت شد`+(j.failed?` | ${fa(j.failed)} ناموفق (پول برگشت)`:''):j.unknown?'سفارش در حال بررسیه':'ثبت نشد؛ پولی کم نشد');
$('#links').value='';$('#qty').value='';await load();tab('orders')}
async function loadOrders(q){if(!q)$('#ol').innerHTML='<span class="mu">در حال بارگذاری…</span>';const j=await api('orders');
$('#ol').innerHTML=j.error?E(j.error):(j.orders.length?j.orders.map(o=>`<div class="row"><div><b>#${fa(o.id)}</b> ${E(o.label)}<s>${E(o.link)}</s></div><div style="text-align:left">${/صف|پردازش|بررسی|انتظار|در حال/.test(o.status)?'<img class="hs" src="/app/img/hourglass.png?v=2">':''}${E(o.status)}<span class="mu" style="display:block">${E(o.date)}</span></div></div>`).join(''):'<span class="mu">هنوز سفارشی نداری </span>')}
function tpHtml(t){if(!t){$('#tp').innerHTML='';$('#chg').style.display='block';return}$('#chg').style.display='none';
const hg='<img class="hg" src="/app/img/hourglass.png?v=2">',st=t.status==='claimed'?hg+'<p>رسیدت برای ادمین ارسال شد؛ بعد از تایید کیف پولت شارژ می‌شه.</p>':t.status==='awaiting'?hg+'<p>رسید (عکس یا شماره‌ی پیگیری) رو توی چت ربات بفرست.</p><button class="btn" onclick="closeApp()">رفتن به چت ربات</button>':
`<p>دقیقاً این مبلغ رو کارت‌به‌کارت کن (رقم‌های آخر هم مهمه):</p><div class="cp" onclick="cpy('${t.amount}')">${t.amount}</div><p class="mu">شماره کارت:</p><div class="cp" onclick="cpy('${E(t.card)}')">${E(t.card)}</div><p class="mu">${E(t.holder)}</p><p class="mu">مبلغی که به کیف پولت اضافه می‌شه: <b>${fa(t.credit)}</b> تومان | مهلت: ${fa(t.left)} دقیقه</p>
<button class="btn" onclick="paid(${t.id})">پرداخت کردم</button><button class="btn g" onclick="cancelT(${t.id})">لغو</button>`;
$('#tp').innerHTML='<div class="box"><b>پرداخت کارت‌به‌کارت</b>'+st+(t.status==='claimed'?'':'')+'</div>'}
function cpy(x){navigator.clipboard&&navigator.clipboard.writeText(x).then(()=>toast('کپی شد '),()=>{})}
function closeApp(){window.Telegram&&Telegram.WebApp?Telegram.WebApp.close():toast('به چت ربات برگرد')}
async function loadWallet(){$('#pre').innerHTML=(S.presets||[]).map(p=>`<span class="chip" onclick="$('#amt').value=${p}">${fa(p)}</span>`).join('');
const [t,l]=await Promise.all([api('topup_state'),api('ledger')]);tpHtml(t.topup);
$('#lg').innerHTML=l.error?E(l.error):(l.items.length?l.items.map(i=>`<div class="row"><div>${E(i.label)}<span class="mu" style="display:block">${E(i.date)}</span></div><b class="${i.amount>0?'pos':'neg'}" dir="ltr">${i.amount>0?'+':''}${fa(i.amount)}</b></div>`).join(''):'<span class="mu">هنوز تراکنشی نداری </span>')}
async function mkTopup(){const j=await api('topup_create',{amount:$('#amt').value});if(j.error)return toast(j.error);tpHtml(j.topup)}
async function paid(id){const j=await api('topup_paid',{id});if(j.error){toast(j.error);return loadWallet()}tpHtml(j.topup);toast('رسید رو توی چت ربات بفرست')}
async function cancelT(id){await api('topup_cancel',{id});loadWallet()}
function sup(){if(!S.support)return toast('پشتیبانی هنوز تنظیم نشده');const u='https://t.me/'+S.support;window.Telegram&&Telegram.WebApp?Telegram.WebApp.openTelegramLink(u):location.href=u}
addEventListener('load',()=>{try{Telegram.WebApp.ready();Telegram.WebApp.expand();Telegram.WebApp.setHeaderColor('#050505');Telegram.WebApp.setBackgroundColor('#050505')}catch(e){}});
const t0=Date.now();setTimeout(ovClose,9000);
load().then(()=>{tab('home');setTimeout(ovClose,Math.max(0,1200-(Date.now()-t0)));['order','charge'].forEach(k=>{new Image().src='/app/anim/'+k+'.jpg?v=1'})});
setInterval(()=>{if(!document.hidden){load(true);if($('#orders').classList.contains('on'))loadOrders(true)}},15000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)load(true)});
</script></body></html>"""

HTML = HTML.replace("@@W@@", ANIM_JPG["welcome"], 1)
