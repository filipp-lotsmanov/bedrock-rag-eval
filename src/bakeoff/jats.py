"""Extract ground-truth text from JATS XML for parser fidelity comparison.

Ground truth is the main abstract plus body section text. Excluded:
    ref-list        bibliography - large, and irrelevant to document QA
    table-wrap      tables are measured separately, not as prose
    fig             figure captions describe content no visual modality can see
    inline-formula  MathML yields stray symbols on both sides of the comparison
    disp-formula
    ack             acknowledgements
    fn-group        footnotes

Inline markup (xref, bold, italic, sub, sup) is flattened rather than removed:
its text genuinely appears on the rendered page, so it is joined without
added spaces ('Ca<sup>2+</sup>' -> 'Ca2+'). Block elements are separated by a
space so words at block boundaries never merge.

Text is built by a read-only walk rather than by removing elements: in
ElementTree the text following an element belongs to that element's tail,
so removing a pruned element would also delete the text after it.

The main abstract is the first one without an abstract-type attribute;
typed abstracts (graphical, teaser, summary) are used only as a fallback.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

PRUNE = frozenset(
    {
        "ref-list",
        "table-wrap",
        "fig",
        "inline-formula",
        "disp-formula",
        "ack",
        "fn-group",
    }
)

BLOCK = frozenset(
    {
        "p",
        "title",
        "sec",
        "list",
        "list-item",
        "label",
        "disp-quote",
    }
)

_WHITESPACE = re.compile(r"\s+")


def _local_name(tag: object) -> str:
    """Strip any namespace prefix: '{ns}body' -> 'body'."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element) -> str:
    """Concatenate an element's text, skipping pruned subtrees.

    A pruned child contributes nothing, but its tail is kept, because the
    tail is the parent's text that follows the child.
    """
    out = [elem.text or ""]
    for child in elem:
        name = _local_name(child.tag)
        if name not in PRUNE:
            inner = _text(child)
            out.append(f" {inner} " if name in BLOCK else inner)
        out.append(child.tail or "")
    return "".join(out)


def _first(root: ET.Element, name: str) -> ET.Element | None:
    for elem in root.iter():
        if _local_name(elem.tag) == name:
            return elem
    return None


def _main_abstract(root: ET.Element) -> ET.Element | None:
    abstracts = [e for e in root.iter() if _local_name(e.tag) == "abstract"]
    for abstract in abstracts:
        if "abstract-type" not in abstract.attrib:
            return abstract
    return abstracts[0] if abstracts else None


def extract_ground_truth(path: str | Path) -> str:
    """Return main abstract + body text from a JATS XML article."""
    root = ET.parse(path).getroot()

    parts: list[str] = []
    for elem in (_main_abstract(root), _first(root, "body")):
        if elem is not None:
            parts.append(_text(elem))

    if not parts:
        raise ValueError(f"no abstract or body found in {path}")

    return _WHITESPACE.sub(" ", " ".join(parts)).strip()
