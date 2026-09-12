# 登入測試版：Google 試算表與 Render

## 現有功能

- 班級 601–605、座號 01–32、五位數字密碼，不收集姓名。
- Python FastAPI 後端以唯讀授權讀取私人 Google 試算表，前台不取得密碼表。
- 正確資料建立兩小時登入狀態，重新整理可恢復；登出後失效。
- 分組欄允許留白，顯示「尚未分配學習介面」。A、B 組顯示指定分組；C 組進入三階段學習頁面。C 頁面與資料 API 都會檢查登入身分及分組。
- 錯誤密碼與不存在學生採用同一錯誤提示。每個班級座號五分鐘最多八次登入嘗試。
- 單一 Python worker、單一服務實例的測試架構；登入與限流狀態在記憶體，重啟會清除。同一學生再次登入會使上一個登入失效。正式擴充多實例前須改為共用 session 與限流儲存。

## 試算表設定

指定試算表 ID：`1k53Yg6bu8__mEY0leZ2_da5TyEru2i3v9cwkmsnpO-A`，頁籤 gid：`0`。

指定服務帳戶：`sci-video@sci-video-495014.iam.gserviceaccount.com`。

第一列 A1:D1 必須依序為「班級、座號、密碼、介面」，第四欄也接受「學習介面」。601–605 是班級值，座號 01–32；可容忍試算表將座號顯示為 1–9。密碼必須恰好五個半形數字，建議密碼欄使用純文字以保留前導零。分組留白或填 A/B/C。空白列會略過；缺欄、無效格式、重複班級座號會停止登入，避免認錯資料。

Google Sheets API 必須在服務帳戶所屬專案啟用。只需將這份表格分享給服務帳戶（檢視者足夠），不需發布到網路。後端使用 spreadsheets.readonly scope。

目前 Google 試算表本身保留教師提供的明文密碼。後端讀取後只將帶有程序內隨機密鑰的 HMAC 摘要快取於記憶體，不落地保存密碼表、不把明文密碼寫進學習紀錄或日誌。這不是正式資料庫的密碼儲存設計；未來改用資料庫時須採用 Argon2id 等密碼雜湊函式。

快取最多 30 秒，修改密碼或分組後最晚約 30 秒在下一次核對生效。快取過期且 Google 連線失敗時，拒絕核對，不使用舊資料放行。密碼變更或刪除學生後，下次 session 驗證將撤銷舊登入。

## 憑證

設定以下其中一種即可，兩者同時存在時 JSON 環境變數優先：

1. `GOOGLE_SERVICE_ACCOUNT_JSON`：在 Render 私密環境變數填入完整服務帳戶 JSON。
2. Render Secret File `google-service-account.json`，並設定 `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/google-service-account.json`。

不要將憑證上傳 GitHub。`.gitignore` 已排除 private/、work/、JSON 金鑰、Excel、CSV 與環境設定檔。Google 試算表網址與服務帳戶地址不是私密金鑰。

## 部署到 Render

將沙盒內程式碼作為 GitHub 儲存庫根目錄，僅提交 backend/、dist/、tests/、requirements.txt、render.yaml、DEPLOY.md、.gitignore、.env.example。若使用其他儲存庫結構，Render Root Directory 要指向這些檔案所在目錄。

- 類型：Python Web Service。
- Build Command：`pip install -r requirements.txt`
- Start Command：`uvicorn backend.app:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log`
- Health Check Path：`/healthz`（只代表網站程序運行，不代表 Google 核對成功）。
- `COOKIE_SECURE=true`（預設），使用 Render HTTPS 網址。
- 加入上述憑證設定。

也可使用 render.yaml 建立 Blueprint，並在 Render 手動填寫憑證環境變數。

學生透過 Render 網址開啟網站，前後台同一來源。不能用雙擊 HTML 或單獨 GitHub Pages 代替此測試版本的 Python 網址。正式部署不依賴教師電腦或 G 槽。

## 本機開發測試

### C 介面的紀錄儲存

本機預設使用 private/learning.sqlite3。保存班級、座號、文章版本與原文、立場、劃記位置及原文、推論、開始／更新／提交時間；不保存登入密碼。每位學生每個文章版本保留一份草稿或提交結果，草稿更新會覆蓋上一版，不是完整互動事件紀錄。提交結果不可由學生修改。資料庫及其附屬檔案已加入 Git 忽略規則。

部署 C 前，須先規劃持久儲存，再設定 LEARNING_DB_PATH 指向實際持久磁碟中的檔案，例如 /var/data/learning.sqlite3。只設定路徑不會自動建立持久磁碟；不可填入一般暫存目錄來取代。未設定此環境變數時，Render 環境下 C 介面會顯示儲存服務未就緒，避免將學生紀錄誤存在暫存檔案系統。現有 render.yaml 不會自動建立磁碟，這次本機開發亦未修改 Render 服務。

維持單一服務實例、單一 worker。後續教師下載功能尚待製作；備份資料庫即可保留目前的原始紀錄。正式文章更新時須更換 backend/article.py 的 ARTICLE_ID。

### 測試方式

建立獨立虛擬環境並安裝 requirements.txt；測試另需 httpx 與 pytest。設定 GOOGLE_APPLICATION_CREDENTIALS 指向私密 JSON 檔案，本機 HTTP 測試時可設定 COOKIE_SECURE=false，再執行 `uvicorn backend.app:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log`。

`python -m pytest -q` 使用虛構資料，涵蓋登入、錯誤密碼、分組、欄位格式、缺失憑證、重複名冊、密碼變更、限流、登出與禁止讀取私人檔案。測試不修改線上試算表。

C 介面測試另涵蓋資料隔離、草稿持久化、提交重試、版本衝突及不合法劃記。以 `node tests/test_highlights.mjs` 檢查文字範圍的加入、取消、部分移除、合併與 Unicode 索引。


