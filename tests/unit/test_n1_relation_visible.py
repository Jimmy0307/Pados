# SPDX-License-Identifier: AGPL-3.0-only
"""N-1: optional-relation probes resolve on the search_path, exactly as the unqualified queries after them do."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from pados.db.relations import OPTIONAL_RELATIONS, relation_visible

SRC = Path(__file__).resolve().parents[2] / "src" / "pados"


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _Recorder:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: object, parameters: Any = None) -> _Result:
        self.calls.append((str(statement), parameters))
        return _Result(self.value)


@pytest.mark.parametrize("name", sorted(OPTIONAL_RELATIONS))
def test_the_name_is_bound_unqualified_and_never_pinned_to_public(name: str) -> None:
    recorder = _Recorder("x")
    assert relation_visible(recorder, name) is True  # type: ignore[arg-type]
    (sql, params), = recorder.calls
    assert "public" not in sql and name not in sql  # the name travels only as a bound parameter
    assert params == {"name": name}


def test_an_absent_relation_is_not_visible() -> None:
    assert relation_visible(_Recorder(None), "result_acceptance_candidates") is False  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["public.result_acceptance_candidates", '"result_acceptance_candidates"',
                                  "pg_catalog.pg_class", "control_approvals", ""])
def test_unregistered_or_qualified_names_are_refused_before_any_sql(name: str) -> None:
    recorder = _Recorder("x")
    with pytest.raises(ValueError, match="^UNREGISTERED_RELATION$"):
        relation_visible(recorder, name)  # type: ignore[arg-type]
    assert recorder.calls == []


def test_no_runtime_probe_is_pinned_to_the_public_schema() -> None:
    """A ``to_regclass('public.…')`` probe followed by an unqualified query is the N-1 defect; none may come back."""
    pinned = re.compile(r"to_regclass\(\s*'public\.", re.IGNORECASE)
    offenders = [f"{path.relative_to(SRC)}:{number}"
                 for path in SRC.rglob("*.py") if path.name != "relations.py"
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if pinned.search(line)]
    assert offenders == []
