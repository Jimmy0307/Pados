# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Local Intelligence Stack: measured evidence about this machine, its runtimes and its models.

This package MEASURES. It does not decide. Every output is descriptive evidence with provenance:

    discovered != audited != downloaded != benchmarked != registered != authorized != routable

* ``device`` profiles the host (OS, CPU, memory, disk, GPU, CUDA facts, network mode) and projects the result
  into the existing ``pados.resources.hardware.HardwareProfile`` so the existing ResourceInventory and planners
  consume it unchanged. ``pados.resources`` itself still never probes.
* ``runtimes`` describes local inference runtimes through the Compute Node backend kinds
  (``pados.compute_node.backends.BACKEND_KINDS``); Ollama is one runtime kind among several, never an authority.
* ``probes`` runs fixed, structured commands (argv lists, no shell, bounded output, timeouts) and records the
  outcome and a SHA-256 of every raw output it relied on.

Nothing here routes, schedules, registers, authorizes, downloads a model or writes semantic state. Routing stays
with ``pados.worker.router.select_worker`` (via the Capability Fabric bridge and the Compute Node provider).
"""
