import os
import sys
import logging
import asyncio

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

from pyrogram import Client as PyroClient, filters as pyro_filters
from pyrogram.handlers import MessageHandler as PyroMsgHandler
from pyrogram.errors import (
    UsernameNotOccupied, UsernameInvalid, PeerIdInvalid, FloodWait,
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler
)

import database as db

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
ADMIN_ID  = int(os.environ.get("ADMIN_CHAT_ID", "8101656671"))

pyro = PyroClient("lookup", api_id=API_ID, api_hash=API_HASH,
                  bot_token=BOT_TOKEN, in_memory=True)
user_client: PyroClient | None = None

_tele_queue: asyncio.Queue = asyncio.Queue()
_tele_lock:  asyncio.Lock  = asyncio.Lock()

WAIT_PHONE, WAIT_OTP, WAIT_2FA = range(3)
_auth_tmp: dict = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_date(dt_str):
    try:
        from datetime import datetime
        return datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%y")
    except Exception:
        return str(dt_str)[:10]

def esc(t) -> str:
    return str(t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ تەنها ئادمین.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

async def get_bot_username(ctx) -> str:
    me = await ctx.bot.get_me()
    return me.username or ""

async def auto_register(u) -> bool:
    """یوزەر خۆکاری تۆمار بکە، True ئەگەر نوێ بوو."""
    return await db.register_user(
        u.id, u.first_name or "", u.last_name or "", u.username or "",
        getattr(u, "is_premium", False) or False
    )


# ─── Forced join ──────────────────────────────────────────────────────────────

async def check_membership(uid: int) -> list:
    channels = await db.get_required_channels()
    missing = []
    for ch_id, ch_uname, ch_title in channels:
        try:
            m = await pyro.get_chat_member(ch_id, uid)
            if m.status.name in ("BANNED","LEFT","RESTRICTED"):
                missing.append((ch_id, ch_uname, ch_title))
        except Exception:
            missing.append((ch_id, ch_uname, ch_title))
    return missing

async def join_keyboard(missing: list) -> InlineKeyboardMarkup:
    btns = []
    for ch_id, ch_uname, ch_title in missing:
        url = f"https://t.me/{ch_uname}" if ch_uname else f"tg://resolve?domain={ch_id}"
        btns.append([InlineKeyboardButton(f"📢 {ch_title or ch_uname}", url=url)])
    btns.append([InlineKeyboardButton("✅ جۆینم کردووە — چەک بکە", callback_data="check_join")])
    return InlineKeyboardMarkup(btns)


# ─── OSINT helpers ────────────────────────────────────────────────────────────

async def do_lookup(query: str):
    try:
        q = query.strip().lstrip("@")
        target = int(q) if q.lstrip("-").isdigit() else q
        return await pyro.get_chat(target), None
    except UsernameNotOccupied:
        return None, "❌ ئەم یوزەرنەیمە بوونی نییە."
    except UsernameInvalid:
        return None, "❌ یوزەرنەیمەکە هەڵەیە."
    except PeerIdInvalid:
        return None, "❌ ئەم ئایدییە نەدۆزرایەوە."
    except FloodWait as e:
        return None, f"⏳ {e.value} چرکە چاوەڕێ بکە."
    except Exception as e:
        return None, f"❌ نەدۆزرایەوە: {esc(str(e))}"

def build_result(chat, name_hist, uname_hist) -> str:
    first = getattr(chat,'first_name',None) or getattr(chat,'title',None) or ""
    last  = getattr(chat,'last_name',None) or ""
    full  = f"{first} {last}".strip() or "—"
    uname = getattr(chat,'username',None)
    uid   = getattr(chat,'id',None)
    bio   = getattr(chat,'bio',None) or getattr(chat,'description',None)
    prem  = getattr(chat,'is_premium',False) or False
    veri  = getattr(chat,'is_verified',False) or False
    isbot = getattr(chat,'is_bot',False) or False
    scam  = getattr(chat,'is_scam',False) or False
    phone = getattr(chat,'phone_number',None)

    link    = f"https://t.me/{uname}" if uname else f"tg://user?id={uid}"
    uname_d = f"@{uname}" if uname else "—"

    L = [f"<b>{esc(full)}</b>"]
    if uname: L.append(f"<code>@{esc(uname)}</code>")
    L += ["",
          f"🆔 <b>ID:</b> <code>{uid}</code>",
          f"👤 <b>Name:</b> {esc(full)}"]
    if phone: L.append(f"📱 <b>Phone:</b> <code>{esc(phone)}</code>")
    L.append(f"🔗 <b>Link:</b> <a href='{link}'>{esc(uname_d)}</a>")
    if bio:
        L.append(f"📝 <b>Bio:</b> {esc(bio[:150])}{'…' if len(bio)>150 else ''}")

    flags = []
    if prem:  flags.append("⭐ Premium")
    if veri:  flags.append("✔️ Verified")
    if isbot: flags.append("🤖 Bot")
    if scam:  flags.append("⚠️ Scam")
    if flags: L.append("  ".join(flags))

    L.append("")
    if uname_hist:
        L.append("📝 <b>Username history:</b>")
        for i,(u,d) in enumerate(uname_hist,1):
            L.append(f"  {i}. <code>@{esc(u)}</code> <i>({fmt_date(d)})</i>")
    else:
        L.append("📝 <b>Username history:</b> —")

    L.append("")
    if name_hist:
        L.append("📋 <b>Name history:</b>")
        for i,(fn,ln,d) in enumerate(name_hist,1):
            n = f"{fn or ''} {ln or ''}".strip()
            L.append(f"  {i}. <b>{esc(n)}</b> <i>({fmt_date(d)})</i>")
    else:
        L.append("📋 <b>Name history:</b> —")

    return "\n".join(L)


# ─── TeleSINT relay ────────────────────────────────────────────────────────────

TELESIN_BOT = "Telesinrobot"  # ئەگەر یوزەرنەیمەکە جیاوازە لێرە بیگۆڕە

async def _on_telesin_msg(client, message):
    log.info(f"📥 TeleSINT reply from @{getattr(message.chat,'username',None)}: {(message.text or '')[:80]!r}")
    if message.text:
        await _tele_queue.put(message.text)

def _attach_telesin_handler(uc: PyroClient):
    f = pyro_filters.create(
        lambda _, __, m: bool(
            m.chat and (m.chat.username or "").lower() == TELESIN_BOT.lower()
        )
    )
    uc.add_handler(PyroMsgHandler(_on_telesin_msg, f & pyro_filters.incoming))
    log.info(f"TeleSINT handler attached for @{TELESIN_BOT} ✅")

_TELE_SKIP = {"processing query", "processing", "please wait", "searching", "loading"}

async def ask_telesinbot(q: str) -> str | None:
    if not user_client:
        return None
    async with _tele_lock:
        # queue پاک بکەرەوە
        while not _tele_queue.empty():
            try: _tele_queue.get_nowait()
            except: pass
        try:
            await user_client.send_message("Telesinrobot", q.strip().lstrip("@"))
        except Exception as e:
            log.warning(f"TeleSINT send: {e}")
            return None
        # چاوەڕێی وەڵامی ڕاستەقینە — وەڵامی "processing" رەت دەکەینەوە
        deadline = asyncio.get_event_loop().time() + 20.0
        last_real = None
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                msg = await asyncio.wait_for(_tele_queue.get(), timeout=min(remaining, 3.0))
                if msg and msg.strip().lower() not in _TELE_SKIP:
                    last_real = msg
                    # چاوەڕێی پەیامی تر ئەگەر هەبوو (٢ چرکە)
                    try:
                        extra = await asyncio.wait_for(_tele_queue.get(), timeout=2.0)
                        if extra and extra.strip().lower() not in _TELE_SKIP:
                            last_real = extra
                    except asyncio.TimeoutError:
                        pass
                    return last_real
            except asyncio.TimeoutError:
                break
        return last_real


# ─── /genauth ─────────────────────────────────────────────────────────────────

async def genauth_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    await update.message.reply_text(
        "📱 ژمارەی موبایلەکەت بنووسە:\nنموونە: <code>+9647XXXXXXXX</code>",
        parse_mode="HTML"
    )
    return WAIT_PHONE

async def genauth_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    uid   = update.effective_user.id
    try:
        client = PyroClient(f"auth_{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await client.connect()
        sent = await client.send_code(phone)
        _auth_tmp[uid] = {"phone": phone, "hash": sent.phone_code_hash, "client": client}
        await update.message.reply_text(
            "✅ کۆد نێردرا! لە <b>Saved Messages</b> تێلێگرامت ببینە.\n\n🔢 کۆدەکە بنووسە:",
            parse_mode="HTML"
        )
        return WAIT_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        return ConversationHandler.END

async def genauth_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ","")
    uid  = update.effective_user.id
    tmp  = _auth_tmp.get(uid)
    if not tmp:
        await update.message.reply_text("❌ دووبارە /genauth")
        return ConversationHandler.END
    try:
        await tmp["client"].sign_in(tmp["phone"], tmp["hash"], code)
    except SessionPasswordNeeded:
        await update.message.reply_text("🔐 پاسۆردی 2FA بنووسە:")
        return WAIT_2FA
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await update.message.reply_text("❌ کۆد هەڵەیە/کاتی تەواو. /genauth دووبارە")
        await tmp["client"].disconnect(); _auth_tmp.pop(uid, None)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")
        await tmp["client"].disconnect(); _auth_tmp.pop(uid, None)
        return ConversationHandler.END
    return await _finish_auth(update, uid, tmp["client"])

async def genauth_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tmp = _auth_tmp.get(uid)
    if not tmp:
        await update.message.reply_text("❌ دووبارە /genauth"); return ConversationHandler.END
    try:
        await tmp["client"].check_password(update.message.text.strip())
    except Exception as e:
        await update.message.reply_text(f"❌ پاسۆرد هەڵەیە: {e}")
        await tmp["client"].disconnect(); _auth_tmp.pop(uid, None)
        return ConversationHandler.END
    return await _finish_auth(update, uid, tmp["client"])

async def _finish_auth(update: Update, uid: int, client: PyroClient):
    global user_client
    session = await client.export_session_string()
    await client.disconnect(); _auth_tmp.pop(uid, None)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".user_session"),"w") as f: f.write(session)
    if user_client:
        try: await user_client.stop()
        except: pass
    user_client = PyroClient("user", api_id=API_ID, api_hash=API_HASH,
                              session_string=session, in_memory=True)
    await user_client.start()
    _attach_telesin_handler(user_client)
    await update.message.reply_text("✅ سێشن دروست کرا! ئێستا بۆتەکەت تەواوە.")
    return ConversationHandler.END

