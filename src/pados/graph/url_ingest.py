# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import multiprocessing as mp
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx

from pados.corpus.pdf import parse_pdf

MAX_RESPONSE_BYTES = 5_000_000
MAX_REDIRECTS = 3
TOTAL_TIMEOUT_SECONDS = 15.0
DNS_TIMEOUT_SECONDS = 2.0
PDF_PARSE_TIMEOUT_SECONDS = 10.0
MAX_EXTRACTED_TEXT_CHARS = 5_000_000
ALLOWED_URL_PORTS = {80, 443}
SUPPORTED_CONTENT_TYPES = {"text/html", "text/plain", "application/pdf"}
_CREDENTIAL_KEY = re.compile(
    r"(^|[_-])(access[_-]?key|api[_-]?key|apikey|auth|code|credential|key|password|"
    r"secret|sig|signature|token)($|[_-])",
    re.IGNORECASE,
)
_CREDENTIAL_FRAGMENTS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authtoken",
    "clientsecret",
    "credential",
    "bearer",
    "jwt",
    "password",
    "secret",
    "signature",
    "session",
    "sso",
    "ticket",
    "token",
)
Resolver = Callable[..., list[tuple[Any, ...]]]
_PDF_WORKER_LIMIT = threading.BoundedSemaphore(1)


class UrlIngestError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]
    request_target: str


@dataclass(frozen=True)
class UrlIngestResult:
    original_url: str
    final_url: str
    title: str | None
    content_type: str
    source_host: str
    fetched_at: str
    retrieval_status: str
    extracted_text: str
    content_hash: str | None
    extracted_text_hash: str | None
    author: str | None = None
    publication_date: str | None = None
    canonical_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _FetchedResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    encoding: str


def classify_input(value: str) -> str:
    parsed = urlsplit(value.strip())
    return "URL" if parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc) else "IDEA"


def _blocked_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UrlIngestError("FETCH_TIMEOUT", "URL retrieval exceeded total deadline", status="FAILED")
    return remaining


def _resolve(
    resolver: Resolver,
    host: str,
    port: int,
    deadline: float,
) -> list[tuple[Any, ...]]:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pados-url-dns")
    future = executor.submit(resolver, host, port, type=socket.SOCK_STREAM)
    try:
        return future.result(timeout=min(DNS_TIMEOUT_SECONDS, _remaining(deadline)))
    except FutureTimeout as exc:
        future.cancel()
        raise UrlIngestError("DNS_TIMEOUT", "URL host resolution timed out", status="FAILED") from exc
    except OSError as exc:
        raise UrlIngestError("DNS_RESOLUTION_FAILED", "URL host could not be resolved", status="FAILED") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _sanitized_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", "", ""))


def validate_url(
    value: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
    deadline: float | None = None,
) -> ValidatedTarget:
    candidate = value.strip()
    active_deadline = deadline or (time.monotonic() + TOTAL_TIMEOUT_SECONDS)
    if not candidate or candidate.startswith(("\\", "//")) or "\\" in candidate:
        raise UrlIngestError("MALFORMED_URL", "URL is malformed", status="BLOCKED")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UrlIngestError("MALFORMED_URL", "URL is malformed", status="BLOCKED") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlIngestError("UNSUPPORTED_SCHEME", "Only HTTP and HTTPS URLs are supported", status="BLOCKED")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UrlIngestError("MALFORMED_URL", "URL host is missing or contains credentials", status="BLOCKED")
    for raw_key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_key)
        canonical_key = re.sub(r"[^a-z0-9]+", "", raw_key.casefold())
        if (
            _CREDENTIAL_KEY.search(normalized_key)
            or canonical_key in {"auth", "code", "key", "sig"}
            or any(fragment in canonical_key for fragment in _CREDENTIAL_FRAGMENTS)
        ):
            raise UrlIngestError(
                "URL_CREDENTIALS_BLOCKED",
                "Credential-like URL parameters are not accepted",
                status="BLOCKED",
            )
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise UrlIngestError("UNSAFE_URL_TARGET", "Local URL targets are blocked", status="BLOCKED")
    target_port = port or (443 if scheme == "https" else 80)
    if target_port not in ALLOWED_URL_PORTS:
        raise UrlIngestError("UNSAFE_URL_PORT", "Only standard HTTP and HTTPS ports are supported", status="BLOCKED")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(host)))
    except ValueError:
        resolved = _resolve(resolver, host, target_port, active_deadline)
        addresses.update(str(item[4][0]) for item in resolved if len(item) > 4 and item[4])
    if not addresses:
        raise UrlIngestError("DNS_RESOLUTION_FAILED", "URL host resolved to no addresses", status="FAILED")
    if any(_blocked_address(address) for address in addresses):
        raise UrlIngestError("UNSAFE_URL_TARGET", "Private or local URL targets are blocked", status="BLOCKED")
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return ValidatedTarget(
        url=_sanitized_url(candidate),
        host=host,
        port=target_port,
        addresses=tuple(sorted(addresses)),
        request_target=request_target,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: ValidatedTarget, timeout: float) -> None:
        self._tls_context = ssl.create_default_context()
        super().__init__(
            target.host,
            target.port,
            timeout=timeout,
            context=self._tls_context,
        )
        self._pinned_address = target.addresses[0]

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
        )
        self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)


