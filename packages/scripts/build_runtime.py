"""Build a PADOS platform runtime payload into packages/runtime-<platform>/runtime/ (local build; never publishes).

Layout produced (relocatable; copied into PADOS_HOME/runtime/<version>/ by ``pados init`` / ``pados update``):

    runtime/python/            python-build-standalone CPython (install_only_stripped) + locked dependency wheels
    runtime/app/src/pados/     PADOS Core source (tracked + new unignored files of this checkout)
    runtime/app/migrations/    the existing Alembic graph; alembic.ini / alembic_p6_19b.ini next to it
    runtime/app/web-dist/      the prebuilt web UI (optional; base /ui/)
    runtime/app/runtime-manifest.json
    runtime/licenses/          CPython + component licenses, every bundled distribution's license files, web licenses
    runtime/sources/           corresponding source for LGPL-3.0 components (psycopg, psycopg-c)

Must run ON the target platform (wheels and bytecode come from the bundled interpreter). Python 3.12 stdlib only.
Usage: python packages/scripts/build_runtime.py --platform linux-x64 [--web-dist DIR] [--cache DIR]
       python packages/scripts/build_runtime.py --platform linux-x64 --lock   (regenerate the hashed wheel lock)

Wheels are installed only from packages/scripts/runtime-wheels-<platform>.txt (``name==version --hash=sha256:...``
for every bundled wheel; ``pip --require-hashes --no-deps``); any unlisted wheel or hash mismatch fails the build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PBS_RELEASE = "20261003"
PBS_PYTHON = "3.12.15"
PBS_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_RELEASE}"
# Pinned archives (sha256 from the GitHub release asset digests).
PBS = {
    "win32-x64": {
        "triple": "x86_64-pc-windows-msvc",
        "sha256": "6fba7f2ae506facf41d457ea8293c7497910a675c69a4e954875169410a50402",
        "full": "pgo-full", "full_sha256": "200c8cd8b3b157d5c415c4cddb84ba4cb9def5499a08fe311f1e661fd195d116",
        "exe": "python/python.exe", "site": "python/Lib/site-packages", "stdlib": "python/Lib",
    },
    "linux-x64": {
        "triple": "x86_64-unknown-linux-gnu",
        "sha256": "731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91",
        "full": "pgo+lto-full", "full_sha256": "aaeeeeea4f98a41c7acd7379229a374c201aee45c1954be5788c18af83733d2b",
        "exe": "python/bin/python3.12", "site": "python/lib/python3.12/site-packages",
        "stdlib": "python/lib/python3.12",
        # psycopg-binary's Linux wheel vendors LGPL native libraries (libcrypt, libkeyutils) whose exact
        # corresponding source we cannot pin: the compliant choice is the pure-Python psycopg implementation
        # over the distribution's own libpq (doctor reports DATABASE_DRIVER_UNAVAILABLE if it is missing).
        "rewrite": "psycopg[binary]=>psycopg",
    },
    "darwin-arm64": {
        "triple": "aarch64-apple-darwin",
        "sha256": "ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2",
        "full": None, "full_sha256": None,
        "exe": "python/bin/python3.12", "site": "python/lib/python3.12/site-packages",
        "stdlib": "python/lib/python3.12",
    },
}
# Not needed by PADOS; removed to shrink the payload and its license surface (recorded in the manifest).
REMOVED_STDLIB = ("test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "turtle.py")
LGPL_SOURCES = (("psycopg", "psycopg"), ("psycopg-binary", "psycopg-c"))
APP_PATHS = ("src/pados", "migrations", "alembic.ini", "alembic_p6_19b.ini")


def run(command: list[str], **kwargs: object) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)  # type: ignore[call-overload]
    return str(completed.stdout)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, target: Path, expected: str | None) -> Path:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as response, target.open("wb") as out:  # noqa: S310 - pinned https
            shutil.copyfileobj(response, out)
    actual = sha256_file(target)
    if expected and actual != expected:
        target.unlink()
        raise SystemExit(f"SHA256_MISMATCH {target.name}: {actual} != {expected}")
    return target


def extract(archive: Path, destination: Path, members: str | None = None) -> None:
    if archive.suffix == ".zst":
        command = ["tar", "--zstd", "-xf", str(archive), "-C", str(destination)]
        run(command + ([members] if members else []))
        return
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def remove_symlinks(root: Path) -> list[str]:
    """npm tarballs cannot carry symlinks: drop launcher aliases, materialize anything else as a copy."""
    handled: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        target = path.resolve()
        relative = path.relative_to(root).as_posix()
        alias = path.parent.name == "bin" or path.name.startswith("libpython") and path.suffix == ".so"
        path.unlink()
        if not alias and target.is_file() and root.resolve() in target.parents:
            shutil.copy2(target, path)
            handled.append(f"copied:{relative}")
        else:
            handled.append(f"removed:{relative}")
    return handled


def strip_python(runtime: Path, spec: dict[str, str | None]) -> list[str]:
    removed: list[str] = []
    stdlib = runtime / str(spec["stdlib"])
    for name in REMOVED_STDLIB:
        target = stdlib / name
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(f"stdlib/{name}")
        elif target.is_file():
            target.unlink()
            removed.append(f"stdlib/{name}")
    for pattern in ("python/tcl", "python/lib/tcl8*", "python/lib/tk8*", "python/lib/tcl9*", "python/lib/tk9*",
                    "python/lib/itcl*", "python/lib/thread2*",
                    "python/lib/libtcl*", "python/lib/libtk*", "python/DLLs/_tkinter.pyd", "python/DLLs/tcl*.dll",
                    "python/DLLs/tk*.dll", "python/lib/python3.12/lib-dynload/_tkinter*", "python/include",
                    "python/Scripts", "python/share"):
        for target in runtime.glob(pattern):
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            removed.append(target.relative_to(runtime).as_posix())
    site = runtime / str(spec["site"])
    for target in list(site.glob("pip")) + list(site.glob("pip-*.dist-info")):
        shutil.rmtree(target)
        removed.append(f"site-packages/{target.name}")
    for target in (runtime / "python" / "bin").glob("*") if (runtime / "python" / "bin").exists() else []:
        if target.name not in {"python3.12"}:
            target.unlink()
            removed.append(f"bin/{target.name}")
    return removed


def runtime_requirements(rewrite: str | None = None) -> list[str]:
    """pyproject runtime dependencies; ``rewrite`` = "old=>new" replaces a requirement prefix (per platform)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    old, _, new = (rewrite or "").partition("=>")
    requirements = []
    for requirement in project["project"]["dependencies"]:
        if old and requirement.startswith(old):
            requirement = new + requirement.removeprefix(old)
        requirements.append(requirement)
    return requirements