async def genauth_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tmp = _auth_tmp.pop(uid, None)
    if tmp:
        try: await tmp["client"].disconnect()
        except: pass
    await update.message.reply_text("❌ هەڵوەشاندرایەوە.")
    return ConversationHandler.END


# ─── /start ───────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    uid  = u.id
    args = ctx.args

    is_new = await auto_register(u)

    # ڕافایل
    if is_new and args and args[0].startswith("ref_"):
        try:
            ref_by = int(args[0][4:])
            if ref_by != uid:
                import aiosqlite
                async with aiosqlite.connect(db.DB_PATH) as conn:
                    await conn.execute("UPDATE users SET ref_by=? WHERE id=? AND ref_by IS NULL",
                                       (ref_by, uid))
                    await conn.commit()
                reward = int(await db.get_setting("ref_reward","5"))
                if reward > 0:
                    await db.add_points(ref_by, reward)
                    try:
                        await ctx.bot.send_message(ref_by,
                            f"🎁 کەسێک بە لینکی ڕافایلەکەت داخڵ بوو!\n+{reward} خاڵ وەرگرتیت 🎉")
                    except: pass
        except: pass

    # جۆین چەک
    missing = await check_membership(uid)
    if missing:
        await update.message.reply_text(
            "⚠️ پێشتر ئەم کەنەڵانە جۆین بکە:",
            reply_markup=await join_keyboard(missing)
        )
        return

    pts    = await db.get_points(uid)
    refs   = await db.get_referral_count(uid)
    cost   = await db.get_user_cost(uid)
    bot_un = await get_bot_username(ctx)
    ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"

    cost_txt = f"{cost} خاڵ" if cost > 0 else "خۆڕایە"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 خاڵەکانم", callback_data="my_points"),
            InlineKeyboardButton("🔗 ڕافایلم",  callback_data="my_ref"),
        ],
        [InlineKeyboardButton("ℹ️ چۆن بەکاربهێنم", callback_data="how_to")],
    ])

    await update.message.reply_text(
        f"سڵاو <b>{esc(u.first_name)}</b>! 👋\n\n"
        f"💰 خاڵەکانت: <b>{pts}</b>\n"
        f"👥 ڕافایلەکانت: <b>{refs}</b>\n"
        f"🔍 تێچووی گەڕان: <b>{cost_txt}</b>\n\n"
        "یوزەرنەیم یان ئایدی بنووسە بۆ گەڕان 👇",
        parse_mode="HTML",
        reply_markup=kb
    )


