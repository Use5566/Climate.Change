# 登入測試版：Google 試算表與 Render

## 現有功能

### A、B 介面與 Gemini 設定

A、B 共用三階段聊天介面，prompt 檔案分別為 prompts/interface_a.txt 和 prompts/interface_b.txt。暫用 A 提示為雙面理由整理，B 為蘇格拉底式追問；教師可改寫整份文字檔。兩組共用模型與生成參數，後端只把立場與對話內容交給 Gemini，不傳班級、座號、登入密碼或 Google 憑證。模型名稱與 prompt 雜湊保留在系統恢復資料中，方便辨識版本。

在既有 Render 服務的 Environment 新增 GEMINI_API_KEY（填真實 API key）與 GEMINI_MODEL（填可用模型 ID，不含 models/），再儲存並部署。程式刻意未預設模型，避免替教師選定費率或不可用型號。原 Google 憑證繼續使用，服務帳戶須為試算表編輯者。可設定 LEARNING_STORAGE=google_sheets；不需持久磁碟。render.yaml 提供設定名稱，既有非 Blueprint 服務仍須手動新增環境變數。

紀錄分頁沿用 A學習歷程、B學習歷程、C學習歷程，均使用下述 11 欄。每位學生保留一列，首次使用時接在末端，不預留座號空白列；每 35 秒依班級、座號與介面合併覆蓋有變更的進度，提交立即同步。A、B 保留完整對話；AI 失敗保留問題供重試。前台不能覆寫歷史對話或指定另一組 prompt。沒有金鑰不會產生假 AI 回答。本機 work/preview_ab.py 僅使用隔離的模擬 AI 與假名冊，未納入部署。

測試版限制：每題最多 1,000 字，最多 20 輪問答，累積對話接近容量上限時需進入摘要。推論最多 10,000 字，Google 備註與單格總容量也有檢查。每位學生五分鐘最多 30 次對話請求；目前仍需單一 worker／實例，尚未完成全班同時使用的配額與負載驗證。真實模型可用性、API 費用與教學回覆品質由教師設定後實測。

ABC 預設使用 Google 試算表紀錄。不需要持久磁碟；下方 SQLite 路徑說明僅適用於手動選用 LEARNING_STORAGE=sqlite 的 C 舊儲存方式。

### Google 試算表學習歷程

沿用現有服務帳戶憑證，該帳戶須具有指定試算表的「編輯者」權限。登入模組仍使用唯讀 scope；學習紀錄模組單獨使用 spreadsheets 讀寫 scope。此授權的範圍是整份試算表，程式對「A學習歷程」、「B學習歷程」、「C學習歷程」分頁寫入，不對 gid 0 寫入。首次載入各介面時，將舊歷程與對應進度分頁中的最新版本合併，去除重複紀錄及空白列。更新歷程與隱藏對應進度分頁在同一次原子 batchUpdate 完成；進度分頁內容不刪除，不再建立新的進度分頁。若版本衝突或備註損壞則停止合併。活動進行中分析請使用副本，不可直接排序、刪列或插列。

資料列統一高度 60 像素、文字靠上並裁切顯示，不因長篇對話撐高。完整內容仍保存於儲存格，可選取儲存格檢視或下載分析。每次程序首次載入會重新建立學生與資料列的對應，完成合併及格式設定；後續每 35 秒只更新有變更的紀錄。

第一次開啟各介面時會建立紀錄分頁與欄名。若已有同名分頁但欄名不符，停止寫入而不覆蓋（允許欄名首尾空白）。每次同步覆蓋學生固定列，A:K 共 11 欄：紀錄時間、班級、座號、密碼、介面、成績統計、立場、提交狀態、劃記原文、學生提問及AI回答內容、推論。時間使用臺灣時區，表示內容更新時間；座號保留前導零；提交狀態為「草稿」或「已提交」。密碼、成績統計與 C 不使用的 AI 對話欄留白，未評分不填 0。

系統恢復資料（文章版本、劃記位置、草稿版本等）保存於 K 欄推論儲存格的備註，不增加表格欄位；與該列內容透過同一次 updateCells 請求寫入。所有顯示值使用明確的 stringValue，學生文字不會被當成公式執行。請勿修改／刪除原始紀錄及備註；可另製副本分析。CSV 下載可讀取 11 欄顯示內容，但不包含恢復備註，不能作為完整系統備份。既有舊版 14 欄不會自動轉換。