SCRIPTS = Path(__file__).resolve().parent
EXTRA_LOCK = SCRIPTS / "runtime-extra.lock"
HASHED_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)((?:\s+--hash=sha256:[0-9a-f]{64})+)\s*$")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def hashed_lock_path(platform: str) -> Path:
    return SCRIPTS / f"runtime-wheels-{platform}.txt"


def parse_hashed_lock(path: Path) -> dict[str, tuple[str, frozenset[str]]]:
    """{normalized name: (version, sha256 set)}; every non-comment line must be ``name==version --hash=sha256:<hex>``."""
    entries: dict[str, tuple[str, frozenset[str]]] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = HASHED_LINE.match(line)
        if match is None:
            raise SystemExit(f"HASHED_LOCK_INVALID {path.name}:{number}: {line!r}")
        hashes = frozenset(re.findall(r"sha256:([0-9a-f]{64})", match.group(3)))
        entries[normalize(match.group(1))] = (match.group(2), hashes)
    return entries


def wheel_identity(wheel: Path) -> tuple[str, str]:
    name, version = wheel.name.split("-")[:2]
    return normalize(name), version


def lock_text() -> str:
    base = (ROOT / "requirements.lock").read_text(encoding="utf-8-sig")
    return base.rstrip("\n") + "\n" + EXTRA_LOCK.read_text(encoding="utf-8")


