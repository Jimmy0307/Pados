from pados.db import check_database


def test_database_has_pgvector() -> None:
    result = check_database()
    assert result["status"] == "ok"
    assert result["pgvector"] == "true"

