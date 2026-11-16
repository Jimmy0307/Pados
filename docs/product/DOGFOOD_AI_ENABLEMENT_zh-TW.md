# 讓 `PADOS-First-Dogfood` 使用 AI：還缺什麼（zh-TW）

適用版本：#35–#39 合併後的 `main`（升級程序見 [LOCAL_CUTOVER_RUNBOOK_zh-TW.md](LOCAL_CUTOVER_RUNBOOK_zh-TW.md)）。
本文件**只描述步驟**，撰寫時沒有對 Owner 的環境做任何變更。

每一步都標示狀態：

| 標示 | 意義 |
| --- | --- |
| **僅在 TEST 完成** | 在驗證用的**臨時** runtime 上做過（合成資料），有機器本機的證據；**不代表** Owner 的環境已經具備。 |
| **使用者環境尚未完成** | Owner 的環境（`PADOS-worktrees/pados-acceleration-20261114`）目前沒有這一步。 |
| **使用者環境已具備** | 撰寫時以唯讀方式確認存在。 |

三條不變的規則：

- **可連線 ≠ 已授權**。Ollama 有回應、看得到 `qwen3:8b`，只代表「已連線」。
- **匯入 ≠ 授權**。把已測量的設定檔放進模型目錄只是「登錄」，授權必須另外、明確地寫在 compute-node 註冊裡
  （[MEASURED_CAPABILITY_EXECUTION_CONTRACT.md](../architecture/MEASURED_CAPABILITY_EXECUTION_CONTRACT.md)「Operator steps」）。
- **模型輸出永遠不是已接受的事實**。見最後一節。

---

## 0. 現況（撰寫時，2026-11-16，唯讀確認）

| 項目 | Owner 的環境 | TEST 環境 |
| --- | --- | --- |
| Ollama 0.40.16，`qwen3:8b` 已下載 | **使用者環境已具備**（主機上執行中） | 同一台主機 |
| `.env` 的 `PADOS_MODEL_CATALOG` | **使用者環境尚未完成**（沒有這個鍵） | 臨時 runtime 內設定過 |
| `.env` 的 `PADOS_COMPUTE_NODES_CONFIG` | **使用者環境尚未完成**（沒有這個鍵） | 臨時設定，驗證後移除 |
| `.env` 的 `PADOS_CHAT_PROVIDER`／`PADOS_CHAT_MODEL` | **使用者環境尚未完成**（沒有 `PADOS_CHAT_PROVIDER`） | 臨時設定 |
| `PADOS_PROVIDERS_SETUP` | 沒有這個鍵（預設 `runtime/local/providers.json`，不需要設定） | — |
| `qwen3:8b`／Ollama 0.40.16 的已測量設定檔 `mcp-5c6a96b026b6…` | **使用者環境尚未完成**：沒有登錄、沒有授權 | **僅在 TEST 完成**：M2 identity PASS、M3 設定檔產生並登錄（未授權），再以臨時的明確授權跑一次 M4.1 受治理流程 `REAL_TASK_DOGFOOD=PASS`，之後移除授權。證據在驗證 worktree 的 `runtime/artifacts/reports/`（機器本機，未提交） |

結論：Owner 的環境目前**沒有任何模型被視為已測量或已授權**。啟動時顯示
`chat_provider=NOT_CONFIGURED (CHAT_PROVIDER_UNCONFIGURED)` 是正確的。不設定 AI 時，PADOS 的非 AI 流程（專案、
Markdown、工作需求、核准、確定性的 Markdown 參照檢查、Evidence、候選結果）照常運作。

---

## 1. 決定模型目錄的位置 — **使用者環境尚未完成**

- 設定：`PADOS_MODEL_CATALOG`（`src/pados/config.py`，預設空白；空白時所有本機模型呼叫都會被拒絕，絕不會在沒有綁定
  的情況下執行）。
- 格式：一個 JSON 文件，schema `pados.resources.model-catalog.v1`，頂層鍵 `schema_version`、`models`、
  `capability_profiles`、`artifact_locations`（`src/pados/resources/models.py`）。檔案不存在 = 空目錄；超過 4 MiB 或
  不是一般檔案時回報 `MODEL_CATALOG_STORE_INVALID`（`src/pados/local_intelligence/catalog_store.py`）。
