# SPDX-License-Identifier: AGPL-3.0-only
"""``redact`` decides exactly what the original credential patterns decide, only faster.

The patterns below are the detector as it stood before the speed-up, copied verbatim. They are the oracle: for every
input, ``redact`` must redact if and only if one of them matches. The speed-up must never let a credential through,
and must never start redacting text the original detector passed.
"""
from __future__ import annotations

import random
import re
import time

import pytest

from pados.execution.context import REDACTED_MARKER, redact

_ORACLE_WORDS = (
    r"api[_-]?key|apikey|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?key|private[_-]?key"
    r"|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|passwd|pwd"
    r"|authorization"
)
_ORACLE_URL_USERINFO = r"(?<=[A-Za-z0-9])" + ":" + "//" + r"[^\s/:@]{1,256}" + ":" + r"[^\s/@]{1,256}" + "@"
_ORACLE: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?<![A-Za-z0-9])(?:" + _ORACLE_WORDS + r")(?![A-Za-z0-9_])"
        r"[\"']?\s{0,16}[:=]\s{0,16}[\"']?(?!(?:true|false|null|none)(?![A-Za-z0-9]))"
        r"[^\s\"',;{}\[\]()<>]{4}",
        re.IGNORECASE,
    ),
    re.compile(r"bearer\s{1,16}[A-Za-z0-9._~+/=-]{8}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{16}"),
    re.compile(r"(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{8}"),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(r"-----BEGIN[A-Z0-9 ]{0,40}PRIVATE KEY-----"),
    re.compile(_ORACLE_URL_USERINFO),
)


def _oracle_redacts(text: str) -> bool:
    return any(pattern.search(text) for pattern in _ORACLE)


def _redacts(text: str) -> bool:
    result = redact(text)
    assert result in (text, REDACTED_MARKER)
    return result == REDACTED_MARKER and text != REDACTED_MARKER


# Credential-shaped fixtures are split into concatenated parts so the source holds no complete shape.
# Boundary cases: each detector at the start of the text, after a letter, a digit, "_" and "-", after punctuation,
# and with characters that case-insensitive matching folds onto ASCII (Kelvin sign, long s, dotted and dotless i).
_STEMS = ("api_key", "APIKEY", "secret", "client-secret", "access_key", "private_key", "token", "auth_token",
          "Access-Token", "refresh_token", "password", "PassWd", "pwd", "Authorization", "api\u212aey",
          "pa\u017fsword", "authorİzation", "authorızation", "\u212aey", "to\u212aen", "secre\u0442")
_VALUES = ("=abcd", ": \"hunter22\"", "='x'", "=true", "= none", "=nullable", ":  abc", "=\tqwerty1", "= ab",
           "\"=\"wxyz", "=[abcd]", "= {}", ":", "")
_TOKENS = ("sk-abcdefgh", "sk-abc", "ghp_" + "a" * 16, "gho_" + "b" * 15, "github_pat_" + "c" * 16,
           "xoxb-12345678", "xoxB-12345678", "AKIA" + "A" * 16, "ASIA" + "B1" * 8, "AKIA" + "A" * 17,
           "bearer abcdefgh", "Bearer\t\tA.b~c+d/e=", "BEARER  1234567", "-----BEGIN " + "RSA PRIVATE KEY-----",
           "-----BEGIN " + "PRIVATE KEY-----", "-----begin private key-----", "https:" + "//user:pa55" + "@host", "x://u:p@h",
           "://u:p@h", "a://:p@h", "a://u:@h", "s\u212a-abcdefgh")
_PREFIXES = ("", "a", "Z", "9", "_", "-", " ", "\"", "x_", "foo-", ".", "\n", "\u212a", "İ")
_SUFFIXES = ("", "a", "_", "-", " ", "0", "Z", "\"")


def _boundary_cases() -> list[str]:
    cases = [prefix + stem + value + suffix
             for stem in _STEMS for value in _VALUES for prefix in _PREFIXES[:8] for suffix in _SUFFIXES[:3]]
    cases += [prefix + token + suffix for token in _TOKENS for prefix in _PREFIXES for suffix in _SUFFIXES]
    return cases


def test_redact_matches_the_original_detector_on_boundary_cases() -> None:
    cases = _boundary_cases()
    assert any(_oracle_redacts(case) for case in cases) and not all(_oracle_redacts(case) for case in cases)
    mismatches = [case for case in cases if _redacts(case) != _oracle_redacts(case)]
    assert mismatches == []


_FRAGMENTS = ("sk-", "gh", "p_", "o_", "github_pat_", "xox", "b-", "AKIA", "ASIA", "://", "@", ":", "=", "\"", "'",
              " ", "\t", "\n", "token", "Token", "key", "KEY", "api", "secret", "pass", "wd", "pwd", "auth",
              "orization", "bearer ", "Bearer", "-----BEGIN ", "PRIVATE KEY-----", "true", "none", "_", "-", ".",
              "abcd", "WXYZ", "0123", "a", "Z", "9", "{", "}", "(", ")", "[", "]", ";", ",", "<", ">", "/",
              "\u212a", "\u017f", "İ", "ı", "\u0442")


@pytest.mark.parametrize("seed", range(4))
def test_redact_matches_the_original_detector_on_generated_text(seed: int) -> None:
    rng = random.Random(seed)
    mismatches = []
    redacted = 0
    for _ in range(5_000):
        text = "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 24)))
        expected = _oracle_redacts(text)
        redacted += expected
        if _redacts(text) != expected:
            mismatches.append(text)
    assert mismatches == []
    assert 0 < redacted < 5_000  # the corpus exercises both outcomes


def test_redact_on_two_mebibytes_of_whitespace_around_a_token_is_well_inside_its_budget() -> None:
    """The case that took 0.99-1.5 s on the VMware VM against the 1.0 s budget of test_clean_room_performance."""
    filler = " " * (1024 * 1024)
    text = filler + "Bearer " + "q" * 24 + filler
    started = time.perf_counter()
    assert redact(text) == REDACTED_MARKER
    assert time.perf_counter() - started < 0.5


# --- the two facts the speed-up rests on -----------------------------------------------------------------------------


def test_the_stem_classes_fold_exactly_like_ignorecase() -> None:
    """Each stem letter's explicit class matches the same characters as the letter under re.IGNORECASE."""
    import sys

    from pados.execution.context import _CREDENTIAL_WORD_STEMS, _caseless

    for letter in sorted(set(_CREDENTIAL_WORD_STEMS) - {"|"}):
        folded = re.compile(letter, re.IGNORECASE)
        spelled = re.compile(_caseless(letter))
        expected = {ch for ch in map(chr, range(sys.maxunicode + 1)) if folded.fullmatch(ch)}
        actual = {ch for ch in map(chr, range(sys.maxunicode + 1)) if spelled.fullmatch(ch)}
        assert actual == expected, letter


def test_every_credential_word_contains_a_stem_outside_its_optional_parts() -> None:
    """A word added without a stem would be skipped by the prefilter, so the oracle and this check must both change."""
    from pados.execution.context import _CREDENTIAL_WORD_STEMS, _CREDENTIAL_WORDS

    assert _CREDENTIAL_WORDS == _ORACLE_WORDS
    stems = _CREDENTIAL_WORD_STEMS.split("|")
    for word in _CREDENTIAL_WORDS.split("|"):
        required = re.sub(r"\(\?:[^()]*\)\?|\[[^\]]*\]\?", "", word)  # drop optional groups and optional classes
        assert re.fullmatch(r"[a-z]+", required), word
        assert any(stem in required for stem in stems), word
