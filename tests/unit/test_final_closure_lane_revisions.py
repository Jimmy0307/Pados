"""Operational lane revisions must admit the P6.19B formal main schema exactly."""

from pados.config import Settings, lane_revisions_accepted


def _settings() -> Settings:
    return Settings(
        PADOS_OPERATIONAL_REVISION="20260930_0014",
        PADOS_CORPUS_REVISION="20260730_0011",
        PADOS_EXCLUDED_REVISION="20260730_0012",
    )


def test_operational_lane_accepts_single_operational_head() -> None:
    assert lane_revisions_accepted(_settings(), "operational", ["20260930_0014"])


def test_operational_lane_rejects_proof_formal_schema() -> None:
    # P6.15 separation: proof-chain main heads never belong to the operational lane.
    assert not lane_revisions_accepted(_settings(), "operational", ["20260730_0011", "20260808_0013"])
    assert not lane_revisions_accepted(_settings(), "operational", ["20260808_0013"])  # stale: no memory authority


def test_proof_lane_requires_exact_formal_main_schema() -> None:
    settings = _settings()
    assert lane_revisions_accepted(settings, "proof", ["20260730_0011", "20260808_0013"])
    assert not lane_revisions_accepted(settings, "proof", ["20260808_0013"])
    # The proof lane's formal schema is fixed history; it never follows the operational head.
    assert not lane_revisions_accepted(settings, "proof", ["20260730_0011", "20260930_0014"])
    assert not lane_revisions_accepted(settings, "proof", ["20260730_0011", "20260730_0012", "20260808_0013"])


def test_operational_lane_rejects_partial_foreign_or_excluded_heads() -> None:
    settings = _settings()
    assert not lane_revisions_accepted(settings, "operational", [])
    assert not lane_revisions_accepted(settings, "operational", ["20260730_0011"])
    assert not lane_revisions_accepted(settings, "operational", ["20260711_0009"])
    assert not lane_revisions_accepted(
        settings, "operational", ["20260730_0011", "20260730_0012", "20260808_0013"]
    )
    assert not lane_revisions_accepted(settings, "operational", ["20260730_0012", "20260808_0013"])


def test_corpus_lane_remains_exactly_one_corpus_head() -> None:
    settings = _settings()
    assert lane_revisions_accepted(settings, "corpus", ["20260730_0011"])
    assert not lane_revisions_accepted(settings, "corpus", ["20260730_0011", "20260808_0013"])
    assert not lane_revisions_accepted(settings, "corpus", ["20260808_0013"])
