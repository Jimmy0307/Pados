# Enterprise Gateway Live Acceptance (owner procedure)

Purpose: move the gateway adapter from `ENTERPRISE_LIVE_NOT_VALIDATED` to a recorded live result, safely, once VPN
access to the enterprise AI gateway exists. Read [ENTERPRISE_GATEWAY_ADAPTER.md](ENTERPRISE_GATEWAY_ADAPTER.md) first.

## Guardrails (read before every step)

- **Do not send internal documents, code, customer data or anything classified to the gateway during acceptance.**
  The only text sent is the fixed probe sentence PADOS uses ("PADOS connection probe. Reply with the single word
  OK."), at classification PUBLIC.
- Keep `policy.execution_enabled` **false** for steps 1–5. Enable it only for step 6, and set it back afterwards.
- Never put a key into the contract, the provider setup, a ticket or a screenshot. PADOS reads it only from the
  environment variable named in the contract.
- Keep the private contract outside every Git repository, readable only by your user account.
- Use a scratch runtime directory (environment variable `PADOS_PROVIDERS_SETUP`), not your everyday PADOS runtime.
- Stop at the first unexpected result and record it. Do not "fix" it by widening the policy.

## Private contract template (placeholders only)

Save it outside the repository, for example in a private folder in your user profile, and fill in the placeholders
from the gateway's own documentation. Every value in angle brackets is a placeholder.

```json
{
  "schema_version": "pados.compute-node.gateway-contract.v1",
  "provider_id": "enterprise-gateway",
  "display_name": "Enterprise AI gateway",
  "base_url": "https://<gateway-host>.example.invalid",
  "tls_ca_file": "<absolute path to the private CA bundle, or remove this line>",
  "auth": {"scheme": "bearer", "auth_ref": "secretref://env/PADOS_ENTERPRISE_GATEWAY_KEY"},
  "health": {"route": {"method": "GET", "path": "<health path>"}},
  "discovery": {"route": {"method": "GET", "path": "<model list path>"}, "models_path": "<path to the list>",
                "id_field": "<model id field>", "digest_field": "<optional identity field>"},
  "chat": {
    "route": {"method": "POST", "path": "<chat path>"},
    "request_template": {"<model field>": "{{model}}", "<messages field>": "{{messages}}",
                         "<max tokens field>": "{{max_output_tokens}}", "<temperature field>": "{{temperature}}"},
    "text_path": "<path to the answer text>",
    "model_path": "<path to the model named in the answer>",
    "usage_prompt_path": "<optional>", "usage_completion_path": "<optional>",
    "error_code_path": "<optional path to an error code>",
    "error_codes": {"<gateway code for a model that is not loaded>": "GATEWAY_MODEL_NOT_LOADED"}
  },
  "timeouts": {"connect_seconds": 5, "read_seconds": 60, "probe_seconds": 10},
  "retry": {"max_attempts": 3, "base_delay_seconds": 0.5, "max_delay_seconds": 4, "max_retry_after_seconds": 10},
  "circuit": {"failure_threshold": 3, "open_seconds": 60},
  "rate_limit_headers": {"retry_after": "retry-after", "remaining": "<optional header>"},
  "idempotency": {"header_name": "<only if the gateway documents one>", "honoured": false},
  "max_context_tokens": 8192,
  "policy": {"max_classification": "PUBLIC", "execution_enabled": false}
}
```

Remove optional lines you cannot fill. Set `idempotency.honoured` to true only when the gateway's owner confirms in
writing that a repeated key never has a second side effect.

## Procedure

| # | Step | How | Expected result | Record |
|---|---|---|---|---|
| 0 | Contract validates offline | `python -c "from pados.compute_node.gateway_contract import load_gateway_contract as l; print(l(r'<contract path>').provider_id)"` | prints the provider id; any refusal is one closed code (for example `GATEWAY_BASE_URL_MUST_BE_HTTPS`) | the command output |
| 1 | VPN **off**: failure is explained | In a scratch runtime, add the provider on the AI 模型與連線 (Providers) page (kind enterprise gateway, the contract path, ceiling PUBLIC), enable it, press 測試連線 | 無法連線 Unreachable with `GATEWAY_NETWORK_UNREACHABLE` or `GATEWAY_DNS_FAILED` and a plain-language reason; after repeated tests the circuit shows 開啟 | screenshot, the provider view (copy the JSON from `GET /control/providers/<id>`) |
| 2 | Key not set | VPN on, environment variable not set, 測試連線 | `GATEWAY_AUTH_NOT_CONFIGURED`; nothing is sent | screenshot |
| 3 | VPN **on**, key set: reachability | Set the variable in the shell that starts `pados serve`, restart it, 測試連線 | 已連線 Reachable; the circuit closes on the first successful probe; models are listed with their identity; every model shows 未授權（尚未測量） | screenshot, provider view |
| 4 | Wrong key | Temporarily set the variable to a wrong value, 測試連線 | `GATEWAY_AUTH_FAILED`; the circuit stays closed | screenshot |
| 5 | TLS | If the gateway uses a private CA: remove `tls_ca_file` once, 測試連線, then restore it | without it `GATEWAY_TLS_FAILED`; with it reachable | screenshots |
| 6 | Probe message | Set `policy.execution_enabled` to true (ceiling stays PUBLIC), press 以探測訊息測試 | `probe_chat` PASS, labelled 非權威觀察結果, with an answer hash and length only; or a closed code such as `GATEWAY_MODEL_NOT_LOADED` or `GATEWAY_RATE_LIMITED` | screenshot, provider view |
| 7 | Classification refusal | Keep the ceiling at PUBLIC. On a node with this backend, submit any INTERNAL job (synthetic text) | refused with `CLASSIFICATION_ABOVE_MODEL_CEILING` or `GATEWAY_NOT_AUTHORIZED_FOR_EXECUTION`; the gateway receives nothing (check the attempt receipts: attempt 0, write outcome NO) | receipt lines |
| 8 | Restore | Set `execution_enabled` back to false, disable the provider, unset the variable | 已停用 Disabled | screenshot |

## Evidence

Store everything outside the repository, in a dated folder (for example
`<runtime root>/artifacts/reports/enterprise-gateway-live-<date>/`):

- the provider views (JSON) from each step and the screenshots;
- the attempt receipts: the gateway-attempts folder next to the provider setup file (content-free JSON lines);
- the `pados serve` log for the session;
- a short ENTERPRISE_GATEWAY_LIVE_RESULT.md listing each step as PASS / FAIL with its code, the PADOS commit, the date,
  and the statement "no internal data was sent".

Before sharing, check that no file contains a key, an internal host name you may not disclose, or any document
content. The receipts hold only hashes and codes by design.

## Status after acceptance

- Steps 0–6 and 8 pass: record `ENTERPRISE_LIVE_REACHABLE_VALIDATED`. The gateway is reachable and correctly refused
  or answered, and its models are still not authorized for governed work.
- Any use for governed work needs a separate decision: a measured profile (a measurement design that can verify the
  gateway's execution configuration does not exist yet) and an explicit registration. Reachability never grants it.
