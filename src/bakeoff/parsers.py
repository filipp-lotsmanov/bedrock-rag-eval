"""PDF text extractors under comparison, with default settings.

Shared by the bake-off runner and the failure analysis, so both always use
exactly the same extraction. Imported explicitly (not re-exported from the
package) because it pulls in the parsing dependency group.
"""

from collections.abc import Callable
from pathlib import Path

import pdfplumber
import pypdf

Extractor = Callable[[Path], list[str]]


def extract_pypdf(path: Path) -> list[str]:
    return [page.extract_text() for page in pypdf.PdfReader(path).pages]


def extract_pdfplumber(path: Path) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


PARSERS: dict[str, Extractor] = {
    "pypdf": extract_pypdf,
    "pdfplumber": extract_pdfplumber,
}
