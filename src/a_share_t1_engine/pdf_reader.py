from __future__ import annotations

from pathlib import Path


def extract_text(path: str | Path) -> str:
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
        if text:
            return text
    except Exception:
        pass

    return pdf_path.read_text(encoding="utf-8")
