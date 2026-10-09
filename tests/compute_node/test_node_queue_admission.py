"""Deterministic admission tests for the node's bounded queue (Stage B R4).

Contract: with ``max_concurrent_jobs = N`` and ``max_queue_depth = Q`` at most ``N + Q`` admitted jobs may be
unfinished at any time. A slot is reserved atomically before ``202`` is returned and is held until the worker
call has returned (success, failure, timeout or cancellation); the HTTP request never owns it.

No test here sleeps: worker progress is controlled with held executors, events, semaphores and barriers.
Request ids are UUIDs: Python 3.12 on Windows has a 15.625 ms ``time.monotonic`` resolution, so clock-derived
ids collide and turn distinct requests into idempotent replays of one job.
"""
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from node_support import ENDPOINT, auth, make_node, node_config, resolver

from pados.compute_node.backends import BackendError, DeterministicBackend
from pados.compute_node.jobs import ExecuteRequest, NodeRejection
from pados.compute_node.service import NodeRuntime, create_node_app

WAIT = 10.0  # upper bound for event waits; a passing run never waits this long


class HeldExecutor:
    """Accepts work but starts none of it until ``drain``: a worker thread not yet scheduled."""

    def __init__(self) -> None:
        self.held: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        self.held.append((fn, args))

    def drain(self) -> None:
        held, self.held = self.held, []
        for fn, args in held:
            fn(*args)

    def shutdown(self, **_: Any) -> None:
        self.held.clear()


class RefusingExecutor:
    def submit(self, *_: Any) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    def shutdown(self, **_: Any) -> None:
        return None


class GatedBackend(DeterministicBackend):
    """Each ``run`` signals ``started`` and blocks until ``gate`` opens (optionally failing afterwards)."""

    def __init__(self) -> None:
        super().__init__("deterministic")
        self.started = threading.Semaphore(0)
        self.gate = threading.Event()
        self.fail = False

    def run(self, capability, backend_model, item, *, max_output_tokens, timeout_seconds):
        self.started.release()
        assert self.gate.wait(WAIT)
        if self.fail:
            raise BackendError("BACKEND_TEST_FAILURE")
        return super().run(capability, backend_model, item, max_output_tokens=max_output_tokens,
                           timeout_seconds=timeout_seconds)


class GatedNode:
    def __init__(self, tmp_path, **limits: Any) -> None:
        values = {"max_concurrent_jobs": 1, "max_queue_depth": 1, "default_timeout_seconds": 5, "max_timeout_seconds": 30,
                  "max_input_chars": 20_000, "max_batch_items": 8, **limits}
        self.backend = GatedBackend()
        self.runtime = NodeRuntime(node_config(tmp_path, limits=values), resolver=resolver(),
                                   backends={"deterministic": self.backend})
        self.client = TestClient(create_node_app(self.runtime), base_url=ENDPOINT)
        self.jobs = self.runtime.jobs
        self.released = threading.Semaphore(0)
        original = self.jobs._release

        def release() -> None:
            original()
            self.released.release()

        self.jobs._release = release  # observe the true end of each worker call

    def wait_started(self, count: int = 1) -> None:
        for _ in range(count):
            assert self.backend.started.acquire(timeout=WAIT)

    def wait_released(self, count: int = 1) -> None:
        for _ in range(count):
            assert self.released.acquire(timeout=WAIT)

    def close(self) -> None:
        self.backend.gate.set()
        self.jobs.shutdown()


@pytest.fixture
def gated(tmp_path):
    node = GatedNode(tmp_path)
    yield node
    node.close()


def _body(**updates: Any) -> dict[str, Any]:
    body = {"request_id": f"r-{uuid.uuid4()}", "work_package_id": "wc-1", "run_id": "run-1",
            "capability": "LOCAL_INFERENCE", "model_id": "deterministic/pados-deterministic-v1@1",
            "classification": "INTERNAL", "inputs": [{"text": "q"}], "output_path": "out.md"}
    body.update(updates)
    return body


def _execute(node, **updates: Any):
    return node.client.post("/execute", json=_body(**updates), headers=auth())


