from pathlib import Path

import pytest

from bakeoff import extract_ground_truth


def extract(tmp_path: Path, xml: str) -> str:
    path = tmp_path / "article.xml"
    path.write_text(f"<article>{xml}</article>", encoding="utf-8")
    return extract_ground_truth(path)


def test_text_after_pruned_element_survives(tmp_path: Path) -> None:
    text = extract(
        tmp_path,
        "<body><p>We set <inline-formula>x=1</inline-formula> here.</p></body>",
    )
    assert text == "We set here."


def test_inline_markup_joins_without_spaces(tmp_path: Path) -> None:
    xml = (
        "<body><p>Ca<sup>2+</sup> in H<sub>2</sub>O with "
        "amyloid-<italic>\u03b2</italic> [<xref>1</xref>].</p></body>"
    )
    assert extract(tmp_path, xml) == "Ca2+ in H2O with amyloid-\u03b2 [1]."


def test_consecutive_and_nested_pruned_siblings_removed(tmp_path: Path) -> None:
    xml = (
        "<body><p>Start.<fig>a</fig><fig>b</fig>"
        "<table-wrap><fig>c</fig></table-wrap> End.</p></body>"
    )
    assert extract(tmp_path, xml) == "Start. End."


def test_block_boundaries_do_not_merge_words(tmp_path: Path) -> None:
    xml = "<body><sec><title>Methods</title><p>We measured.</p></sec></body>"
    assert extract(tmp_path, xml) == "Methods We measured."


def test_abstract_precedes_body(tmp_path: Path) -> None:
    xml = "<front><abstract><p>Summary.</p></abstract></front><body><p>Main.</p></body>"
    assert extract(tmp_path, xml) == "Summary. Main."


def test_typed_abstract_skipped_when_main_exists(tmp_path: Path) -> None:
    xml = (
        '<front><abstract abstract-type="graphical"><p>Graphic.</p></abstract>'
        "<abstract><p>Main abstract.</p></abstract></front>"
    )
    assert extract(tmp_path, xml) == "Main abstract."


def test_typed_abstract_used_as_fallback(tmp_path: Path) -> None:
    xml = '<front><abstract abstract-type="summary"><p>Only one.</p></abstract></front>'
    assert extract(tmp_path, xml) == "Only one."


def test_namespaced_mathml_is_pruned(tmp_path: Path) -> None:
    xml = (
        '<body xmlns:mml="http://www.w3.org/1998/Math/MathML"><p>Value '
        "<inline-formula><mml:math><mml:mi>k</mml:mi></mml:math></inline-formula>"
        " rises.</p></body>"
    )
    assert extract(tmp_path, xml) == "Value rises."


def test_missing_abstract_and_body_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no abstract or body"):
        extract(tmp_path, "<front><title>No content</title></front>")
