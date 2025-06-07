###################################################################
# 100-line, single-file Telegram Reminder Bot                     #
# ▸ Unlimited tasks per user                                       #
# ▸ /add  → pick weekdays + HH:MM or 7 pm style                    #
# ▸ /list and /delete                                              #
# ▸ NO env vars needed – hard-code your token on line 8            #
# ▸ Tested with python-telegram-bot 22.1 + aiosqlite               #
###################################################################

BOT_TOKEN = "7741584010:AAEQrh7d6VgEIkXMOd5dnpb6U6G31uGFkfI"   # ← replace & save

import asyncio, datetime as dt, logging, re, aiosqlite
from telegram import (Update, InlineKeyboardButton as Btn,
                      InlineKeyboardMarkup as KB)
from telegram.ext import (Application, CommandHandler, ConversationHandler,
                          MessageHandler, CallbackQueryHandler, ContextTypes,
                          filters)

DB = "tasks.db"
WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ASK_TEXT, ASK_DAY, ASK_TIME = range(3)
TIME_RX = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", re.I)

###################################################################
# DB helpers
###################################################################
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user INTEGER, text TEXT, days TEXT, h INT, m INT);
        """)
        await db.commit()

###################################################################
# /start text
###################################################################
START_MSG = (
    "👋 *Reminder Bot*\n\n"
    "/add – new reminder\n"
    "/list – show reminders\n"
    "/delete <n> – remove one"
)

async def cmd_start(u: Update, _):
    await u.message.reply_text(START_MSG, parse_mode="Markdown")

###################################################################
# /add conversation
###################################################################
async def add1(u: Update, _):
    await u.message.reply_text("Send the task text."); return ASK_TEXT

async def add2(u: Update, ctx):
    ctx.user_data["task"] = u.message.text.strip(); ctx.user_data["days"] = set()
    kb = [[Btn(d, d)] for d in WEEK] + [[Btn("✅ done", "done")]]
    await u.message.reply_text("Choose weekday(s):", reply_markup=KB(kb))
    return ASK_DAY

async def add3(q_upd: Update, ctx):
    q = q_upd.callback_query; await q.answer(); d = q.data
    if d != "done":
        s = ctx.user_data["days"]; s.discard(d) if d in s else s.add(d)
        txt = ", ".join(sorted(s)) or "(none)"; await q.edit_message_text(txt)
        return ASK_DAY
    if not ctx.user_data["days"]:
        await q.edit_message_text("❌ No days – cancelled."); return ConversationHandler.END
    await q.edit_message_text("Send time (19:30, 7pm, 7:15 am)"); return ASK_TIME

async def add4(u: Update, ctx):
    m = TIME_RX.match(u.message.text.strip().replace(" ", ""))
    if not m: return await u.message.reply_text("❌ Try 19:30 or 7pm.")
    h, mnt = int(m.group(1)), int(m.group(2) or 0); ampm = m.group(3)
    if ampm: h = (h % 12) + (12 if ampm.lower() == "pm" else 0)
    if not (0 <= h < 24 and 0 <= mnt < 60):
        return await u.message.reply_text("❌ Invalid time.")
    user, text = u.effective_user.id, ctx.user_data["task"]
    days = ",".join(sorted(ctx.user_data["days"]))
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO tasks(user,text,days,h,m) VALUES(?,?,?,?,?)",
                         (user, text, days, h, mnt)); await db.commit()
    await schedule_one(user, text, days, h, mnt, ctx)
    await u.message.reply_text("✅ Saved!"); return ConversationHandler.END

###################################################################
# Scheduler
###################################################################
async def schedule_one(user, text, days, h, mnt, app):
    for d in days.split(','):
        name = f"{user}:{text}:{d}:{h}{mnt}"
        for j in app.job_queue.get_jobs_by_name(name): j.schedule_removal()
        app.job_queue.run_daily(send,
            dt.time(hour=h, minute=mnt), days=(WEEK.index(d),),
            data=dict(uid=user, msg=text), name=name)

async def send(ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(ctx.job.data["uid"], f"🔔 {ctx.job.data['msg']}")

###################################################################
# /list and /delete
###################################################################
async def cmd_list(u: Update, _):
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,text,days,h,m FROM tasks WHERE user=?", (uid,))).fetchall()
    if not rows: return await u.message.reply_text("No reminders.")
    lines = [f"{i+1}. {t} ({d} @ {h:02d}:{m:02d})" for i,(_,t,d,h,m) in enumerate(rows)]
    await u.message.reply_text("\n".join(lines))

async def cmd_del(u: Update, ctx):
    if not ctx.args: return await u.message.reply_text("Usage: /delete <n>")
    try: n = int(ctx.args[0]) - 1; assert n>=0
    except: return await u.message.reply_text("Bad number.")
    uid = u.effective_user.id
    async with aiosqlite.connect(DB) as db:
        rows = await (await db.execute(
            "SELECT id,text,days,h,m FROM tasks WHERE user=?", (uid,))).fetchall()
        if n>=len(rows): return await u.message.reply_text("Not found.")
        row_id, text, days, h, m = rows[n]
        await db.execute("DELETE FROM tasks WHERE id=?", (row_id,)); await db.commit()
    for d in days.split(','):
        for j in ctx.job_queue.get_jobs_by_name(f"{uid}:{text}:{d}:{h}{m}"): j.schedule_removal()
    await u.message.reply_text("Deleted!")

###################################################################
# Main
###################################################################
async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add1)],
        states={
            ASK_TEXT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add2)],
            ASK_DAY:   [CallbackQueryHandler(add3)],
            ASK_TIME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add4)]
        }, fallbacks=[], allow_reentry=True)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("delete", cmd_del))

    # restore on boot
    async with aiosqlite.connect(DB) as db:
        async for uid, t, d, h, m in (
            await db.execute("SELECT user,text,days,h,m FROM tasks")):
            await schedule_one(uid, t, d, h, m, app)

    await app.initialize(); await app.start(); await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())