環境變數 LEARNING_STORAGE=google_sheets（預設），不需 LEARNING_DB_PATH，也不需新增環境變數。原 SQLite 紀錄不會自動搬移。必須採單一 worker、單一服務實例；每 35 秒批次同步最新狀態，無變更不呼叫寫入，失敗保留暫存於下輪重試。提交立即同步並等待確認。服務正常時重登入可恢復記憶體暫存，服務重啟後恢復 Google 最近成功同步的版本；網路中斷時可能遺失超過 35 秒的未同步內容。新活動或文章版本須先規劃新的分頁，避免混用既有資料。班級實際壓力測試由教師後續安排。

- 班級 601–605、座號 01–32、五位數字密碼，不收集姓名。
- Python FastAPI 後端以唯讀授權讀取私人 Google 試算表，前台不取得密碼表。
- 正確資料建立兩小時登入狀態，重新整理可恢復；登出後失效。
- 分組欄允許留白，顯示「尚未分配學習介面」。ABC 均進入各自三階段學習頁面；頁面與資料 API 都會檢查登入身分及分組。
- 錯誤密碼與不存在學生採用同一錯誤提示。每個班級座號五分鐘最多八次登入嘗試。
- 單一 Python worker、單一服務實例的測試架構；登入與限流狀態在記憶體，重啟會清除。同一學生再次登入會使上一個登入失效。正式擴充多實例前須改為共用 session 與限流儲存。

## 試算表設定

指定試算表 ID：`1k53Yg6bu8__mEY0leZ2_da5TyEru2i3v9cwkmsnpO-A`，頁籤 gid：`0`。

指定服務帳戶：`sci-video@sci-video-495014.iam.gserviceaccount.com`。

名冊支援原本 4 欄「班級、座號、密碼、介面」，以及教師目前的 11 欄格式（B、C、D、E 欄為班級、座號、密碼、介面）。介面欄也接受「學習介面」。601–605 是班級值，座號 01–32；可容忍試算表將座號顯示為 1–9。密碼必須恰好五個半形數字，建議密碼欄使用純文字以保留前導零。分組留白或填 A/B/C。空白列會略過；缺欄、無效格式、重複班級座號會停止登入，避免認錯資料。2026-09-13 已唯讀核對目前 11 欄名冊中的 160 位學生均可解析。

Google Sheets API 必須在服務帳戶所屬專案啟用。請將這份表格分享給服務帳戶並授予編輯者權限，不需發布到網路。登入核對使用唯讀 scope，學習紀錄寫入使用讀寫 scope。

目前 Google 試算表本身保留教師提供的明文密碼。後端讀取後只將帶有程序內隨機密鑰的 HMAC 摘要快取於記憶體，不落地保存密碼表、不把明文密碼寫進學習紀錄或日誌。這不是正式資料庫的密碼儲存設計；未來改用資料庫時須採用 Argon2id 等密碼雜湊函式。

快取最多 30 秒，修改密碼或分組後最晚約 30 秒在下一次核對生效。快取過期且 Google 連線失敗時，拒絕核對，不使用舊資料放行。密碼變更或刪除學生後，下次 session 驗證將撤銷舊登入。

## 憑證

### 科學教材與 Gemini 明確快取

教材為 UTF-8 TXT，與 A/B prompt 分開保存，不放入公開 GitHub 或 dist/。本機可放在 private/science-knowledge.txt；Render 則以 Secret File 保存 science-knowledge.txt，再設定：

| 環境變數 | 值 |
|---|---|
| GEMINI_KNOWLEDGE_MODE | explicit |
| GEMINI_KNOWLEDGE_PATH | /etc/secrets/science-knowledge.txt |
| GEMINI_CACHE_TTL_SECONDS | 3600 |

既有 GEMINI_API_KEY、GEMINI_MODEL 繼續使用。未設定教材模式與路徑時維持原本對話；設定 explicit 後若教材缺失、配額不足或快取失敗，就提示錯誤，不會退回沒有教材的回答。TTL 可設 300–14400 秒，預設一小時，到期不在背景續期；下次提問才重建。教材檔上限 2 MB，建立前以 countTokens 確認實際用量，並依模型 inputTokenLimit 預留 32,768 tokens 給對話。

A/B 各建立一份「完整教材＋本組教學指令」快取；學生立場、問題、對話不會進入共用快取。快取依模型、教材內容雜湊與指令版本識別，程序重啟時會搜尋可重用的快取，避免每位學生各建立一份。修改教材或 prompt 後舊快取仍會存到原到期時間；不自動刪除可能仍被處理中的請求使用的快取。不要把快取當成永久教材備份。

教師可在已設定上述環境變數的後端執行 `python -m backend.knowledge_cache`，在上課前先建立／重用 A、B 快取。此命令會呼叫付費快取 API，僅顯示教材雜湊、token 數（API 有提供時）、模型及到期時間，不顯示教材與金鑰。未預先準備時，第一題可能較慢；A/B 前台等候上限調整為 180 秒。

