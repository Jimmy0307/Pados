# PADOS P7 — Capability Discovery, Adaptive Learning & Human-AI Capability Loop

## Status

Approved design baseline for implementation on `feat/p7-capability-learning-loop`.

Immutable release baseline:

- Tag: `PADOS_RC2_LOCAL_DOGFOOD_V1`
- Main baseline commit: `9e376e6e69e48c48b10eed0c0421e0f47a3dd058`

P7 must not mutate, retag, rewrite, or retroactively modify RC2.

## Architectural Authority

PADOS remains the sole authority for authenticated intent, `TaskEnvelope`, authorization, policy, scheduling, capability routing, semantic acceptance, CAS, evidence/receipts, and governed outcome learning.

REA and OpenMAIC are optional external runtimes. They may provide capabilities and evidence, but they never become PADOS authorities.

## Delivery Strategy

Use one development branch with four independently revertible checkpoints:

1. **P7.0 — Freeze Evidence Baseline**
2. **P7.1 — REA Capability Discovery Runtime**
3. **P7.2 — OpenMAIC Adaptive Learning Runtime**
4. **P7.3 — Human-AI Capability Loop**

Each checkpoint must have an obvious commit boundary, pass its own tests before the next starts, preserve rollback, leave PADOS Core semantics unchanged, and fail closed when an external runtime is absent, unhealthy, unauthorized, incompatible, or returns malformed data.

Failure in P7.2 must not invalidate P7.1. Failure in REA or OpenMAIC must not prevent normal PADOS operation.

---

## P7.0 — Freeze Evidence Baseline

Version durable RC2 release evidence without changing the historical RC2 tag.

Target documentation set:

```text
docs/releases/PADOS_RC2_LOCAL_DOGFOOD_V1/
├── FREEZE_MANIFEST.md
├── SECURITY_RESPIN.md
├── NPM_AUDIT_RELEASE_IMPACT_TRIAGE.md
├── KNOWN_ISSUES.md
└── DOGFOOD_BOUNDARY.md
```

Include release commit/tree, tag, CI run identities, artifact SHA-256, dependency security verdict, `anyio 4.14.2`, `pypdf 6.20.1`, OCR-01 Configuration migration, D1/D2/D4 results, 8 known P1 issues, 26 unresolved review threads, local/supervised/non-sensitive dogfood boundary, and no-public-release/no-npm-publish status.

Do not commit machine-local absolute paths, credentials, secret-bearing logs, duplicate large CI artifacts, or ephemeral test environments.

Exit gate:

```text
FREEZE_EVIDENCE_VERSIONED=PASS
RC2_TAG_MUTATED=NO
SECRET_SCAN=PASS
```

---

## P7.1 — REA Capability Discovery Runtime

External source: `morluto/rea`

Role: **Capability Discovery Provider**.

Do not vendor the full REA repository into PADOS. Prefer an external MCP/runtime boundary.

### Provider-neutral contract

Introduce only the smallest necessary PADOS-owned abstractions, conceptually:

```text
CapabilityDiscoveryProvider
DiscoveryRequest
DiscoveryEvidence
CapabilityCandidate
```

REA-specific schemas stay behind the adapter.

### Flow

```text
Task / capability gap
        ↓
PADOS Policy
        ↓
Authorization / target scope
        ↓
CapabilityDiscoveryProvider
        ↓
REA MCP
        ↓
Evidence + limitations
        ↓
PADOS normalization
        ↓
Receipt
        ↓
CapabilityCandidate
```

### Capability state

Discovery must never directly create a production capability. Initial states are limited to:

```text
DISCOVERED
EXPERIMENTAL
VALIDATION_REQUIRED
BLOCKED
```

Promotion remains governed by PADOS.

### Security gate

Before invoking REA, evaluate target ownership/authorization, requested scope, project scope, classification, credential/secret exposure, and policy restrictions.

No inspection request may bypass PADOS authorization.

### Isolation

REA absence, timeout, malformed response, cancellation, or runtime failure must produce normalized failure evidence without corrupting PADOS state.

