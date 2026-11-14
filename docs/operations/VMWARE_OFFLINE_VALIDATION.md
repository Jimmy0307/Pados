# VMware Linux validation without VM internet (2026-11-14)

The VMware lanes run `scripts/validation/vm_online_chain.sh`, which needs the network for three things: the git
clone, `pip install`, and `npm ci`. This runbook runs the **unmodified** script with offline sources. The gates,
test selections and commands stay as they are; only where the bytes come from changes.

## Environment diagnosis (host and VM, 2026-11-14)

| Finding | Evidence | Effect | Fix (owner) |
| --- | --- | --- | --- |
| The host Wi-Fi is an address in a Windows mobile-hotspot `/24`, whose gateway is the `.1` host of that `/24`. VMnet8 is the same `/24`, and the host's own VMnet8 adapter **is** that same `.1` address. | `Get-NetIPConfiguration`; two on-link routes for the same `/24` (Wi-Fi and VMnet8, metric 256); the NAT address in `vmnetnat.conf` lies in the same `/24` | The VM's NAT has no working upstream, so the VM has no internet | Use an upstream network outside that `/24`, or move VMnet8 to another subnet (Virtual Network Editor / `vnetlib`, needs admin), then restart the VMware NAT service |
| On the VM, `docker0` has **no IPv4 address** and is DOWN/NO-CARRIER even with a container running | `ip -br addr show docker0` shows only `fe80::…`; connecting to the container IP gives "No route to host"; `docker-proxy` accepts on 127.0.0.1 and drops the connection; inside the container, PostgreSQL accepts TCP | Every PostgreSQL container is unreachable from the VM, so the E2E, `integration_pg` and `full_pytest` API tests fail | `sudo systemctl restart docker` (or re-add the bridge's default IPv4 address), needs root |
| No passwordless sudo on the VM | `sudo -n true` refuses | Neither fix above can be applied from this session | Owner |

Both fixes change host or VM networking. They are owner actions and were not made silently.

## Offline sources

| Need | Offline source | Integrity |
| --- | --- | --- |
| `git clone` of the pushed branch | `PADOS_REPO_URL=<git bundle>`, created on the host from the pushed branch | The script itself checks `git rev-parse HEAD == <sha>` |
| `pip install -c requirements.lock -e ".[dev]"` | `PIP_NO_INDEX=1`, `PIP_FIND_LINKS=<wheelhouse>`. The wheelhouse is `pip download -c requirements.lock <pyproject dependencies + dev extras> setuptools>=80 wheel pip`, for manylinux cp312, binaries only (49 wheels). `requirements.lock` alone is not enough: `python-multipart` is resolved from `pyproject.toml`. | `wheelhouse-SHA256SUMS` beside the wheelhouse |
| `npm ci --ignore-scripts` (web) | `npm_config_offline=true` over the existing `~/.npm` cache | npm verifies the lockfile integrity hashes |
| PostgreSQL | The cached `pgvector/pgvector:pg16` image | — |

The script copied to the VM must be the git blob, with LF line endings. A copy from the Windows checkout has CRLF
(`core.autocrlf`), and bash cannot parse it: attempt r1 failed before any gate ran. Copy it from a Linux clone of
the bundle and compare `git hash-object`.

```text
PADOS_REPO_URL=$HOME/pados-worktrees/<bundle> PIP_NO_INDEX=1 \
PIP_FIND_LINKS=$HOME/pados-worktrees/offline-deps-<sha>/wheelhouse-lock npm_config_offline=true \
bash vm_online_chain-<sha>-lf.sh <40-hex sha> <branch> -offline-<tag>
```

## Results at `74df2479` (PR #24 head)

| Attempt | Outcome |
| --- | --- |
| r1 | Script had CRLF line endings: parse error, nothing ran |
| r2 | Install failed: `python-multipart` was missing from a lock-only wheelhouse (exit 14) |
| r3 | Ran completely. 17 of 19 gates exit 0: `ci_policy`, `ruff`, `mypy`, `mypy_win32`, `compileall`, `migration_gate`, `d1`, `d2`, `d4`, `pack_focused`, `public_export`, `secret_scan`, `wheel_build`, `operability_gate`, `doc_lint`, `frontend` and `npm_cli_tests`. **`full_pytest` and `integration_pg` FAIL, and so does the separate E2E step**, all on the `docker0` cause. |

The lane gate records both VMware lanes as **FAIL** (`runtime-lane-gate-74df247-r3-*`), never as PASS or as
NOT_VALIDATED. A completed failure is a failure. Rerun with a new tag once Docker is restored.
