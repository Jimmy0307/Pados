# Deadlines, retries, idempotency and leases (OP-004)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §5. Code:
`src/pados/operability/resilience.py`, `src/pados/operability/mutations.py`, `src/pados/control/service.py`
(content conflicts), `src/pados/loop/service.py` (`_hold_lease`). Tests: `tests/operability/test_op004_resilience.py`,
`tests/operability/test_op004_mutation_inventory.py`, `tests/integration/test_op004_idempotency_postgres.py`,
`tests/integration/test_op004_lease_postgres.py`, `tests/integration/test_op004_lease_model_takeover_postgres.py`.

These extend the existing TaskLoop, worker-job and node mechanisms. No second scheduler or retry loop exists. The
TaskLoop owns iterations, each worker job keeps its own timeout, and nothing here authorizes work.

## Parent deadline

- An API request may send `X-Request-Deadline-Ms` (an integer from 1 to 600000). Without it nothing changes.
- A child call takes `min(its own timeout, remaining budget)`, and an inner deadline never extends an outer one.
  - Compute-node calls are refused with `NODE_DEADLINE_EXCEEDED` before any network call once the budget is spent.
  - A local-model call closes the job as `TIMED_OUT` / `PARENT_DEADLINE_EXCEEDED` without calling the model when less
    than one second is left (the provider takes whole seconds).
- Database calls keep their 2-second connect timeout. A per-statement deadline is not applied, and is listed as
  remaining work.

## Retry

`retry_call` retries only failures that declare `retryable = True`, bounded by attempts and by the remaining budget,
with exponential backoff and ±25 % jitter. Every retry logs `retry.attempt`; the end logs `retry.exhausted` with reason
`ATTEMPTS` or `DEADLINE`. Authorization, validation, authority, not-found, conflict and unclassified failures are never
retried.

The one retried boundary today is the compute-node job-status read (`NodeClient.job`, a GET): one retry on
`NODE_TIMEOUT` / `NODE_OFFLINE`. `execute` and `cancel` are mutations and are never retried.

## Idempotency

- Control records (`create_work_request`, `add_message`, `create_approval`, task goals, plans and loops): the same key
  with the same content returns the same record. The same key with different content is
  `*_IDEMPOTENCY_CONTENT_CONFLICT` (HTTP 409), never a silent success that returns the earlier content. Work requests,
  messages and unbound approvals did not check this before OP-004.
- Every mutating core API route is classified in `pados.operability.mutations.MUTATIONS`:

| Kind | Meaning | Client rule |
|---|---|---|
| `IDEMPOTENCY_KEY` | Takes an idempotency key | Retry with the same key and the same body |
| `STATE_GUARDED` | A transition guarded by current state (loop start, pause, resume, cancel; job cancel) | Retry is safe; read the returned state |
| `RECONCILE_BEFORE_RETRY` | Each call may create or change a record (graph, corpus, memory, projections, result candidate, context assembly) | Read current state first; never retry automatically |

  A test fails when a mutating route has no entry, or when an `IDEMPOTENCY_KEY` route takes no key.

## TaskLoop lease

- `start_loop` takes the 5-minute lease under a nominal owner (`control-api`, `graph-api`, or the CLI's `--owner`).
- A caller that names an owner (`run_step(..., owner_instance=...)`, `pados loop step --owner NAME`) holds the lease:
  it is taken when free, lapsed or already that owner's, and renewed on every iteration (`task_loop.lease.held`).
  While another owner holds a live lease, the iteration is refused with `LOOP_LEASE_HELD` before any step is
  selected, routed or published (`task_loop.lease.refused`, naming the holder).
- After a crash the lease lapses within five minutes and another owner resumes the loop from its persisted iterations.
- An owner-less step (the API step route, or the CLI without `--owner`) acts as the loop's **starting owner**, the
  owner named in its `LOOP_STARTED` event. It renews that owner's lease, takes a free or lapsed lease as that owner,
  and is refused with `LOOP_LEASE_HELD` while any other owner holds a live lease. So one operator who starts and
  steps a loop keeps one lease, and a worker that took the lease over is never overridden by an owner-less step. A
  loop with no recorded starting owner is refused (`LOOP_LEASE_OWNER_UNKNOWN`).
- A deferred local-model call (RC2 SEC-9) runs with no transaction open, so the lease can lapse and be taken over
  while the model runs. The whole `run_step` holds the lease as one owner:
  - the model result is recorded only while that owner still holds the lease, checked with the lease row locked in
    the same transaction. Otherwise the job closes `FAILED` / `LOOP_LEASE_LOST`, and the output is neither recorded
    nor kept on disk;
  - an owner fenced before the claim leaves the job `ROUTED` for the current holder, and its model is never called;
  - the resumed iteration holds the lease again, so an owner that lost it decides and publishes nothing
    (`LOOP_LEASE_HELD`). A result recorded while the owner still held the lease is validated and decided by whoever
    holds the lease next, through the normal path.

Trade-off, kept deliberately: an owner-less step shares the starting owner's identity. Two operator surfaces used at
the same time on one loop (the API and the CLI) are therefore not fenced from each other; they are serialised by the
loop row lock and the single-active-iteration check, and each model job is claimed exactly once. A deployment with
several schedulers must pass each instance's own owner name. Requiring an explicit owner on every step would remove
the shared identity, but it changes the operator API; that is a product decision, not part of this fix.

## Recovery

| Symptom | Code | Safe action |
|---|---|---|
| A step is refused because another worker holds the loop | `LOOP_LEASE_HELD` | Wait for that owner, or for the lease to lapse (≤ 5 min) if the owner is gone |
| A model job closed after the call | `LOOP_LEASE_LOST` | The lease was taken over while the model ran. The output was discarded; the current holder's next step routes the work again |
| Every step is refused for a loop | `LOOP_LEASE_OWNER_UNKNOWN` | The loop has no `LOOP_STARTED` owner; step it with an explicit `--owner` |
| A retried create returns a conflict | `*_IDEMPOTENCY_CONTENT_CONFLICT` | The key was already used for different content; use a new key for the new content |
| A model job closes immediately | `PARENT_DEADLINE_EXCEEDED` | The caller's budget was spent; retry with a larger `X-Request-Deadline-Ms` |
| A node call is refused | `NODE_DEADLINE_EXCEEDED` | Same as above; nothing was sent to the node |
