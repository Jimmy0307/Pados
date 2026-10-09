# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass
from pathlib import Path

REQUIRED_ROLES = {
    "orchestrator",
    "repo_mapper",
    "architect",
    "builder",
    "qa",
    "reviewer",
    "fixer",
    "release",
}

WRITE_CAPABLE_ROLES = {"orchestrator", "repo_mapper", "architect", "builder", "qa", "fixer", "release"}


@dataclass(frozen=True)
class AgentConfig:
    role: str
    write_scope: str
    provider: str


def _parse_simple_yaml(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if separator:
            result[key.strip()] = value.strip()
    return result


def load_agent_registry(config_dir: Path = Path("config/agents")) -> dict[str, AgentConfig]:
    registry: dict[str, AgentConfig] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        payload = _parse_simple_yaml(path)
        config = AgentConfig(
            role=payload["role"],
            write_scope=payload["write_scope"],
            provider=payload["provider"],
        )
        registry[config.role] = config
    missing = REQUIRED_ROLES.difference(registry)
    if missing:
        raise ValueError(f"missing agent roles: {sorted(missing)}")
    return registry


def role_can_write(role: str, registry: dict[str, AgentConfig]) -> bool:
    config = registry[role]
    if role == "reviewer":
        return False
    return role in WRITE_CAPABLE_ROLES and config.write_scope != "none"

