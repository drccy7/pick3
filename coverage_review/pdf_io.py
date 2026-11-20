"""PDF helpers: decrypt with the customer's password, extract plain text (for validation and fallback)."""

from __future__ import annotations

import io

import pdfplumber
from pypdf import PdfReader, PdfWriter


class PdfPasswordError(ValueError):
    pass


def is_encrypted(raw: bytes) -> bool:
    try:
        return PdfReader(io.BytesIO(raw)).is_encrypted
    except Exception:
        return False


def decrypt(raw: bytes, password: str = "") -> bytes:
    """Return an unencrypted copy of the PDF (unchanged bytes if it was not encrypted)."""
    reader = PdfReader(io.BytesIO(raw))
    if not reader.is_encrypted:
        return raw
    if not reader.decrypt(password or ""):
        raise PdfPasswordError("PDF 암호가 맞지 않습니다.")
    writer = PdfWriter(clone_from=reader)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_texts(raw: bytes) -> list[str]:
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages]


def encrypt(raw: bytes, password: str) -> bytes:
    reader = PdfReader(io.BytesIO(raw))
    writer = PdfWriter(clone_from=reader)
    writer.encrypt(user_password=password, owner_password=None, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