- 載入時每個設定檔的 `profile_id` 都會重新計算；內容被改過就會被拒絕。目錄本身**不授權**任何東西。
- 建議位置：`<checkout>\runtime\local\model-catalog.json`（在 `.gitignore` 內，與 token 同一個只屬於您的資料夾）。
  `pados init` 只會寫一行註解 `# PADOS_MODEL_CATALOG=...`，不會建立檔案；**沒有** `pados` 指令會建立或匯入目錄。

---

## 2. 在這台機器、這個執行環境上量測（M2 → M2.1 → M3） — **僅在 TEST 完成**

已測量的設定檔綁定：模型檔案（manifest digest 與 layer digests）× 執行環境（`ollama`、版本 `0.40.16`）× 裝置
（`device_profile_id`）× 執行條件 × 證據。任何一項改變（例如 Ollama 升級）都會產生**不同的** `profile_id`
（`mcp-` 加 64 個十六進位字元），舊的設定檔對新環境顯示為「證據不適用」。

1. **M2 量測**（Windows，從一個乾淨、已知 commit 的 checkout，用它自己的 `.venv`）：

   ```powershell
   .\.venv\Scripts\python.exe scripts\stage_b\windows_m2_benchmark.py --expected-commit <sha> --expected-tree <tree>
   ```

   - 模型、tag、manifest digest 與 layer digests 寫死在腳本裡（`qwen3:8b`，M1 選定的版本），參數無法改變；本機的
     digest 不同就拒絕量測。`qwen3:8b` 已在主機上，所以**不需要** `--pull`。
   - `--ollama-url` 預設 `http://127.0.0.1:11434`；`--out` 預設在使用者資料夾的 `pados-stage-b-evidence\m2-<UTC>`，
     不寫進 checkout。
   - 輸出：`BENCHMARK_RAW.jsonl`、`BENCHMARK_SUMMARY.json`、`WINDOWS_M2_RESULT.json`、`SHA256SUMS` 與一個 zip，
     並印出 `EVIDENCE_ARCHIVE=` 與 `EVIDENCE_ARCHIVE_SHA256=`。證據不含模型權重、憑證或主機名稱。
2. **M2.1 獨立重算**（M3 會自動執行，也可以單獨跑）：
   `python -I scripts\stage_b\m2_1_independent_recompute.py <證據資料夾> <out.json>`
3. **M3 設定檔**：

   ```powershell
   .\.venv\Scripts\python.exe scripts\stage_b\m3_capability_profile.py --archive <m2-...zip> --sha256 <hex> --out <資料夾>
   ```

   輸出 `M3_MODEL_CAPABILITY_PROFILE.json`（內含 `profile` 與 `catalog_description`），並印出
   `M3_CAPABILITY_PROFILE_ID=mcp-...`。M3 的「登錄」只在記憶體裡（`"authorization": "NOT_AUTHORIZED"`、
   `"routable": false`），**不會**寫入 `PADOS_MODEL_CATALOG`。

**Owner 決策：** TEST 在同一台主機、同一個 Ollama 0.40.16、同一個 `qwen3:8b` manifest 上產生了 `mcp-5c6a96b026b6…`。
您可以 (A) 在自己的環境重跑 M2 → M3（建議，出處最清楚），或 (B) 在核對 TEST 的 M2 zip 與 SHA-256 後，用 TEST 的
`M3_MODEL_CAPABILITY_PROFILE.json` 進行第 3 步。兩者都只在 Ollama 版本、模型 digest 與裝置都沒變時有效。

---

## 3. 把設定檔登錄到模型目錄 — **使用者環境尚未完成**（TEST 只登錄在臨時 runtime）

沒有 CLI；使用 repo 既有的 store API（契約文件「Operator steps」第 1 步，與 `scripts/acceptance/live_acceptance.py`
的 `prepare` 相同的呼叫）。在 `<checkout>` 用它的 `.venv` 執行，`<expected id>` 是第 2 步印出的 id：

