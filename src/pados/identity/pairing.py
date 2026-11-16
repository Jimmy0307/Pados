# SPDX-License-Identifier: AGPL-3.0-only
"""Pairing codes (design §3.2) as pure transitions of the store state.

A code is 10 Crockford base32 characters (about 50 bits), stored only as SHA-256, valid for 300 s and consumed by its
first successful use. At most 3 codes are outstanding: minting a fourth drops the oldest.

Failed attempts never burn a code. ``/pair`` is unauthenticated and any local process can call it, so burning codes
on failures would let that process destroy the operator's freshly minted code. Guessing is bounded instead by a short
global pause: 30 failures within 60 s pause pairing (``PAIRING_RATE_LIMITED``) until the window clears. That allows
at most about 150 guesses over a code's 300 s life against 2^50 codes. A process that keeps flooding can still delay
pairing (availability only; the control-token path is unaffected), which is a documented residual.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pados.identity.errors import PAIRING_CODE_EXPIRED, PAIRING_CODE_INVALID, PAIRING_RATE_LIMITED
from pados.identity.store import PairingCodeRecord, StoreState
from pados.identity.tokens import digest, new_pairing_code, normalize_code

CODE_TTL = timedelta(seconds=300)
MAX_OUTSTANDING_CODES = 3
GLOBAL_FAILURE_LIMIT = 30
GLOBAL_FAILURE_WINDOW = timedelta(seconds=60)
# An expired code is kept a while so its holder hears PAIRING_CODE_EXPIRED rather than PAIRING_CODE_INVALID.
EXPIRED_CODE_RETENTION = timedelta(hours=1)


@dataclass(frozen=True)
class MintedCode:
    state: StoreState
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class PairAttempt:
    state: StoreState
    consumed: PairingCodeRecord | None
    error: str | None


def prune(state: StoreState, now: datetime) -> StoreState:
    codes = {key: record for key, record in state.pairing_codes.items()
             if record.expires_at + EXPIRED_CODE_RETENTION > now}
    failures = tuple(at for at in state.pair_failures if at + GLOBAL_FAILURE_WINDOW > now)
    return state.model_copy(update={"pairing_codes": codes, "pair_failures": failures})


def _outstanding(state: StoreState, now: datetime) -> list[tuple[str, PairingCodeRecord]]:
    return sorted(((key, record) for key, record in state.pairing_codes.items() if record.expires_at > now),
                  key=lambda item: item[1].created_at)


def mint(state: StoreState, now: datetime, label_hint: str | None) -> MintedCode:
    current = prune(state, now)
    outstanding = _outstanding(current, now)
    dropped = {key for key, _ in outstanding[: max(0, len(outstanding) - (MAX_OUTSTANDING_CODES - 1))]}
    code = new_pairing_code()
    while digest(code) in current.pairing_codes:  # a collision is ~2^-50; never overwrite a live record
        code = new_pairing_code()
    expires_at = now + CODE_TTL
    record = PairingCodeRecord(created_at=now, expires_at=expires_at, failed_attempts=0, label_hint=label_hint)
    codes = {key: value for key, value in current.pairing_codes.items() if key not in dropped}
    return MintedCode(state=current.model_copy(update={"pairing_codes": {**codes, digest(code): record}}),
                      code=code, expires_at=expires_at)


def attempt(state: StoreState, now: datetime, presented: str) -> PairAttempt:
    current = prune(state, now)
    if len(current.pair_failures) >= GLOBAL_FAILURE_LIMIT:
        return PairAttempt(state=current, consumed=None, error=PAIRING_RATE_LIMITED)
    normalized = normalize_code(presented)
    key = digest(normalized) if normalized is not None else None
    record = current.pairing_codes.get(key) if key is not None else None
    if key is not None and record is not None and record.expires_at > now:
        codes = {k: v for k, v in current.pairing_codes.items() if k != key}
        return PairAttempt(state=current.model_copy(update={"pairing_codes": codes}), consumed=record, error=None)
    error = PAIRING_CODE_EXPIRED if record is not None else PAIRING_CODE_INVALID
    return PairAttempt(state=_record_failure(current, now), consumed=None, error=error)


def _record_failure(state: StoreState, now: datetime) -> StoreState:
    # Only the global failure window grows; outstanding codes are left intact (see the module docstring).
    return state.model_copy(update={"pair_failures": (*state.pair_failures, now)})
