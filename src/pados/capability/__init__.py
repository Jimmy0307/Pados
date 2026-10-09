# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Capability Fabric: the smallest useful capabilities, not a monolithic LLM agent.

LLM != AI SYSTEM: a language model is one reasoning capability among OCR engines, parsers,
deterministic algorithms, retrieval, code tools, compute resources and human approval paths.

Authority is unchanged and lives outside this package. A capability is descriptive metadata and an
adapter is replaceable code; neither can commit semantic state, move a ScopeVersion, write audit or
Memory authority, or authorize itself:

    installed != registered != authorized != selected != accepted

* installed  - an adapter implementation exists in the process;
* registered - its descriptor passed validation into the registry;
* authorized - a project-bound PADOS grant names ``CAPABILITY:<capability_id>``;
* selected   - the planner chose it for one stage under policy;
* accepted   - only the existing receipt -> validation -> CAS path accepts anything.

Model inference stages delegate to the existing PADOS Worker Router; this package never ranks
language-model providers itself.
"""
