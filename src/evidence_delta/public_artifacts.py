from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from evidence_delta.ocr import OCRAdapter
from evidence_delta.schemas import DocumentInput

ALLOWED_PUBLIC_HOSTS = frozenset({"www.justice.gov", "justice.gov", "www.fbi.gov", "fbi.gov"})
CHALLENGE_MARKERS = (
    "bm-verify",
    "challenge-platform",
    "just a moment...",
    "enable javascript and cookies to continue",
)
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactAcquisition:
    requested_uri: str
    resolved_uri: str | None
    status: str
    http_status: int | None
    content_type: str | None
    content_bytes: int
    content_sha256: str | None
    extraction_method: str
    extracted_characters: int
    page_count: int | None
    assertions_total: int
    assertions_verified: int
    verification_status: str
    error_class: str | None = None
    acquisition_method: str = "PUBLIC_HTTP"
    content: bytes | None = field(default=None, repr=False, compare=False)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _verify_assertions(
    document: DocumentInput,
    extracted_text: str,
    *,
    allow_ocr_tolerance: bool = False,
) -> tuple[int, str]:
    if not extracted_text.strip():
        return 0, "NOT_RUN"
    normalized_artifact = _normalized_text(extracted_text)
    artifact_words = normalized_artifact.split()
    verified = 0
    for assertion in document.assertions:
        normalized_source = _normalized_text(assertion.source_text)
        if normalized_source in normalized_artifact or (
            allow_ocr_tolerance and _ocr_tolerant_span_match(normalized_source, artifact_words)
        ):
            verified += 1
    if verified == len(document.assertions):
        return verified, "VERIFIED"
    if verified:
        return verified, "PARTIAL"
    return 0, "UNVERIFIED"


def _ocr_tolerant_span_match(normalized_source: str, artifact_words: list[str]) -> bool:
    """Accept small OCR substitutions while rejecting reviewed paraphrases."""

    source_words = normalized_source.split()
    if len(source_words) < 8 or len(artifact_words) < len(source_words):
        return False
    window_size = len(source_words)
    for start in range(len(artifact_words) - window_size + 1):
        candidate = " ".join(artifact_words[start : start + window_size])
        if SequenceMatcher(None, normalized_source, candidate).ratio() >= 0.82:
            return True
    return False


