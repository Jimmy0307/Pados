# PADOS Master Plan

Last verified: 2026-07-10
Canonical source: this repository plus PostgreSQL runtime state when database is available. Runtime validation is capability-based; Docker is an optional adapter, not a core requirement. P6.6 native acceptance is complete.

## Current Release Line

- P6 development: `ACCEPTED_NATIVE_CORE`
- P6.5 independent audit: `COMPLETED`
- P6 release: `ACCEPTED`
- P6.6 remediation: `COMPLETED`
- P6.7 context memory foundation: `COMPLETED`
- P6.8 Obsidian control surface: `UNBLOCKED`

P6.6 remediated the P6.5 High findings before P6.7 began. P6.7 builds SQL Context Memory before Loop Engineering because the loop requires durable Goal, Plan, event, decision, observation and evidence memory.

## Governance Model

- Obsidian is a human control and projection surface, not source of truth.
- SQL Context Memory is the structured memory layer for metadata, hashes, state, evidence, summaries, relationships, retrieval priority, freshness, and verification status.
- Harness is the governance kernel: policy enforcement, worker routing, approval control, state machine, context assembly, validation, rollback, and audit event production.
- Worker layers remain distinct:
  - Ollama: local lightweight reasoning, retrieval, summarization, classification.
  - Claude Code plus Ollama: local operational runtime, tool usage, skills, scripts, repository actions.
  - Codex CLI: complex coding, refactoring, deep writing, testing, release engineering.

## Loop Engineering Contract

Goal is the stable contract. Plan is a versioned and revisable hypothesis.

Loop sequence:

1. Understand current state.
2. Generate or revise plan.
3. Select next action.
4. Assemble minimal context.
5. Route worker.
6. Execute.
7. Observe.
8. Verify.
9. Write memory.
10. Continue, replan, escalate, rollback, or complete.

Loop governance must define maximum iterations, replans, worker failures, context tokens, execution time, progress delta requirements, and human gates for production changes, destructive actions, release, deployment, scope expansion, and external side effects.

## Roadmap

- P6.5 Independent Audit: completed, result `REJECTED_PENDING_FIXES`.
- P6.6 Governance and Release Remediation: completed fixes for state machine enforcement, provider decoupling, repository boundary control, release allowlist, secret scanning, clean extract validation, and adversarial suite integration.
- P6.7 Context Memory Foundation: completed SQL project registry, episodic memory, semantic facts, decision memory, artifact registry, run events, retrieval, packet assembly, freshness, and verification metadata.
- P6.8 Obsidian Control Surface: project cards, portfolio command center, Goal/Plan/Progress display, approval controls, SQL-to-Markdown projection, Canvas regeneration.
- P6.9 Worker Router: Ollama adapter, Claude Code runtime adapter, Codex CLI adapter, capability routing, context budget, timeout, retry, isolation, worktree governance, usage logging.
- P6.10 Deterministic Task Loop: plan, execute, verify, repair, stop with limits and stop policy.
- P6.11 Project Goal Loop: persistent Goal, versioned Plan, multiple task loops, acceptance criteria, evidence aggregation, replanning, escalation.
- P6.12 Portfolio Loop: cross-project priority, stale project detection, deadline/dependency/resource conflicts, today plan, executive summary, decision queue.
