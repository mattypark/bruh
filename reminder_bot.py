# reminder_bot.py
################################################################################################
# Telegram “To-Do Reminder” Bot with Upstash QStash scheduling
# • /add : one-off dates *or* weekly weekdays → time → saved to DB + QStash
# • /settz <Region/City> to set / change timezone
# • /list  / /delete <n>
# Requires: python-telegram-bot 22.x , aiosqlite, httpx, aiohttp, python-dotenv
################################################################################################

from dotenv import load_dotenv
load_dotenv()  # loads .env into os.environ

import os, asyncio, datetime as dt, logging, re, aiosqlite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from telegram import Update, Bot, InlineKeyboardButton as Btn, InlineKeyboardMarkup as KB, constants as TG
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)
import httpx
from aiohttp import web
import dateparser
from dateparser.search import search_dates

# ───────────────────────── constants ───────────────────────── #
BOT_TOKEN   = os.getenv("BOT_TOKEN")
DB          = os.getenv("REMINDER_DB", "tasks.db")
WEEK        = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
ASK_TXT, CHOOSE_KIND, GET_DATES, GET_WEEKDAYS, GET_TIME = range(5)
DATE_RX     = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:,\d{4}-\d{1,2}-\d{1,2})*$")
TIME_RX     = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)
QSTASH_API  = "https://qstash.upstash.io/v1/publish"