def lock_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in lock_text().splitlines():
        name, _, version = line.strip().partition("==")
        if name and not name.startswith("#"):
            pins[re.sub(r"[-_.]+", "-", name).lower()] = version
    return pins


def pip_command(python: Path) -> list[str]:
    return [str(python), "-m", "pip", "--disable-pip-version-check", "--no-input"]


def write_hashed_lock(python: Path, work: Path, platform: str, rewrite: str | None = None) -> Path:
    """--lock: resolve the runtime requirements under requirements.lock (+ runtime-extra.lock) with the pinned
    interpreter, download binary wheels into an empty directory and record ``name==version --hash=sha256:...`` for
    every one. Output is sorted and carries no timestamp, so the same index state gives the same file."""
    constraints = work / "constraints.txt"
    constraints.write_text(lock_text(), encoding="utf-8")
    requirements = work / "requirements.txt"
    requirements.write_text("\n".join(runtime_requirements(rewrite)) + "\n", encoding="utf-8")
    wheels = work / "lock-wheels"
    wheels.mkdir()
    run(pip_command(python) + ["download", "--only-binary=:all:", "-c", str(constraints), "-r", str(requirements),
                               "-d", str(wheels)])
    pins = lock_pins()
    lines = []
    for wheel in sorted(wheels.iterdir(), key=lambda p: wheel_identity(p) if p.suffix == ".whl" else (p.name, "")):
        if wheel.suffix != ".whl":
            raise SystemExit(f"NOT_A_WHEEL {wheel.name}")
        key, version = wheel_identity(wheel)
        if pins.get(key) != version:
            raise SystemExit(f"LOCK_MISMATCH {wheel.name}: requirements.lock pins {pins.get(key)}")
        lines.append(f"{key}=={version} --hash=sha256:{sha256_file(wheel)}")
    target = hashed_lock_path(platform)
    header = [
        f"# Hashed runtime wheel lock for @pados-ai/runtime-{platform}. GENERATED, do not edit by hand:",
        f"#   python packages/scripts/build_runtime.py --platform {platform} --lock",
        f"# Interpreter: CPython {PBS_PYTHON} (python-build-standalone {PBS_RELEASE}, {PBS[platform]['triple']}).",
        "# Versions come from requirements.lock + packages/scripts/runtime-extra.lock; one line per bundled wheel.",
    ]
    target.write_text("\n".join(header + lines) + "\n", encoding="utf-8", newline="\n")
    return target


def verify_downloaded(wheels: Path, entries: dict[str, tuple[str, frozenset[str]]]) -> dict[str, Path]:
    """Independent re-check of what pip downloaded: exactly the listed wheels, each with a listed hash."""
    found: dict[str, Path] = {}
    for wheel in sorted(wheels.iterdir()):
        key, version = wheel_identity(wheel) if wheel.suffix == ".whl" else (wheel.name, "")
        expected = entries.get(key)
        if expected is None or expected[0] != version:
            raise SystemExit(f"WHEEL_NOT_LISTED {wheel.name}")
        if sha256_file(wheel) not in expected[1]:
            raise SystemExit(f"WHEEL_HASH_MISMATCH {wheel.name}")
        found[key] = wheel
    missing = sorted(set(entries) - set(found))
    if missing:
        raise SystemExit(f"WHEEL_MISSING {', '.join(missing)}")
    return found


