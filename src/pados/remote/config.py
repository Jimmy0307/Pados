# SPDX-License-Identifier: AGPL-3.0-only
"""Remote Operations configuration (opt-in via ``PADOS_REMOTE_CONFIG``).

Unset means remote mode is off and Core behaviour is unchanged. The file holds
the overlay bind address, PADOS-owned staging/ledger paths, an optional bulk
inbox and hashed actor credentials; it never holds plaintext credentials.
``notification_outbox_path`` (optional) defaults to a file next to the ledger.
``presence`` (``{"rp_id": ..., "origins": [...]}``) configures the WebAuthn
relying party used for fresh approval step-up; without it every approval and
rejection fails closed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pados.remote.auth import RemoteActorRegistry


@dataclass(frozen=True)
class RemoteConfig:
    bind_host: str
    staging_root: Path
    ledger_path: Path
    inbox_root: Path | None
    registry: RemoteActorRegistry
    malware_scanner: str | None = None
    project_workspaces: tuple[tuple[str, Path], ...] = ()
    notification_outbox_override: Path | None = None
    allowed_origins: tuple[str, ...] = ()
    presence_rp_id: str | None = None
    presence_origins: tuple[str, ...] = ()

    @property
    def notification_outbox_path(self) -> Path:
        return self.notification_outbox_override or self.ledger_path.parent / "notification-outbox.json"

    def workspace_for(self, project_id: str) -> Path | None:
        for pid, path in self.project_workspaces:
            if pid == project_id:
                return path
        return None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> RemoteConfig:
        for key in ("bind_host", "staging_root", "ledger_path"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"REMOTE_CONFIG_{key.upper()}_REQUIRED")
        inbox = data.get("inbox_root")
        outbox = data.get("notification_outbox_path")
        if outbox is not None and (not isinstance(outbox, str) or not outbox):
            raise ValueError("REMOTE_CONFIG_NOTIFICATION_OUTBOX_PATH_INVALID")
        raw_origins = data.get("allowed_origins", [])
        if not isinstance(raw_origins, list):
            raise ValueError("REMOTE_CONFIG_ALLOWED_ORIGINS_INVALID")
        allowed_origins = _canonical_origins(raw_origins)
        presence = data.get("presence")
        presence_rp_id: str | None = None
        presence_origins: tuple[str, ...] = ()
        if presence is not None:
            if not isinstance(presence, dict) or set(presence) - {"rp_id", "origins"}:
                raise ValueError("REMOTE_CONFIG_PRESENCE_INVALID")
            rp_id = presence.get("rp_id")
            origins = presence.get("origins")
            if not isinstance(rp_id, str) or not rp_id or not isinstance(origins, list) or not origins:
                raise ValueError("REMOTE_CONFIG_PRESENCE_INVALID")
            presence_rp_id = rp_id
            presence_origins = tuple(_canonical_origins(origins))
            for origin in presence_origins:
                host = urlsplit(origin).hostname or ""
                if host != rp_id and not host.endswith("." + rp_id):
                    raise ValueError("REMOTE_CONFIG_PRESENCE_ORIGIN_NOT_IN_RP_ID")
        return cls(
            bind_host=data["bind_host"],
            staging_root=Path(data["staging_root"]),
            ledger_path=Path(data["ledger_path"]),
            inbox_root=Path(inbox) if isinstance(inbox, str) and inbox else None,
            registry=RemoteActorRegistry.from_mapping(data),
            malware_scanner=data.get("malware_scanner") or None,
            project_workspaces=tuple(
                (str(pid), Path(str(path)))
                for pid, path in dict(data.get("project_workspaces") or {}).items()
            ),
            notification_outbox_override=Path(outbox) if outbox else None,
            allowed_origins=tuple(allowed_origins),
            presence_rp_id=presence_rp_id,
            presence_origins=presence_origins,
        )

    @classmethod
    def load(cls, path: str) -> RemoteConfig:
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))


def _canonical_origins(raw_origins: list[Any]) -> list[str]:
    allowed_origins: list[str] = []
    for origin in raw_origins:
        if not isinstance(origin, str) or len(origin) > 512:
            raise ValueError("REMOTE_CONFIG_ALLOWED_ORIGIN_INVALID")
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ""
        local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            origin == "*"
            or parsed.scheme != "https" and not local_http
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("REMOTE_CONFIG_ALLOWED_ORIGIN_INVALID")
        canonical = f"{parsed.scheme}://{parsed.netloc}"
        if canonical in allowed_origins:
            raise ValueError("REMOTE_CONFIG_ALLOWED_ORIGIN_DUPLICATE")
        allowed_origins.append(canonical)
    return allowed_origins