# ───────────────────────── database ───────────────────────── #
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user INTEGER,
          text TEXT,
          mode TEXT,
          payload TEXT,
          hour INT,
          minute INT
        );
        CREATE TABLE IF NOT EXISTS user_tz(
          user INTEGER PRIMARY KEY,
          tz   TEXT
        );
        """)
        await db.commit()

async def user_tz(uid):
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute(
            "SELECT tz FROM user_tz WHERE user=?", (uid,)
        )).fetchone()
    return row[0] if row else "UTC"

# ───────────────────────── QStash webhook ───────────────────────── #
_bot = Bot(token=BOT_TOKEN)
app_web = web.Application()

async def qstash_webhook(request):
    payload = await request.json()
    print("DEBUG: QStash webhook received payload:", payload)
    body    = payload.get("body", {})
    uid     = body.get("uid")
    msg     = body.get("msg")
    if uid and msg:
        await _bot.send_message(uid, f"🔔 Reminder — {msg}‼️")
    return web.Response(text="OK")

app_web.router.add_post("/qstash", qstash_webhook)

# ───────────────────────── scheduling ───────────────────────── #
async def schedule(uid, text, mode, payload, h, m, app):
    """
    Queue reminders locally using python-telegram-bot's JobQueue.
    """
    tz = ZoneInfo(await user_tz(uid))
    jq = app.job_queue

    if mode == "weekday":
        # weekly pattern
        for wd in payload.split(","):
            job_name = f"{uid}:{text}:{wd}:{h:02d}{m:02d}"
            # remove old jobs
            for job in jq.get_jobs_by_name(job_name):
                job.schedule_removal()
            jq.run_daily(
                send,
                time=dt.time(hour=h, minute=m, tzinfo=tz),
                days=(WEEK.index(wd),),
                name=job_name,
                data={"uid": uid, "msg": text},
            )
    else:
        # one-off dates
        now = dt.datetime.now(tz)
        for ds in payload.split(","):
            y,mo,dy = map(int, ds.split("-"))
            target = dt.datetime(y,mo,dy,h,m, tzinfo=tz)
            delay = (target - now).total_seconds()
            if delay <= 0:
                continue
            job_name = f"{uid}:{text}:{ds}:{h:02d}{m:02d}"
            # remove old jobs
            for job in jq.get_jobs_by_name(job_name):
                job.schedule_removal()
            jq.run_once(
                send,
                delay,
                name=job_name,
                data={"uid": uid, "msg": text},
            )
# ───────────────────────── reminder sending ───────────────────────── #
async def send(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    await ctx.bot.send_message(data["uid"], f"🔔 Reminder — {data['msg']}‼️")
    # auto-delete one-off tasks
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "DELETE FROM tasks WHERE user=? AND text=? AND mode='date'",
            (data["uid"], data["msg"])
        )
        await db.commit()

# ───────────────────────── conversation / commands ───────────────────────── #
WELCOME = (
    "👋  Reminder Bot\n\n"
    "/add – new reminder\n"
    "/list – show reminders\n"
    "/delete <n> – remove one\n"
    "/settz <Region/City> – timezone"
)
async def start(u: Update, _): await u.message.reply_text(WELCOME)

async def settz(u: Update, ctx):
    if not ctx.args:
        return await u.message.reply_text("Usage: /settz EST  or  /settz PST")
    TZ_MAP = {
        "est":"America/New_York","eastern standard time":"America/New_York",
        "pst":"America/Los_Angeles","pacific time":"America/Los_Angeles",
    }
    inp = " ".join(ctx.args).lower()
    tz  = TZ_MAP.get(inp, ctx.args[0])
    try: ZoneInfo(tz)
    except ZoneInfoNotFoundError: return await u.message.reply_text("❌ Unknown TZ.")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO user_tz(user,tz) VALUES(?,?) ON CONFLICT(user) DO UPDATE SET tz=excluded.tz",
            (u.effective_user.id, tz)
        ); await db.commit()
    await u.message.reply_text(f"✅ Timezone set to {tz}")

async def add_1(u: Update, _):
    await u.message.reply_text("Send the *task text*.", parse_mode=TG.ParseMode.MARKDOWN)
    return ASK_TXT

async def add_2(u: Update, ctx):
    ctx.user_data["task"] = u.message.text.strip()
    kb = KB([[Btn("📅 one-off date(s)", callback_data="date")],
    [Btn("🔁 weekly pattern", callback_data="week")]])
    await u.message.reply_text("One-off or weekly?", reply_markup=kb)
    return CHOOSE_KIND

async def choose_kind(q: Update, ctx):
    await q.callback_query.answer()
    kind = q.callback_query.data; ctx.user_data["kind"] = kind
    if kind=="date":
        await q.callback_query.edit_message_text(
            "Enter date(s) in *YYYY-MM-DD* format.\nExamples: `2025-06-12` or `2025-06-12,2025-07-01`",
            parse_mode=TG.ParseMode.MARKDOWN
        )
        return GET_DATES
    ctx.user_data["wds"] = set()
    kb = [[Btn(d, callback_data=d)] for d in WEEK] + [[Btn("✅ done", callback_data="done")]]
    await q.callback_query.edit_message_text("Choose weekday(s):", reply_markup=kb)
    return GET_WEEKDAYS

async def collect_dates(u: Update, ctx):
    txt = u.message.text.strip()
    if not DATE_RX.match(txt):
        return await u.message.reply_text("❌ Wrong date format.")
    ctx.user_data["dates"] = txt.replace(" ","").split(",")
    await u.message.reply_text("Now send the time (e.g. `19:30` or `7 pm`)")
    return GET_TIME

async def collect_wd(q: Update, ctx):
    await q.callback_query.answer()
    d = q.callback_query.data
    if d!="done":
        s = ctx.user_data["wds"]
        s.discard(d) if d in s else s.add(d)
        return GET_WEEKDAYS
    if not ctx.user_data["wds"]:
        await q.callback_query.edit_message_text("❌ No days – cancelled.")
        return ConversationHandler.END
    await q.callback_query.edit_message_text("Time? (24-h or `7 pm`)")
    return GET_TIME

async def collect_time(u: Update, ctx):
    m = TIME_RX.match(u.message.text)
    if not m: return await u.message.reply_text("❌ Time format.")
    h, mn = int(m.group(1)), int(m.group(2) or 0)
    ap = m.group(3)
    if ap: h = (h%12)+(12 if ap.lower()=="pm" else 0)
    uid, txt = u.effective_user.id, ctx.user_data["task"]
    if ctx.user_data["kind"]=="date":
        payload, mode = ",".join(ctx.user_data["dates"]), "date"
    else:
        payload, mode = ",".join(sorted(ctx.user_data["wds"])), "weekday"
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO tasks(user,text,mode,payload,hour,minute) VALUES(?,?,?,?,?,?)",
            (uid, txt, mode, payload, h, mn)
        ); await db.commit()
    await schedule(uid, txt, mode, payload, h, mn, ctx.application)
    await u.message.reply_text("✅ Saved!")
    return ConversationHandler.END

async def list_cmd(u: Update, _):
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT text,mode,payload,hour,minute FROM tasks WHERE user=?", (uid,)
        )).fetchall()
    if not rows:
        return await u.message.reply_text("No reminders.")
    tz = await user_tz(uid); out=[]
    for i,(t,mode,p,h,mn) in enumerate(rows,1):
        when = p if mode=="weekday" else p.replace(","," • ")
        out.append(f"{i}. {t} ({when} @ {h:02d}:{mn:02d} {tz})")
    await u.message.reply_text("\n".join(out))

async def del_cmd(u: Update, ctx):
    if not ctx.args:
        return await u.message.reply_text("Usage: /delete <n>")
    try:
        idx = int(ctx.args[0])-1
        assert idx>=0
    except:
        return await u.message.reply_text("Bad number.")
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,text,mode,payload,hour,minute FROM tasks WHERE user=?", (uid,)
        )).fetchall()
        if idx>=len(rows):
            return await u.message.reply_text("No such item.")
        row_id,text,mode,payload,h,mn = rows[idx]
        await db.execute("DELETE FROM tasks WHERE id=?", (row_id,))
        await db.commit()
    await u.message.reply_text("Deleted!")
    
async def natural_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Free-form natural-language reminder parser."""
    text_raw = update.message.text
    settings = {
        "TIMEZONE": await user_tz(update.effective_user.id),
        "RETURN_AS_TIMEZONE_AWARE": True
    }
    # find any date-like substring + its datetime
    result = search_dates(text_raw, settings=settings)
    if not result:
        return await update.message.reply_text(
            "❌ Couldn't understand that time. Please rephrase."
        )
    matched_str, dt = result[0]

    # strip out the matched time phrase, what remains is the subject
    subject = text_raw.replace(matched_str, "").strip()
    if not subject:
        subject = "Reminder"

    # now build payload & persist exactly as before
    y, mo, d, h, m = dt.year, dt.month, dt.day, dt.hour, dt.minute
    payload = f"{y}-{mo}-{d}"
    mode    = "date"
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO tasks(user,text,mode,payload,hour,minute) VALUES(?,?,?,?,?,?)",
            (update.effective_user.id, subject, mode, payload, h, m)
        )
        await db.commit()

    # schedule a job whose .data['msg'] is *just* the subject
    await schedule(update.effective_user.id, subject, mode, payload, h, m, ctx.application)

    # tell the user exactly when it’s set
    await update.message.reply_text(f"✅ Reminder set for {dt:%c}")
# ───────────────────────── main ───────────────────────────── #
async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settz", settz))
    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_1)],
        states={
            ASK_TXT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_2)],
            CHOOSE_KIND:   [CallbackQueryHandler(choose_kind)],
            GET_DATES:     [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_dates)],
            GET_WEEKDAYS:  [CallbackQueryHandler(collect_wd)],
            GET_TIME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_time)],
        },
        fallbacks=[], allow_reentry=True
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", del_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_add))

    # republish any existing tasks to QStash
    async with aiosqlite.connect(DB) as db:
        async for uid,text,mode,payload,h,mn in (
            await db.execute("SELECT user,text,mode,payload,hour,minute FROM tasks")
        ):
            await schedule(uid,text,mode,payload,h,mn,app)

    # start QStash webhook listener
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT","3000")))
    await site.start()

    # start Telegram polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())