# ─── Callbacks ────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data

    if data == "check_join":
        missing = await check_membership(uid)
        if missing:
            await q.edit_message_text("❌ هێشتا جۆینت نەکردووە:",
                                      reply_markup=await join_keyboard(missing))
        else:
            await q.edit_message_text("✅ سوپاس! ئێستا دەتوانیت بۆتەکە بەکار بهێنیت.\n\nیوزەرنەیم یان ئایدی بنووسە 👇")

    elif data == "my_points":
        pts  = await db.get_points(uid)
        refs = await db.get_referral_count(uid)
        cost = await db.get_user_cost(uid)
        cost_txt = f"{cost} خاڵ" if cost > 0 else "خۆڕایە"
        bot_un = (await ctx.bot.get_me()).username
        ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"
        await q.edit_message_text(
            f"💰 <b>خاڵەکانت:</b> {pts}\n"
            f"🔍 <b>تێچووی گەڕان:</b> {cost_txt}\n"
            f"👥 <b>ڕافایلەکانت:</b> {refs}\n\n"
            f"🔗 <b>لینکی ڕافایلت:</b>\n<code>{ref_link}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 گەڕانەوە", callback_data="back_start")
            ]])
        )

    elif data == "my_ref":
        refs  = await db.get_referral_count(uid)
        reward = await db.get_setting("ref_reward","5")
        bot_un = (await ctx.bot.get_me()).username
        ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"
        await q.edit_message_text(
            f"🔗 <b>لینکی ڕافایلت:</b>\n<code>{ref_link}</code>\n\n"
            f"👥 <b>کەسانی داخڵبووە بە لینکەکەت:</b> {refs}\n"
            f"🎁 <b>خاڵی هەر ڕافایل:</b> {reward}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 گەڕانەوە", callback_data="back_start")
            ]])
        )

    elif data == "how_to":
        await q.edit_message_text(
            "📖 <b>چۆن بەکاربهێنم؟</b>\n\n"
            "• یوزەرنەیم بنووسە: <code>@username</code>\n"
            "• یان ئایدی: <code>123456789</code>\n\n"
            "بۆتەکە ئەم زانیاریانەت دەدات:\n"
            "✅ ئایدی و ناو\n"
            "✅ هیستۆری یوزەرنەیم\n"
            "✅ هیستۆری ناو\n"
            "✅ بایۆ و لینک\n"
            "✅ زانیاری زیاتر لە TeleSINT",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 گەڕانەوە", callback_data="back_start")
            ]])
        )

    elif data == "back_start":
        pts    = await db.get_points(uid)
        refs   = await db.get_referral_count(uid)
        cost   = await db.get_user_cost(uid)
        cost_txt = f"{cost} خاڵ" if cost > 0 else "خۆڕایە"
        fn = q.from_user.first_name or ""
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💰 خاڵەکانم", callback_data="my_points"),
                InlineKeyboardButton("🔗 ڕافایلم",  callback_data="my_ref"),
            ],
            [InlineKeyboardButton("ℹ️ چۆن بەکاربهێنم", callback_data="how_to")],
        ])
        await q.edit_message_text(
            f"سڵاو <b>{esc(fn)}</b>! 👋\n\n"
            f"💰 خاڵەکانت: <b>{pts}</b>\n"
            f"👥 ڕافایلەکانت: <b>{refs}</b>\n"
            f"🔍 تێچووی گەڕان: <b>{cost_txt}</b>\n\n"
            "یوزەرنەیم یان ئایدی بنووسە بۆ گەڕان 👇",
            parse_mode="HTML",
            reply_markup=kb
        )


