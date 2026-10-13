# Sandbox Fabric Foundation V1

A sandbox is an **isolation substrate**. It runs bounded work in an ephemeral
workspace and returns artifacts. It holds no semantic authority: sandbox output
is a candidate that enters PADOS only through receipt -> validation -> CAS.
Sandbox execution can never mutate semantic truth.

## Components (`pados.sandbox`)

| Module | Contract |
| --- | --- |
| `policy.SandboxProfile` | `L3_ISOLATED` (default), `L3_REDTEAM`, `OFFLINE_SANDBOX`, `L2_SECURITY_LAB` (explicit opt-in, never default) |
| `policy.NetworkPolicy` | rules over source/destination zone, direction, protocol, and port; Internet and management access flags; default **DENY** |
| `policy.IsolationClass` | `HOST_NATIVE`, `CONTAINER`, `VM_SANDBOX`, `REMOTE_NODE` |
| `policy.DataEgressPolicy` | classification ceiling, remote-node and cloud eligibility, Internet egress, artifact export, size, and type, sandbox profile |
| `exchange.assert_mount_allowed` | artifact-only exchange; only per-run PADOS staging directories may be mounted |
| `adapter.SandboxAdapter` | lifecycle protocol; `SimulatedSandboxAdapter` is deterministic for CI |

## Lifecycle

`CREATE -> STAGE -> EXECUTE -> CAPTURE -> VERIFY -> DESTROY`. The workspace is
destroyed on success **and** on failure. Captured artifacts are hashed at capture
time and re-verified before release.

## Status

* `L3_SANDBOX_POLICY=CONTRACT_AND_SIMULATION_ACCEPTED`: the policy contracts and
  the simulated adapter are tested.
* `LIVE_VM_ENFORCEMENT=HYPERVISOR_UAT_PENDING`: a live VM adapter implements the
  same protocol, and its enforcement (network isolation, no host mounts) must be
  proven in a hypervisor UAT before it is claimed.
