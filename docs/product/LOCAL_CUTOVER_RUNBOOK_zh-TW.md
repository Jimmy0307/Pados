# 本機環境升級手冊（Local Cutover Runbook，zh-TW）

適用情境：Owner **已經**把 #35 → #36 → #37 → #38 → #39 合併進 `main` 之後，要把現有的本機環境
`PADOS-worktrees/pados-acceleration-20261114`（以下稱 `<checkout>`）從舊的 `main` `9b975a9` 升級到合併後的 `main`。

**這份手冊只是程序，撰寫時沒有對 Owner 的環境執行任何一步。** 撰寫時只做過唯讀檢查（2026-11-16）：
`<checkout>` 是 detached checkout、位於 `9b975a9`、`git status --porcelain` 為空；`pados serve` 在 `127.0.0.1:8088`，
Vite 開發伺服器在 `127.0.0.1:5173`，兩者都是在前景視窗中啟動的；`.env` 只檢查了**鍵名**（沒有讀取或印出任何值）。

原則：

- **不破壞**：不刪資料庫、不重跑 `pados init`、不改 `.env` 的既有值、不換 control token。
- **可回退**：先記錄目前的 SHA 並備份，再更新程式碼。
- **不印出機密**：資料庫密碼、`control.token` 的內容都不要顯示在畫面上或貼到任何地方。
- 所有指令都在 Windows PowerShell 5.1、`<checkout>` 資料夾內執行，除非另外說明。

```powershell
$checkout = Join-Path $env:USERPROFILE 'PADOS-worktrees\pados-acceleration-20261114'
Set-Location $checkout
```

---

## (a) 停止正在執行的服務（只停 8088 與 5173 上的 PADOS）

**建議做法：** 回到當初啟動它們的兩個視窗，各按一次 `Ctrl+C`：

- `pados serve` 的 PowerShell 視窗（指令是 `.venv\Scripts\python.exe -m pados.cli.main serve` 或 `pados serve`）；
- Vite 的視窗（`npm run dev`，實際程序是 `node ...\web\node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5173`）。

**找不到視窗時：** 先找出持有連接埠的程序，**確認身分後**才停止。絕不使用 `Stop-Process -Name python` 或
`Stop-Process -Name node`（會停掉其他不相關的程式）。

```powershell
foreach ($port in 8088, 5173) {
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
    [pscustomobject]@{ Port = $port; Pid = $p.ProcessId; Name = $p.Name
                       CommandLine = $p.CommandLine; ParentExe = $parent.ExecutablePath }
  }
} | Format-List
```

只有在符合下列條件時才停止該 PID：

