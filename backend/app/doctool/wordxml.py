"""Word XML helpers shared by the formatting report, the careful replace and the diff.

python-docx's `paragraph.text` and `paragraph.runs` skip runs inside `w:ins` and `w:hyperlink` (probe 2026-11-21,
section 5), so the toolkit reads paragraphs from their XML: every `w:p` of the body in document order (table cells
included, text boxes left out) and their visible text (inserted text in, deleted text out: the accepted view).
"""

from collections.abc import Iterator

from docx.oxml.ns import qn

# What makes a paragraph unsafe to edit run by run, by the word an error message uses.
BLOCKING = {
    qn("w:hyperlink"): "a link",
    qn("w:fldSimple"): "a field",
    qn("w:fldChar"): "a field",
    qn("w:instrText"): "a field",
    qn("w:ins"): "tracked changes",
    qn("w:del"): "tracked changes",
    qn("w:moveFrom"): "tracked changes",
    qn("w:moveTo"): "tracked changes",
    qn("w:rPrChange"): "tracked changes",
    qn("w:pPrChange"): "tracked changes",
}
TEXT_BOX = qn("w:txbxContent")
T, TAB, BR, CR, DEL_TEXT = qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:delText")


def body_paragraphs(document) -> list:
    """Every paragraph element of the body, in order, including those in tables; not those inside text boxes."""
    body = document.element.body
    return [p for p in body.iter(qn("w:p")) if next(p.iterancestors(TEXT_BOX), None) is None]


def _own(element) -> Iterator:
    """The element's descendants in document order, skipping anything inside a nested text box."""
    for child in element:
        if child.tag == TEXT_BOX:
            continue
        yield child
        yield from _own(child)


def paragraph_text(p, deleted: bool = False) -> str:
    """The paragraph's text as it reads with tracked changes accepted (or, `deleted`, only its deleted text)."""
    parts = []
    for element in _own(p):
        tag = element.tag
        if deleted:
            if tag == DEL_TEXT:
                parts.append(element.text or "")
            continue
        if tag == T:
            parts.append(element.text or "")
        elif tag == TAB and element.getparent().tag == qn("w:r"):
            parts.append("\t")
        elif tag in (BR, CR):
            parts.append("\n")
    return "".join(parts)


def blockers(p) -> list[str]:
    """Why the paragraph can't be edited run by run (links, fields, tracked changes), without repeats."""
    found: list[str] = []
    for element in _own(p):
        reason = BLOCKING.get(element.tag)
        if reason and reason not in found:
            found.append(reason)
    return found
