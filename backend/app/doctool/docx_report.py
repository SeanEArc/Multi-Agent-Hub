"""Reading Word documents and the formatting report, from two sources (decided 2026-11-23).

BetterOffice's structure export (`betteroffice-docx`) has the structure: headings, list numbering, links, and
tracked changes with their authors, including text python-docx can't see inside `w:ins`. It has no fonts, sizes,
alignment or spacing (probe 2026-11-21, section 1), so python-docx supplies those: each run's direct formatting,
paragraph spacing and alignment (direct or from the style), and the style definitions with their inherited sizes.

The report is JSON for the agent: `paragraphs` in document order, `styles` used, `issues` (inconsistencies such as
direct formatting standing in for a heading style) and `sections` (page size and margins).
"""

import re
from collections import Counter
from typing import Any

from docx.oxml.ns import qn

from app.doctool.wordxml import body_paragraphs, paragraph_text

PARAGRAPH_LIMIT = 2000
MARKDOWN_LIMIT = 300_000
EMPTY_RUN_LIMIT = 2  # this many empty paragraphs in a row are spacing done by hand
HEADING_WORDS = 15
LEGEND = ("Run font and size are set directly on the run (null: they come from the paragraph's style, see "
          "style_font/style_size). Paragraph spacing, indents and alignment are in points, with `direct` listing "
          "what is set on the paragraph itself rather than by its style.")


def read(path: str, view: str) -> dict[str, Any]:
    import betteroffice_docx as bd

    document = bd.Document.open_path(path)
    exported = document.export_markdown(revision_view=view)
    markdown = re.sub(r"<!-- docx-(?:export|story)[^>]*-->\n?", "", exported.get("markdown") or "").strip()
    truncated = bool(exported.get("truncated"))
    if len(markdown) > MARKDOWN_LIMIT:
        markdown, truncated = markdown[:MARKDOWN_LIMIT], True
    reply: dict[str, Any] = {"markdown": markdown, "view": view}
    if truncated:
        reply["truncated"] = True
    return reply


# --- The report ----------------------------------------------------------------------------------------------


def report(path: str) -> dict[str, Any]:
    import betteroffice_docx as bd
    from docx import Document
    from docx.text.paragraph import Paragraph

    structure = bd.Document.open_path(path)
    blocks = _blocks(structure.export_structured(revision_view="markup"))
    document = Document(path)
    elements = body_paragraphs(document)
    matched = _align(elements, blocks)
    defaults = _doc_defaults(document)

    paragraphs, used_styles = [], Counter()
    for index, element in enumerate(elements[:PARAGRAPH_LIMIT]):
        paragraph = Paragraph(element, document._body)
        block = matched.get(index)
        style = paragraph.style
        used_styles[style.name if style is not None else "Normal"] += 1
        entry = _paragraph(paragraph, element, block, defaults)
        entry["index"] = index
        paragraphs.append(entry)
        for run in entry["runs"]:
            if run.get("char_style"):
                used_styles[run["char_style"]] += 1

    styles = _styles(document, used_styles, defaults)
    result: dict[str, Any] = {
        "paragraphs": paragraphs,
        "styles": styles,
        "issues": _issues(paragraphs, defaults),
        "sections": _sections(structure),
        "legend": LEGEND,
    }
    if len(elements) > PARAGRAPH_LIMIT:
        result["truncated"] = True
        result["paragraph_count"] = len(elements)
    return result