def install_dependencies(python: Path, work: Path, platform: str, rewrite: str | None = None) -> list[dict[str, str]]:
    lock_file = hashed_lock_path(platform)
    if not lock_file.is_file():
        raise SystemExit(f"HASHED_LOCK_MISSING {lock_file.name}: run build_runtime.py --platform {platform} --lock")
    entries = parse_hashed_lock(lock_file)
    pins = lock_pins()
    for key, (version, _hashes) in entries.items():
        if pins.get(key) != version:
            raise SystemExit(f"HASHED_LOCK_STALE {key}=={version} but requirements.lock pins {pins.get(key)}")
    for requirement in runtime_requirements(rewrite):
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        if name is None or normalize(name.group(0)) not in entries:
            raise SystemExit(f"HASHED_LOCK_INCOMPLETE runtime requirement {requirement!r} is not listed")
    wheels = work / "wheels"
    wheels.mkdir()
    pip = pip_command(python)
    run(pip + ["download", "--only-binary=:all:", "--require-hashes", "--no-deps", "-r", str(lock_file),
               "-d", str(wheels)])
    found = verify_downloaded(wheels, entries)
    run(pip + ["install", "--no-index", "--find-links", str(wheels), "--only-binary=:all:", "--require-hashes",
               "--no-deps", "--no-compile", "--no-warn-script-location", "-r", str(lock_file)])
    run(pip + ["check"])  # --no-deps: the lock itself must be dependency-complete
    installed = json.loads(run(pip + ["list", "--format", "json"]))
    rows: list[dict[str, str]] = []
    for item in installed:
        key = normalize(item["name"])
        if key == "pip":
            continue
        if key not in entries or entries[key][0] != item["version"] or pins.get(key) != item["version"]:
            raise SystemExit(f"LOCK_MISMATCH {item['name']} {item['version']} != {pins.get(key)}")
        wheel = found[key]
        rows.append({"name": item["name"], "version": item["version"],
                     "wheel": wheel.name, "wheel_sha256": sha256_file(wheel)})
    if {normalize(row["name"]) for row in rows} != set(entries):
        raise SystemExit("LOCK_MISMATCH installed distributions differ from the hashed lock")
    return sorted(rows, key=lambda row: row["name"].lower())


def copy_app(app: Path) -> dict[str, object]:
    listed = run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *APP_PATHS], cwd=ROOT)
    count = 0
    for name in sorted(set(filter(None, listed.split("\0")))):
        source = ROOT / name
        if not source.is_file() or "__pycache__" in name:
            continue
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    commit = run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip()
    dirty = bool(run(["git", "status", "--porcelain", "--", *APP_PATHS], cwd=ROOT).strip())
    return {"commit": commit, "dirty": dirty, "files": count}


