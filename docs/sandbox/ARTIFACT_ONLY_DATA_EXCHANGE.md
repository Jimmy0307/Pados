# Artifact-Only Data Exchange

Sandboxes receive explicit staged inputs and return artifacts. Nothing else crosses the boundary.

## Never mounted

`pados.sandbox.exchange.assert_mount_allowed` rejects:

* `/Users/...` and other home or profile trees (`/home`, `/root`);
* Windows user profiles (`C:\Users\...`);
* the PADOS repository and any other protected path passed in;
* database directories (for example `/var/lib/postgresql`);
* whole host drives (`/`, `C:\`, `D:\`) and any host-drive path;
* system trees (`/etc`, `/proc`, `/sys`, `/dev`, `/mnt`);
* anything outside the per-run staging root, including symlink escapes (paths are resolved first).

## Artifacts out

`DataEgressPolicy.admit_artifact` enforces export on/off, a size ceiling, and an
extension allowlist (by default `.txt`, `.json`, `.md`, `.csv`, `.log`, `.diff`,
`.patch`). Every artifact is SHA-256 hashed at capture and re-verified before
release. Artifacts become ResultReceipt candidates and are accepted only through validation and CAS.
