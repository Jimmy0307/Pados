"""CR-16 H-03: a numeric cell is exact in the dataset's numeric representation or it is refused - never rounded."""
from __future__ import annotations

from decimal import Decimal

import pytest

from pados.packs.algorithms.contracts import Column, validated
from pados.packs.extraction.table_dataset import ExtractionError, ExtractionSpec, _cell, table_dataset
from pados.packs.ocr.contracts import Table

NUMERIC = validated(Column, {"name": "cost", "type": "NUMERIC", "criterion": "COST", "domain": []}, "COLUMN")


@pytest.mark.parametrize("text", ["999999999999999.999999999", "123456789012345.6789", "100000000000000.000000001",
                                  "-99999999999999.999999999"])
def test_a_decimal_that_does_not_round_trip_exactly_is_refused(text: str) -> None:
    with pytest.raises(ExtractionError, match="^EXTRACTION_VALUE_NOT_EXACT$"):
        _cell(NUMERIC, text)


@pytest.mark.parametrize(("text", "value"), [("0.1", 0.1), ("12.5", 12.5), ("42", 42.0), ("-7", -7.0),
                                             ("+3.25", 3.25), ("0.10", 0.1), ("-0", 0.0), ("0.000000001", 1e-9),
                                             ("999999999999999", 999999999999999.0)])
def test_exact_decimals_and_integers_are_accepted(text: str, value: float) -> None:
    assert _cell(NUMERIC, text) == value


def test_an_inexact_cell_fails_the_whole_table_closed() -> None:
    spec = ExtractionSpec.parse({"object_column": "Id", "column_1": "Cost|cost|NUMERIC|COST"})
    cells = (("Id", "Cost"), ("a1", "12.5"), ("a2", "999999999999999.999999999"))
    table = Table(table_id="t1", rows=3, columns=2, cells=cells, header_row=True, method="test")
    with pytest.raises(ExtractionError, match="^EXTRACTION_VALUE_NOT_EXACT$"):
        table_dataset(table, spec)
    exact = Table(table_id="t1", rows=3, columns=2, cells=(cells[0], cells[1], ("a2", "0.1")), header_row=True,
                  method="test")
    assert [row[0] for row in table_dataset(exact, spec).rows] == [12.5, 0.1]


def test_two_texts_with_distinct_decimal_values_never_map_to_the_same_accepted_double() -> None:
    """CR-16b H-03 numeric contract: a cell is accepted only when its decimal value equals the shortest
    round-trip ``repr`` of the IEEE-754 double it parses to, so accepted cells map injectively (by decimal
    value) to doubles. This is an unambiguous, reproducible mapping, not exact binary representation (0.1 has
    no exact binary form; downstream algorithms compute on the double)."""
    assert _cell(NUMERIC, "0.1") == 0.1
    assert float("0.10000000000000001") == 0.1  # the same double as "0.1" ...
    with pytest.raises(ExtractionError, match="^EXTRACTION_VALUE_NOT_(NUMERIC|EXACT)$"):
        _cell(NUMERIC, "0.10000000000000001")  # ... so refused (here already by the 9-fraction-digit grammar)
    assert _cell(NUMERIC, "100000000000000") == float("100000000000000.000000001")
    with pytest.raises(ExtractionError, match="^EXTRACTION_VALUE_NOT_EXACT$"):
        _cell(NUMERIC, "100000000000000.000000001")  # inside the grammar: refused by the exactness rule
    texts = [f"{whole}.{frac}" for whole in ("0", "1", "12", "4503599627370", "999999999999999")
             for frac in ("1", "10", "2", "3", "5", "05", "000000001", "999999999", "100000000", "333333333")]
    texts += ["0", "-0", "9007199254", "999999999999999", "+3.25", "-3.250"]
    accepted: dict[float, Decimal] = {}
    refused = 0
    for text in texts:
        try:
            value = _cell(NUMERIC, text)
        except ExtractionError as exc:
            assert exc.code == "EXTRACTION_VALUE_NOT_EXACT", text
            refused += 1
            continue
        assert isinstance(value, float) and Decimal(repr(value)) == Decimal(text), text
        assert accepted.setdefault(value, Decimal(text)) == Decimal(text), text  # same double => same decimal
    assert len(accepted) > 20 and refused > 0