def _held_node(tmp_path, executor):
    node = make_node(tmp_path)  # max_concurrent_jobs=1, max_queue_depth=1
    node.runtime.jobs._executor.shutdown(wait=False)
    node.runtime.jobs._executor = executor
    return node


def _load(jobs) -> tuple[int, int]:
    load = jobs.load()
    return load.running, load.queued


# A, B, C -------------------------------------------------------------------------------------------------
def test_capacity_is_enforced_before_any_worker_is_scheduled(tmp_path):
    executor = HeldExecutor()
    node = _held_node(tmp_path, executor)
    responses = [_execute(node) for _ in range(4)]
    assert [r.status_code for r in responses] == [202, 202, 429, 429]
    assert [r.json()["detail"] for r in responses[2:]] == ["QUEUE_FULL", "QUEUE_FULL"]
    accepted = [r.json() for r in responses[:2]]
    assert len({job["job_id"] for job in accepted}) == 2 and {job["state"] for job in accepted} == {"QUEUED"}
    assert len(executor.held) == 2  # no worker has started, yet both 202s hold a slot (C)
    assert _load(node.runtime.jobs) == (1, 1) and node.runtime.jobs.load().saturated
    assert node.client.get("/health", headers=auth()).json()["state"] == "BUSY"


def test_admission_does_not_depend_on_the_clock(tmp_path, monkeypatch):
    node = _held_node(tmp_path, HeldExecutor())
    monkeypatch.setattr(time, "monotonic_ns", lambda: 1_000_000_000)  # a clock that never advances
    assert [_execute(node).status_code for _ in range(4)] == [202, 202, 429, 429]


# D, E ------------------------------------------------------------------------------------------------------
def test_slot_is_held_while_the_worker_runs_and_released_only_after_it_returns(gated):
    first, second = _execute(gated).json(), _execute(gated).json()
    gated.wait_started()
    assert gated.jobs.get(first["job_id"])["state"] == "RUNNING" and _load(gated.jobs) == (1, 1)
    assert _execute(gated).status_code == 429  # D: busy worker + queued job = full
    gated.backend.gate.set()
    gated.wait_released(2)  # E: both worker calls have returned
    assert _load(gated.jobs) == (0, 0)
    assert {gated.jobs.get(j["job_id"])["state"] for j in (first, second)} == {"SUCCEEDED"}
    assert _execute(gated).status_code == 202


# F -----------------------------------------------------------------------------------------------------------
def test_a_failed_schedule_does_not_leak_a_slot(tmp_path):
    node = _held_node(tmp_path, RefusingExecutor())
    with pytest.raises(RuntimeError):
        _execute(node)
    assert _load(node.runtime.jobs) == (0, 0)


# G -----------------------------------------------------------------------------------------------------------
def test_worker_failure_releases_its_slot(gated):
    gated.backend.fail = True
    job = _execute(gated).json()
    gated.wait_started()
    assert _load(gated.jobs) == (1, 0)
    gated.backend.gate.set()
    gated.wait_released()
    record = gated.jobs.get(job["job_id"])
    assert record["state"] == "FAILED" and record["error_code"] == "BACKEND_TEST_FAILURE"
    assert _load(gated.jobs) == (0, 0)


# H, I --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("terminate", ["timeout", "cancel"])
def test_timeout_or_cancellation_leaves_no_ghost_capacity(gated, terminate):
    running, queued = _execute(gated).json(), _execute(gated).json()
    gated.wait_started()
    if terminate == "timeout":
        gated.jobs._expire(running["job_id"])  # the job timer firing, deterministically
        expected = "TIMED_OUT"
    else:
        assert gated.client.post(f"/jobs/{running['job_id']}/cancel", headers=auth()).status_code == 200
        expected = "CANCELLED"
    assert gated.jobs.get(running["job_id"])["state"] == expected
    assert _load(gated.jobs) == (1, 1)  # the worker still executes: its slot is still held
    assert _execute(gated).status_code == 429
    gated.backend.gate.set()
    gated.wait_released(2)
    final = gated.jobs.get(running["job_id"])
    assert final["state"] == expected and final["artifacts"] == []  # the late result is discarded
    assert gated.jobs.get(queued["job_id"])["state"] == "SUCCEEDED" and _load(gated.jobs) == (0, 0)