# ─── /points /ref ─────────────────────────────────────────────────────────────

async def points_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    await auto_register(update.effective_user)
    pts  = await db.get_points(uid)
    refs = await db.get_referral_count(uid)
    cost = await db.get_user_cost(uid)
    bot_un = await get_bot_username(ctx)
    ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"
    cost_txt = f"{cost} خاڵ" if cost > 0 else "خۆڕایە"
    await update.message.reply_text(
        f"💰 <b>خاڵەکانت:</b> {pts}\n"
        f"🔍 <b>تێچووی گەڕان:</b> {cost_txt}\n"
        f"👥 <b>ڕافایلەکانت:</b> {refs}\n\n"
        f"🔗 <b>لینکت:</b>\n<code>{ref_link}</code>",
        parse_mode="HTML", disable_web_page_preview=True
    )

async def ref_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    await auto_register(update.effective_user)
    refs  = await db.get_referral_count(uid)
    reward = await db.get_setting("ref_reward","5")
    bot_un = await get_bot_username(ctx)
    ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"
    await update.message.reply_text(
        f"🔗 <b>لینکی ڕافایلت:</b>\n<code>{ref_link}</code>\n\n"
        f"👥 <b>داخڵبووانت:</b> {refs}\n"
        f"🎁 <b>خاڵی هەر ڕافایل:</b> {reward}",
        parse_mode="HTML", disable_web_page_preview=True
    )


