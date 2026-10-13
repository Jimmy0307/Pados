# Break-Glass Remote Access (documented only)

PADOS V1 does **not** implement remote desktop, remote shell, or screen sharing.

If the Home Base needs hands-on repair while you are away:

1. Use the operating system's own remote-administration tool over the same
   private overlay. Never expose it publicly.
2. Treat the session as privileged: time-box it, log it, and rotate the remote
   actor proofs afterwards if there is any doubt.
3. PADOS authority rules still apply. Manual changes to PADOS state outside
   receipt -> validation -> CAS are out of policy and must be recorded as an incident.

This path is intentionally outside PADOS, so the remote console never gains
host-level power.
