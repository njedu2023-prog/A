from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .image_ocr import ocr_pdf_with_macos_vision

STOCK_CODE_RE = re.compile(r"(?:00|30|60|68|43|83|87)\d{4}")


def extract_text(path: str | Path, config: dict[str, Any] | None = None) -> str:
    pdf_path = Path(path)
    with pdf_path.open("rb") as fh:
        header = fh.read(5)
    if header != b"%PDF-":
        return pdf_path.read_text(encoding="utf-8")

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages).strip()
        min_chars = int((config or {}).get("ocr", {}).get("min_extracted_text_chars", 1))
        if _has_enough_extracted_content(text, min_chars):
            return text
    except Exception:
        pass

    if config and config.get("ocr", {}).get("enabled", False):
        text = ocr_pdf_with_macos_vision(pdf_path, config)
        if _has_stock_evidence(text):
            return text

    raise RuntimeError(
        "PDF text extraction returned too little stock data. Enable OCR, improve OCR quality, or provide a text-based PDF."
    )


def _has_enough_extracted_content(text: str, min_chars: int) -> bool:
    if len(text) < min_chars:
        return False
    return _has_stock_evidence(text)


def _has_stock_evidence(text: str) -> bool:
    code_count = len(STOCK_CODE_RE.findall(text))
    if code_count >= 3:
        return True
    has_list_headers = all(token in text for token in ("Top", "Premium"))
    return has_list_headers and code_count > 0