每則 AI 回覆的系統恢復資料保存 knowledge_sha256、cache_fingerprint 與 token_usage（輸入、快取、回答、思考、總量，依 API 回傳）。沿用 11 欄，放在 K 欄備註內，不把教材全文複製進試算表。這是成功保留回覆的用量，不等同完整帳單：失敗請求、快取儲存與預先建立等費用仍以 Google 帳單為準。模型被要求引用教材來源／章節，但來源正確性與教學品質仍須真實問答驗證。

本次已完成模擬測試；實際教材 token 數、帳號的快取支援、配額與回答品質須在私密教材及金鑰可用後確認。官方參考：https://ai.google.dev/api/caching 。

### Google 試算表憑證

設定以下其中一種即可，兩者同時存在時 JSON 環境變數優先：

1. `GOOGLE_SERVICE_ACCOUNT_JSON`：在 Render 私密環境變數填入完整服務帳戶 JSON。
2. Render Secret File `google-service-account.json`，並設定 `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/google-service-account.json`。

不要將憑證上傳 GitHub。`.gitignore` 已排除 private/、work/、JSON 金鑰、Excel、CSV 與環境設定檔。Google 試算表網址與服務帳戶地址不是私密金鑰。

## 部署到 Render

將沙盒內程式碼作為 GitHub 儲存庫根目錄，僅提交 backend/、dist/、prompts/、tests/、requirements.txt、render.yaml、README.md、DEPLOY.md、.gitignore、.env.example。若使用其他儲存庫結構，Render Root Directory 要指向這些檔案所在目錄。

- 類型：Python Web Service。
- Build Command：`pip install -r requirements.txt`
- Start Command：`uvicorn backend.app:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log`
- Health Check Path：`/healthz`（只代表網站程序運行，不代表 Google 核對成功）。
- `COOKIE_SECURE=true`（預設），使用 Render HTTPS 網址。
- 加入上述憑證設定。

也可使用 render.yaml 建立 Blueprint，並在 Render 手動填寫憑證環境變數。

學生透過 Render 網址開啟網站，前後台同一來源。不能用雙擊 HTML 或單獨 GitHub Pages 代替此測試版本的 Python 網址。正式部署不依賴教師電腦或 G 槽。

## 本機開發測試

### C 介面的舊 SQLite 儲存（僅 LEARNING_STORAGE=sqlite）

本機預設使用 private/learning.sqlite3。保存班級、座號、文章版本與原文、立場、劃記位置及原文、推論、開始／更新／提交時間；不保存登入密碼。每位學生每個文章版本保留一份草稿或提交結果，草稿更新會覆蓋上一版，不是完整互動事件紀錄。提交結果不可由學生修改。資料庫及其附屬檔案已加入 Git 忽略規則。

部署 C 前，須先規劃持久儲存，再設定 LEARNING_DB_PATH 指向實際持久磁碟中的檔案，例如 /var/data/learning.sqlite3。只設定路徑不會自動建立持久磁碟；不可填入一般暫存目錄來取代。未設定此環境變數時，Render 環境下 C 介面會顯示儲存服務未就緒，避免將學生紀錄誤存在暫存檔案系統。現有 render.yaml 不會自動建立磁碟，這次本機開發亦未修改 Render 服務。

維持單一服務實例、單一 worker。後續教師下載功能尚待製作；備份資料庫即可保留目前的原始紀錄。正式文章更新時須更換 backend/article.py 的 ARTICLE_ID。

### 測試方式

建立獨立虛擬環境並安裝 requirements.txt；測試另需 httpx 與 pytest。設定 GOOGLE_APPLICATION_CREDENTIALS 指向私密 JSON 檔案，本機 HTTP 測試時可設定 COOKIE_SECURE=false，再執行 `uvicorn backend.app:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log`。

`python -m pytest tests -q` 使用虛構資料，涵蓋登入、錯誤密碼、分組、欄位格式、缺失憑證、重複名冊、密碼變更、限流、登出與禁止讀取私人檔案。新增批次儲存測試涵蓋 30 位模擬學生合併一次請求、固定列覆蓋、重啟恢復、提交失敗重試與 ABC 合併。測試不修改線上試算表，不等同實際壓力測試。

C 介面測試另涵蓋資料隔離、草稿持久化、提交重試、版本衝突及不合法劃記。以 `node tests/test_highlights.mjs` 檢查文字範圍的加入、取消、部分移除、合併與 Unicode 索引。