# ─── Admin commands ───────────────────────────────────────────────────────────

@admin_only
async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    count  = await db.get_user_count()
    cost   = await db.get_setting("default_cost","0")
    reward = await db.get_setting("ref_reward","5")
    spts   = await db.get_setting("start_points","0")
    chans  = await db.get_required_channels()
    ch_txt = "\n".join(f"  • {t or u} <code>{cid}</code>" for cid,u,t in chans) or "  نییە"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 یوزەرەکان", callback_data="adm_users"),
         InlineKeyboardButton("📢 براودکاست", callback_data="adm_broadcast")],
        [InlineKeyboardButton("⚙️ ڕێکخستنەکان", callback_data="adm_settings")],
    ])
    await update.message.reply_text(
        f"🛠 <b>پانێڵی ئادمین</b>\n\n"
        f"👥 کۆی یوزەر: <b>{count}</b>\n"
        f"💰 تێچووی گەڕان: <b>{cost}</b> خاڵ\n"
        f"🎁 خاڵی ڕافایل: <b>{reward}</b>\n"
        f"🎉 خاڵی خۆش‌هاتن: <b>{spts}</b>\n\n"
        f"📢 کەنەڵی ناچاری:\n{ch_txt}\n\n"
        "<b>فەرمانەکان:</b>\n"
        "<code>/addpoints ID مەبەڵەغ</code>\n"
        "<code>/setpoints ID مەبەڵەغ</code>\n"
        "<code>/setcost ID مەبەڵەغ</code>\n"
        "<code>/setdefcost مەبەڵەغ</code>\n"
        "<code>/setreward مەبەڵەغ</code>\n"
        "<code>/setstartpts مەبەڵەغ</code>\n"
        "<code>/addrequired @channel</code>\n"
        "<code>/removerequired channel_id</code>\n"
        "<code>/broadcast پەیام</code>",
        parse_mode="HTML", reply_markup=kb
    )

