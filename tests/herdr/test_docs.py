"""Herdr docs stay in sync with the code (roles, state table, schema, live UAT pending list)."""
from __future__ import annotations

from pathlib import Path

from pados.herdr import HERDR_LIVE_UAT
from pados.herdr.adapter import TERMINAL_CONTENT_TOKENS
from pados.herdr.boundary import STATE_MEANING
from pados.herdr.contract import RECORD_KEYS, SCHEMA_ID

DOCS = Path(__file__).resolve().parents[2] / "docs" / "herdr"


def test_integration_doc_covers_roles_states_privacy_retention_and_live_uat() -> None:
    text = (DOCS / "HERDR_INTEGRATION_V1.md").read_text(encoding="utf-8")
    assert HERDR_LIVE_UAT == "PENDING" and "HERDR_LIVE_UAT = PENDING" in text
    for principle in ("HERDR WORKING != PADOS ACCEPTED", "HERDR IDLE != PADOS SUCCESS",
                      "HERDR BLOCKED != PADOS FAILED", "HERDR SESSION != PADOS MEMORY",
                      "HERDR TERMINAL HISTORY != PROJECT CONTEXT"):
        assert principle in text
    for state, meaning in STATE_MEANING.items():
        assert f"| {state.value} | {meaning.value} |" in text
    for section in ("## 4. Privacy", "## 5. Retention", "## 8. Live UAT pending list"):
        assert section in text
    for check in ("Real status JSON shape", "State names", "Pane and session ids", "Scrollback absence in the store",
                  "Latency", "Failure modes"):
        assert check in text
    for token in TERMINAL_CONTENT_TOKENS:
        assert f"`{token}`" in text


def test_adapter_contract_lists_the_exact_schema() -> None:
    text = (DOCS / "ADAPTER_CONTRACT.md").read_text(encoding="utf-8")
    assert SCHEMA_ID in text
    for key in RECORD_KEYS:
        assert f"| `{key}` |" in text, key


def test_cr06_docs_record_the_fixer_round_decisions() -> None:
    integration = (DOCS / "HERDR_INTEGRATION_V1.md").read_text(encoding="utf-8")
    contract = (DOCS / "ADAPTER_CONTRACT.md").read_text(encoding="utf-8")
    for text in (integration, contract):
        assert "HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS" in text
        assert "UNCHANGED" in text and "source_observed_at" in text and ".refkey" in text
        assert "HERDR_SOURCE_ERROR" in text
    assert "snapshot_at" in contract and "bindings" in integration