```powershell
.\.venv\Scripts\python.exe -c @"
import json, sys
from pathlib import Path
from pados.local_intelligence.catalog_store import ModelCatalogStore
from pados.resources.capability_profile import ModelCapabilityProfile
from pados.resources.models import ModelRuntimeProfile
report = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
profile = ModelCapabilityProfile.model_validate(report['profile'])
if profile.profile_id != sys.argv[3]: sys.exit('PROFILE_ID_UNEXPECTED')
description = ModelRuntimeProfile.model_validate(report['catalog_description'])
ModelCatalogStore(Path(sys.argv[2])).update(lambda c: c.register(description).register_capability_profile(profile))
print(profile.artifact.model_ref)
"@ <M3_MODEL_CAPABILITY_PROFILE.json> runtime\local\model-catalog.json <expected id>
```

印出的 `model_ref`（格式 `ollama/qwen3:8b@sha256:...`）在第 4 步要用。然後在 `.env` 加上：

```text
PADOS_MODEL_CATALOG=<checkout 的絕對路徑>\runtime\local\model-catalog.json
```

到這一步，模型是「已登錄（已測量）」但**未授權**。

---

## 4. 明確授權：compute-node 註冊 — **使用者環境尚未完成**（TEST 的授權是臨時的，已移除）

- 設定：`PADOS_COMPUTE_NODES_CONFIG`，指向一個 JSON 檔，schema `pados.compute-nodes.registry.v1`
  （`src/pados/compute_node/registry.py`；範本 `examples/config/compute-nodes-loopback.json`，它的 `authorized` 是
  `false`，也沒有 `allowed_models`）。
- 只有**同時**符合下列條件的註冊才算授權（`src/pados/capability/inference.py`、契約文件「Operator steps」第 2 步）：

  | 鍵 | 必須是 |
  | --- | --- |
  | `authorized` | `true`（預設 `false`） |
  | `allowed_capabilities` | 包含 `LOCAL_INFERENCE` |
  | `project_ids` | 包含 Dogfood 專案的 id `95100768-ec1d-4343-b5f5-ce0c4c848dc9`（建議只列這一個，不要用 `*`） |
  | `max_classification` | 允許 `INTERNAL`（見第 6 節） |
  | `allowed_models` | 包含第 3 步的**完整** `model_ref`；空清單**不授權任何模型**，不是萬用字元 |
  | `node_id` | `lcn-` 加 20 個十六進位小寫字元 |
  | `endpoint` | 本機 Ollama 的位址（`LOCAL_SINGLE_USER` 只允許 loopback，例如 `http://127.0.0.1:11434`） |
  | `auth_ref` | `secretref://env/<環境變數名稱>`：檔案裡只寫變數**名稱**，絕不寫值 |
  | `security_profile` | `LOCAL_SINGLE_USER` |

- 檔案放在 `<checkout>\runtime\local\`，在 `.env` 加上 `PADOS_COMPUTE_NODES_CONFIG=<絕對路徑>`。
- **撤銷：** 從 `allowed_models` 移除 `model_ref`，或把 `authorized` 改成 `false`。下一次呼叫就會被拒絕
  （`MODEL_NOT_AUTHORIZED`）。
- **AI 模型與連線** 頁面**不能**授權模型：它的設定（`PADOS_PROVIDERS_SETUP`）只能停用提供者或縮小專案範圍
  （`src/pados/providers/setup.py`；頁面上的說明「這些設定只能縮小 PADOS 可以做的事；它們不會授權任何模型」）。

---

## 5. 聊天提供者設定 — **使用者環境尚未完成**

在 `.env` 加上：

```text
PADOS_CHAT_PROVIDER=ollama
PADOS_CHAT_MODEL=qwen3:8b
```

- `PADOS_CHAT_MODEL` 是 Ollama 的 **tag**（只用來查詢），不是目錄的 model id；必須**剛好一個**已登錄的設定檔符合
  `ollama` + `qwen3:8b`，否則拒絕（`MODEL_PROFILE_NOT_REGISTERED`／`MODEL_PROFILE_AMBIGUOUS`）。
- `ollama` 一定透過已測量的綁定執行，沒有「未綁定」的後備路徑（`src/pados/providers/factory.py`）。未設定 = 
  `UNCONFIGURED`；`mock` 只供契約測試。
- `OLLAMA_BASE_URL` 預設 `http://127.0.0.1:11434`，與主機上的 Ollama 相同時不需要設定。
  `PADOS_LOCAL_INFERENCE_TIMEOUT_SECONDS` 預設 30 秒（範圍 1–600，尚未重新調校）。
