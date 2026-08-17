"""Reading text and metadata out of a PDF.

Split out of app.py: this is the bottom of the dependency stack — the audit,
evidence and evaluation layers all need it, and none of them should have to
import Flask to get it.
"""
from __future__ import annotations

# How pages are joined into the single text block the LLM sees. Named because
# evidence offsets are computed against exactly this layout.
PAGE_SEPARATOR = "\n\n"

try:
    import pypdf

    def get_pdf_metadata(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            info = reader.metadata or {}
            return {
                "title": info.get("/Title", ""),
                "author": info.get("/Author", ""),
                "creator": info.get("/Creator", ""),
                "pages": len(reader.pages),
            }

    def extract_pdf_pages(path):
        """Per-page text. extract_pdf_text() is the concatenation of these, so
        a character offset into that string can be mapped back to a page."""
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return [page.extract_text() or "" for page in reader.pages]

    def extract_pdf_text(path):
        return PAGE_SEPARATOR.join(extract_pdf_pages(path))

except ImportError:
    pypdf = None

    def get_pdf_metadata(path):
        return {"title": "", "author": "", "creator": "", "pages": None}

    def extract_pdf_pages(path):
        return []

    def extract_pdf_text(path):
        return ""


def page_for_offset(offset: int, pages: list) -> int | None:
    """1-based page number containing a character offset in the joined text.

    Page-level evidence was previously impossible because pages were flattened
    into one string with no record of the boundaries. This reconstructs them."""
    if offset is None or not pages:
        return None
    cursor = 0
    for index, page_text in enumerate(pages, start=1):
        end = cursor + len(page_text)
        if offset < end:
            return index
        cursor = end + len(PAGE_SEPARATOR)
    return len(pages)