```text
REA unavailable
→ Capability Discovery = DEGRADED
→ PADOS Core = AVAILABLE
```

Exit gates:

```text
REA_ADAPTER=PASS
REA_MCP_DISCOVERY=PASS
REA_POLICY_GATE=PASS
REA_EVIDENCE_NORMALIZATION=PASS
REA_RECEIPT_LINEAGE=PASS
REA_FAILURE_ISOLATION=PASS
CORE_SEMANTICS_CHANGED=NO
```

---

## P7.2 — OpenMAIC Adaptive Learning Runtime

External source: `THU-MAIC/OpenMAIC`

Role: **Adaptive Learning Provider**.

OpenMAIC remains an external runtime. Do not merge its source tree into PADOS Core.

### Minimal initial capability

P7.2 requires only enough functionality to establish the learning contract:

- create a learning session;
- supply a learning goal;
- attach or reference learning materials;
- retrieve curriculum/session state;
- retrieve assessment/result;
- normalize learner progress;
- emit capability-learning evidence.

TTS, video, image generation, and full multimedia/classroom parity are non-goals for P7.2.

### Provider-neutral contract

Conceptually:

```text
LearningProvider
LearningRequest
LearningSession
LearningAssessment
CapabilityGapObservation
```

OpenMAIC is the first implementation, not the definition of the interface.

### Model routing

OpenMAIC must not become final model-routing authority.

Preferred flow:

```text
OpenMAIC learning operation
        ↓
PADOS model/capability routing boundary
        ↓
Local / permitted cloud / tool resource
```

If direct provider configuration cannot yet be routed through PADOS, mark that boundary explicitly as external/provider-controlled. Do not fake full routing integration.

### State ownership

OpenMAIC session state remains external runtime state. PADOS stores only normalized capability gap, learning progress, evidence/receipt linkage, and assessment outcome.

OpenMAIC must not write PADOS semantic state directly.

Exit gates:

```text
OPENMAIC_ADAPTER=PASS
LEARNING_SESSION=PASS
LEARNING_MATERIAL_BINDING=PASS
ASSESSMENT_NORMALIZATION=PASS
CAPABILITY_GAP_OBSERVATION=PASS
FAILURE_ISOLATION=PASS
MODEL_ROUTING_AUTHORITY=PADOS_OR_EXPLICIT_EXTERNAL_BOUNDARY
CORE_SEMANTICS_CHANGED=NO
```

---

## P7.3 — Human-AI Capability Loop

P7.3 connects existing PADOS planning/execution with P7.1 and P7.2. It does not create a second scheduler, replace `TaskEnvelope`, or create a second semantic acceptance path.

### Decision paths

```text
User Goal
   ↓
Capability Assessment
   ↓
┌────────────────┬───────────────────┬────────────────┐
│ Human Gap      │ System Gap        │ Ready          │
│                │                   │                │
│ OpenMAIC       │ REA               │ Execute        │
│ Learn          │ Discover          │ Existing Pack  │
└────────────────┴───────────────────┴────────────────┘
```

P7 requires deterministic classification and evidence for each path.

### Closed loop

```text
Goal
 ↓
Capability Gap
 ↓
Learn / Discover / Execute
 ↓
Outcome
 ↓
Evidence
 ↓
Receipt
 ↓
Evaluation
 ↓
Capability Candidate / Progress Update
```

Outcome Learning remains advisory and cannot grant authorization, dispatch work, override policy, auto-promote an unvalidated capability, or commit semantic state outside existing acceptance paths.

### Capability lifecycle

Minimum lifecycle:

```text
UNKNOWN
↓
DISCOVERED
↓
EXPERIMENTAL
↓
VALIDATION_REQUIRED
↓
VALIDATED
```

Human learning state and system executable capability state remain separate. Do not collapse `human_mastery`, `system_availability`, `validation_status`, and `evidence_refs` into one score.

---

## Thin Core Rule

P7 may add thin interfaces required to connect external runtimes.

Acceptable examples:

```text
CapabilityDiscoveryProvider
LearningProvider
CapabilityCandidate
LearningAssessment
```

Implementation must stop and report an architecture escalation if it requires redesign of:

