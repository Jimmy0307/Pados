# SPDX-License-Identifier: AGPL-3.0-only
"""Composition layer for the capability loop: Core-backed implementations injected into the boundaried
``pados.capability_loop`` package. Nothing here is imported BY capability_loop (dependency inversion); these
modules may import the anchor / persistence primitives that capability_loop is forbidden to touch.
"""
