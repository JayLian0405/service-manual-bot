# -*- coding: utf-8 -*-
"""快速檢視 manual.db 內容：python peek_db.py"""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual.db")
db = sqlite3.connect(DB)

print("=== 資料表清單 ===")
for (name,) in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
):
    cnt = db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    print(f"  {name:16} {cnt} 筆")

print("\n=== pages 範例 (前2頁，文字截前80字) ===")
for pno, part, text in db.execute("SELECT pdf_page, part, text FROM pages LIMIT 2"):
    print(f"  p.{pno} [{part}] {text[:80].strip()!r}")

print("\n=== sections 章節 (前8筆) ===")
for r in db.execute(
    "SELECT num, title, start_page, end_page FROM sections ORDER BY start_page LIMIT 8"
):
    print(f"  {r[0]:6} {r[1][:40]:42} p.{r[2]}~{r[3]}")

print("\n=== section_codes 故障碼對照 (前12筆) ===")
for code, num, title in db.execute(
    "SELECT c.code, s.num, s.title FROM section_codes c "
    "JOIN sections s ON s.id=c.section_id ORDER BY c.code LIMIT 12"
):
    print(f"  {code:8} -> {num:6} {title[:38]}")
db.close()
