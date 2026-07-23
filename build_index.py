# -*- coding: utf-8 -*-
"""
維修手冊索引建置器
讀取大金維修手冊 PDF，產出 manual.db (SQLite)：
  - pages       : 逐頁文字（pdf_page 為 1-based PDF 頁碼）
  - pages_fts   : FTS5 全文索引
  - toc         : PDF 書籤目錄
  - sections    : Part 6 故障碼明細章節 (4.NN 標題 + 起訖頁)
  - section_codes : 故障碼 -> 章節 對照（一碼可對多章節）
用法: python build_index.py
"""
import json
import os
import re
import sqlite3
import sys

import fitz  # PyMuPDF

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8-sig"))
PDF_PATH = CONFIG["pdf_path"]
DB_PATH = os.path.join(BASE, "manual.db")

# 故障碼樣式：兩碼英數 (A0/E3/UH...)，可帶子碼 (A3-01/AH-12)
CODE_RE = re.compile(r"^[A-Z][0-9A-Z](?:-\d{1,2})?$")
HEAD_RE = re.compile(r"^(\d+\.\d+)\s+(\S.*)$")


def main():
    doc = fitz.open(PDF_PATH)
    print(f"pages: {doc.page_count}")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE pages (pdf_page INTEGER PRIMARY KEY, part TEXT, text TEXT);
        CREATE VIRTUAL TABLE pages_fts USING fts5(text, content='pages', content_rowid='pdf_page');
        CREATE TABLE toc (level INTEGER, title TEXT, pdf_page INTEGER);
        CREATE TABLE sections (id INTEGER PRIMARY KEY, num TEXT, title TEXT,
                               start_page INTEGER, end_page INTEGER);
        CREATE TABLE section_codes (code TEXT, section_id INTEGER);
        CREATE INDEX idx_code ON section_codes(code);
        """
    )

    # --- TOC 與 Part 標籤 ---
    toc = doc.get_toc()
    parts = []  # (start_page, part_title)
    for level, title, page in toc:
        db.execute("INSERT INTO toc VALUES (?,?,?)", (level, title.strip(), page))
        if level == 1:
            parts.append((page, title.strip()))
    parts.sort()

    def part_of(pno1):
        cur = ""
        for start, title in parts:
            if pno1 >= start:
                cur = title
            else:
                break
        return cur

    # --- 逐頁文字 ---
    for pno in range(doc.page_count):
        text = doc[pno].get_text()
        db.execute(
            "INSERT INTO pages VALUES (?,?,?)", (pno + 1, part_of(pno + 1), text)
        )
        db.execute(
            "INSERT INTO pages_fts(rowid, text) VALUES (?,?)", (pno + 1, text)
        )

    # --- Part 6 故障碼章節 ---
    # 找 "Part 6 Service Diagnosis" 起訖
    p6_start = next(p for p, t in parts if t.startswith("Part 6"))
    p6_end = next((p - 1 for p, t in parts if t.startswith("Part 7")), doc.page_count)
    print(f"Part 6: PDF p{p6_start} ~ p{p6_end}")

    sections = []  # (num, title, start_page)
    for pno1 in range(p6_start, p6_end + 1):
        lines = [l.strip() for l in doc[pno1 - 1].get_text().split("\n") if l.strip()]
        for i, line in enumerate(lines[:14]):  # 標題在頁面上方
            m = HEAD_RE.match(line)
            if m:
                num, title = m.group(1), m.group(2).rstrip(", ")
                # 標題可能折行：下一行若非欄位關鍵字則併入
                if i + 1 < len(lines) and not re.match(
                    r"^(Applicable|Error Code|Method|Note|k:|\d)", lines[i + 1]
                ) and not HEAD_RE.match(lines[i + 1]) and len(lines[i + 1]) < 60:
                    title = (title + " " + lines[i + 1]).rstrip(", ")
                sections.append((num, title, pno1))
                break
            # 標題拆成兩行：'4.40' / 'Thermistor Abnormality'
            if re.fullmatch(r"\d+\.\d+", line) and i + 1 < len(lines):
                sections.append((line, lines[i + 1].rstrip(", "), pno1))
                break

    # 去重（同一章節跨頁重印標題時取首頁），補 end_page
    seen, uniq = set(), []
    for num, title, sp in sections:
        if num not in seen:
            seen.add(num)
            uniq.append([num, title, sp])
    uniq.sort(key=lambda s: s[2])
    for i, sec in enumerate(uniq):
        end = uniq[i + 1][2] - 1 if i + 1 < len(uniq) else p6_end
        sid = i + 1
        db.execute(
            "INSERT INTO sections VALUES (?,?,?,?,?)", (sid, sec[0], sec[1], sec[2], end)
        )
        # 章節範圍內掃 'Error Code' 區塊抓碼
        codes = set()
        for pno1 in range(sec[2], end + 1):
            lines = [
                l.strip() for l in doc[pno1 - 1].get_text().split("\n") if l.strip()
            ]
            for j, line in enumerate(lines):
                if line == "Error Code" and j + 1 < len(lines):
                    # 碼列可能是 'H1, H9, J3' 或單碼，連續多行
                    k = j + 1
                    while k < len(lines):
                        cand = [c.strip() for c in lines[k].split(",")]
                        if all(CODE_RE.match(c) for c in cand if c):
                            codes.update(c for c in cand if c)
                            k += 1
                        else:
                            break
        for code in sorted(codes):
            db.execute("INSERT INTO section_codes VALUES (?,?)", (code, sid))

    db.commit()
    n_sec = db.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    n_code = db.execute("SELECT COUNT(DISTINCT code) FROM section_codes").fetchone()[0]
    print(f"sections: {n_sec}, distinct codes: {n_code}")
    for row in db.execute(
        "SELECT code, s.num, s.title, s.start_page FROM section_codes c"
        " JOIN sections s ON s.id=c.section_id ORDER BY code LIMIT 15"
    ):
        print("  ", row)
    db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