def _blocks(exported: dict[str, Any]) -> list[dict[str, Any]]:
    """The body's paragraph-like blocks in document order, table cells included."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("kind") in ("paragraph", "heading", "listItem") and isinstance(node.get("paragraph"), dict):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for story in exported.get("stories") or []:
        if story.get("story") == "body":
            walk(story.get("blocks") or [])
    return found


def _block_text(block: dict[str, Any], deleted: bool = False) -> str:
    parts = []

    def walk(inlines: list) -> None:
        for inline in inlines or []:
            is_deleted = any(r.get("kind") == "deletion" for r in inline.get("revisions") or [])
            if inline.get("kind") == "text" and is_deleted == deleted:
                parts.append(inline.get("text") or "")
            result = inline.get("cachedResult")
            if isinstance(result, dict):
                walk(result.get("inlines") or [])

    walk(block["paragraph"].get("inlines") or [])
    return "".join(parts)


def _align(elements: list, blocks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Pair python-docx paragraphs with BetterOffice blocks: in order when the counts agree, else by text."""
    def norm(text: str) -> str:
        return " ".join(text.split())

    if len(elements) == len(blocks):
        return dict(enumerate(blocks))
    paired, start = {}, 0
    for index, element in enumerate(elements):
        text = norm(paragraph_text(element))
        for offset in range(start, min(len(blocks), start + 8)):
            if norm(_block_text(blocks[offset])) == text:
                paired[index], start = blocks[offset], offset + 1
                break
    return paired


def _pt(length: Any) -> float | None:
    return round(length.pt, 2) if length is not None else None


def _chain(style):
    while style is not None:
        yield style
        style = style.base_style


def _from_styles(style, getter) -> Any:
    for current in _chain(style):
        try:
            value = getter(current)
        except (AttributeError, ValueError):
            value = None
        if value is not None:
            return value
    return None


def _style_font(style) -> str | None:
    """The style's own font: a named font, or the theme font it points to (`asciiTheme`, e.g. a heading's major
    font), which python-docx's `font.name` doesn't report."""
    if style.font.name:
        return style.font.name
    rpr = style.element.find(qn("w:rPr"))
    fonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
    theme = fonts.get(qn("w:asciiTheme")) if fonts is not None else None
    return f"theme ({theme})" if theme else None


def _doc_defaults(document) -> dict[str, Any]:
    """docDefaults' font and size: what a paragraph gets when no style sets them."""
    defaults: dict[str, Any] = {"font": None, "size": None}
    element = document.styles.element.find(qn("w:docDefaults"))
    if element is None:
        return defaults
    rpr = element.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}")
    if rpr is not None:
        size = rpr.find(qn("w:sz"))
        if size is not None and (size.get(qn("w:val")) or "").isdigit():
            defaults["size"] = int(size.get(qn("w:val"))) / 2
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is not None:
            defaults["font"] = fonts.get(qn("w:ascii")) or (
                f"theme ({fonts.get(qn('w:asciiTheme'))})" if fonts.get(qn("w:asciiTheme")) else None)
    return defaults


def _alignment(value: Any) -> str | None:
    return value.name.lower() if value is not None and getattr(value, "name", None) else None


