# Sandbox Network Profiles

Every profile has `default_action=DENY` and `internet_egress=false`. Regardless of
rules, a sandbox or lab segment can never reach `HOST` or `LAN`, and `MANAGEMENT`
is denied unless `management_access` is explicitly set.

| Profile | Allowed flows | Use |
| --- | --- | --- |
| `L3_ISOLATED` (default) | sandbox -> PADOS artifact exchange, tcp/443 | normal isolated execution |
| `OFFLINE_SANDBOX` | none | fully offline processing |
| `L3_REDTEAM` | sandbox -> artifact exchange tcp/443; sandbox -> isolated lab segment | defensive exercises against synthetic lab targets only |
| `L2_SECURITY_LAB` | none by default; requires `explicit_l2_opt_in` and VM isolation | specialised lab work; never the default |

Zones: `SANDBOX`, `LAB_SEGMENT`, `PADOS_ARTIFACT_EXCHANGE`, `HOST`, `LAN`, `INTERNET`, `MANAGEMENT`.
