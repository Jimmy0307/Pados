# SPDX-License-Identifier: AGPL-3.0-only
class ClaudeAdapter:
    def healthcheck_sync(self) -> dict[str, str]:
        return {"status": "not_configured"}

    async def healthcheck(self) -> dict[str, str]:
        return {"status": "not_configured", "provider": "claude"}

