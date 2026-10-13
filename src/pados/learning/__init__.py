# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Outcome Learning V1: which capability/adapter/resource combination works best for a task context.

Non-authoritative by construction. The Learning Plane observes content-free outcome metadata (closed
enums, bounded tokens, counts, metrics, hashes and references) and turns it into explainable, bounded
advisory scores for adapters that policy has ALREADY found feasible and authorized. It is not
autonomous reinforcement learning and it never rewrites PADOS:

* it cannot change policy decisions, ACL, grants, classification, provider authorization, security policy,
  human-approval requirements or semantic acceptance (advice reaches a plan only through
  ``pados.capability.seams.apply_advice``, which drops anything outside the feasible set);
* it is disabled by default and reversible: disabling it yields exactly the no-advice plan;
* it ingests only outcomes authenticated against the executor's trusted receipts, the trusted plan context
  and governed verdicts, aged by their trusted execution time;
* every advice set is logged to a durable, hash-chained log with a derivation snapshot, so a plan's advice
  can be re-derived and audited;
* it persists no user content, prompts or document text, only metadata that passes ``assert_minimized``.

Human decisions are taken only from the verdict recorded in the existing governed approval path, never
inferred.
"""
