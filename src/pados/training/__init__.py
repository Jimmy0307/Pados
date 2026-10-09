# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Training Plane (foundation V1): governed, reproducible training and evaluation candidates.

    TRAINED != DEPLOYED; BETTER BENCHMARK != AUTHORIZED; TRAINING COMPLETE != PRODUCTION ACCEPTANCE.

A training job turns an immutable ``DatasetSnapshot`` and a hashed ``TrainingContract`` into a
content-addressed model artifact. Nothing here is authority: a job cannot register, authorize, deploy or
promote what it trained. A ``ModelRegistryCandidate`` becomes PROMOTED only through ``PromotionGate`` with a
signed, single-use human decision bound to the exact registry entry, and even then it yields only an
adapter descriptor DRAFT that the operator must register and a project grant must authorize:

    installed != registered != authorized != selected != accepted

The package never writes CAS, ScopeVersion, semantic state, Memory or audit authority, and never ranks
language-model providers (``pados.worker.router.select_worker`` stays the only router).
"""
