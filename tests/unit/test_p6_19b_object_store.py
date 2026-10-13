from pathlib import Path

import pytest

from pados.state_proof.object_store import ObjectRef, ObjectStore


def test_identical_bytes_share_one_physical_payload(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "approved-objects")
    first = store.put_bytes(b"same", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    second = store.put_bytes(b"same", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    different = store.put_bytes(b"different", media_type="text/plain", object_kind="RESULT_PAYLOAD")

    assert first.content_hash == second.content_hash
    assert first.storage_uri == second.storage_uri
    assert first.content_hash != different.content_hash
    assert len([path for path in store.root.rglob("*") if path.is_file()]) == 2


def test_payload_mutation_is_detected_and_paths_are_root_contained(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "approved-objects")
    ref = store.put_bytes(b"immutable", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    Path(ref.storage_uri).write_bytes(b"mutated")

    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        store.read(ref)
    outside = ObjectRef(
        object_id=ref.object_id,
        content_hash=ref.content_hash,
        size_bytes=ref.size_bytes,
        media_type=ref.media_type,
        storage_uri=str(tmp_path / "outside"),
        object_kind=ref.object_kind,
    )
    with pytest.raises(ValueError, match="OUTSIDE_APPROVED_ROOT"):
        store.read(outside)
