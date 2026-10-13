"""Linux rehearsal of the M4.1 live chain: real node service, planner, router, executor and gate; a loopback fake
runtime and a fake host stand in for the model and the device. Never evidence about a model (REHEARSAL only)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "stage_b"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
from capability_profile_support import LICENSE, counted, make_profile, observations  # noqa: E402
from fake_ollama import MANIFEST_HEX  # noqa: E402
from li_fakes import NOW, session, windows_rtx_host, windows_rtx_runner  # noqa: E402

from pados.local_intelligence.catalog_store import ModelCatalogStore  # noqa: E402
from pados.local_intelligence.device import DeviceProfiler  # noqa: E402
from pados.local_intelligence.profile_builder import device_profile_id  # noqa: E402
from pados.resources.capability_profile import MeasuredCapability, ModelCapabilityProfile, profile_id_for  # noqa: E402
from pados.resources.models import ModelCatalog, profile_from_capability  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> Any:
        return self.now


def fake_profiler(clock: Clock, *, vram_free: int | None = None, fail: list[bool] | None = None) -> Any:
    def profile() -> Any:
        if fail and fail[0]:
            raise RuntimeError("profiler failed")
        runner = windows_rtx_runner()
        if vram_free is not None:
            key = next(k for k in runner.replies if "--query-gpu" in k)
            reply = runner.replies[key]
            fields = reply.stdout.decode().split(", ")
            fields[4] = str(vram_free)
            runner.replies[key] = type(reply)(0, ", ".join(fields).encode())
        return DeviceProfiler(host=windows_rtx_host(), session=session(runner), clock=lambda: clock.now).profile()
    return profile


def device_id() -> str:
    clock = Clock()
    return device_profile_id(fake_profiler(clock)().evidence())


def rehearsal_profile(**overrides: Any) -> ModelCapabilityProfile:
    values: dict[str, Any] = dict(artifact_ref="qwen3:8b", manifest_seed="", runtime_family="ollama",
                                  runtime_version="0.34.3", device_id=device_id(),
                                  obs=observations(extract=counted(MeasuredCapability.INFORMATION_EXTRACTION, 2, 2,
                                                                   lower=0.3424)))
    values.update(overrides)
    profile = make_profile(**{k: v for k, v in values.items() if k != "manifest_seed"},
                           manifest_seed=values["manifest_seed"] or "a")
    if not values["manifest_seed"]:  # bind to the digest the fake runtime reports
        body = profile.model_dump(mode="json", exclude={"profile_id"})
        body["artifact"]["manifest_digest"] = "sha256:" + MANIFEST_HEX
        profile = ModelCapabilityProfile.model_validate({**body, "profile_id": profile_id_for(body)})
    return profile


def persist(path: Path, *profiles: ModelCapabilityProfile) -> ModelCatalogStore:
    catalog = ModelCatalog([profile_from_capability(p, license=LICENSE) for p in profiles])
    for profile in profiles:
        catalog = catalog.register_capability_profile(profile)
    store = ModelCatalogStore(path)
    store.save(catalog)
    return store
