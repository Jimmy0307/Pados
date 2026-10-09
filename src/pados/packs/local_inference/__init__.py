# SPDX-License-Identifier: AGPL-3.0-only
"""Local-inference capability pack: the Capability Fabric adapter for measured local profiles (additive; never Core).

It implements a MODEL_RUNTIME adapter that executes on the profile the Worker Router selected, through the
existing ``LocalInferenceProvider`` and its measured execution gate. It holds no authority: it never selects,
authorizes, registers or accepts anything.
"""