def collect_licenses(runtime: Path, spec: dict[str, str | None], full_archive: Path | None, work: Path) -> list[str]:
    licenses = runtime / "licenses"
    (licenses / "python-packages").mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    for candidate in ("python/LICENSE.txt", f"{spec['stdlib']}/LICENSE.txt"):
        if (runtime / candidate).is_file():
            shutil.copy2(runtime / candidate, licenses / "CPython-LICENSE.txt")
            break
    if full_archive is not None:
        scratch = work / "full"
        scratch.mkdir(exist_ok=True)
        extract(full_archive, scratch, "python/licenses")
        shutil.copytree(scratch / "python" / "licenses", licenses / "python-build-standalone", dirs_exist_ok=True)
        notes.append("python-build-standalone component licenses copied from the matching full distribution")
    else:
        notes.append("RESIDUAL: component license directory of the python-build-standalone full distribution not bundled")
    site = runtime / str(spec["site"])
    for dist in sorted(site.glob("*.dist-info")):
        target = licenses / "python-packages" / dist.name.removesuffix(".dist-info")
        files = [p for p in dist.rglob("*") if p.is_file() and re.match(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", p.name, re.I)]
        for path in files:
            destination = target / path.relative_to(dist)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        if not files:
            notes.append(f"RESIDUAL: no license file shipped in {dist.name} (see METADATA)")
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dist / "METADATA", target / "METADATA")
    shutil.copytree(ROOT / "THIRD_PARTY_LICENSES", licenses / "web", dirs_exist_ok=True)
    notes += native_library_notices(site, licenses)
    return notes


# Vendored (no build-time fetch). Provenance: PostgreSQL tag REL_17_11 = commit
# 083ac033419f690758508e08c1736089384bbee8, file COPYRIGHT, fetched from
# https://raw.githubusercontent.com/postgres/postgres/083ac033419f690758508e08c1736089384bbee8/COPYRIGHT
POSTGRES_COPYRIGHT = SCRIPTS / "vendor" / "libpq-PostgreSQL-COPYRIGHT.txt"
POSTGRES_COPYRIGHT_SHA256 = "3d6af92ff8a4c2cdf69afb1cf44edea727922f5cd0cf8b5f72b11cdecac8fdfd"


def postgres_copyright() -> bytes:
    data = POSTGRES_COPYRIGHT.read_bytes().replace(b"\r\n", b"\n")  # tolerate an autocrlf checkout
    if hashlib.sha256(data).hexdigest() != POSTGRES_COPYRIGHT_SHA256:
        raise SystemExit(f"SHA256_MISMATCH {POSTGRES_COPYRIGHT.name}")
    return data


def native_library_notices(site: Path, licenses: Path) -> list[str]:
    """Native libraries vendored inside binary wheels (delvewheel/auditwheel) do not ship their license texts."""
    native = licenses / "native"
    native.mkdir(exist_ok=True)
    libraries = sorted(p.name for p in site.glob("*.libs/*") if p.is_file())
    (native / "VENDORED_NATIVE_LIBRARIES.txt").write_text(
        "Native libraries vendored inside binary wheels of this runtime:\n" + "\n".join(libraries) + "\n",
        encoding="utf-8")
    notes: list[str] = []
    if any(name.startswith("libpq") for name in libraries):
        (native / "libpq-PostgreSQL-COPYRIGHT.txt").write_bytes(postgres_copyright())
    openssl = licenses / "python-build-standalone" / "LICENSE.openssl-3.txt"
    if openssl.is_file() and any(name.startswith(("libssl", "libcrypto")) for name in libraries):
        shutil.copy2(openssl, native / "OpenSSL-3-LICENSE.txt")
    covered = ("libpq", "libssl", "libcrypto")
    uncovered = [name for name in libraries if not name.startswith(covered)]
    if uncovered:
        notes.append("RESIDUAL: license texts not bundled for vendored native libraries: " + ", ".join(uncovered))
    return notes


def distribution_license(dist: Path) -> str:
    from email.parser import Parser

    meta = Parser().parsestr((dist / "METADATA").read_text(encoding="utf-8", errors="replace"), headersonly=True)
    expression = meta.get("License-Expression")
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    raw = (meta.get("License") or "").strip().splitlines()
    return str(expression or "; ".join(classifiers) or (raw[0][:80] if raw else "UNKNOWN"))


def write_notices(runtime: Path, spec: dict[str, str | None], installed: list[dict[str, str]],
                  lgpl: list[dict[str, str]], platform: str, notes: list[str]) -> Path:
    site = runtime / str(spec["site"])
    licenses = {re.sub(r"[-_.]+", "-", d.name.split("-")[0]).lower(): distribution_license(d)
                for d in site.glob("*.dist-info")}
    lines = [
        f"# Third-party notices: @pados-ai/runtime-{platform}",
        "",
        "This package redistributes the components below, unmodified. License texts are in `runtime/licenses/`.",
        "PADOS itself is licensed under AGPL-3.0-only (see LICENSE and NOTICE). Automated inventory; not legal advice.",
        "",
        "## Python runtime",
        "",
        f"- CPython {PBS_PYTHON} (PSF-2.0 and the component licenses in `runtime/licenses/CPython-LICENSE.txt` and",
        f"  `runtime/licenses/python-build-standalone/`), built by python-build-standalone {PBS_RELEASE}",
        "  (https://github.com/astral-sh/python-build-standalone). Tk/Tcl, IDLE, ensurepip and pip were removed.",
        "",
        "## Python distributions (exact pins from requirements.lock)",
        "",
        "| Distribution | Version | License (package metadata) |",
        "|---|---|---|",
    ]
    for row in installed:
        key = re.sub(r"[-_.]+", "-", row["name"]).lower()
        lines.append(f"| {row['name']} | {row['version']} | {licenses.get(key, 'UNKNOWN')} |")
    lines += [
        "",
        "## Web UI",
        "",
        "The prebuilt UI (`runtime/app/web-dist/`) contains react, react-dom and scheduler (MIT) and a Vite helper (MIT);",
        "texts in `runtime/licenses/web/`.",
        "",
        "## Source availability (LGPL-3.0 and MPL-2.0 components)",
        "",
        "- The LGPL-3.0-only distributions below are shipped as separate, unmodified, replaceable modules inside",
        "  `runtime/python` and are used only through their public Python API. Their complete corresponding",
        "  source is included in `runtime/sources/`:",
    ]
    lines += [f"  - `{row['sdist']}` (sha256 `{row['sha256']}`) for {row['for']}" for row in lgpl]
    if any(row["for"] == "psycopg-binary" for row in lgpl):
        lines += [
            "  The native libraries vendored inside the psycopg-binary wheel (listed in",
            "  `runtime/licenses/native/VENDORED_NATIVE_LIBRARIES.txt`: libpq under the PostgreSQL License, OpenSSL",
            "  under Apache-2.0) carry permissive licenses; their texts are in `runtime/licenses/native/`.",
        ]
    else:
        lines += [
            "  psycopg-binary is NOT bundled on this platform: psycopg runs its pure-Python implementation over the",
            "  operating system's own libpq (install your distribution's libpq package; PADOS never installs it).",
        ]
    lines += [
        "  You may replace these modules with your own builds.",
        "- certifi is MPL-2.0. Its covered files are shipped in source form (Python source and `cacert.pem`).",
        "",
        "## Known gaps (to be closed before any public distribution)",
        "",
    ]
    residuals = [note for note in notes if note.startswith("RESIDUAL")]
    lines += [f"- {note}" for note in residuals] or ["- none recorded by the build"]
    lines.append("")
    target = runtime / "licenses" / "THIRD_PARTY_NOTICES.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def fetch_lgpl_sources(runtime: Path, installed: list[dict[str, str]], cache: Path) -> list[dict[str, str]]:
    versions = {row["name"].lower(): row["version"] for row in installed}
    sources = runtime / "sources"
    sources.mkdir(exist_ok=True)
    rows = []
    for distribution, project in LGPL_SOURCES:
        version = versions.get(distribution)
        if not version:
            continue
        with urllib.request.urlopen(f"https://pypi.org/pypi/{project}/{version}/json", timeout=60) as response:  # noqa: S310
            release = json.load(response)
        sdist = next(item for item in release["urls"] if item["packagetype"] == "sdist")
        path = fetch(sdist["url"], cache / sdist["filename"], sdist["digests"]["sha256"])
        shutil.copy2(path, sources / sdist["filename"])
        rows.append({"for": distribution, "sdist": sdist["filename"], "sha256": sdist["digests"]["sha256"],
                     "license": "LGPL-3.0-only"})
    return rows


# Recorded as co_filename instead of the build machine's absolute path (review R-2). Harmless at run time: the
# import system replaces co_filename with the real source path when it loads a .pyc next to its source.
NEUTRAL_SOURCE_PREFIX = "pados-runtime"


def compile_bytecode(python: Path, runtime: Path, spec: dict[str, str | None]) -> None:
    for cache_dir in list(runtime.rglob("__pycache__")):
        shutil.rmtree(cache_dir)
    neutral = ["-s", str(runtime), "-p", NEUTRAL_SOURCE_PREFIX]
    # -B: modules imported by compileall itself must not leave timestamp .pyc files (with absolute paths) behind;
    # -f: rewrite every .pyc even if one with a matching header already exists.
    base = [str(python), "-B", "-m", "compileall", "-f", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash",
            *neutral]
    # The stdlib keeps a few intentionally uncompilable files (e.g. lib2to3-style test data): best effort.
    subprocess.run([*base, str(runtime / str(spec["stdlib"]))], check=False, capture_output=True)
    for target in (runtime / "app" / "src", runtime / "app" / "migrations"):
        completed = subprocess.run([*base, str(target)], check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise SystemExit(f"COMPILE_FAILED {target.name}: {completed.stdout[-2000:]}")


def tree_digest(root: Path) -> dict[str, object]:
    """Same algorithm as packages/cli/lib/runtime.js treeDigest."""
    entries = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"SYMLINK_NOT_ALLOWED {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if not relative.isascii():
                raise SystemExit(f"NON_ASCII_PATH {relative!r}")
            if relative in {"app/runtime-manifest.json", ".installed"}:
                continue
            entries.append(relative)
    index = hashlib.sha256()
    total = 0
    for relative in sorted(entries):
        data = (root / relative).read_bytes()
        total += len(data)
        index.update(f"{relative}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    return {"count": len(entries), "bytes": total, "digest": index.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", required=True, choices=sorted(PBS))
    parser.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "pados-runtime-build-cache")
    parser.add_argument("--web-dist", type=Path, default=None)
    parser.add_argument("--no-full-licenses", action="store_true", help="skip the PBS full archive (license dir)")
    parser.add_argument("--lock", action="store_true",
                        help="regenerate packages/scripts/runtime-wheels-<platform>.txt (hashed wheel lock) and exit")
    args = parser.parse_args()
    spec = PBS[args.platform]
    name = f"cpython-{PBS_PYTHON}+{PBS_RELEASE}-{spec['triple']}-install_only_stripped.tar.gz"
    archive = fetch(f"{PBS_BASE}/{name.replace('+', '%2B')}", args.cache / name, str(spec["sha256"]))
    if args.lock:
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            extract(archive, work)
            target = write_hashed_lock(work / str(spec["exe"]), work, args.platform, spec.get("rewrite"))
        print(f"HASHED_LOCK_WRITTEN {target.relative_to(ROOT).as_posix()} ({len(parse_hashed_lock(target))} wheels)")
        return 0
    package = ROOT / "packages" / f"runtime-{args.platform}"
    package_json = json.loads((package / "package.json").read_text(encoding="utf-8"))
    runtime = package / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime, onerror=lambda f, p, _e: (os.chmod(p, stat.S_IWRITE), f(p)))
    runtime.mkdir(parents=True)
    extract(archive, runtime)
    full = None
    if spec["full"] and not args.no_full_licenses:
        full_name = f"cpython-{PBS_PYTHON}+{PBS_RELEASE}-{spec['triple']}-{spec['full']}.tar.zst"
        full = fetch(f"{PBS_BASE}/{full_name.replace('+', '%2B')}", args.cache / full_name, str(spec["full_sha256"]))
    links = remove_symlinks(runtime)
    python = runtime / str(spec["exe"])
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        installed = install_dependencies(python, work, args.platform, spec.get("rewrite"))
        removed = strip_python(runtime, spec)
        app = runtime / "app"
        source = copy_app(app)
        site = runtime / str(spec["site"])
        relative_src = os.path.relpath(app / "src", site).replace(os.sep, "/")
        (site / "pados-app.pth").write_text(relative_src + "\n", encoding="utf-8")
        web = False
        if args.web_dist is not None:
            shutil.copytree(args.web_dist, app / "web-dist")
            web = (app / "web-dist" / "index.html").is_file()
        notes = collect_licenses(runtime, spec, full, work)
        lgpl = fetch_lgpl_sources(runtime, installed, args.cache)
        write_notices(runtime, spec, installed, lgpl, args.platform, notes)
    links += remove_symlinks(runtime)
    compile_bytecode(python, runtime, spec)
    version_check = run([str(python), "-I", "-B", "-c", "import pados.install, sys; print(sys.version.split()[0])"]).strip()
    manifest = {
        "schema": "pados.runtime-manifest.v1", "platform": args.platform, "package": package_json["name"],
        "package_version": package_json["version"], "python_executable": spec["exe"],
        "python": {"version": version_check, "distribution": f"python-build-standalone {PBS_RELEASE}",
                   "archive": name, "sha256": spec["sha256"]},
        "source": source, "dependencies": installed, "lgpl_sources": lgpl, "web_ui": web,
        "removed_components": removed, "symlinks": links, "license_notes": notes,
        "files": tree_digest(runtime),
    }
    (app / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"platform": args.platform, "files": manifest["files"], "dependencies": len(installed),
                      "web_ui": web, "license_notes": notes}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