- 改完 `.env` 後依 Runbook (h) 重新啟動。

每次呼叫前，PADOS 依序檢查（任一步失敗就拒絕，代碼見契約文件的「Resolver steps」表）：目錄已設定且有效 → 剛好一個設定檔
符合 → 主機上確實有該 manifest digest、執行環境版本與裝置都是量測時的 → 註冊已設定 → 註冊授權這個專案與 `model_ref`
→ 執行條件一致。例如 Ollama 被升級時是 `LIVE_BINDING_REFUSED`（原因 `RUNTIME_VERSION_NOT_MEASURED`）。

---

## 6. 專案範圍與資料分級 — **使用者環境尚未完成**

- **範圍：** 由第 4 步註冊的 `project_ids` 決定。只列 Dogfood 專案的 id，其他專案就不能使用這個模型。
- **分級：** 分級順序是 `PUBLIC` < `INTERNAL` < `CONFIDENTIAL` < `RESTRICTED`。本機模型的呼叫一律以 `INTERNAL`、
  `LOCAL_ONLY`（只在本機）提出授權請求，不讀取專案自己的分級（`src/pados/providers/execution_binding.py`）。所以註冊的
  `max_classification` 設 `INTERNAL` 即可；不要提高。產品迴圈的 maker 取 `INTERNAL` 與證據分級中的最大值，
  `RESTRICTED` 需要另外明確的 `RESTRICTED` 註冊（[LOCAL_COMPUTE_NODE_V1.md](../local-compute/LOCAL_COMPUTE_NODE_V1.md)）。
- **「非敏感」不是一個分級值，而是使用界線：** Dogfood 只用個人、非敏感的資料；不放公司正式資料、人事資料或 8D 資料，
  不放任何密碼或金鑰（[DOGFOOD_BOUNDARY.md](../releases/PADOS_RC2_LOCAL_DOGFOOD_V1/DOGFOOD_BOUNDARY.md)）。PADOS 無法替您
  判斷內容是否敏感，這個責任在 Owner。

---

## 7. **AI 模型與連線（Providers）** 頁面在每一步應該顯示什麼

先按 **測試連線**。**Ollama (PADOS settings)** 這一列來自 `OLLAMA_BASE_URL`，不需要另外新增。

| 完成到 | 第一層（連線） | `qwen3:8b` 的第二層（治理） | 頁面上的其他提示 |
| --- | --- | --- | --- |
| 目前（第 0 步） | 已連線 Reachable | **未授權（尚未測量）** | 「尚未設定模型目錄（PADOS_MODEL_CATALOG），因此沒有任何模型被視為已測量。」「尚未設定 compute-node 註冊（PADOS_COMPUTE_NODES_CONFIG），因此沒有任何模型被授權。」「此處沒有任何模型可以執行受治理的工作（未授權）」 |
| 第 1–3 步（已登錄，未授權） | 已連線 | **已測量但未授權（未授權）** | 仍是「尚未設定 compute-node 註冊…」 |
| 已登錄，但 Ollama 版本、裝置或 digest 與量測時不同 | 已連線 | **已測量，但證據不適用於此環境（未授權）**；digest 不同時另有 `MEASURED_ARTIFACT_DIGEST_DIFFERS` | 需要重新量測（第 2 步） |
| 第 4–5 步（已授權） | 已連線 | **已授權並測量 Authorized & measured** | 「至少一個模型可以執行受治理的工作」；設定的聊天模型的 `governed_binding` 檢查為 `BOUND` |
| Ollama 停止 | 無法連線 Unreachable | （無法判斷） | 不會顯示任何成功 |

在 Providers 頁面上，**已授權並測量** 只要求有某一筆註冊授權了這個 `model_ref`；實際呼叫時還會再檢查**專案**範圍
（第 6 節），所以 Dogfood 以外的專案仍會被拒絕。