def _host_header(target: ValidatedTarget) -> str:
    host = f"[{target.host}]" if ":" in target.host else target.host
    default_port = 443 if urlsplit(target.url).scheme == "https" else 80
    return host if target.port == default_port else f"{host}:{target.port}"


def _declared_size(headers: dict[str, str], max_bytes: int) -> None:
    declared = headers.get("content-length")
    if not declared:
        return
    try:
        size = int(declared)
    except ValueError as exc:
        raise UrlIngestError("INVALID_RESPONSE", "Response size header is invalid", status="FAILED") from exc
    if size < 0 or size > max_bytes:
        raise UrlIngestError("RESPONSE_TOO_LARGE", "Response exceeds size ceiling", status="FAILED")


def _read_http_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    *,
    max_bytes: int,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = _remaining(deadline)
        if connection.sock is not None:
            connection.sock.settimeout(min(1.0, remaining))
        chunk = response.read(min(65_536, max_bytes - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise UrlIngestError("RESPONSE_TOO_LARGE", "Response exceeds size ceiling", status="FAILED")
        chunks.append(chunk)


def _pinned_request_once(
    target: ValidatedTarget,
    *,
    max_bytes: int,
    deadline: float,
) -> _FetchedResponse:
    timeout = min(3.0, _remaining(deadline))
    scheme = urlsplit(target.url).scheme
    if scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(target, timeout)
    else:
        connection = http.client.HTTPConnection(target.addresses[0], target.port, timeout=timeout)
    try:
        connection.request(
            "GET",
            target.request_target,
            headers={
                "Host": _host_header(target),
                "User-Agent": "PADOS-URL-Ingest/1",
                "Accept": "text/html,text/plain,application/pdf",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        headers = {key.casefold(): value for key, value in response.getheaders()}
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        content = b""
        if response.status not in {301, 302, 303, 307, 308} and content_type in SUPPORTED_CONTENT_TYPES:
            _declared_size(headers, max_bytes)
            content = _read_http_response(response, connection, max_bytes=max_bytes, deadline=deadline)
        encoding = response.headers.get_content_charset() or "utf-8"
        return _FetchedResponse(response.status, headers, content, encoding)
    except UrlIngestError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            raise UrlIngestError("FETCH_TIMEOUT", "URL retrieval timed out", status="FAILED") from exc
        raise UrlIngestError("FETCH_FAILED", "URL retrieval failed", status="FAILED") from exc
    finally:
        connection.close()


def _pinned_request(
    target: ValidatedTarget,
    *,
    max_bytes: int,
    deadline: float,
) -> _FetchedResponse:
    last_error: UrlIngestError | None = None
    for address in target.addresses:
        try:
            return _pinned_request_once(
                replace(target, addresses=(address,)),
                max_bytes=max_bytes,
                deadline=deadline,
            )
        except UrlIngestError as exc:
            if exc.code not in {"FETCH_FAILED", "FETCH_TIMEOUT"}:
                raise
            last_error = exc
            _remaining(deadline)
    if last_error is not None:
        raise last_error
    raise UrlIngestError("FETCH_FAILED", "URL target has no validated address", status="FAILED")


def _mock_request(
    target: ValidatedTarget,
    *,
    max_bytes: int,
    deadline: float,
    transport: httpx.BaseTransport,
) -> _FetchedResponse:
    timeout = httpx.Timeout(min(10.0, _remaining(deadline)), connect=3.0, read=5.0, write=5.0, pool=3.0)
    try:
        with httpx.Client(  # noqa: SIM117 - response lifetime is nested under client lifetime.
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            headers={"User-Agent": "PADOS-URL-Ingest/1", "Accept": "text/html,text/plain,application/pdf"},
        ) as client:
            with client.stream("GET", urljoin(target.url, target.request_target)) as response:
                headers = {key.casefold(): value for key, value in response.headers.items()}
                content_type = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                content = b""
                if response.status_code not in {301, 302, 303, 307, 308} and content_type in SUPPORTED_CONTENT_TYPES:
                    _declared_size(headers, max_bytes)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        _remaining(deadline)
                        total += len(chunk)
                        if total > max_bytes:
                            raise UrlIngestError("RESPONSE_TOO_LARGE", "Response exceeds size ceiling", status="FAILED")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                return _FetchedResponse(response.status_code, headers, content, response.encoding or "utf-8")
    except UrlIngestError:
        raise
    except httpx.TimeoutException as exc:
        raise UrlIngestError("FETCH_TIMEOUT", "URL retrieval timed out", status="FAILED") from exc
    except httpx.HTTPError as exc:
        raise UrlIngestError("FETCH_FAILED", "URL retrieval failed", status="FAILED") from exc


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.fragments: list[tuple[str, str, tuple[tuple[str, dict[str, str]], ...]]] = []
        self.main_headings: list[str] = []
        self.in_title = False
        self.ignored_depth = 0
        self.saw_script = False
        self.author: str | None = None
        self.publication_date: str | None = None
        self.canonical_url: str | None = None
        self.stack: list[tuple[str, dict[str, str]]] = []

    @staticmethod
    def _is_main_candidate(ancestors: tuple[tuple[str, dict[str, str]], ...]) -> bool:
        for tag, attrs in ancestors:
            identity = f"{attrs.get('id', '')} {attrs.get('class', '')}".casefold()
            if tag in {"main", "article"} or attrs.get("role", "").casefold() == "main":
                return True
            if re.search(r"\b(main|article|content|post|entry|story|body)\b", identity):
                return True
        return False

    @staticmethod
    def _is_boilerplate(ancestors: tuple[tuple[str, dict[str, str]], ...]) -> bool:
        blocked_tags = {"aside", "dialog", "footer", "form", "header", "menu", "nav"}
        for tag, attrs in ancestors:
            if tag in blocked_tags:
                return True
            role = attrs.get("role", "").casefold()
            if role in {"banner", "complementary", "contentinfo", "navigation", "search"}:
                return True
            identity = f"{attrs.get('id', '')} {attrs.get('class', '')}".casefold()
            if re.search(
                r"\b(nav|navigation|menu|footer|header|cookie|consent|social|share|breadcrumb|recommend|related|promo|advert|sidebar|skip|accessibility)\b",
                identity,
            ):
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "title":
            self.in_title = True
        if lowered in {"script", "style", "noscript", "template", "svg", "canvas"}:
            self.ignored_depth += 1
            self.saw_script = self.saw_script or lowered == "script"
        if lowered == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content", "").strip()
            if name == "author" and content:
                self.author = content[:500]
            if name in {"article:published_time", "date", "datepublished"} and content:
                self.publication_date = content[:100]
        if lowered == "link" and "canonical" in values.get("rel", "").casefold():
            self.canonical_url = values.get("href", "")[:2048] or None
        if lowered not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((lowered, values))

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self.in_title = False
        if lowered in {"script", "style", "noscript", "template", "svg", "canvas"} and self.ignored_depth:
            self.ignored_depth -= 1
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == lowered:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self.in_title:
            self.title_parts.append(cleaned)
        if not self.ignored_depth and not self.in_title:
            ancestors = tuple(self.stack)
            current_tag = ancestors[-1][0] if ancestors else ""
            self.fragments.append((cleaned, current_tag, ancestors))
            if current_tag in {"h1", "h2"} and self._is_main_candidate(ancestors) and not self._is_boilerplate(ancestors):
                self.main_headings.append(cleaned)


def _normalized_title(title: str | None, headings: list[str]) -> str | None:
    candidate = " ".join(headings).strip() if headings else " ".join((title or "").split())
    if not candidate:
        return None
    if not headings:
        parts = re.split(r"\s+(?:\||–|—|-)\s+", candidate, maxsplit=1)
        if len(parts) == 2 and re.search(
            r"facebook|instagram|linkedin|social|cookie|aws|product catalog|navigation",
            parts[1],
            re.IGNORECASE,
        ):
            candidate = parts[0]
    candidate = re.sub(r"\s+", " ", candidate).strip(" -–—|")
    return candidate[:500] or None


def _normalized_fragments(
    fragments: list[tuple[str, str, tuple[tuple[str, dict[str, str]], ...]]],
) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for text, _tag, _ancestors in fragments:
        normalized = re.sub(r"\s+", " ", text).strip()
        key = normalized.casefold()
        if not normalized or len(normalized) < 2 or key in seen:
            continue
        seen.add(key)
        lines.append(normalized)
    return "\n".join(lines)[:MAX_EXTRACTED_TEXT_CHARS]


def extract_html(content: bytes, encoding: str) -> dict[str, Any]:
    parser = _TextExtractor()
    parser.feed(content.decode(encoding, errors="replace"))
    main_fragments = [
        fragment
        for fragment in parser.fragments
        if parser._is_main_candidate(fragment[2]) and not parser._is_boilerplate(fragment[2])
    ]
    fallback_fragments = [fragment for fragment in parser.fragments if not parser._is_boilerplate(fragment[2])]
    selected_fragments = main_fragments or fallback_fragments
    text = _normalized_fragments(selected_fragments)
    status = "FETCHED" if text else "EXTRACTION_PARTIAL"
    if not text and parser.saw_script:
        status = "UNSUPPORTED"
    content_scope = "MAIN_CONTENT" if main_fragments else "DOCUMENT_TEXT_FALLBACK"
    return {
        "title": _normalized_title(" ".join(parser.title_parts), parser.main_headings),
        "author": parser.author,
        "publication_date": parser.publication_date,
        "canonical_url": parser.canonical_url,
        "text": text,
        "status": status,
        "metadata": {
            "extraction_pipeline": "HTML_MAIN_CONTENT_V1",
            "content_scope": content_scope,
            "main_content_detected": bool(main_fragments),
            "excluded_fragment_count": max(0, len(parser.fragments) - len(selected_fragments)),
            "parse_status": "PARSED",
            "normalization_status": "NORMALIZED" if text else "NOT_NORMALIZED",
            "evidence_eligibility": "EVIDENCE_ELIGIBLE" if main_fragments and text else "UNKNOWN",
            **({"reason": "UNSUPPORTED_DYNAMIC_CONTENT"} if status == "UNSUPPORTED" else {}),
        },
    }


def _parse_pdf_worker(path_value: str, output: Any) -> None:
    try:
        parsed = parse_pdf(Path(path_value))
        text_parts: list[str] = []
        text_length = 0
        truncated = False
        for page in parsed.pages:
            if not page.normalized_text:
                continue
            separator = 1 if text_parts else 0
            remaining = MAX_EXTRACTED_TEXT_CHARS - text_length - separator
            if remaining <= 0:
                truncated = True
                break
            value = page.normalized_text[:remaining]
            text_parts.append(value)
            text_length += len(value) + separator
            if len(value) < len(page.normalized_text):
                truncated = True
                break
        text = "\n".join(text_parts)
        if parsed.extraction_status == "PARSED":
            status = "EXTRACTION_PARTIAL" if truncated else "FETCHED"
        elif parsed.extraction_status in {"OCR_REQUIRED", "PARSE_FAILED"}:
            status = "EXTRACTION_PARTIAL"
        else:
            status = "UNSUPPORTED"
        output.send(("ok", {
            "title": str(parsed.title)[:500] if parsed.title else None,
            "author": (", ".join(parsed.authors)[:500] or None),
            "publication_date": str(parsed.publication_year) if parsed.publication_year else None,
            "text": text,
            "status": status,
            "metadata": {
                "pages": len(parsed.pages),
                "parser": str(parsed.parser_version)[:200],
                "warnings": [str(value)[:500] for value in list(parsed.warnings)[:100]],
                "extraction_status": parsed.extraction_status,
                "text_truncated": truncated,
            },
        }))
    except Exception as exc:  # noqa: BLE001 - process boundary must convert parser failures.
        output.send(("error", type(exc).__name__))
    finally:
        output.close()


def extract_pdf(content: bytes, timeout_seconds: float = PDF_PARSE_TIMEOUT_SECONDS) -> dict[str, Any]:
    active_timeout = max(0.1, min(PDF_PARSE_TIMEOUT_SECONDS, timeout_seconds))
    if not _PDF_WORKER_LIMIT.acquire(timeout=active_timeout):
        raise UrlIngestError("PDF_EXTRACTION_BUSY", "PDF extraction capacity is busy", status="FAILED")
    try:
        with TemporaryDirectory(prefix="pados-url-pdf-") as temp_root:
            path = Path(temp_root) / "source.pdf"
            path.write_bytes(content)
            context = mp.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_parse_pdf_worker, args=(str(path), sender), daemon=True)
            process.start()
            sender.close()
            if not receiver.poll(active_timeout):
                process.terminate()
                process.join(1.0)
                if process.is_alive():
                    process.kill()
                    process.join(1.0)
                raise UrlIngestError("PDF_EXTRACTION_TIMEOUT", "PDF extraction exceeded its deadline", status="FAILED")
            outcome, payload = receiver.recv()
            process.join(1.0)
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            receiver.close()
    finally:
        _PDF_WORKER_LIMIT.release()
    if outcome != "ok" or not isinstance(payload, dict):
        raise UrlIngestError("PDF_EXTRACTION_FAILED", "PDF extraction failed safely", status="FAILED")
    return payload


def fetch_url(
    original_url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = socket.getaddrinfo,
) -> UrlIngestResult:
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    current_url = original_url
    for redirect_count in range(MAX_REDIRECTS + 1):
        target = validate_url(current_url, resolver=resolver, deadline=deadline)
        response = (
            _mock_request(target, max_bytes=max_bytes, deadline=deadline, transport=transport)
            if transport is not None
            else _pinned_request(target, max_bytes=max_bytes, deadline=deadline)
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise UrlIngestError("REDIRECT_LOCATION_MISSING", "Redirect has no location", status="FAILED")
            if redirect_count == MAX_REDIRECTS:
                raise UrlIngestError("TOO_MANY_REDIRECTS", "Redirect ceiling exceeded", status="FAILED")
            current_url = urljoin(target.url, location)
            validate_url(current_url, resolver=resolver, deadline=deadline)
            continue
        if response.status_code >= 400:
            raise UrlIngestError("FETCH_HTTP_ERROR", f"URL returned HTTP {response.status_code}", status="FAILED")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        fetched_at = datetime.now(UTC).isoformat()
        if content_type not in SUPPORTED_CONTENT_TYPES:
            return UrlIngestResult(
                original_url=_sanitized_url(original_url),
                final_url=target.url,
                title=None,
                content_type=content_type or "application/octet-stream",
                source_host=target.host,
                fetched_at=fetched_at,
                retrieval_status="UNSUPPORTED",
                extracted_text="",
                content_hash=None,
                extracted_text_hash=None,
                metadata={"reason": "UNSUPPORTED_CONTENT_TYPE"},
            )
        if content_type == "application/pdf":
            extracted = extract_pdf(response.content, _remaining(deadline))
        elif content_type == "text/html":
            extracted = extract_html(response.content, response.encoding)
        else:
            extracted = {
                "title": None,
                "author": None,
                "publication_date": None,
                "text": response.content.decode(response.encoding, errors="replace"),
                "status": "FETCHED",
                "metadata": {},
            }
        text = str(extracted.get("text", ""))
        canonical = extracted.get("canonical_url")
        canonical_url = None
        metadata = dict(extracted.get("metadata", {}))
        metadata.setdefault("parse_status", "PARSED" if text else "PARSE_FAILED")
        metadata.setdefault("normalization_status", "NORMALIZED" if text else "NOT_NORMALIZED")
        metadata.setdefault(
            "evidence_eligibility",
            "EVIDENCE_ELIGIBLE" if text and str(extracted.get("status")) == "FETCHED" else "UNKNOWN",
        )
        if canonical:
            try:
                canonical_url = validate_url(
                    urljoin(target.url, str(canonical)),
                    resolver=resolver,
                    deadline=deadline,
                ).url
            except UrlIngestError:
                metadata["canonical_url_status"] = "REJECTED_UNSAFE"
        return UrlIngestResult(
            original_url=_sanitized_url(original_url),
            final_url=target.url,
            title=extracted.get("title"),
            content_type=content_type,
            source_host=target.host,
            fetched_at=fetched_at,
            retrieval_status=str(extracted.get("status", "EXTRACTION_PARTIAL")),
            extracted_text=text,
            content_hash=hashlib.sha256(response.content).hexdigest(),
            extracted_text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            author=extracted.get("author"),
            publication_date=extracted.get("publication_date"),
            canonical_url=canonical_url,
            metadata=metadata,
        )
    raise UrlIngestError("FETCH_FAILED", "URL retrieval did not complete", status="FAILED")