- `TaskEnvelope` authority;
- scheduler ownership;
- CAS semantics;
- authorization model;
- semantic acceptance;
- authoritative database model;
- receipt ownership;
- core execution lifecycle.

No silent Core redesign is allowed.

---

## Dependency Strategy

REA and OpenMAIC are optional runtimes/adapters.

```text
Runtime installed + healthy
→ capability AVAILABLE

Runtime absent
→ capability UNAVAILABLE / DEGRADED
→ PADOS remains operational
```

Normal PADOS startup must not require either runtime. Heavy third-party engines must not be auto-installed without explicit invocation. Licensing and dependency manifests must be recorded.

---

## Observability and Evidence

Every external operation must provide enough evidence to answer:

- what PADOS asked;
- what provider handled it;
- what capability was invoked;
- what resource/runtime was used;
- what evidence was returned;
- what limitations were declared;
- what failed;
- whether the result was accepted;
- whether capability state changed.

External runtime logs are not a substitute for PADOS receipts.

---

## Testing Strategy

### Contract tests

Provider-neutral contracts must work against fake adapters.

### Adapter tests

REA and OpenMAIC adapters must normalize upstream results correctly.

### Failure tests

Explicitly cover runtime absent, connection refused, timeout, malformed response, unauthorized request, unsupported capability, cancellation, and partial result.

### Real integration tests

When a runtime is locally available:

- one REA discovery vertical slice;
- one OpenMAIC learning vertical slice.

Mocks/fakes cannot be reported as real-runtime PASS evidence.

### P7.3 scenario tests

**Ready capability**

```text
Goal → existing validated capability → execute → receipt
```

**Human gap**

```text
Goal → learning required → OpenMAIC → assessment → progress evidence
```

**System gap**

```text
Goal → capability missing → REA discovery → capability candidate → validation required
```

Discovery success must never auto-promote a capability to `VALIDATED`.

---

## Checkpoint Commits

Expected logical checkpoints:

```text
P7.0  docs(p7): version RC2 freeze evidence
P7.1  feat(p7): add capability discovery provider and REA adapter
P7.2  feat(p7): add adaptive learning provider and OpenMAIC adapter
P7.3  feat(p7): connect learning discovery and execution capability loop
```

Additional fix commits are allowed when needed, but checkpoint identity must remain obvious. Do not squash away the logical checkpoint history during development.

---

## Definition of Done

```text
P7_0_FREEZE_EVIDENCE=PASS

P7_1_REA_ADAPTER=PASS
REA_POLICY_GATE=PASS
REA_EVIDENCE_NORMALIZATION=PASS
REA_FAILURE_ISOLATION=PASS

P7_2_OPENMAIC_ADAPTER=PASS
LEARNING_SESSION=PASS
ASSESSMENT_NORMALIZATION=PASS
OPENMAIC_FAILURE_ISOLATION=PASS

P7_3_CAPABILITY_LOOP=PASS
READY_PATH=PASS
LEARN_PATH=PASS
DISCOVER_PATH=PASS
RECEIPT_LINEAGE=PASS

CORE_SEMANTICS_CHANGED=NO
RC2_TAG_MUTATED=NO

RUFF=PASS
MYPY=PASS
TESTS=PASS
PR_CI=PASS
SECURITY_GATE=PASS
```

If a real external runtime integration cannot be completed, provider-neutral contracts and fake-adapter tests may pass, but the corresponding real-runtime gate must remain `NOT_VALIDATED`.

---

## Non-goals

P7 does not include:

- public production release;
- automatic commercial packaging;
- full multimedia OpenMAIC parity;
- bundling Hopper/Ghidra/IDA/JADX;
- automatic reverse engineering of arbitrary targets;
- autonomous capability promotion;
- a new scheduler;
- a new CAS;
- replacement of PADOS policy;
- rewriting frozen Core semantics;
- unrelated UI redesign.

## Final Principle

PADOS remains the control plane.

REA expands what the system can discover.

OpenMAIC expands what the human can learn.

Existing capability packs expand what the system can execute.

P7 connects these paths without transferring authority away from PADOS.
