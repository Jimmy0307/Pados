# SPDX-License-Identifier: AGPL-3.0-only
"""Load the one PADOS configuration file into the process environment.

The file (``PADOS_HOME/config/pados.env``) holds ordinary ``PADOS_*`` keys that Core's ``Settings`` already
reads; this module only places them into ``os.environ`` (process environment wins, exactly the precedence
pydantic-settings applies to ``.env`` files) so Core remains the single reader and validator.

Credentials are never stored in the file. The database password is named by a SecretRef
(``PADOS_DB_PASSWORD_REF``):

* ``secretref://env/<NAME>``: the value of environment variable ``NAME`` at launch time (memory only);
* ``secretref://libpq/passfile``: libpq resolves it from ``PGPASSFILE`` / the default passfile, or the server
  uses trust/peer authentication. ``PADOS_DB_PASSFILE`` (a path, not a secret) may name the passfile.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

from pados.enterprise.secrets import is_secret_ref, looks_like_secret

_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
PASSWORD_REF_KEY = "PADOS_DB_PASSWORD_REF"
PASSFILE_KEY = "PADOS_DB_PASSFILE"
LIBPQ_PASSFILE_REF = "secretref://libpq/passfile"
DEFAULT_PASSWORD_REF = "secretref://env/PADOS_DB_PASSWORD"
# Keys whose non-empty value in the configuration file means a credential was written to disk.
SECRET_KEYS = frozenset({"PADOS_DB_PASSWORD"})


class ConfigFileError(ValueError):
    def __init__(self, code: str, line: int | None = None) -> None:
        super().__init__(code if line is None else f"{code} (line {line})")
        self.code = code
        self.line = line


@dataclass(frozen=True)
class ConfigFindings:
    secrets_in_file: tuple[str, ...]
    password_ref: str | None
    password_ref_valid: bool


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (``#`` comments, blank lines). Values are taken literally (no expansion)."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise ConfigFileError("CONFIG_LINE_INVALID", number)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in values:
            raise ConfigFileError("CONFIG_KEY_DUPLICATED", number)
        values[key] = value
    return values


def read_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigFileError("CONFIG_MISSING")
    return parse_env_text(path.read_text(encoding="utf-8-sig"))


def findings(values: Mapping[str, str]) -> ConfigFindings:
    stored = tuple(sorted(key for key in values if key in SECRET_KEYS and values[key]))
    stored += tuple(sorted(key for key, value in values.items()
                           if key not in SECRET_KEYS and value and looks_like_secret(value)))
    ref = values.get(PASSWORD_REF_KEY) or None
    return ConfigFindings(secrets_in_file=stored, password_ref=ref,
                          password_ref_valid=ref is None or is_secret_ref(ref))


def apply_config(values: Mapping[str, str], environ: MutableMapping[str, str]) -> tuple[str, ...]:
    """Copy file values into ``environ`` where the environment does not already define them."""
    applied: list[str] = []
    for key, value in values.items():
        if key not in environ:
            environ[key] = value
            applied.append(key)
    _resolve_password(environ)
    return tuple(applied)


def _resolve_password(environ: MutableMapping[str, str]) -> None:
    ref = environ.get(PASSWORD_REF_KEY, "")
    if not ref:
        return
    if ref == LIBPQ_PASSFILE_REF:
        passfile = environ.get(PASSFILE_KEY, "")
        if passfile and "PGPASSFILE" not in environ:
            environ["PGPASSFILE"] = passfile
        # Empty, not unset: Core's built-in development default must never be used for an installation.
        environ.setdefault("PADOS_DB_PASSWORD", "")
        return
    if ref.startswith("secretref://env/") and is_secret_ref(ref):
        name = ref.removeprefix("secretref://env/")
        value = environ.get(name, "")
        if name != "PADOS_DB_PASSWORD" or not value:
            environ["PADOS_DB_PASSWORD"] = value
        return
    environ.setdefault("PADOS_DB_PASSWORD", "")


def bootstrap(home: Path, environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Load ``home``'s configuration into the environment and reset Core's cached settings."""
    from pados.config import get_settings
    from pados.install.home import config_file

    target = os.environ if environ is None else environ
    values = read_config(config_file(home))
    target["PADOS_HOME"] = str(home)
    apply_config(values, target)
    get_settings.cache_clear()
    return values