| 連接埠 | 應該看到 |
| --- | --- |
| 8088 | `python.exe`，`CommandLine` 含 `-m pados.cli.main serve`，`ParentExe` 是 `<checkout>\.venv\Scripts\python.exe`（venv 的轉接程式，真正的直譯器是它的子程序） |
| 5173 | `node.exe`，`CommandLine` 含 `<checkout>\web\node_modules\` 底下的 `vite.js` 與 `--port 5173` |

```powershell
Stop-Process -Id <8088 的 Pid>
Stop-Process -Id <5173 的 Pid>
```

停止持有連接埠的程序後，它的父程序（venv 轉接程式、`npm`/`cmd.exe`）會自己結束。確認兩個連接埠都已釋放：

```powershell
Get-NetTCPConnection -LocalPort 8088, 5173 -State Listen -ErrorAction SilentlyContinue   # 應該沒有輸出
```

> 不要用新的啟動器 `-Stop` 來停舊的服務：`Start-PADOS.ps1 -Stop` 只會停止**它自己**啟動並記在
> `runtime\local\pados-launcher.json` 的程序，而目前的服務不是由它啟動的（舊的 `9b975a9` 也還沒有這個啟動器）。

PostgreSQL **不用停**，資料庫在整個升級過程中保持運作（備份需要它）。

---

## (b) 確認工作樹乾淨，並記錄回退用的 SHA

```powershell
git status --porcelain          # 必須沒有任何輸出
git rev-parse HEAD              # 撰寫時是 9b975a9ed1831c6040234cee3089f9fc37fc34d6
git rev-parse --abbrev-ref HEAD # 撰寫時是 HEAD（detached）
```

`git status --porcelain` 有任何輸出就**停下來**：先弄清楚那些變更是什麼，不要繼續。`.env`、`runtime\local\`、`.venv\`、
`web\node_modules\` 都在 `.gitignore` 裡，所以它們不會出現在這裡；這是正常的，它們由 (c) 備份。

建立備份資料夾（放在 checkout **之外**），把 SHA 寫進去：

```powershell
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $env:USERPROFILE "PADOS-backups\cutover-$stamp"
New-Item -ItemType Directory -Force $backup | Out-Null
# 只有自己（與 SYSTEM）可以讀寫：備份裡有 .env 與 control.token
icacls $backup /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
git rev-parse HEAD | Set-Content -Encoding ascii (Join-Path $backup 'ROLLBACK_SHA.txt')
```

---

## (c) 備份 `.env`、`runtime\local\` 與三個 PADOS 資料庫

### 檔案

```powershell
Copy-Item .env (Join-Path $backup '.env')
robocopy runtime\local (Join-Path $backup 'runtime-local') /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }   # 0 到 7 代表成功
```

`runtime\local\` 包含 `control.token`、`workspace\`（專案資料夾，例如 `first-dogfood`）、`objects\` 與 `artifacts\`。
若 `.env` 的 `PADOS_ARTIFACT_ROOT` 指向 `runtime\local\` **以外**的位置，請把那個資料夾也一起複製。

### 資料庫（`pg_dump`，密碼不顯示）

資料庫名稱來自 `.env` 的三個鍵：`PADOS_OPERATIONAL_DATABASE_NAME`、`PADOS_PRODUCT_DATABASE_NAME`、
`PADOS_CORPUS_DATABASE_NAME`（撰寫時唯讀確認：三個名稱各不相同，`PADOS_DB_NAME` 與其中一個相同，所以三份就是全部）。
下面的程式碼把 `.env` 讀進記憶體，**不會**印出任何值；密碼只放在這個 PowerShell 程序的 `PGPASSWORD` 環境變數裡，
做完立刻移除。

```powershell
$cfg = @{}
Get-Content .env | Where-Object { $_ -match '^[A-Z0-9_]+=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; $cfg[$k.Trim()] = $v.Trim().Trim('"')
}
$pgDump    = 'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe'      # 依實際安裝位置調整
$pgRestore = 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe'
$env:PGPASSWORD = $cfg['PADOS_DB_PASSWORD']
try {
  foreach ($key in 'PADOS_OPERATIONAL_DATABASE_NAME', 'PADOS_PRODUCT_DATABASE_NAME', 'PADOS_CORPUS_DATABASE_NAME') {
    $out = Join-Path $backup "db-$key.dump"
    & $pgDump -h $cfg['PADOS_DB_HOST'] -p $cfg['PADOS_DB_PORT'] -U $cfg['PADOS_DB_USER'] -Fc -f $out $cfg[$key]
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed for $key" }
    & $pgRestore --list $out | Out-Null                                  # 確認備份檔可以讀
    if ($LASTEXITCODE -ne 0) { throw "dump for $key is not readable" }
  }
} finally {
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  $cfg = $null
}
Get-ChildItem $backup | Select-Object Name, Length
```

錯誤訊息只會顯示鍵名（例如 `PADOS_PRODUCT_DATABASE_NAME`），不會顯示密碼。

---

## (d) 更新程式碼

`<merged main sha>` 是 Owner 合併 #39 後 `main` 的 commit（在 GitHub 的 PR 頁面或 `git log origin/main` 查到）。

```powershell
git fetch origin
git log --oneline -5 origin/main                 # 找到 <merged main sha>，確認 #35 到 #39 都在裡面
git checkout --detach <merged main sha>
git rev-parse HEAD                               # 必須等於 <merged main sha>
git status --porcelain                           # 仍然必須沒有輸出
```

不要用 `git pull`、`git reset --hard` 或 `git clean`：它們不需要，而且 `git clean -x` 會刪掉 `.env` 與 `runtime\local\`。

---

## (e) 相依套件

```powershell
.\.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[dev]"
Set-Location web; npm ci; Set-Location ..
```

撰寫時查證：`git diff 9b975a9..4671127 -- requirements.lock pyproject.toml web/package.json web/package-lock.json`
沒有任何差異（#35–#39 沒有改相依套件）。所以這一步預期不會改變任何版本；仍然要執行，因為合併後的 `main` 可能含有其他
PR 的變更。`npm ci` 會重建 `web\node_modules\`，執行前 Vite 必須已經停止（見 (a)）。

---

## (f) 資料庫 migration：**不需要**

撰寫時查證：

```powershell
git diff 9b975a9..4671127 --stat -- migrations 'alembic*'    # 沒有任何輸出
```

`4671127` 是撰寫時 #39 的起點（堆疊頂端）；之後加到 #39 的 commit 只改 CI 工作流程、測試與文件，沒有 migration 也沒有相依套件。#35–#39 **沒有新增或修改任何 Alembic migration**，`src/pados/config.py` 中預期的
資料庫版本也沒有變（操作資料庫 `20261101_0020`、corpus `20260730_0011`）。所以**不要**執行 `alembic upgrade`；
三個資料庫保持原樣。Owner 合併時若 `main` 另外收進了含 migration 的 PR，請對 `<merged main sha>` 重跑上面的指令
（把 `4671127` 換成 `<merged main sha>`）；有輸出時先停下來，依該 PR 的說明處理，不要套用本手冊。

---

## (g) 保留 `.env`、token 與資料庫；新的選用設定

`.env`、`runtime\local\control.token` 與三個資料庫**都保留原樣**，升級後照常使用。

這個堆疊新增的設定**全部是選用的**，沒設定時就是關閉或使用預設值，行為和以前一樣。撰寫時唯讀確認：您的 `.env`
目前**沒有**下表任何一個鍵。

| 設定 | 沒設定時 | 建議 |
| --- | --- | --- |
| `PADOS_IDENTITY_SESSIONS_ENABLED` | `false`：瀏覽器配對關閉，Control Center 照舊要求貼上 control token | 保持 `false`，除非您決定使用配對（見下方） |
| `PADOS_IDENTITY_STORE_DIR` | `runtime/local/identity`，相對於 `pados serve` 的工作目錄；用啟動器時就是 `<checkout>\runtime\local\identity\`（`sessions.json`、`server.key`，第一次使用時以只有您可存取的權限建立） | 不需要加；只有想放到別處時才設定 |
| `PADOS_IDENTITY_SESSION_TTL_SECONDS`／`PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS` | 24 小時／2 小時 | 不需要 |
| `PADOS_IDENTITY_ALLOWED_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088` | 只有改了連接埠才需要 |
| `PADOS_IDENTITY_OIDC_CONFIG`／`PADOS_IDENTITY_ACTORS_CONFIG` | 未設定 = 關閉（沒有 SSO、沒有 actor 設定） | 不要設定（OIDC 是 NOT_LIVE_VALIDATED） |
| `PADOS_PROVIDERS_SETUP` | `runtime/local/providers.json`；檔案不存在時 **AI 模型與連線** 頁面沒有任何自訂提供者 | 不需要加 |
| `PADOS_STORAGE_POLICY` | 未設定 = 沒有位元組預算，只套用磁碟門檻 | 不需要 |
| `PADOS_COMPUTE_NODES_CONFIG`、`PADOS_MODEL_CATALOG`、`PADOS_CHAT_PROVIDER` | 未設定 = 沒有授權任何模型 | 見 [DOGFOOD_AI_ENABLEMENT_zh-TW.md](DOGFOOD_AI_ENABLEMENT_zh-TW.md) |

### 選擇性：手動開啟瀏覽器配對

只有在您決定要用時才做。開啟後，已配對的瀏覽器權限**比 control token 小**（不能接受結果、不能改管理層級的設定）。

1. 用文字編輯器在 `.env` 最後**加一行**（不要改其他行）：

   ```text
   PADOS_IDENTITY_SESSIONS_ENABLED=true
   ```

2. 依 (h) 重新啟動。
3. 另開 PowerShell 視窗：`.\.venv\Scripts\pados.exe device pair --base-url http://127.0.0.1:8088`，畫面會顯示
   `Pairing code: XXXXX-XXXXX`（token 本身不會顯示）。
