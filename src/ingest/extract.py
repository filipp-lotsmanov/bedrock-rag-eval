"""PDF text extraction with pypdf's default behaviour, as chosen in the parser bake-off."""

from io import BytesIO

from pypdf import PdfReader


def extract_pages(pdf: bytes) -> list[str]:
    """Return the extracted text of each page, in page order."""
    reader = PdfReader(BytesIO(pdf))
    return [page.extract_text() for page in reader.pages]
