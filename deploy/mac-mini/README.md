# Mac mini node pack: `PADOS-LCN-01` (DEPLOYMENT_READY_HARDWARE_PENDING)

No Apple Silicon hardware has been used; nothing here is live-validated. Do not assume a specific RAM size or model.

## Install

1. Install Python 3.12 and create a venv. Then `pip install -c requirements.lock .` (or install a PADOS wheel).
2. Install one runtime:
   - **Ollama**: `ollama serve` on 127.0.0.1:11434.
   - **MLX**: an OpenAI-compatible MLX server, e.g. `mlx_lm.server` on 127.0.0.1:8080. This is a placeholder; validate when hardware exists.
   - **llama.cpp**: `llama-server` on 127.0.0.1:8080.
3. Copy `node.ollama.json` or `node.mlx.json` to `~/pados-node/node.json` and replace every `REPLACE_…` value with discovery results.
4. Store the node credential in the launch environment (or a future keychain resolver) as `PADOS_NODE_CREDENTIAL`. It is referenced as `secretref://env/PADOS_NODE_CREDENTIAL`.
5. Install the service: `cp com.pados.lcn.plist ~/Library/LaunchAgents/`, then `launchctl load ~/Library/LaunchAgents/com.pados.lcn.plist`.
6. Health: `./health_probe.sh` (unauthenticated requests must return 401; the PADOS-side probe then derives state).

## Register on the PADOS host

1. Read the node id from the service log (`{"node_id": "lcn-…"}`).
2. Add a registration with:
   - the Mac's private LAN address;
   - `security_profile: TRUSTED_LAN`;
   - `authorized: true` (a human decision);
   - `allowed_capabilities`, `max_classification` and `project_ids`.

   See `examples/config/compute-nodes-private-lan.json`.
3. Run `pados node probe`. The node must be `ONLINE` before routing.

## Rollback and uninstall

1. `launchctl unload ~/Library/LaunchAgents/com.pados.lcn.plist && rm ~/Library/LaunchAgents/com.pados.lcn.plist`.
2. On the PADOS host, set `authorized: false` (or remove the registration), then run `pados node probe`.
3. Delete `~/pados-node/` (identity key and artifacts) only after any receipts you need are archived on the PADOS host.

## Acceptance when hardware arrives

Run `runtime/artifacts/reports/local-compute-node-v1/scripts/node_protocol_uat.py` on the Mac. It selects the model by discovery. Record measured resource class and accelerators (`APPLE_SILICON`).
