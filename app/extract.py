"""Yüklenen dosyalardan metin çıkarma: PDF, Word (.docx), düz metin ve fotoğraf."""
import io

from . import ai_engine

TEXT_EXT = {".txt", ".md"}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_EXT = TEXT_EXT | PDF_EXT | DOCX_EXT | IMAGE_EXT


class ExtractionError(Exception):
    """Kullanıcıya gösterilebilecek, anlaşılır hata mesajı taşır."""


def _ext(filename: str) -> str:
    name = (filename or "").lower()
    return name[name.rfind("."):] if "." in name else ""


def _from_txt(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ExtractionError("Metin dosyasının karakter kodlaması okunamadı.")


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # bozuk / şifreli PDF
        raise ExtractionError(f"PDF okunamadı: {exc}") from exc
    text = "\n\n".join(pages).strip()
    if len(text) < 20:
        raise ExtractionError(
            "Bu PDF taranmış (resim) bir belge gibi görünüyor. Sayfaların fotoğrafını "
            "JPG/PNG olarak yükleyin ya da metni kopyalayıp yapıştırın."
        )
    return text


def _from_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Word dosyası okunamadı: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))
    return "\n".join(parts)


def _from_image(data: bytes, ext: str) -> str:
    if not ai_engine.is_enabled():
        raise ExtractionError(
            "Fotoğraftan metin okuma şu an kullanılamıyor. Metni yapıştırın ya da sözleşmeyi PDF veya "
            "Word dosyası olarak yükleyin."
        )
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[ext]
    try:
        return ai_engine.transcribe_image(data, media)
    except ai_engine.AIError as exc:
        raise ExtractionError(
            "Fotoğraftaki metin şu an okunamadı. Birkaç dakika sonra tekrar deneyin ya da metni yapıştırın."
        ) from exc


def extract_text(filename: str, data: bytes) -> str:
    ext = _ext(filename)
    if ext not in SUPPORTED_EXT:
        raise ExtractionError(
            "Desteklenmeyen dosya türü. PDF, DOCX, TXT, JPG, PNG veya WEBP yükleyin."
        )
    if ext in TEXT_EXT:
        return _from_txt(data)
    if ext in PDF_EXT:
        return _from_pdf(data)
    if ext in DOCX_EXT:
        return _from_docx(data)
    return _from_image(data, ext)