async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌"); return
    await q.answer()
    data = q.data

    if data == "adm_users":
        rows  = await db.get_users_page(limit=15)
        count = await db.get_user_count()
        lines = [f"👥 <b>یوزەرەکان ({count}):</b>"]
        for uid,fn,un,pts,ca in rows:
            un_d = f"@{esc(un)}" if un else "—"
            lines.append(f"• <code>{uid}</code> {esc(fn or '—')} {un_d} — {pts}خاڵ")
        await q.edit_message_text("\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 گەڕانەوە", callback_data="adm_back")]]))

    elif data == "adm_settings":
        cost   = await db.get_setting("default_cost","0")
        reward = await db.get_setting("ref_reward","5")
        spts   = await db.get_setting("start_points","0")
        await q.edit_message_text(
            f"⚙️ <b>ڕێکخستنەکان:</b>\n\n"
            f"💰 تێچووی پێش‌وزا: <b>{cost}</b>\n"
            f"🎁 خاڵی ڕافایل: <b>{reward}</b>\n"
            f"🎉 خاڵی خۆش‌هاتن: <b>{spts}</b>\n\n"
            "بۆ گۆڕین فەرمانەکان بەکاربهێنە:\n"
            "<code>/setdefcost N</code>\n<code>/setreward N</code>\n<code>/setstartpts N</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="adm_back")]]))

    elif data == "adm_broadcast":
        await q.edit_message_text(
            "📢 بۆ براودکاست:\n<code>/broadcast پەیامەکە</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="adm_back")]]))

    elif data == "adm_back":
        count  = await db.get_user_count()
        cost   = await db.get_setting("default_cost","0")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 یوزەرەکان", callback_data="adm_users"),
             InlineKeyboardButton("📢 براودکاست", callback_data="adm_broadcast")],
            [InlineKeyboardButton("⚙️ ڕێکخستنەکان", callback_data="adm_settings")],
        ])
        await q.edit_message_text(
            f"🛠 <b>پانێڵی ئادمین</b>\n👥 کۆی یوزەر: <b>{count}</b>\n💰 تێچوو: <b>{cost}</b>",
            parse_mode="HTML", reply_markup=kb)


@admin_only
async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows  = await db.get_users_page(limit=20)
    count = await db.get_user_count()
    lines = [f"👥 <b>یوزەرەکان ({count}) — دواترین ٢٠:</b>"]
    for uid,fn,un,pts,ca in rows:
        un_d = f"@{esc(un)}" if un else "—"
        lines.append(f"• <code>{uid}</code> {esc(fn or '—')} {un_d} — <b>{pts}</b>خاڵ")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

@admin_only
async def broadcast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /broadcast پەیام"); return
    text = " ".join(ctx.args)
    ids  = await db.get_all_user_ids()
    ok, fail = 0, 0
    msg = await update.message.reply_text(f"⏳ نێردن بۆ {len(ids)} کەس…")
    for uid in ids:
        try:
            await ctx.bot.send_message(uid, text)
            ok += 1
        except: fail += 1
        await asyncio.sleep(0.05)
    await msg.edit_text(f"✅ نێردرا: {ok}\n❌ نەنێردرا: {fail}")

