# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Remote Operations V1.

A remote client (phone, browser, chat bridge) is an interface. It sends a
short, bounded intent; PADOS reconstructs authoritative context server-side
from its own stores. Remote clients never carry project context, never hold
semantic authority and never bypass receipt -> validation -> CAS acceptance.
"""
