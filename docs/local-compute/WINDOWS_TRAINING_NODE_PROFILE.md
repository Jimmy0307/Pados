# Windows training node profile (CONTRACT_READY)

A future capability profile for a Windows NVIDIA machine. The current development machine reports an RTX 5070 Laptop GPU with 8 GB via `nvidia-smi`. **Training is a capability profile only; the Training Plane is not implemented.**

## Candidate capability profile

| Capability (future) | Scope | Output |
|---|---|---|
| CUDA experimentation | Bounded jobs on the node | Artifacts plus metrics |
| LoRA / QLoRA fine-tuning | Datasets referenced by lineage (`DATASET_REFERENCE`) | Adapter weights as `TRAINED_ARTIFACT` lineage nodes |
| Embedding training | Same | Model artifact plus evaluation artifact |
| Reranker training | Same | Model artifact plus evaluation artifact |
| Vision experiments | Same | Artifacts |
| Evaluation | Batch Work Contracts | `EVALUATION_ARTIFACT` |

## Contract rules

- **Governed execution.** Training jobs would use the same node protocol: `POST /execute` with a future capability such as `TRAINING`, a bounded job, and an immutable artifact with a SHA-256 checked against the ExecutionReceipt. None exists yet, and `IMPLEMENTED_CAPABILITIES` rejects unknown capabilities at node start.
- **Artifacts are evidence, not acceptance.** A trained artifact is an artifact, not a deployment. Promotion would require validation, an evaluation gate and a human decision.
- **Lineage and deletion.** The Enterprise lineage graph already models `DATASET_REFERENCE → TRAINED_ARTIFACT`. Revocation flags trained artifacts for human review and never deletes weights automatically.
- **Classification.** RESTRICTED datasets require an explicitly RESTRICTED registration and local-only execution.
- **Scope.** No scheduler, queue broker or cluster manager; the node manages only its own capacity.

**To move beyond CONTRACT_READY:** define the `TRAINING` capability, the dataset handoff (content-addressed, SecretRef-protected), GPU memory reporting in `NodeLoad`, and an evaluation gate. Then validate with a small LoRA job and record it through the existing receipts.

## Training is not inference authority (Productization V1)

```text
Windows RTX node → training/evaluation capability (future) → artifact / model candidate (hashed)
→ evaluation artifact → human or governed promotion decision → only then a model manifest may be registered
```

Rules:

- **No automatic authorization.** Training completion does **not** register a model as production-authorized or add it to any node's advertised models. A candidate becomes routable only when three things happen:
  1. an operator adds it to a node configuration;
  2. the node advertises it after discovery;
  3. the PADOS registration's `allowed_models` includes it.
- **Separate from inference.** A promotion decision is a governed PADOS decision (Decision Inbox and approval), not a node action.
- **Scope.** The Training Plane is not built in this phase.
