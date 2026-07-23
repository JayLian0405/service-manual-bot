# -*- coding: utf-8 -*-
"""Gemini 問答模組（多模態版）
中文問題 -> 英文檢索詞 -> FTS 撈手冊頁 -> 改餵「頁面圖片」-> 繁中回答附頁碼

v2 變更：
  - AI 問答預設改送整頁 PNG（Gemini 多模態直接看原版表格/圖面），
    不再送攤平文字，消除表格欄位配錯的風險。
  - 頁面圖與 bot.py 共用 page_png/ 快取（同一命名 pN.png），
    /ask 撈過的頁，之後翻頁秒出圖。
  - config.json 新增 "llm_use_page_images": true（設 false 退回純文字模式）；
    任一頁渲染失敗時，該頁自動退回文字，不中斷回答。
"""
import json
import os
import re
import sqlite3

import fitz  # PyMuPDF：渲染頁面圖用
from google import genai
from google.genai import types

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8-sig"))
DB_PATH = os.path.join(BASE, "manual.db")
PNG_DIR = os.path.join(BASE, "page_png")
os.makedirs(PNG_DIR, exist_ok=True)

MODEL = CONFIG.get("model", "gemini-3.6-flash")
MAX_PAGES = CONFIG.get("max_llm_pages", 8)
USE_IMAGES = CONFIG.get("llm_use_page_images", True)
RENDER_DPI = CONFIG.get("render_dpi", 150)

client = (
    genai.Client(api_key=CONFIG["gemini_api_key"])
    if CONFIG.get("gemini_api_key")
    else None
)

_pdf = None  # lazy-open，只在需要渲染新頁時開啟


SYSTEM = (
    "你是大金 VRV 空調 (REYQ12-60BYLT 系列) 維修手冊查詢助理，服務台灣的維修技師。"
    "手冊為英文，內容以「頁面圖片」提供，每張圖前一行標示其 PDF 頁碼；"
    "表格與圖面請直接依圖片判讀，你只能根據提供的手冊頁回答，不可以憑空編造。"
    "回答一律使用繁體中文（台灣用語），專有名詞可附英文原文。"
    "每個論點都要標註出處 PDF 頁碼，格式如 (p.301)。"
    "若提供的頁面不足以回答，直接說明查不到，並建議技師可嘗試的關鍵字或故障碼。"
    "回答要精簡、條列化，方便技師在手機上閱讀。最後用一行列出建議查看的頁碼，"
    "格式固定為：建議頁面: p.301, p.302（讓系統解析出附圖頁）。"
)


def extract_keywords(question: str) -> list[str]:
    """中文問題轉英文檢索關鍵字（手冊是英文，FTS 需要英文詞）"""
    resp = client.models.generate_content(
        model=MODEL,
        contents=question,
        config=types.GenerateContentConfig(
            system_instruction=(
                "Convert the technician's question about a Daikin VRV air conditioner "
                "into 3-6 English search keywords/phrases likely to appear in the "
                "English service manual (e.g. 'high pressure switch', 'thermistor', "
                "'drain pump', 'error code'). Reply with ONLY a JSON array of strings."
            ),
            temperature=0,
        ),
    )
    m = re.search(r"\[.*\]", resp.text or "", re.S)
    return json.loads(m.group(0)) if m else []


def fts_pages(keywords: list[str], limit: int = MAX_PAGES) -> list[tuple[int, str, str]]:
    """回傳 [(pdf_page, part, text)]，以 FTS bm25 排序"""
    db = sqlite3.connect(DB_PATH)
    try:
        seen, results = set(), []
        for kw in keywords:
            tokens = [t for t in re.findall(r"[A-Za-z0-9]+", kw) if t]
            if not tokens:
                continue
            q = " ".join(tokens) if len(tokens) > 1 else tokens[0]
            try:
                rows = db.execute(
                    "SELECT rowid FROM pages_fts WHERE pages_fts MATCH ? "
                    "ORDER BY bm25(pages_fts) LIMIT ?",
                    (f'"{q}"' if len(tokens) > 1 else q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for (pno,) in rows:
                if pno not in seen:
                    seen.add(pno)
                    results.append(pno)
        results = results[:limit]
        out = []
        for pno in results:
            part, text = db.execute(
                "SELECT part, text FROM pages WHERE pdf_page=?", (pno,)
            ).fetchone()
            out.append((pno, part, text))
        return out
    finally:
        db.close()


def _page_png_bytes(pdf_page: int) -> bytes:
    """取頁面 PNG（與 bot.py 共用 page_png/ 快取；沒有就現場渲染並存檔）"""
    path = os.path.join(PNG_DIR, f"p{pdf_page}.png")
    if not os.path.exists(path):
        global _pdf
        if _pdf is None:
            _pdf = fitz.open(CONFIG["pdf_path"])
        pix = _pdf[pdf_page - 1].get_pixmap(dpi=RENDER_DPI)
        pix.save(path)
    with open(path, "rb") as f:
        return f.read()


def _build_contents(question: str, pages: list[tuple[int, str, str]]) -> list:
    """組多模態 contents：每頁一行頁碼標示 + 整頁圖片；渲染失敗的頁退回文字"""
    contents: list = []
    for pno, part, text in pages:
        header = f"=== PDF p.{pno} ({part}) ==="
        if USE_IMAGES:
            try:
                png = _page_png_bytes(pno)
                contents.append(header + "（頁面圖如下）")
                contents.append(
                    types.Part.from_bytes(data=png, mime_type="image/png")
                )
                continue
            except Exception:
                pass  # 單頁渲染失敗 -> 該頁退回文字，不中斷
        contents.append(f"{header}\n{text[:3500]}")
    contents.append(f"技師的問題：{question}")
    return contents


def answer(question: str) -> tuple[str, list[int]]:
    """回傳 (繁中回答, 建議附圖的 pdf 頁碼列表)"""
    if client is None:
        return ("尚未設定 gemini_api_key，無法使用智慧問答。"
                "請直接輸入故障碼（如 E3）或英文關鍵字查詢。", [])

    keywords = extract_keywords(question)
    pages = fts_pages(keywords)
    if not pages:
        return (f"用關鍵字 {keywords} 在手冊中找不到相關頁面，"
                "請改用故障碼或其他說法再試一次。", [])

    resp = client.models.generate_content(
        model=MODEL,
        contents=_build_contents(question, pages),
        config=types.GenerateContentConfig(system_instruction=SYSTEM),
    )
    text = resp.text or ""
    cited = [int(p) for p in re.findall(r"p\.?\s*(\d{1,3})", text)]
    # 只保留真的有撈到的頁碼，去重保序（防幻覺驗證，維持不變）
    valid = {pno for pno, _, _ in pages}
    pics, seen = [], set()
    for p in cited:
        if p in valid and p not in seen:
            seen.add(p)
            pics.append(p)
    return text, pics[:3]