4. 在 `http://127.0.0.1:5173/operator.html` → **Control Center** → **This browser & devices** → **Pair this browser**
   輸入配對碼。詳見 [OPERATING_GUIDE_zh-TW.md](OPERATING_GUIDE_zh-TW.md) 第 2 節。

關閉：把那一行改回 `false`（或刪掉）並重新啟動；要同時作廢已發出的工作階段，刪除
`runtime\local\identity\sessions.json`。

---

## (h) 重新啟動與驗證

### 啟動

```powershell
.\scripts\windows\Start-PADOS.ps1 -DryRun   # 只檢查並印出計畫，不啟動任何東西
.\scripts\windows\Start-PADOS.ps1           # 啟動（預設 API 8088、網頁 5173）
```

`-DryRun` 有任何 `[FAIL]` 都先修正再啟動。啟動器**不會**執行 `pados init`，也**不會**修改 `.env` 或資料庫。
輸出中的 `chat_provider=NOT_CONFIGURED (CHAT_PROVIDER_UNCONFIGURED)` 是正常的。

### 驗證（全部是唯讀）

1. 健康狀態：

   ```powershell
   .\scripts\windows\Start-PADOS.ps1 -Status
   Invoke-RestMethod http://127.0.0.1:8088/health/ready
   ```

   就緒報告的 `status` 應為 `OK`（HTTP 200；`NOT_READY` 時是 HTTP 503，`Invoke-RestMethod` 會顯示錯誤）。
   `chat_provider` 這一項顯示未設定是正常的。有未通過的檢查時，啟動器只顯示封閉的錯誤代碼，詳情在 `runtime\logs\`。
2. 程式版本：`git rev-parse HEAD` 等於 `<merged main sha>`。
3. Dogfood 專案還在：開啟 `http://127.0.0.1:5173/operator.html` → **Control Center** → **專案 Projects**，清單中有
   `PADOS-First-Dogfood`（專案 id `95100768-ec1d-4343-b5f5-ce0c4c848dc9`）。配對關閉時，依提示貼上 control token。