def _paragraph(paragraph, element, block: dict[str, Any] | None, defaults: dict[str, Any]) -> dict[str, Any]:
    style = paragraph.style
    fmt = paragraph.paragraph_format
    direct = []
    values: dict[str, Any] = {}
    for key, getter in (("space_before", lambda f: f.space_before), ("space_after", lambda f: f.space_after),
                        ("left_indent", lambda f: f.left_indent), ("first_line_indent", lambda f: f.first_line_indent)):
        own = getter(fmt)
        if own is not None:
            direct.append(key)
        values[key] = _pt(own if own is not None else _from_styles(style, lambda s: getter(s.paragraph_format)))
    own_alignment = fmt.alignment
    if own_alignment is not None:
        direct.append("alignment")
    alignment = own_alignment if own_alignment is not None else _from_styles(style, lambda s: s.paragraph_format.alignment)
    line = fmt.line_spacing if fmt.line_spacing is not None else _from_styles(style, lambda s: s.paragraph_format.line_spacing)
    if fmt.line_spacing is not None:
        direct.append("line_spacing")

    heading = (block or {}).get("heading") or None
    listed = (block or {}).get("list") or None
    revisions = []
    links = []
    if block:
        for inline in block["paragraph"].get("inlines") or []:
            for revision in inline.get("revisions") or []:
                revisions.append({"kind": revision.get("kind"), "author": revision.get("author"),
                                  "date": revision.get("date"), "text": inline.get("text")})
            link = inline.get("link")
            if isinstance(link, dict) and link.get("href"):
                links.append({"text": inline.get("text"), "href": link.get("href")})
    return {
        "text": paragraph_text(element),
        "style": style.name if style is not None else None,
        "heading_level": (heading.get("outlineLevel", 0) + 1) if heading else None,
        "alignment": _alignment(alignment),
        "space_before": values["space_before"],
        "space_after": values["space_after"],
        "left_indent": values["left_indent"],
        "first_line_indent": values["first_line_indent"],
        "line_spacing": round(line, 2) if isinstance(line, float) else _pt(line) if line is not None else None,
        "direct": direct,
        "style_font": _from_styles(style, _style_font) or defaults["font"],
        "style_size": _pt(_from_styles(style, lambda s: s.font.size)) or defaults["size"],
        "style_bold": bool(_from_styles(style, lambda s: s.font.bold)),
        "runs": _runs(paragraph, element),
        "list": {"format": listed.get("format"), "level": listed.get("level"), "marker": listed.get("marker")}
        if listed else None,
        "revisions": revisions,
        "links": links,
        "in_table": next(element.iterancestors(qn("w:tc")), None) is not None,
    }


def _runs(paragraph, element) -> list[dict[str, Any]]:
    """Every run with text (in links, insertions and deletions too), neighbours with the same formatting merged."""
    from docx.text.run import Run

    runs: list[dict[str, Any]] = []
    for r in element.iter(qn("w:r")):
        if next(r.iterancestors(qn("w:txbxContent")), None) is not None:
            continue
        run = Run(r, paragraph)
        deleted = next(r.iterancestors(qn("w:del")), None) is not None
        text = "".join(t.text or "" for t in r.iter(qn("w:delText"))) if deleted else run.text
        if not text:
            continue
        font = run.font
        char_style = run.style.name if run.style is not None and run.style.name != "Default Paragraph Font" else None
        entry = {"text": text, "font": font.name, "size": _pt(font.size), "bold": font.bold, "italic": font.italic,
                 "underline": bool(font.underline) if font.underline is not None else None,
                 "color": str(font.color.rgb) if font.color is not None and font.color.type is not None
                 and font.color.rgb is not None else None,
                 "char_style": char_style}
        if deleted:
            entry["deleted"] = True
        if runs and {k: v for k, v in runs[-1].items() if k != "text"} == {k: v for k, v in entry.items() if k != "text"}:
            runs[-1]["text"] += text
        else:
            runs.append(entry)
    return runs


def _styles(document, used: Counter, defaults: dict[str, Any]) -> list[dict[str, Any]]:
    """The styles the document uses (and the ones they're based on), with sizes inherited through the chain."""
    wanted = set(used) | {"Normal"}
    styles = []
    seen = set()
    for style in document.styles:
        if style.name not in wanted:
            continue
        for current in _chain(style):
            if current.name in seen:
                continue
            seen.add(current.name)
            entry: dict[str, Any] = {
                "name": current.name,
                "type": str(current.type).split(".")[-1].split(" ")[0].lower() if current.type is not None else None,
                "based_on": current.base_style.name if current.base_style is not None else None,
                "font": _from_styles(current, _style_font) or defaults["font"],
                "size": _pt(_from_styles(current, lambda s: s.font.size)) or defaults["size"],
                "bold": _from_styles(current, lambda s: s.font.bold),
                "italic": _from_styles(current, lambda s: s.font.italic),
                "used": used.get(current.name, 0),
            }
            if hasattr(current, "paragraph_format"):
                entry["space_before"] = _pt(_from_styles(current, lambda s: s.paragraph_format.space_before))
                entry["space_after"] = _pt(_from_styles(current, lambda s: s.paragraph_format.space_after))
            styles.append(entry)
    return styles


