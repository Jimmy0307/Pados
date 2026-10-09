# SPDX-License-Identifier: AGPL-3.0-only
def select_provider(risk_level: str, preferred: str = "mock") -> str:
    if risk_level.lower() == "low":
        return preferred
    return "mock"

