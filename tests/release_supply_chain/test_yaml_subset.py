import pytest

from pados.release.yaml_subset import YamlSubsetError, load


def test_parses_workflow_shapes() -> None:
    text = (
        "name: ci\n"
        "on:\n  push:\n    branches: [\"**\"]\n  pull_request:\n"
        "permissions: {}\n"
        "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@0123 # v1.2.3\n        with:\n          persist-credentials: false\n"
        "      - run: |\n          echo one # kept\n          echo two\n"
        "      - name: x\n        run: echo 'a # b' && echo c # comment\n"
        "compact:\n- 1\n- k: v\n  j: [a, 'b c', {x: \"y\\tz\"}]\n"
    )
    doc = load(text)
    assert doc["on"] == {"push": {"branches": ["**"]}, "pull_request": None}
    assert doc["permissions"] == {}
    steps = doc["jobs"]["a"]["steps"]
    assert steps[0] == {"uses": "actions/checkout@0123", "with": {"persist-credentials": False}}
    assert steps[1]["run"] == "echo one # kept\necho two\n"
    assert steps[2]["run"] == "echo 'a # b' && echo c"
    assert doc["compact"] == [1, {"k": "v", "j": ["a", "b c", {"x": "y\tz"}]}]


def test_block_scalar_chomping_and_folding() -> None:
    assert load("a: |-\n  x\n  y\n") == {"a": "x\ny"}
    assert load("a: >\n  x\n  y\n\n  z\n") == {"a": "x y\nz\n"}


@pytest.mark.parametrize("text,code", [
    ("a: &x 1\n", "UNSUPPORTED_SCALAR"),
    ("a: *x\n", "UNSUPPORTED_SCALAR"),
    ("a: !!str 1\n", "UNSUPPORTED_SCALAR"),
    ("&a k: v\n", "UNSUPPORTED_KEY"),
    ("---\na: 1\n", "DOCUMENT_MARKER_UNSUPPORTED"),
    ("a: 1\na: 2\n", "DUPLICATE_KEY"),
    ("a: {b: 1, b: 2}\n", "DUPLICATE_KEY"),
    ("a:\n\tb: 1\n", "TAB_INDENTATION"),
    ("a: [1,\n  2]\n", "UNTERMINATED_FLOW"),
    ("a: b\n  c\n", "MULTILINE_SCALAR_UNSUPPORTED"),
    ("? a\n: b\n", "UNSUPPORTED_SCALAR"),
    ("a: |+\n  x\n", "UNSUPPORTED_BLOCK_SCALAR"),
    ("a: 'open\n", "UNTERMINATED_QUOTE"),
    ("a: \"bad \\q\"\n", "UNSUPPORTED_ESCAPE"),
    ("a:\n  b: 1\n   c: 2\n", "MULTILINE_SCALAR_UNSUPPORTED"),
    ("a:\n  b: 1\n e: 3\n", "UNEXPECTED_INDENT"),
])
def test_rejects_unsupported_yaml(text: str, code: str) -> None:
    with pytest.raises(YamlSubsetError) as error:
        load(text)
    assert error.value.code == code


def test_keys_stay_strings() -> None:
    assert load("on: push\ntrue_key: true\nn: 3\nz: null\n") == {"on": "push", "true_key": True, "n": 3, "z": None}
