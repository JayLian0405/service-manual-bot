# -*- coding: utf-8 -*-
"""
維修手冊 Telegram 查詢 bot（long-polling，不需對外開 port）
路由規則：
  1. 訊息中含故障碼 (E3 / U4-03 / e3...)  -> 故障碼章節查詢 + 附頁面圖
  2. 純英數短訊息                          -> 全文關鍵字搜尋
  3. 其他（中文問題）                      -> Claude 智慧問答 (RAG) + 附引用頁圖
指令：/start /help /id /code /find /ask /page
"""
import asyncio
import json
import logging
import os
import re
import sqlite3

import fitz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import llm

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8-sig"))
DB_PATH = os.path.join(BASE, "manual.db")
PNG_DIR = os.path.join(BASE, "page_png")
os.makedirs(PNG_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("manual_bot")

CODE_RE = re.compile(r"\b([A-Za-z][0-9A-Za-z](?:-\d{1,2})?)\b")
ALLOWED = set(CONFIG.get("allowed_user_ids", []))

_pdf = None  # lazy-open，render 用


def db():
    return sqlite3.connect(DB_PATH)


def render_page(pdf_page: int) -> str:
    """渲染指定 PDF 頁為 PNG（有快取），回傳檔案路徑"""
    path = os.path.join(PNG_DIR, f"p{pdf_page}.png")
    if not os.path.exists(path):
        global _pdf
        if _pdf is None:
            _pdf = fitz.open(CONFIG["pdf_path"])
        dpi = CONFIG.get("render_dpi", 150)
        pix = _pdf[pdf_page - 1].get_pixmap(dpi=dpi)
        pix.save(path)
    return path


def page_buttons(pdf_page: int, end_page: int | None = None):
    """上一頁/下一頁按鈕"""
    total = None
    with db() as conn:
        total = conn.execute("SELECT MAX(pdf_page) FROM pages").fetchone()[0]
    row = []
    if pdf_page > 1:
        row.append(InlineKeyboardButton("⬅ 上一頁", callback_data=f"pg:{pdf_page-1}"))
    row.append(InlineKeyboardButton(f"p.{pdf_page}", callback_data="noop"))
    if pdf_page < total:
        row.append(InlineKeyboardButton("下一頁 ➡", callback_data=f"pg:{pdf_page+1}"))
    return InlineKeyboardMarkup([row])


def allowed(update: Update) -> bool:
    if not ALLOWED:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED


async def send_page(update_or_query, context, pdf_page: int, caption: str = ""):
    path = await asyncio.to_thread(render_page, pdf_page)
    chat_id = (
        update_or_query.effective_chat.id
        if isinstance(update_or_query, Update)
        else update_or_query.message.chat_id
    )
    # 公司網路對 api.telegram.org 間歇變慢，圖片上傳偶發 TimedOut -> 重試，
    # 最終仍失敗必須告知使用者，不得無聲（防呆鐵則）
    last_err = None
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                await context.bot.send_photo(
                    chat_id,
                    photo=f,
                    caption=(caption or f"PDF p.{pdf_page}")[:1020],
                    reply_markup=page_buttons(pdf_page),
                )
            return
        except NetworkError as e:  # TimedOut 是 NetworkError 子類
            last_err = e
            log.warning("send_photo p.%s 第 %s 次失敗：%s", pdf_page, attempt + 1, e)
            await asyncio.sleep(2)
    log.error("send_photo p.%s 重試後仍失敗：%s", pdf_page, last_err)
    try:
        await context.bot.send_message(
            chat_id, f"⚠ p.{pdf_page} 圖片傳送逾時，請再點一次「看 p.{pdf_page}」。"
        )
    except NetworkError:
        pass  # 連文字都送不出去，只能靠 log


# ---------- 查詢 ----------

def lookup_code(code: str):
    """故障碼 -> [(num, title, start_page, end_page)]"""
    with db() as conn:
        rows = conn.execute(
            "SELECT s.num, s.title, s.start_page, s.end_page FROM section_codes c "
            "JOIN sections s ON s.id=c.section_id WHERE c.code=? ORDER BY s.start_page",
            (code,),
        ).fetchall()
        if not rows and "-" in code:  # 子碼找不到時退回主碼
            rows = conn.execute(
                "SELECT s.num, s.title, s.start_page, s.end_page FROM section_codes c "
                "JOIN sections s ON s.id=c.section_id WHERE c.code=? ORDER BY s.start_page",
                (code.split("-")[0],),
            ).fetchall()
    return rows


def search_fts(query: str, limit: int = 6):
    tokens = re.findall(r"[A-Za-z0-9]+", query)
    if not tokens:
        return []
    q = '"' + " ".join(tokens) + '"' if len(tokens) > 1 else tokens[0]
    with db() as conn:
        try:
            return conn.execute(
                "SELECT p.pdf_page, p.part, snippet(pages_fts, 0, '[', ']', '…', 12) "
                "FROM pages_fts JOIN pages p ON p.pdf_page = pages_fts.rowid "
                "WHERE pages_fts MATCH ? ORDER BY bm25(pages_fts) LIMIT ?",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


# ---------- handlers ----------

HELP = (
    "🔧 大金 VRV 維修手冊查詢 (REYQ12-60BYLT)\n\n"
    "直接輸入即可：\n"
    "• 故障碼：E3、U4、A3-01 → 排障章節＋頁面圖\n"
    "• 英文關鍵字：drain pump → 全文搜尋\n"
    "• 中文問題：室外機風扇不轉怎麼查？ → AI 讀手冊回答\n\n"
    "指令：\n"
    "/code E3 — 強制故障碼查詢\n"
    "/find high pressure — 強制關鍵字搜尋\n"
    "/ask 問題 — 強制 AI 問答\n"
    "/page 301 — 看指定 PDF 頁\n"
    "/id — 顯示你的 Telegram ID"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(HELP)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"你的 Telegram ID：{update.effective_user.id}")


async def cmd_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        pno = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("用法：/page 301")
        return
    await send_page(update, context, pno)


async def do_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    rows = lookup_code(code)
    if not rows:
        await update.message.reply_text(
            f"手冊中查不到故障碼 {code}，請確認碼別或改用關鍵字搜尋。"
        )
        return
    for num, title, sp, ep in rows:
        cap = f"故障碼 {code}｜{num} {title}\nPDF p.{sp}~p.{ep}"
        await send_page(update, context, sp, cap)


async def do_find(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    rows = search_fts(query)
    if not rows:
        await update.message.reply_text(f"全文找不到「{query}」。")
        return
    lines = [f"🔍 「{query}」搜尋結果："]
    kb = []
    for pno, part, snip in rows:
        lines.append(f"• p.{pno}（{part}）{snip}")
        kb.append([InlineKeyboardButton(f"看 p.{pno}", callback_data=f"pg:{pno}")])
    await update.message.reply_text(
        "\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(kb)
    )


async def do_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text, pics = await asyncio.to_thread(llm.answer, question)
    await update.message.reply_text(text[:4000])
    for pno in pics:
        await send_page(update, context, pno)


async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/code E3")
        return
    await do_code(update, context, context.args[0].upper())


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/find high pressure")
        return
    await do_find(update, context, " ".join(context.args))


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/ask 室外機高壓跳脫怎麼查")
        return
    await do_ask(update, context, " ".join(context.args))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update) or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    has_cjk = bool(re.search(r"[一-鿿]", text))

    # 1) 短訊息且整體就是故障碼
    m = CODE_RE.fullmatch(text.upper())
    if m and lookup_code(m.group(1)):
        await do_code(update, context, m.group(1))
        return
    # 2) 中文訊息裡夾帶故障碼（如「E3是什麼」) -> 先給章節，再看要不要問答
    codes = [c.upper() for c in CODE_RE.findall(text.upper())]
    hit = next((c for c in codes if lookup_code(c)), None)
    if hit and len(text) <= 12:
        await do_code(update, context, hit)
        return
    # 3) 純英數 -> 關鍵字搜尋
    if not has_cjk:
        await do_find(update, context, text)
        return
    # 4) 中文問題 -> AI 問答
    await do_ask(update, context, text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "noop":
        return
    if q.data.startswith("pg:"):
        await send_page(q, context, int(q.data[3:]))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全域錯誤處理：任何 handler 炸掉都要留 log＋回覆使用者，不得無聲"""
    log.error("handler exception", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id, "⚠ 處理失敗，請稍後再試一次。"
            )
    except NetworkError:
        pass


def main():
    token = CONFIG.get("telegram_token")
    if not token:
        raise SystemExit("請先在 config.json 填入 telegram_token（向 @BotFather 申請）")
    app = (
        Application.builder()
        .token(token)
        # PTB 預設各 5s，公司網路上傳圖片間歇撞牆 -> 放寬
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(60)
        .media_write_timeout(120)
        .pool_timeout(10)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("page", cmd_page))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("bot started (long-polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
