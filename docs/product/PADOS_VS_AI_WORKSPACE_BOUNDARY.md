# PADOS vs AI Workspace: Product Boundary

PADOS is an **execution authority**, not a chat workspace. Chat, web, and mobile
are interfaces into PADOS; they are not where truth lives.

| Concern | AI chat workspace | PADOS |
| --- | --- | --- |
| Source of truth | conversation history | ScopeVersion + CAS-accepted ResultReceipts + hash-chained audit |
| Memory | chat transcript | governed memory authority (promotion events); **chat history is not memory** |
| Model output | the answer | a candidate; **model output != truth** |
| Completion | the model says "done" | **execution complete != semantic acceptance**; validation + CAS decide |
| Remote client | ships the context | sends intent only; the server reconstructs context |
| Provider | the product | a replaceable reasoning provider; **provider is not authority** |
| Execution | in-chat tool calls | governed work packages on compute nodes and sandboxes, with receipts |

## Guardrails enforced by tests

`tests/product/test_anti_clone_boundary.py` checks that:

* the remote and sandbox packages never import model providers, workers, agents, or HTTP clients;
* remote routes expose no chat, completion, prompt, generate, shell, exec, SQL, proxy, or model surface;
* the remote package never writes semantic tables directly;
* acceptance goes through `commit_result(... expected_version=...)`, and ScopeVersion drift fails closed;
* chat/history/message/memory keys are rejected in remote intents;
* the web console never persists conversation or history into browser storage.

Status: `PRODUCT_DIFFERENTIATION=EXECUTION_AUTHORITY_BOUNDARY_PRESERVED`.