---

## 8. 用不含敏感內容的方式驗證 — **使用者環境尚未完成**（TEST 以 M4.1 合成任務驗證過）

1. **不呼叫模型的檢查（建議先做）：** Providers 頁面 **測試連線**（健康、模型清單、模型身分與 `governed_binding`）。
   本機 Ollama 的 **測試連線** **不會**呼叫模型。`BOUND` 代表第 1–5 步都正確；`REFUSED` 會顯示拒絕代碼。
2. **命令列：** checkout 裡的 `pados doctor` 只回報 `chat_provider` 與 `chat_provider_mode`，**不**檢查已測量的綁定。
   契約文件第 4 步所說、會回報 `providers: MEASURED_BINDING_UNAVAILABLE` 與拒絕代碼的，是**安裝版**的 doctor
   （`python -m pados.install doctor`，`src/pados/install/doctor.py`），它需要已安裝的 PADOS home，不適用於這個
   git checkout 環境。它以專案範圍 `*` 檢查，所以只授權 Dogfood 專案時會顯示 `MODEL_NOT_AUTHORIZED`，這是預期的。
3. **真的呼叫一次模型：目前沒有現成、適用於您環境的非敏感探測。**
   - 本機 Ollama **沒有**「以探測訊息測試」（那個按鈕只出現在企業閘道，而且只送固定的 PUBLIC 句子）。
   - Control Center 的工作需求目前執行的是**確定性的 Markdown 參照檢查**，不呼叫模型。
   - repo 中會呼叫受治理模型的路徑是：圖形引擎的認知回應（intent `STRUCTURED_OUTPUT`，範圍是 workspace 的專案；成功時
     證據 `measured_execution` 寫在候選節點的 metadata）與產品迴圈的本機 compute maker（需要
     `PADOS_PRODUCT_LOOP_ENABLED=true`，您的 `.env` 目前是 `false`）。
   - TEST 的真實呼叫是 M4.1 的合成任務（`scripts/stage_b/windows_m4_1_dogfood.py`，在臨時 runtime 上）。**不要**對您的
     環境執行該腳本：它會建立自己的狀態與目錄。

   **Owner 決策：** 要不要、以及用哪一條路徑做第一次真實呼叫。無論選哪一條，內容都只用固定、合成、不含個人或公司資訊的
   句子，並確認證據中有 `profile_id`、`model_ref`、`runtime_version`、`binding_hash`（證據不含提示詞或內容）。

---

## 9. 模型輸出永遠不會自動成為已接受的結果

- 模型或 compute node 的執行**不是**語意上的接受；只有 CAS 會推進專案狀態
  （[LOCAL_COMPUTE_NODE_V1.md](../local-compute/LOCAL_COMPUTE_NODE_V1.md)）。執行收據帶有 `semantic_authority: false`，
  已測量的設定檔的 `authority` 必須是 `NONE`。
- 結果只能成為 **ResultCandidate**，狀態是等待決策。只有您在 Remote 主控台以**新的 WebAuthn step-up** 做出決策，它才會被
  接受；其他任何呼叫者（包括 control token）都會得到 `RESULT_ACCEPTANCE_REQUIRES_STEP_UP`，核准維持 PENDING
  （`src/pados/control/service.py`；[OPERATING_GUIDE_zh-TW.md](OPERATING_GUIDE_zh-TW.md) 第 8 節）。
- TEST 的 M4.1 執行也是如此：receipt SUCCEEDED、驗證器 5/5 欄位、`semantic_authority` NONE，**沒有**提交給 CAS。

---

## 10. 回退

依序任選：從註冊的 `allowed_models` 移除 `model_ref` 或設 `authorized: false`（立即撤銷）；刪除 `.env` 的
`PADOS_CHAT_PROVIDER`／`PADOS_CHAT_MODEL`（回到 `NOT_CONFIGURED`）；刪除 `PADOS_MODEL_CATALOG`、
`PADOS_COMPUTE_NODES_CONFIG` 兩行與對應的檔案。之後重新啟動。專案、Evidence 與已接受的結果都不受影響。