def test_cancelling_a_queued_job_keeps_its_slot_until_its_worker_call_returns(tmp_path):
    executor = HeldExecutor()
    node = _held_node(tmp_path, executor)
    first, second = _execute(node).json(), _execute(node).json()
    for job in (first, second):
        assert node.client.post(f"/jobs/{job['job_id']}/cancel", headers=auth()).json()["state"] == "CANCELLED"
    assert _execute(node).status_code == 429
    executor.drain()
    assert _load(node.runtime.jobs) == (0, 0) and _execute(node).status_code == 202


# J -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("running", "queue", "callers"), [(1, 1, 16), (2, 3, 24)])
def test_concurrent_admission_never_exceeds_the_bound(tmp_path, running, queue, callers):
    node = GatedNode(tmp_path, max_concurrent_jobs=running, max_queue_depth=queue)
    node.jobs._executor.shutdown(wait=False)
    node.jobs._executor = HeldExecutor()
    barrier = threading.Barrier(callers)

    def admit(_: int) -> str:
        request = ExecuteRequest.model_validate(_body())
        barrier.wait(WAIT)
        try:
            node.jobs.submit(request)
        except NodeRejection as exc:
            return exc.code
        return "ADMITTED"

    with ThreadPoolExecutor(max_workers=callers) as pool:
        outcomes = list(pool.map(admit, range(callers)))
    assert outcomes.count("ADMITTED") == running + queue
    assert outcomes.count("QUEUE_FULL") == callers - running - queue
    assert sum(_load(node.jobs)) == running + queue
    node.close()


# Idempotency ---------------------------------------------------------------------------------------------------
def test_identical_replay_returns_the_admitted_job_without_a_new_reservation(tmp_path):
    node = _held_node(tmp_path, HeldExecutor())
    body = _body()
    first = node.client.post("/execute", json=body, headers=auth()).json()
    again = node.client.post("/execute", json=body, headers=auth())
    assert again.status_code == 202 and again.json()["job_id"] == first["job_id"]
    assert _load(node.runtime.jobs) == (1, 0)


def test_a_different_request_reusing_an_admitted_id_is_refused(tmp_path):
    node = _held_node(tmp_path, HeldExecutor())
    body = _body()
    node.client.post("/execute", json=body, headers=auth())
    reused = node.client.post("/execute", json={**body, "inputs": [{"text": "something else"}]}, headers=auth())
    assert reused.status_code == 409 and reused.json()["detail"] == "REQUEST_ID_CONFLICT"
    assert _load(node.runtime.jobs) == (1, 0)


def test_concurrent_duplicates_admit_exactly_one_job(tmp_path):
    node = _held_node(tmp_path, HeldExecutor())
    request = ExecuteRequest.model_validate(_body())
    barrier = threading.Barrier(12)

    def admit(_: int) -> str:
        barrier.wait(WAIT)
        return node.runtime.jobs.submit(request)["job_id"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        job_ids = set(pool.map(admit, range(12)))
    assert len(job_ids) == 1 and _load(node.runtime.jobs) == (1, 0)


# Real HTTP / ASGI server boundary ------------------------------------------------------------------------------
def test_real_http_concurrency_respects_the_bound(tmp_path):
    node = GatedNode(tmp_path)
    server = uvicorn.Server(uvicorn.Config(create_node_app(node.runtime), host="127.0.0.1", port=0, log_level="error",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)  # waiting for the listening socket, not for job progress
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    callers = 8
    barrier = threading.Barrier(callers)

    def post(_: int) -> int:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=WAIT) as client:
            barrier.wait(WAIT)
            return client.post("/execute", json=_body(), headers=auth()).status_code

    try:
        with ThreadPoolExecutor(max_workers=callers) as pool:
            codes = list(pool.map(post, range(callers)))
        assert sorted(codes) == [202, 202] + [429] * (callers - 2)
        node.backend.gate.set()
        node.wait_released(2)
        assert _load(node.jobs) == (0, 0)
    finally:
        server.should_exit = True
        thread.join(WAIT)
        node.close()
