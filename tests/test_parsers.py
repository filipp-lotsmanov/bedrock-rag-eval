from pathlib import Path

import pypdf
import pytest

from bakeoff.parsers import PARSERS


@pytest.fixture
def blank_pdf(tmp_path: Path) -> Path:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    return path


@pytest.mark.parametrize("name", sorted(PARSERS))
def test_one_string_per_page(name: str, blank_pdf: Path) -> None:
    pages = PARSERS[name](blank_pdf)
    assert pages == ["", ""]