def _sections(structure) -> list[dict[str, Any]]:
    sections = []
    try:
        for section in structure.sections():
            sections.append({key: round(getattr(section, key) / 20, 1) if isinstance(getattr(section, key), float)
                             else getattr(section, key)
                             for key in ("page_width", "page_height", "margin_top", "margin_bottom", "margin_left",
                                         "margin_right", "orientation") if hasattr(section, key)})
    except Exception:  # page setup is extra; the report stands without it
        return []
    return sections


# --- Inconsistencies --------------------------------------------------------------------------------------------


def _is_heading(paragraph: dict[str, Any]) -> bool:
    name = str(paragraph["style"] or "").lower()
    return paragraph["heading_level"] is not None or name.startswith(("heading", "title", "subtitle"))


def _issues(paragraphs: list[dict[str, Any]], defaults: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(index: int, kind: str, message: str) -> None:
        issues.append({"paragraph": index, "kind": kind, "message": message})

    body_size = next((p["style_size"] for p in paragraphs if str(p["style"]) == "Normal" and p["style_size"]),
                     defaults["size"])
    empty_run, last_level = 0, 0
    for p in paragraphs:
        index, text, runs = p["index"], p["text"].strip(), [r for r in p["runs"] if r["text"].strip()]
        if not text:
            empty_run += 1
            if empty_run == EMPTY_RUN_LIMIT and not p["in_table"]:
                add(index, "empty-paragraphs", "empty paragraphs are used for spacing; use paragraph spacing "
                                               "(space before/after) instead")
            continue
        empty_run = 0
        if p["revisions"]:
            authors = sorted({str(r["author"]) for r in p["revisions"] if r["author"]})
            add(index, "tracked-changes", f"tracked changes not yet accepted or rejected"
                                          f"{' (by ' + ', '.join(authors) + ')' if authors else ''}")
        if _is_heading(p):
            level = p["heading_level"] or 1
            if last_level and level > last_level + 1:
                add(index, "heading-skip", f"heading level {level} follows level {last_level}; a level is skipped")
            last_level = level
            continue
        if p["list"] is not None or p["in_table"]:
            continue
        bold = all((r["bold"] if r["bold"] is not None else p["style_bold"]) for r in runs) if runs else False
        sizes = [r["size"] for r in runs if r["size"]]
        bigger = bool(sizes) and body_size is not None and max(sizes) > body_size
        if (bold or bigger) and len(text.split()) <= HEADING_WORDS and not text.endswith((".", ":", ";", ",")):
            looks = ", ".join(filter(None, ["bold" if bold else "", f"{max(sizes):g} pt" if sizes else "",
                                            "centered" if p["alignment"] == "center" else ""]))
            add(index, "fake-heading", f"looks like a heading ({looks}) but uses direct formatting on a "
                                       f"'{p['style']}' paragraph; apply a Heading style so it shows in the "
                                       "outline and table of contents")
            continue
        overrides = [r for r in runs if r["font"] or r["size"]]
        if overrides and len(overrides) < len(runs):
            described = "; ".join(
                f"{r['text'].strip()[:30]!r} is {' '.join(filter(None, [r['font'], f'{r['size']:g} pt' if r['size'] else '']))}"
                for r in overrides[:3])
            add(index, "direct-formatting", f"fonts or sizes set directly on part of the paragraph ({described}), "
                                            "unlike the rest of it")
        if "space_before" in p["direct"] or "space_after" in p["direct"]:
            add(index, "direct-spacing", f"spacing set directly ({p['space_before'] or 0:g} pt before, "
                                         f"{p['space_after'] or 0:g} pt after) instead of by the style")
    return issues
