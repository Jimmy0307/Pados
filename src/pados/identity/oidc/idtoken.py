# SPDX-License-Identifier: AGPL-3.0-only
"""ID token validation (OpenID Connect Core §3.1.3.7), RS256 only.

Order: parse (alg pinned, ``kid`` required) -> key from the provider JWKS -> signature -> claims. Claims: ``iss``
exact (per preset, see ``ProviderConfig.token_issuers``), ``aud`` contains the client id, ``azp`` required and equal
to the client id when there are several audiences (and equal whenever present), ``exp``/``nbf``/``iat`` with at most
``MAX_SKEW`` of clock skew, ``nonce`` exact, ``sub`` present. The identity key is ``(issuer, subject)``; ``email`` is
returned for display only and is never compared.
"""
from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pados.identity.errors import OIDC_TOKEN_INVALID
from pados.identity.oidc.config import ProviderConfig
from pados.identity.oidc.errors import OidcFailure
from pados.identity.oidc.jws import RsaPublicKey, parse_compact, verify_rs256

MAX_SKEW = timedelta(seconds=120)
MAX_SUBJECT_LENGTH = 255


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str  # the identity-key issuer (see ``ProviderConfig.identity_issuer``)
    subject: str
    email: str | None  # display only


def _invalid(reason: str) -> OidcFailure:
    return OidcFailure(OIDC_TOKEN_INVALID, reason)


def _numeric_date(claims: dict[str, Any], name: str) -> datetime:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 2**40:
        raise _invalid(name)
    return datetime.fromtimestamp(value, tz=UTC)


def _check_audience(claims: dict[str, Any], client_id: str) -> None:
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or not audiences or not all(isinstance(a, str) for a in audiences):
        raise _invalid("audience")
    if client_id not in audiences:
        raise _invalid("audience")
    authorized_party = claims.get("azp")
    if len(audiences) > 1 and authorized_party is None:
        raise _invalid("authorized_party")
    if authorized_party is not None and authorized_party != client_id:
        raise _invalid("authorized_party")


def _check_times(claims: dict[str, Any], now: datetime, skew: timedelta) -> None:
    if now >= _numeric_date(claims, "exp") + skew:
        raise _invalid("expired")
    if _numeric_date(claims, "iat") > now + skew:
        raise _invalid("issued_in_future")
    if "nbf" in claims and now < _numeric_date(claims, "nbf") - skew:
        raise _invalid("not_yet_valid")


def validate_id_token(compact: str, *, provider: ProviderConfig, key_for: Callable[[str], RsaPublicKey],
                      expected_nonce: str, now: datetime, skew: timedelta = MAX_SKEW) -> VerifiedIdentity:
    if skew > MAX_SKEW or skew < timedelta(0):
        raise ValueError("clock skew must be within 0..120 seconds")
    parsed = parse_compact(compact)
    key = key_for(parsed.kid)
    if not verify_rs256(key, parsed.signing_input, parsed.signature):
        raise _invalid("signature")
    claims = parsed.claims
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or issuer not in provider.token_issuers(claims.get("tid")):
        raise _invalid("issuer")
    _check_audience(claims, provider.client_id)
    _check_times(claims, now, skew)
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode("utf-8"), expected_nonce.encode("utf-8")):
        raise _invalid("nonce")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= MAX_SUBJECT_LENGTH or not subject.isprintable():
        raise _invalid("subject")
    email = claims.get("email")
    return VerifiedIdentity(issuer=provider.identity_issuer(issuer), subject=subject,
                            email=email if isinstance(email, str) and len(email) <= 254 else None)
