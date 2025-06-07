
import os, asyncio, logging, datetime as dt, re, aiosqlite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("7741584010:AAEQrh7d6VgEIkXMOd5dnpb6U6G31uGFkfI")  # ⇦ export this env-var before running
DB = "tasks.db"

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ADD_TEXT, CHOOSE_DAYS, GET_TIME = range(3)
_TIME = re.compile(r"^(\\d{1,2})(?::(\\d{2}))?(am|pm)?$", re.I)

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER, task TEXT, days TEXT, hour INTEGER, minute INTEGER);
        CREATE TABLE IF NOT EXISTS user_tz(
          user_id INTEGER PRIMARY KEY, tz TEXT);
        """)
        await db.commit()

async def tz_of(uid):                             # default UTC
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute(
            "SELECT tz FROM user_tz WHERE user_id=?", (uid,)
        )).fetchone()
    return row[0] if row else "UTC"

# ───────────────────────── basic commands ───────────────────────── #
async def start(u: Update, _):
    await u.message.reply_text(
        "👋 Reminder Bot\n"
        "/add – new reminder\n"
        "/list – show reminders\n"
        "/delete <number> – remove one\n"
        "/settz Region/City – set timezone",
        parse_mode=None          # <- disable markdown parsing
    )

async def settz(u: Update, ctx):
    if not ctx.args:
        return await u.message.reply_text("Usage: /settz Europe/London")
    tz = ctx.args[0]
    try: ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return await u.message.reply_text("❌ Unknown timezone.")
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO user_tz(user_id,tz) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
            (u.effective_user.id, tz)); await db.commit()
    await u.message.reply_text(f"✅ Timezone set to {tz}")

# ───────────────────────── /add conversation ────────────────────── #
async def add1(u: Update, _):               # ask task
    await u.message.reply_text("Send the *task text*"); return ADD_TEXT

async def add2(u: Update, ctx):             # ask weekdays
    ctx.user_data["task"] = u.message.text.strip()
    ctx.user_data["days"] = set()
    kb = [[InlineKeyboardButton(d, callback_data=d)] for d in WEEKDAYS]
    kb.append([InlineKeyboardButton("✅ done", callback_data="done")])
    await u.message.reply_text("Pick weekday(s):", reply_markup=InlineKeyboardMarkup(kb))
    return CHOOSE_DAYS

async def add3(q_upd: Update, ctx):         # process weekday taps
    q = q_upd.callback_query; await q.answer()
    d = q.data
    if d != "done":
        s = ctx.user_data["days"]; s.discard(d) if d in s else s.add(d)
        await q.edit_message_text(", ".join(sorted(s)) or "(none)"); return CHOOSE_DAYS
    if not ctx.user_data["days"]:
        await q.edit_message_text("❌ No days – cancelled."); return ConversationHandler.END
    await q.edit_message_text("Send the time (19:30, 7pm, 7:15 am)"); return GET_TIME

async def add4(u: Update, ctx):
    m = _TIME.match(u.message.text.strip().replace(" ", ""))
    if not m: return await u.message.reply_text("❌ Try 19:30 or 7pm.")
    h, mnt = int(m.group(1)), int(m.group(2) or 0); ap = m.group(3)
    if ap: h = (h % 12) + (12 if ap.lower()=="pm" else 0)
    if not (0<=h<24 and 0<=mnt<60):
        return await u.message.reply_text("❌ Invalid time.")
    uid, task = u.effective_user.id, ctx.user_data["task"]
    days = ",".join(sorted(ctx.user_data["days"]))
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO tasks(user_id,task,days,hour,minute) VALUES(?,?,?,?,?)",
                         (uid, task, days, h, mnt)); await db.commit()
    await schedule(uid, task, days, h, mnt, ctx)
    await u.message.reply_text(f"✅ Saved for {days} at {h:02d}:{mnt:02d}.")
    return ConversationHandler.END

# ───────────────────────── scheduler helpers ────────────────────── #
async def schedule(uid, task, days, h, m, app_or_ctx):
    tz = ZoneInfo(await tz_of(uid))
    jq = app_or_ctx.job_queue
    for d in days.split(","):
        name = f"{uid}:{task}:{d}:{h}{m}"
        for j in jq.get_jobs_by_name(name): j.schedule_removal()
        jq.run_daily(remind, dt.time(hour=h, minute=m, tzinfo=tz),
                     days=(WEEKDAYS.index(d),),
                     data=dict(user_id=uid, task=task), name=name)

async def remind(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    await ctx.bot.send_message(d["user_id"], f"🔔 {d['task']}")

# ───────────────────────── list / delete ───────────────────────── #
async def list_(u: Update, _):
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,task,days,hour,minute FROM tasks WHERE user_id=?", (uid,)
        )).fetchall()
    if not rows: return await u.message.reply_text("No reminders.")
    await u.message.reply_text("\\n".join(
        f"{i+1}. {t} ({d} @ {h:02d}:{m:02d})"
        for i,(_,t,d,h,m) in enumerate(rows)))

async def delete(u: Update, ctx):
    if not ctx.args: return await u.message.reply_text("Usage: /delete <#>")
    try: n = int(ctx.args[0])-1; assert n>=0
    except: return await u.message.reply_text("Give a valid number.")
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,task,days,hour,minute FROM tasks WHERE user_id=?", (uid,)
        )).fetchall()
        if n>=len(rows): return await u.message.reply_text("No such task.")
        row_id, task, days, h, m = rows[n]
        await db.execute("DELETE FROM tasks WHERE id=?", (row_id,)); await db.commit()
    for d in days.split(","):
        for j in ctx.job_queue.get_jobs_by_name(f"{uid}:{task}:{d}:{h}{m}"):
            j.schedule_removal()
    await u.message.reply_text("Deleted!")

# ───────────────────────── main ───────────────────────── #
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env-var first.")
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add1)],
        states={
            ADD_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add2)],
            CHOOSE_DAYS: [CallbackQueryHandler(add3)],
            GET_TIME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add4)],
        }, fallbacks=[], allow_reentry=True)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settz", settz))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", list_))
    app.add_handler(CommandHandler("delete", delete))

    # restore jobs from DB
    async with aiosqlite.connect(DB) as db:
        async for uid, t, d, h, m in (
            await db.execute("SELECT user_id,task,days,hour,minute FROM tasks")):
            await schedule(uid, t, d, h, m, app)

    await app.initialize(); await app.start(); await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())