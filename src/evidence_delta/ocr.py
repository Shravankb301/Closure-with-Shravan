from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Protocol


class OCRAdapter(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def extract_pdf(self, content: bytes) -> str: ...


class MacOSVisionOCR:
    """Local PDF OCR using Apple's on-device Vision framework."""

    name = "MACOS_VISION_OCR"

    def __init__(self, timeout_seconds: float = 180.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.script = Path(__file__).with_name("macos_vision_ocr.swift")
        self._cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return (
            sys.platform == "darwin" and shutil.which("swift") is not None and self.script.is_file()
        )

    def extract_pdf(self, content: bytes) -> str:
        if not self.available:
            raise RuntimeError("macOS Vision OCR is unavailable")
        digest = sha256(content).hexdigest()
        if digest in self._cache:
            return self._cache[digest]

        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as source:
                source.write(content)
                path = source.name
            result = subprocess.run(
                ["swift", str(self.script), path],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)

        if result.returncode != 0:
            detail = (
                result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
            )
            raise RuntimeError(f"macOS Vision OCR failed: {detail}")
        text = result.stdout.strip()
        self._cache[digest] = text
        return text


class TesseractOCR:
    """Cross-platform PDF OCR backed by Poppler and Tesseract binaries."""

    name = "TESSERACT_OCR"

    def __init__(self, timeout_seconds: float = 180.0, dpi: int = 180) -> None:
        self.timeout_seconds = timeout_seconds
        self.dpi = dpi
        self._cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        return shutil.which("pdftoppm") is not None and shutil.which("tesseract") is not None

    def extract_pdf(self, content: bytes) -> str:
        if not self.available:
            raise RuntimeError("Tesseract OCR is unavailable")
        digest = sha256(content).hexdigest()
        if digest in self._cache:
            return self._cache[digest]

        deadline = monotonic() + self.timeout_seconds
        with tempfile.TemporaryDirectory(prefix="evidence-delta-ocr-") as directory:
            workdir = Path(directory)
            source = workdir / "source.pdf"
            source.write_bytes(content)
            rendered_prefix = workdir / "page"
            render = subprocess.run(
                [
                    "pdftoppm",
                    "-jpeg",
                    "-r",
                    str(self.dpi),
                    str(source),
                    str(rendered_prefix),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            if render.returncode != 0:
                detail = (
                    render.stderr.strip().splitlines()[-1]
                    if render.stderr.strip()
                    else "unknown error"
                )
                raise RuntimeError(f"PDF rendering failed: {detail}")

            pages = sorted(workdir.glob("page-*.jpg"))
            if not pages:
                raise RuntimeError("PDF rendering produced no pages")
            extracted_pages = []
            for index, page in enumerate(pages, start=1):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Tesseract OCR exceeded its time limit")
                result = subprocess.run(
                    ["tesseract", str(page), "stdout", "-l", "eng", "--psm", "3"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                )
                if result.returncode != 0:
                    detail = (
                        result.stderr.strip().splitlines()[-1]
                        if result.stderr.strip()
                        else "unknown error"
                    )
                    raise RuntimeError(f"Tesseract failed on page {index}: {detail}")
                extracted_pages.append(f"--- Page {index} ---\n{result.stdout.strip()}")

        text = "\n\n".join(extracted_pages).strip()
        self._cache[digest] = text
        return text


def default_ocr_adapter() -> OCRAdapter:
    """Choose the best OCR runtime available on the current host."""

    macos = MacOSVisionOCR()
    if macos.available:
        return macos
    return TesseractOCR()
