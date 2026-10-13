# Red-Team Lab Profile (orchestration only)

Status: **REDTEAM_LAB=ORCHESTRATION_READY_TOOLING_DEFERRED**.

PADOS provides only the governance around a security lab:

* The `L3_REDTEAM` profile requires `VM_SANDBOX` isolation.
* Lab targets must be declared, and each one must be a synthetic lab target
  (`lab-synthetic:<name>`). Real hosts, IP addresses, and hostnames are rejected
  (`LAB_TARGET_MUST_BE_SYNTHETIC`).
* There is no Internet egress, and no reachability to the host or LAN.
* Outputs leave only as allow-listed artifact types through the artifact exchange,
  and they are candidates, not truth.

PADOS does **not** bundle offensive tooling, exploit payloads, or target lists.
Any tooling a lab operator installs lives inside the lab VM image, and the
operator is responsible for its authorisation. Tests use synthetic targets only.
