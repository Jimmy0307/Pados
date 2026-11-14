# SPDX-License-Identifier: AGPL-3.0-only
"""Existence probes for optional relations, resolved exactly as the unqualified query that follows them.

PADOS creates its tables unqualified, so they land in the first schema of the connection's ``search_path``, and it
reads them unqualified. A probe pinned to ``public`` (``to_regclass('public.x')``) disagrees with that whenever the
search path does not start with ``public``: the probe misses a table the query would read (a guard silently does not
fire), or it finds a table the query cannot see (the query then fails). ``to_regclass`` on the bare name resolves
through the same ``search_path`` as the query, so probe and query always agree.
"""
from __future__ import annotations

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

# Only registered names are probed: the name reaches SQL as a bound parameter, and the allow-list keeps a caller from
# smuggling a schema-qualified or quoted name that would resolve differently from the unqualified query.
OPTIONAL_RELATIONS = frozenset({"result_acceptance_candidates", "alembic_version", "p6_19b_alembic_version"})


def relation_visible(executor: Connection | Session, name: str) -> bool:
    """Whether ``name`` resolves on the current ``search_path`` (the same resolution an unqualified query uses)."""
    if name not in OPTIONAL_RELATIONS:
        raise ValueError("UNREGISTERED_RELATION")
    return executor.execute(text("SELECT to_regclass(CAST(:name AS text))"), {"name": name}).scalar() is not None
