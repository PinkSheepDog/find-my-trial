"""Document text extraction — the first pipeline stage, always local.

Supported inputs:
  * .txt / .md / .rtf  -> decoded text (RTF control words stripped)
  * .pdf               -> embedded text via PyMuPDF; if a page has no text layer
                          (scanned image), OCR is attempted per page
  * .docx              -> paragraphs via python-docx
  * image (.png/.jpg)  -> OCR

OCR is OPTIONAL and pluggable. If Tesseract/pytesseract is not installed, scanned
PDFs and images degrade gracefully: we return whatever text exists plus a clear
warning, rather than failing. All processing happens on this machine; nothing is
uploaded for text extraction.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field


@dataclass
class ExtractedDocument:
    text: str
    source_kind: str                       # "text" | "pdf" | "pdf+ocr" | "docx" | "image"
    warnings: list[str] = field(default_factory=list)
    ocr_used: bool = False


_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")


def extract_text(filename: str, data: bytes) -> ExtractedDocument:
    name = (filename or "").lower()
    if name.endswith((".txt", ".md")):
        return ExtractedDocument(text=_decode(data), source_kind="text")
    if name.endswith(".rtf"):
        return ExtractedDocument(text=_strip_rtf(_decode(data)), source_kind="text")
    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith(".docx"):
        return _extract_docx(data)
    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")):
        return _extract_image(data)
    # Unknown: best-effort decode.
    return ExtractedDocument(
        text=_decode(data), source_kind="text",
        warnings=[f"Unrecognized file type for {filename!r}; treated as plain text."],
    )


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _strip_rtf(text: str) -> str:
    if "\\rtf" not in text[:64].lower():
        return text
    text = text.replace("\\par", "\n").replace("\\line", "\n").replace("\\tab", "\t")
    text = _RTF_HEX.sub(lambda m: bytes.fromhex(m.group(1)).decode("latin-1", "ignore"), text)
    text = text.replace("{", " ").replace("}", " ")
    text = _RTF_CONTROL.sub(" ", text)
    text = text.replace("\\", "\n")
    return re.sub(r"[ \t]+", " ", text).strip()


def _extract_pdf(data: bytes) -> ExtractedDocument:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ExtractedDocument(text="", source_kind="pdf",
                                 warnings=["PyMuPDF not installed; cannot read PDF."])
    warnings: list[str] = []
    ocr_used = False
    chunks: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            page_text = page.get_text("text").strip()
            if page_text:
                chunks.append(page_text)
            else:
                # No text layer -> likely a scanned page. Try OCR.
                ocr_text, ok, warn = _ocr_pdf_page(page)
                if ok:
                    chunks.append(ocr_text)
                    ocr_used = True
                elif warn:
                    warnings.append(warn)
    text = "\n\n".join(chunks)
    if not text and not warnings:
        warnings.append("PDF contained no extractable text and OCR produced nothing.")
    return ExtractedDocument(
        text=text, source_kind="pdf+ocr" if ocr_used else "pdf",
        warnings=warnings, ocr_used=ocr_used,
    )


def _ocr_pdf_page(page) -> tuple[str, bool, str | None]:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return "", False, ("A page appears to be scanned (no text layer). Install "
                           "OCR support (pytesseract + Pillow + Tesseract) to read it.")
    try:
        import fitz  # noqa: F401
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img), True, None
    except Exception as exc:  # pragma: no cover - depends on tesseract binary
        return "", False, f"OCR failed for a scanned page: {exc}"


def _extract_docx(data: bytes) -> ExtractedDocument:
    try:
        import docx  # python-docx
    except ImportError:
        return ExtractedDocument(text="", source_kind="docx",
                                 warnings=["python-docx not installed; cannot read DOCX."])
    document = docx.Document(io.BytesIO(data))
    paras = [p.text for p in document.paragraphs if p.text.strip()]
    # Include table cells (labs/meds are often tabular).
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                paras.append(" | ".join(cells))
    return ExtractedDocument(text="\n".join(paras), source_kind="docx")


def _extract_image(data: bytes) -> ExtractedDocument:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return ExtractedDocument(
            text="", source_kind="image",
            warnings=["Image OCR requires pytesseract + Pillow + the Tesseract binary."],
        )
    try:
        img = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(img)
        return ExtractedDocument(text=text, source_kind="image", ocr_used=True)
    except Exception as exc:  # pragma: no cover
        return ExtractedDocument(text="", source_kind="image",
                                 warnings=[f"OCR failed: {exc}"])
