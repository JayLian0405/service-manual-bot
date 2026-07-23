# 維修手冊 Telegram 查詢 Bot

技師在 Telegram 輸入「故障碼／英文關鍵字／中文問題」，即時查詢設備維修手冊，回答一律附**頁碼＋原版頁面圖**。範例對象為大金 VRV（REYQ12-60BYLT 系列）英文維修手冊，430 頁、圖表為主。

## 設計核心

- **知識放檢索層，不放模型權重**：手冊不做人工整理、模型不做微調。PDF 跑一次建庫腳本（幾分鐘）即可服務；手冊改版只要重跑腳本重建索引。
- **能查表就不動用 AI**：訊息由嚴到寬四關路由，故障碼與關鍵字走確定性查詢（最準、最快、零 token 成本）；只有中文問題才進 LLM，且套三道防幻覺保險。
- **圖表交給原頁呈現**：切分以「頁」為單位——維修手冊的天然版面單位就是頁，表格、接線圖、排障流程都以頁為界。查詢結果附整頁高解析圖；AI 問答走**多模態**，直接把手冊頁「圖片」餵給模型判讀表格與圖面，不讓 AI 讀攤平文字猜欄位。

## 架構

```
build_index.py  PDF -> manual.db（逐頁文字 FTS5 ＋ PDF 書籤目錄 ＋ 故障碼章節索引）[離線跑一次]
bot.py          Telegram long-polling bot（不需對外開 port / HTTPS）
llm.py          Gemini 多模態問答：中文問題 -> 英文檢索詞 -> FTS 撈頁 -> 餵整頁 PNG -> 繁中回答附頁碼
page_png/       頁面圖快取（首次查詢時渲染，bot 與 llm 共用）
```

### 訊息路由（由嚴到寬）

| 輸入 | 行為 |
|---|---|
| `E3`、`U4`、`A3-01` | 查故障碼對照表，回章節標題＋頁碼＋排障頁面圖（子碼查不到自動退回主碼） |
| `E3是什麼`（短句夾碼） | 一樣走查表，不動用 AI |
| `drain pump`（純英數） | FTS5 全文搜尋，列出命中頁與摘要，按鈕看頁面圖 |
| `室外機高壓跳脫怎麼查？`（中文） | Gemini 多模態問答（下述三道保險） |
| 圖片下方按鈕 | ⬅➡ 直接翻上一頁／下一頁 |

指令版：`/code E3`、`/find high pressure`、`/ask 問題`、`/page 301`、`/id`

### AI 問答三道保險

1. **先翻譯、不搶答**：LLM 的第一個任務只是把中文問題轉成英文檢索詞，找頁交給全文索引（可解釋、可重現）。
2. **只准看撈到的頁**：模型只能根據系統提供的手冊頁回答，查不到就直說並建議關鍵字。
3. **頁碼逐一驗證**：回答中引用的頁碼，程式逐一核對「真的有撈到那一頁」才算數，附圖只出自驗證過的頁。

## 安裝

```bash
pip install -r requirements.txt
copy config.example.json config.json   # 填入 telegram_token / gemini_api_key / pdf_path
python build_index.py                  # 建索引；換版手冊重跑並清空 page_png/
python bot.py
```

- `telegram_token`：Telegram 搜尋 `@BotFather` → `/newbot` 申請。
- `gemini_api_key`：Google AI Studio 申請。不填的話故障碼／關鍵字查詢仍可用，僅中文問答停用。
- `llm_use_page_images`：`true`＝問答餵整頁圖片（多模態，預設）；`false`＝退回純文字模式。單頁渲染失敗會自動退回該頁文字，不中斷回答。

> **注意**：本 repo 不含手冊 PDF、manual.db 與 page_png——手冊內容屬原廠版權，請自備 PDF 建庫。
> `build_index.py` 的章節／故障碼解析針對大金 Si 系列手冊版式（PDF 內建書籤＋Part 6 排障章節首頁的 Error Code 欄位）；其他版式的手冊需自行調整解析規則。

## 權限控管

`config.json` 的 `allowed_user_ids` 留空＝任何人都能用。要限制時請使用者先傳 `/id` 取得 ID，填入陣列後重啟，例如 `[123456789, 987654321]`。

## 常駐服務（Windows / NSSM）

Telegram long-polling 是對外連線，不需要防火牆開洞：

```
nssm install ManualBot "C:\path\to\python.exe" "-X utf8 bot.py"
nssm set ManualBot AppDirectory "C:\path\to\manual_bot"
nssm set ManualBot AppStdout "C:\path\to\manual_bot\service.log"
nssm set ManualBot AppStderr "C:\path\to\manual_bot\service.log"
```

注意：Windows service session 看不到網路磁碟機代號（X: 等），`pdf_path` 若在網路磁碟要改 UNC 路徑（`\\server\share\...`）。

## 網路強韌性

圖片上傳對逾時敏感（預設 5 秒在企業網路容易間歇失敗），bot 已內建：連線逾時放寬（write 60s／media 120s）、`send_photo` 自動重試 3 次、最終仍失敗回覆文字告知使用者，並註冊全域 error handler——任何錯誤都有回應，不會無聲失敗。