class PublicArtifactClient:
    """Fetch and inspect allowlisted official public-record artifacts."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.BaseTransport | None = None,
        ocr_adapter: OCRAdapter | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.ocr_adapter = ocr_adapter

    def acquire_all(self, documents: list[DocumentInput]) -> list[ArtifactAcquisition]:
        def acquire_safely(document: DocumentInput) -> ArtifactAcquisition:
            try:
                return self.acquire(document)
            except Exception as error:
                # Public hosts and serverless runtimes can fail outside httpx's
                # exception hierarchy. Preserve the failed attempt as evidence
                # pipeline state instead of turning the entire case build into
                # an opaque 500 response.
                return self._result(
                    document,
                    document.source_uri or "",
                    status="FETCH_FAILED",
                    extraction_method="NONE",
                    error_class=type(error).__name__,
                )

        with ThreadPoolExecutor(max_workers=min(4, len(documents) or 1)) as executor:
            return list(executor.map(acquire_safely, documents))

    def acquire(self, document: DocumentInput) -> ArtifactAcquisition:
        uri = document.source_uri
        if uri is None:
            return self._result(document, "", status="NO_SOURCE_URI", extraction_method="NONE")
        parsed = urlparse(uri)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_PUBLIC_HOSTS:
            return self._result(
                document,
                uri,
                status="REJECTED_URI",
                extraction_method="NONE",
                error_class="SourceHostNotAllowed",
            )

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={
                    "User-Agent": "EvidenceDelta/0.1 public-record-acquisition",
                    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5",
                },
            ) as client:
                with client.stream("GET", uri) as response:
                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_ARTIFACT_BYTES:
                            return self._result(
                                document,
                                str(response.url),
                                status="TOO_LARGE",
                                http_status=response.status_code,
                                content_type=response.headers.get("content-type"),
                                content_bytes=total,
                                extraction_method="NONE",
                                error_class="ArtifactSizeLimitExceeded",
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    return self._inspect_response(document, response, content)
        except httpx.HTTPError as error:
            return self._result(
                document,
                uri,
                status="FETCH_FAILED",
                extraction_method="NONE",
                error_class=type(error).__name__,
            )

    def inspect_import(
        self,
        document: DocumentInput,
        content: bytes,
        *,
        content_type: str,
        resolved_uri: str | None = None,
    ) -> ArtifactAcquisition:
        """Inspect reviewer-imported bytes obtained through an approved channel."""

        request_uri = resolved_uri or document.source_uri or "https://import.invalid/artifact"
        response = httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=content,
            request=httpx.Request("GET", request_uri),
        )
        return self._inspect_response(
            document,
            response,
            content,
            acquisition_method="REVIEWER_IMPORT",
        )

    def _inspect_response(
        self,
        document: DocumentInput,
        response: httpx.Response,
        content: bytes,
        acquisition_method: str = "PUBLIC_HTTP",
    ) -> ArtifactAcquisition:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        fingerprint = sha256(content).hexdigest() if content else None
        decoded = content[:100_000].decode("utf-8", errors="ignore").casefold()
        challenged = any(marker in decoded for marker in CHALLENGE_MARKERS)

        if challenged:
            return self._result(
                document,
                str(response.url),
                status="ACCESS_CHALLENGE",
                http_status=response.status_code,
                content_type=content_type,
                content_bytes=len(content),
                content_sha256=fingerprint,
                extraction_method="BLOCKED_HTML",
                acquisition_method=acquisition_method,
                content=content,
            )
        if response.status_code >= 400:
            return self._result(
                document,
                str(response.url),
                status="HTTP_ERROR",
                http_status=response.status_code,
                content_type=content_type,
                content_bytes=len(content),
                content_sha256=fingerprint,
                extraction_method="NONE",
                error_class=f"HTTP{response.status_code}",
                acquisition_method=acquisition_method,
                content=content,
            )

        extracted_text = ""
        extraction_method = "BINARY_METADATA"
        page_count = None
        extraction_error = None
        if content.startswith(b"%PDF") or content_type == "application/pdf":
            try:
                reader = PdfReader(BytesIO(content))
                page_count = len(reader.pages)
                extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)
                extraction_method = (
                    "PDF_TEXT" if extracted_text.strip() else "PDF_SCANNED_REQUIRES_OCR"
                )
                if not extracted_text.strip() and self.ocr_adapter and self.ocr_adapter.available:
                    try:
                        extracted_text = self.ocr_adapter.extract_pdf(content)
                        if extracted_text.strip():
                            extraction_method = self.ocr_adapter.name
                    except Exception as error:
                        extraction_error = type(error).__name__
            except Exception:
                extraction_method = "PDF_PARSE_FAILED"
        elif (
            content_type in {"text/html", "application/xhtml+xml"}
            or b"<html" in content[:500].lower()
        ):
            parser = _TextExtractor()
            parser.feed(content.decode(response.encoding or "utf-8", errors="replace"))
            extracted_text = parser.text()
            extraction_method = "HTML_TEXT"
        elif content_type.startswith("text/"):
            extracted_text = content.decode(response.encoding or "utf-8", errors="replace")
            extraction_method = "PLAIN_TEXT"

        assertions_verified, verification_status = _verify_assertions(
            document,
            extracted_text,
            allow_ocr_tolerance="OCR" in extraction_method,
        )
        return self._result(
            document,
            str(response.url),
            status="FETCHED",
            http_status=response.status_code,
            content_type=content_type,
            content_bytes=len(content),
            content_sha256=fingerprint,
            extraction_method=extraction_method,
            extracted_characters=len(extracted_text.strip()),
            page_count=page_count,
            assertions_verified=assertions_verified,
            verification_status=verification_status,
            error_class=extraction_error,
            acquisition_method=acquisition_method,
            content=content,
        )

    @staticmethod
    def _result(
        document: DocumentInput,
        resolved_uri: str,
        *,
        status: str,
        extraction_method: str,
        http_status: int | None = None,
        content_type: str | None = None,
        content_bytes: int = 0,
        content_sha256: str | None = None,
        extracted_characters: int = 0,
        page_count: int | None = None,
        assertions_verified: int = 0,
        verification_status: str = "NOT_RUN",
        error_class: str | None = None,
        acquisition_method: str = "PUBLIC_HTTP",
        content: bytes | None = None,
    ) -> ArtifactAcquisition:
        return ArtifactAcquisition(
            requested_uri=document.source_uri or "",
            resolved_uri=resolved_uri or None,
            status=status,
            http_status=http_status,
            content_type=content_type,
            content_bytes=content_bytes,
            content_sha256=content_sha256,
            extraction_method=extraction_method,
            extracted_characters=extracted_characters,
            page_count=page_count,
            assertions_total=len(document.assertions),
            assertions_verified=assertions_verified,
            verification_status=verification_status,
            error_class=error_class,
            acquisition_method=acquisition_method,
            content=content,
        )