4. 唯讀檢查：開啟該專案，查看 **Markdown 文件** 清單與 **工作心智圖（Work Map）**；不要新增、核准或執行任何東西。
   專案、既有的任務與結果都應該和升級前一樣。

---

## (i) 回退

由輕到重，通常只需要第 1 到 3 步：

1. 停止：`.\scripts\windows\Start-PADOS.ps1 -Stop`（新啟動器啟動的程序可以這樣停）。
2. 回到原本的程式碼：

   ```powershell
   git checkout --detach (Get-Content (Join-Path $backup 'ROLLBACK_SHA.txt'))   # 9b975a9...
   .\.venv\Scripts\python.exe -m pip install -c requirements.lock -e ".[dev]"
   Set-Location web; npm ci; Set-Location ..
   ```

3. 用原本的方式啟動（舊的 `9b975a9` 沒有 `scripts\windows\Start-PADOS.ps1`）：一個視窗執行 `.\.venv\Scripts\pados.exe serve`，
   另一個視窗執行 `cd web; npm run dev`。
4. **只有在需要時**才還原備份：
   - `.env`：`Copy-Item (Join-Path $backup '.env') .env`（只在 `.env` 被改壞時）；
   - `runtime\local\`：從 `$backup\runtime-local` 複製回來（只在檔案遺失或損壞時）；
   - 資料庫：`pg_restore --clean --if-exists -d <資料庫名稱> <備份檔>`。這一步會覆蓋資料庫內容，**只在資料確實損壞時**
     才做。因為 #35–#39 沒有 migration，單純切換程式碼**不需要**還原資料庫。
5. 新增的檔案可以安全刪除：`runtime\local\identity\`（工作階段儲存區；刪除後所有已配對的瀏覽器需要重新配對，control
   token 不受影響）與 `runtime\local\providers.json`（若您建立過）。舊的程式碼不會讀取它們，留著也沒有影響。

接受過的結果（ScopeVersion）、專案與稽核紀錄都在資料庫裡，回退程式碼不會改變它們。

---

## 既有的 `PADOS-First-Dogfood` 專案與新的文件功能

#36 新增的 **Markdown 文件** 功能只會在專案的根資料夾**位於核准的工作區之內**時允許寫入
（`.env` 的 `PADOS_PRODUCT_WORKSPACE_ROOT`，必須是絕對路徑；專案根資料夾必須在它**底下**，不能等於它）。
不符合時，清單與內容仍然可以**讀取**，但新增或編輯會被拒絕：`PROJECT_ROOT_OUTSIDE_WORKSPACE`
（畫面顯示「這個專案的資料夾不在核准的工作區內：文件為唯讀。」）。這不是錯誤，也不會改動任何檔案。

撰寫時唯讀確認：您的 `.env` 有 `PADOS_PRODUCT_WORKSPACE_ROOT`，它是絕對路徑，指向 `<checkout>\runtime\local\workspace`，
而那個資料夾裡有 `first-dogfood`。這**看起來**是 Dogfood 專案的資料夾，但專案實際登記的根資料夾存在資料庫裡，撰寫時沒有
讀取（需要 token 或資料庫密碼），所以請您自己確認。

**怎麼確認（唯讀）：** 升級並啟動後，

- 在 Control Center 開啟專案的 **Markdown 文件**：若顯示上面那句唯讀訊息，或嘗試儲存時出現
  `PROJECT_ROOT_OUTSIDE_WORKSPACE`，就是唯讀；或
- 在 PowerShell 用 control token 讀取文件清單的 `writable` 欄位（token 只放進 HTTP 標頭，不會顯示）：

  ```powershell
  $headers = @{ 'X-PADOS-Control-Token' = (Get-Content runtime\local\control.token -Raw).Trim() }
  (Invoke-RestMethod -Headers $headers `
    http://127.0.0.1:8088/control/projects/95100768-ec1d-4343-b5f5-ce0c4c848dc9/documents).writable
  Remove-Variable headers
  ```

  `True` = 可以在 Control Center 新增與編輯 Markdown；`False` = 文件為唯讀。若回覆
  `PROJECT_ROOT_UNAVAILABLE`，代表登記的根資料夾已不存在或不是資料夾；請檢查 (c) 的備份，不要讓 PADOS 重新建立它。

**唯讀代表什麼：** 只影響 Control Center 的「新增／編輯 Markdown」。專案、工作需求、計畫、核准、TaskLoop、Evidence、
候選結果與 Work Map 都照常運作；您也仍然可以用自己的編輯器直接修改那個資料夾裡的檔案。PADOS 不會搬移或複製專案資料夾；
要讓文件可寫，必須由您決定是否在工作區內建立新專案，本手冊不建議為此修改資料庫。
