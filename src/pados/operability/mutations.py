# SPDX-License-Identifier: AGPL-3.0-only
"""Every mutating route of the core API, with its idempotency or reconciliation contract (OP-004).

``IDEMPOTENCY_KEY``: the request carries an idempotency key. The same key with the same content returns the same
record, and the same key with different content is a ``*_IDEMPOTENCY_CONTENT_CONFLICT``.
``STATE_GUARDED``: a transition guarded by the current state. A repeat returns the current state or a conflict and never
applies the change twice.
``NO_DURABLE_WRITE``: computes or reads only; safe to repeat.
``RECONCILE_BEFORE_RETRY``: each call may create or change a record. A client must read the current state before
retrying and must never retry automatically (OP-007: the browser retries only safe reads). Uncertain routes are
classified here, conservatively.

``tests/operability/test_op004_mutation_inventory.py`` fails when a mutating route is added without an entry. This is
a contract record only: it changes no route's behaviour or authority.
"""
from __future__ import annotations

from enum import StrEnum


class MutationKind(StrEnum):
    IDEMPOTENCY_KEY = "IDEMPOTENCY_KEY"
    STATE_GUARDED = "STATE_GUARDED"
    NO_DURABLE_WRITE = "NO_DURABLE_WRITE"
    RECONCILE_BEFORE_RETRY = "RECONCILE_BEFORE_RETRY"


_KEY = MutationKind.IDEMPOTENCY_KEY
_STATE = MutationKind.STATE_GUARDED
_RECONCILE = MutationKind.RECONCILE_BEFORE_RETRY

# The idempotency key is a required field of a plain JSON payload, not of a body model.
PAYLOAD_KEY_ROUTES = frozenset({("POST", "/control/loops/{loop_id}/step")})

MUTATIONS: dict[tuple[str, str], MutationKind] = {
    ("POST", "/memory/facts"): _RECONCILE,  # refuses anything but CANDIDATE memory; a repeat adds another candidate
    ("POST", "/memory/facts/{memory_id}/promote"): _RECONCILE,
    ("POST", "/context/assemble"): _RECONCILE,  # may store a context packet
    ("POST", "/control/projects/{project_id}/messages"): _KEY,
    ("POST", "/control/work-requests"): _KEY,
    ("POST", "/control/work-requests/{request_id}/route"): _KEY,
    ("POST", "/control/approvals"): _KEY,
    # A decision applies once: a decided approval refuses every later decision ("approval is not pending",
    # APPROVAL_NOT_REVOCABLE, or a stale version). The key only lets an exact replay return the current record; a
    # different decision under the same key is not reported as a content conflict, so these are not IDEMPOTENCY_KEY.
    ("POST", "/control/approvals/{approval_id}/approve"): _STATE,
    ("POST", "/control/approvals/{approval_id}/reject"): _STATE,
    ("POST", "/control/approvals/{approval_id}/revise"): _STATE,
    ("POST", "/control/approvals/{approval_id}/revoke"): _STATE,
    ("POST", "/control/projections/render"): _RECONCILE,
    ("POST", "/control/jobs/{job_id}/cancel"): _STATE,
    ("POST", "/control/task-goals"): _KEY,
    ("POST", "/control/task-plans"): _KEY,
    ("POST", "/control/loops"): _KEY,
    ("POST", "/control/loops/{loop_id}/result-candidate"): _RECONCILE,
    ("POST", "/control/loops/{loop_id}/start"): _STATE,  # a RUNNING loop is returned unchanged
    ("POST", "/control/loops/{loop_id}/step"): _KEY,
    ("POST", "/control/loops/{loop_id}/pause"): _STATE,  # status predicates (RC2 SEC-3)
    ("POST", "/control/loops/{loop_id}/resume"): _STATE,
    ("POST", "/control/loops/{loop_id}/cancel"): _STATE,
    ("POST", "/api/projects/{project_id}/corpus/batches"): _RECONCILE,
    ("POST", "/api/corpus/batches/{batch_id}/scan"): _RECONCILE,
    ("POST", "/api/corpus/batches/{batch_id}/review"): _RECONCILE,
    ("POST", "/api/corpus/batches/{batch_id}/authorize"): _RECONCILE,
    ("POST", "/api/corpus/batches/{batch_id}/ingest"): _RECONCILE,
    ("POST", "/api/corpus/batches/{batch_id}/retry"): _RECONCILE,
    ("POST", "/api/projects/{project_id}/corpus/search"): _RECONCILE,
    ("POST", "/api/graph/workspaces"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/nodes"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/ingest/url"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/intent"): _RECONCILE,
    ("PATCH", "/api/graph/workspaces/{workspace_id}/nodes/{node_id}"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/nodes/{node_id}/artifact"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/refresh"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/confirm"): _RECONCILE,
    ("PATCH", "/api/graph/workspaces/{workspace_id}/syntheses/{node_id}/policy"): _RECONCILE,
    ("DELETE", "/api/graph/workspaces/{workspace_id}/nodes/{node_id}"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/edges"): _RECONCILE,
    ("DELETE", "/api/graph/workspaces/{workspace_id}/edges/{edge_id}"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/groups"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/selection/actions"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/selection/plan"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/selection/search"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/selection/run"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/selection/cognitive"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/ideas/{node_id}/incubate"): _RECONCILE,
    ("POST", "/api/graph/incubation/{item_id}/process"): _RECONCILE,
    ("POST", "/api/graph/workspaces/{workspace_id}/capabilities/{capability_id}/reuse"): _RECONCILE,
}
