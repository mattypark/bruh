################################################################################################
# Telegram “To-Do Reminder” Bot – single file
# • /add : one-off dates *or* weekly weekdays  → time  → saved
# • /settz <Region/City> to set / change timezone
# • /list  / /delete <n>
# Requires: python-telegram-bot 22.x , aiosqlite     (pip install …)
################################################################################################

BOT_TOKEN = "7741584010:AAEQrh7d6VgEIkXMOd5dnpb6U6G31uGFkfI"          #  ← change me

import asyncio, datetime as dt, logging, re, os, aiosqlite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as KB, constants as TG
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, ContextTypes, filters
)

DB      = os.getenv("REMINDER_DB", "tasks.db")
WEEK    = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
ASK_TXT, CHOOSE_KIND, GET_DATES, GET_WEEKDAYS, GET_TIME = range(5)
DATE_RX = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:,\d{4}-\d{1,2}-\d{1,2})*$")
TIME_RX = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)

# ───────────────────────── database ───────────────────────── #
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user INTEGER,
          text TEXT,
          mode TEXT,          -- 'date' | 'weekday'
          payload TEXT,       -- YYYY-MM-DD,… or Mon,Tue
          hour INT,
          minute INT
        );
        CREATE TABLE IF NOT EXISTS user_tz(
          user INTEGER PRIMARY KEY,
          tz   TEXT
        );
        """); await db.commit()

async def user_tz(uid):            # default UTC
    async with aiosqlite.connect(DB) as db:
        row = await (await db.execute("SELECT tz FROM user_tz WHERE user=?", (uid,))).fetchone()
    return row[0] if row else "UTC"

# ───────────────────────── helpers ─────────────────────────── #
async def schedule(uid, text, mode, payload, h, m, app):
    tz = ZoneInfo(await user_tz(uid))
    jq = app.job_queue
    if mode == "weekday":
        for d in payload.split(","):
            name = f"{uid}:{text}:{d}:{h}{m}"
            for j in jq.get_jobs_by_name(name): j.schedule_removal()
            jq.run_daily(send, dt.time(h, m, tzinfo=tz),
                         days=(WEEK.index(d),), name=name,
                         data=dict(uid=uid, msg=text))
    else:                                   # one‑off dates
        now_local = dt.datetime.now(tz)
        for ds in payload.split(","):
            y, mo, dy = map(int, ds.split("-"))
            when_local = dt.datetime(y, mo, dy, h, m, tzinfo=tz)
           
            # if the target time has already passed, skip it
            delay = (when_local - now_local).total_seconds()
            if delay <= 0:
                continue

            # schedule via *delay seconds* so we avoid any timezone confusion
            jq.run_once(send, delay, data=dict(uid=uid, msg=text))
async def send(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    await ctx.bot.send_message(d["uid"], f"🔔 Reminder — {d['msg']}‼️")
    # auto-prune one-off reminders
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM tasks WHERE text=? AND user=? AND mode='date'",
                         (d["msg"], d["uid"]))
        await db.commit()
# ───────────────────────── commands ────────────────────────── #
WELCOME = (
    "👋  Reminder Bot\n\n"
    "/add – new reminder\n"
    "/list – show reminders\n"
    "/delete <n> – remove one\n"
    "/settz Region/City – timezone"
)
async def start(u: Update, _):  await u.message.reply_text(WELCOME, parse_mode=TG.ParseMode.MARKDOWN)

async def settz(u: Update, ctx):
    if not ctx.args:
        return await u.message.reply_text("Usage: /settz EST  or  /settz PST")
    TZ_MAP = {
    "est": "America/New_York",
    "eastern standard time": "America/New_York",
    "pst": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
}
    tz_input = " ".join(ctx.args).lower()
    tz       = TZ_MAP.get(tz_input, tz_input)
    try: ZoneInfo(ctx.args[0])
    except ZoneInfoNotFoundError: return await u.message.reply_text("❌ Unknown TZ.")
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO user_tz VALUES(?,?) ON CONFLICT(user) DO UPDATE SET tz=excluded.tz",
                         (u.effective_user.id, ctx.args[0])); await db.commit()
    await u.message.reply_text(f"✅ Timezone set to {ctx.args[0]}")

# ───── conversation: /add ───── #
async def add_1(u: Update, _):
    await u.message.reply_text("Send the *task text*.", parse_mode=TG.ParseMode.MARKDOWN); return ASK_TXT

async def add_2(u: Update, ctx):
    ctx.user_data["task"] = u.message.text.strip()
    kb = KB([[Btn("📅 one-off date(s)", callback_data="date")],
             [Btn("🔁 weekly pattern", callback_data="week")]])
    await u.message.reply_text("One-off or weekly?", reply_markup=kb); return CHOOSE_KIND

async def choose_kind(q_upd: Update, ctx):
    kind = q_upd.callback_query.data; await q_upd.callback_query.answer()
    ctx.user_data["kind"] = kind
    if kind == "date":
        await q_upd.callback_query.edit_message_text(
    "Enter date(s) in *YYYY-MM-DD* format.\\n"
    "Examples: `2025-06-12`  or  `2025-06-12,2025-07-01`",
    parse_mode=TG.ParseMode.MARKDOWN
)
        return GET_DATES
    ctx.user_data["wds"] = set()
    kb = [[Btn(d, callback_data=d)] for d in WEEK] + [[Btn("✅ done", callback_data="done")]]
    await q_upd.callback_query.edit_message_text("Choose weekday(s):", reply_markup=kb); return GET_WEEKDAYS

async def collect_dates(u: Update, ctx):
    if not DATE_RX.match(u.message.text.strip()):
        return await u.message.reply_text("❌ Wrong date format.")
    ctx.user_data["dates"] = u.message.text.replace(" ", "").split(",")
    await u.message.reply_text("Now send the time (19:30 or 7 pm)"); return GET_TIME

async def collect_wd(q_upd: Update, ctx):
    q = q_upd.callback_query; await q.answer(); d = q.data
    if d != "done":
        s = ctx.user_data["wds"]; s.discard(d) if d in s else s.add(d)
        await q.edit_message_text(", ".join(sorted(s)) or "(none)"); return GET_WEEKDAYS
    if not ctx.user_data["wds"]:
        await q.edit_message_text("❌ No days – cancelled."); return ConversationHandler.END
    await q.edit_message_text("Time? (24-h or 7 pm)"); return GET_TIME

async def collect_time(u: Update, ctx):
    m = TIME_RX.match(u.message.text)
    if not m: return await u.message.reply_text("❌ Time format.")
    h, mn = int(m.group(1)), int(m.group(2) or 0); ap = m.group(3)
    if ap: h = (h % 12) + (12 if ap.lower() == "pm" else 0)
    uid, text = u.effective_user.id, ctx.user_data["task"]
    if ctx.user_data["kind"] == "date":
        payload = ",".join(ctx.user_data["dates"]); mode = "date"
    else:
        payload = ",".join(sorted(ctx.user_data["wds"])); mode = "weekday"
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO tasks(user,text,mode,payload,hour,minute) VALUES(?,?,?,?,?,?)",
                         (uid, text, mode, payload, h, mn)); await db.commit()
    await schedule(uid, text, mode, payload, h, mn, ctx.application)
    await u.message.reply_text("✅ Saved!"); return ConversationHandler.END

# ───────── list / delete ───────── #
# ───────── list / delete ───────── #
async def list_cmd(u: Update, _):
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT text,mode,payload,hour,minute FROM tasks WHERE user=?", (uid,)
        )).fetchall()

    if not rows:
        return await u.message.reply_text("No reminders.")

    tz = await user_tz(uid)
    out = []
    for i, (t, mode, p, h, mn) in enumerate(rows, 1):
        when = p if mode == "weekday" else p.replace(",", " • ")
        out.append(f"{i}. {t} ({when} @ {h:02d}:{mn:02d} {tz})")

    await u.message.reply_text("\n".join(out))


async def del_cmd(u: Update, ctx):
    if not ctx.args:
        return await u.message.reply_text("Usage: /delete <n>")
    try:
        idx = int(ctx.args[0]) - 1
        assert idx >= 0
    except Exception:
        return await u.message.reply_text("Bad number.")

    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,text,mode,payload,hour,minute FROM tasks WHERE user=?", (uid,)
        )).fetchall()

        if idx >= len(rows):
            return await u.message.reply_text("No such item.")

        row_id, text, mode, payload, h, mn = rows[idx]
        await db.execute("DELETE FROM tasks WHERE id=?", (row_id,))
        await db.commit()

    # cancel scheduled jobs
    for d in (payload.split(",") if mode == "weekday" else [""]):
        for j in u.get_bot().job_queue.get_jobs_by_name(f"{uid}:{text}:{d}:{h}{mn}"):
            j.schedule_removal()

    await u.message.reply_text("Deleted!")

# ───────────────────────── main ───────────────────────────── #
async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_1)],
        states={
            ASK_TXT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_2)],
            CHOOSE_KIND:   [CallbackQueryHandler(choose_kind)],
            GET_DATES:     [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_dates)],
            GET_WEEKDAYS:  [CallbackQueryHandler(collect_wd)],
            GET_TIME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_time)],
        },
        fallbacks=[], allow_reentry=True)

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("settz",   settz))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list",    list_cmd))
    app.add_handler(CommandHandler("delete",  del_cmd))

    # initialize FIRST
    await app.initialize()

    # restore jobs NOW that job_queue exists
    async with aiosqlite.connect(DB) as db:
        async for uid,t,m,p,h,mn in (await db.execute(
                "SELECT user,text,mode,payload,hour,minute FROM tasks")):
            await schedule(uid,t,m,p,h,mn,app)

    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())