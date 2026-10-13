from pados.core.context_compiler import context_hash


def test_context_hash_is_stable() -> None:
    left = context_hash({"b": 2, "a": 1})
    right = context_hash({"a": 1, "b": 2})
    assert left == right
    assert len(left) == 64

