# Example configurations (public-safe)

All values are placeholders:

- Credentials are SecretRefs (`secretref://env/<NAME>`); set the named environment variable yourself.
- Addresses are loopback, or `192.0.2.10` (RFC 5737 documentation range) for LAN examples.
- Paths are relative or `./runtime/local/...`.

| File | Scenario | Security profile |
|---|---|---|
| `single-machine.env` | One machine, PADOS Core only (Enterprise Pack disabled, no nodes) | LOCAL_SINGLE_USER |
| `node-deterministic.json` | Development node, no model weights | LOCAL_SINGLE_USER |
| `node-ollama.json` | Node backed by a local Ollama | LOCAL_SINGLE_USER |
| `node-openai-compatible.json` | Node backed by an OpenAI-compatible server (llama.cpp / vLLM / MLX server) | LOCAL_SINGLE_USER |
| `compute-nodes-loopback.json` | PADOS registry for a loopback node | LOCAL_SINGLE_USER |
| `node-private-lan.json` + `compute-nodes-private-lan.json` | Node on another machine in a private LAN | TRUSTED_LAN (HMAC; never Internet-facing) |
| `enterprise-synthetic.json` | Enterprise Pack enabled: local knowledge retrieval, OCR not configured | LOCAL_SINGLE_USER |

## Registering a node

1. `pados node serve --config examples/config/node-deterministic.json` prints the node's stable `node_id`.
2. Copy that id into `node_id` in the registry file.
3. Set `PADOS_COMPUTE_NODES_CONFIG` to the registry file.
4. Run `pados node probe`; the node must show `ONLINE` before any routing.

For model manifests, replace `REPLACE_WITH_DISCOVERED_MODEL` with a model your backend lists (`/api/tags` or `/v1/models`). PADOS never assumes a model.