@admin_only
async def addpoints_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("بەکارهێنان: /addpoints ID مەبەڵەغ"); return
    try:
        uid, amt = int(ctx.args[0]), int(ctx.args[1])
        await db.add_points(uid, amt)
        pts = await db.get_points(uid)
        await update.message.reply_text(f"✅ +{amt} خاڵ بۆ <code>{uid}</code> — کۆ: {pts}", parse_mode="HTML")
        try: await ctx.bot.send_message(uid, f"🎁 ئادمین {amt} خاڵی زیادکردووە! کۆی خاڵەکانت: {pts}")
        except: pass
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

@admin_only
async def setpoints_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("بەکارهێنان: /setpoints ID مەبەڵەغ"); return
    try:
        uid, amt = int(ctx.args[0]), int(ctx.args[1])
        await db.set_points(uid, amt)
        await update.message.reply_text(f"✅ خاڵی <code>{uid}</code> کرایە {amt}", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

@admin_only
async def setcost_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text("بەکارهێنان: /setcost ID مەبەڵەغ"); return
    try:
        uid, amt = int(ctx.args[0]), int(ctx.args[1])
        await db.set_user_cost(uid, amt)
        await update.message.reply_text(f"✅ تێچووی <code>{uid}</code> کرایە {amt}خاڵ", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

@admin_only
async def setdefcost_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /setdefcost مەبەڵەغ"); return
    await db.set_setting("default_cost", ctx.args[0])
    await update.message.reply_text(f"✅ تێچووی پێش‌وزا کرایە {ctx.args[0]}خاڵ")

@admin_only
async def setreward_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /setreward مەبەڵەغ"); return
    await db.set_setting("ref_reward", ctx.args[0])
    await update.message.reply_text(f"✅ خاڵی ڕافایل کرایە {ctx.args[0]}")

@admin_only
async def setstartpts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /setstartpts مەبەڵەغ"); return
    await db.set_setting("start_points", ctx.args[0])
    await update.message.reply_text(f"✅ خاڵی خۆش‌هاتن کرایە {ctx.args[0]}")

@admin_only
async def addrequired_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /addrequired @channel"); return
    raw = ctx.args[0].lstrip("@")
    try:
        chat = await pyro.get_chat(raw)
        await db.add_required_channel(chat.id,
                                      getattr(chat,"username",None) or raw,
                                      getattr(chat,"title",None) or raw)
        await update.message.reply_text(f"✅ کەنەڵی <b>{esc(getattr(chat,'title',raw))}</b> زیادکرا", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

@admin_only
async def removerequired_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("بەکارهێنان: /removerequired channel_id"); return
    try:
        await db.remove_required_channel(int(ctx.args[0]))
        await update.message.reply_text("✅ سڕایەوە")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ─── Query ────────────────────────────────────────────────────────────────────

async def query_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    uid = u.id
    q   = update.message.text.strip()
    log.info(f"Query uid={uid} q={q!r}")

    await auto_register(u)

    if uid != ADMIN_ID:
        missing = await check_membership(uid)
        if missing:
            await update.message.reply_text("⚠️ پێشتر ئەم کەنەڵانە جۆین بکە:",
                                            reply_markup=await join_keyboard(missing))
            return
        cost = await db.get_user_cost(uid)
        if cost > 0:
            ok = await db.deduct_points(uid, cost)
            if not ok:
                pts    = await db.get_points(uid)
                bot_un = await get_bot_username(ctx)
                ref_link = f"https://t.me/{bot_un}?start=ref_{uid}"
                await update.message.reply_text(
                    f"❌ <b>خاڵت بەس نییە!</b>\n"
                    f"💰 خاڵەکانت: {pts} | تێچوو: {cost}\n\n"
                    f"🔗 بە ڕافایل خاڵ وەربگرە:\n<code>{ref_link}</code>",
                    parse_mode="HTML", disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔗 لینکی ڕافایلم", callback_data="my_ref")
                    ]])
                )
                return

    wait = await update.message.reply_text("🔍 گەڕان دەکرێت…")
    try:
        chat, err = await do_lookup(q)
        if err:
            if uid != ADMIN_ID:
                cost = await db.get_user_cost(uid)
                if cost > 0: await db.add_points(uid, cost)
            await wait.edit_text(err)
            return

        first = getattr(chat,'first_name',None) or getattr(chat,'title',None) or ""
        last  = getattr(chat,'last_name',None) or ""
        uname = getattr(chat,'username',None)
        cid   = getattr(chat,'id')
        prem  = getattr(chat,'is_premium',False) or False

        await db.update_osint_user(cid, first, last, uname, prem)
        name_hist  = await db.get_name_history(cid)
        uname_hist = await db.get_username_history(cid)
        msg = build_result(chat, name_hist, uname_hist)

        # ① فوری وەڵام بدەرەوە
        await wait.edit_text(msg, parse_mode="HTML", disable_web_page_preview=True)

        # ② TeleSINT لە پشتەوە — ئەگەر وەڵامی هات پەیامەکە دەستکاری دەکات
        if user_client:
            asyncio.create_task(_append_telesin(wait, msg, q))

    except Exception as e:
        log.error(f"query_cmd: {e}", exc_info=True)
        try: await wait.edit_text(f"❌ هەڵە: {esc(str(e))}")
        except: pass


async def _append_telesin(wait_msg, base_msg: str, q: str):
    """لە پشتەوە TeleSINT دەپرسێت و پەیامەکە دەستکاری دەکات."""
    try:
        tele = await ask_telesinbot(q)
        if tele:
            new_msg = base_msg + f"\n\n<b>━━━━━━━━━━</b>\n{esc(tele)}"
            await wait_msg.edit_text(new_msg, parse_mode="HTML",
                                     disable_web_page_preview=True)
    except Exception as e:
        log.warning(f"TeleSINT append: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global user_client
    await db.init_db()

    sf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".user_session")
    if os.path.exists(sf):
        with open(sf) as f: sess = f.read().strip()
        if sess:
            try:
                user_client = PyroClient("user", api_id=API_ID, api_hash=API_HASH,
                                          session_string=sess, in_memory=True)
                await user_client.start()
                _attach_telesin_handler(user_client)
                log.info("User client loaded ✅")
            except Exception as e:
                log.warning(f"Session load failed: {e}")
                user_client = None

    await pyro.start()
    log.info("Pyrogram bot started ✅")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("genauth", genauth_start)],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, genauth_phone)],
            WAIT_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, genauth_otp)],
            WAIT_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, genauth_2fa)],
        },
        fallbacks=[CommandHandler("cancel", genauth_cancel)],
    )
    app.add_handler(conv)

    # user
    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("points", points_cmd))
    app.add_handler(CommandHandler("ref",    ref_cmd))

    # callbacks
    app.add_handler(CallbackQueryHandler(callback_handler,
        pattern="^(check_join|my_points|my_ref|how_to|back_start)$"))
    app.add_handler(CallbackQueryHandler(admin_callback,
        pattern="^adm_"))

    # admin
    for cmd, fn in [
        ("admin",          admin_cmd),
        ("users",          users_cmd),
        ("broadcast",      broadcast_cmd),
        ("addpoints",      addpoints_cmd),
        ("setpoints",      setpoints_cmd),
        ("setcost",        setcost_cmd),
        ("setdefcost",     setdefcost_cmd),
        ("setreward",      setreward_cmd),
        ("setstartpts",    setstartpts_cmd),
        ("addrequired",    addrequired_cmd),
        ("removerequired", removerequired_cmd),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, query_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot polling started ✅")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await pyro.stop()
        if user_client:
            try: await user_client.stop()
            except: pass


if __name__ == "__main__":
    asyncio.run(main())
