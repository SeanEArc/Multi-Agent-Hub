"""Word edits that keep what python-docx's `paragraph.text = ...` silently drops, and the changed-paragraph list.

The careful replace (decided 2026-11-23): the new text goes into the runs that held the old text, so the first of
them keeps its formatting (bold, font, size) and every other run is untouched. A paragraph holding a link, a field
or a tracked change is refused as a whole (exit 1) with nothing written: rewriting its runs would break those, and
python-docx can't even see text inside `w:ins`, so the search runs over the paragraph's XML text. A run that holds
anything besides text (a picture, a footnote or comment mark) is refused too.

The diff compares the documents' paragraphs as they read with tracked changes accepted (no Office needed); with
Office, `hub-doc docx compare` makes a real tracked-changes copy instead.
"""

import difflib
from typing import Any

from docx.oxml.ns import qn

from app.doctool.contract import Failed, Refused
from app.doctool.wordxml import body_paragraphs, blockers, paragraph_text

# Run children the replace may rewrite; anything else in a matched run (a drawing, a footnote reference, a symbol)
# would be lost by setting its text.
TEXT_PARTS = {qn(f"w:{tag}") for tag in ("rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen",
                                          "lastRenderedPageBreak")}
CHANGES_LIMIT = 300


def _open(path: str):
    from docx import Document

    return Document(path)


def replace(path: str, output: str | None, pairs: list[list[str]]) -> dict[str, Any]:
    if output is None:
        raise Failed("docx edit needs an output file.")
    from docx.text.paragraph import Paragraph

    document = _open(path)
    paragraphs = body_paragraphs(document)
    report = []
    for old, new in pairs:
        if not old:
            raise Failed("--replace needs the text to find (OLD can't be empty).")
        hits = [i for i, p in enumerate(paragraphs) if old in paragraph_text(p)]
        if not hits:
            raise Failed(f"{old!r} wasn't found in the document (it is searched within one paragraph at a time).")
        for index in hits:
            reasons = blockers(paragraphs[index])
            if reasons:
                raise Refused(f"paragraph {index} ({_preview(paragraph_text(paragraphs[index]))}) has "
                              f"{' and '.join(reasons)}; hub-doc won't rewrite it, because that would break "
                              f"{'them' if len(reasons) > 1 else 'it'}. Nothing was written. Edit another "
                              "paragraph, or change this one in Word.")
        count = 0
        for index in hits:
            paragraph = Paragraph(paragraphs[index], document._body)
            count += _replace_in_runs(paragraph, old, new, index)
        report.append({"old": old, "new": new, "count": count, "paragraphs": hits})
    document.save(output)
    return {"output": output, "replaced": report}


def _replace_in_runs(paragraph, old: str, new: str, index: int) -> int:
    runs = paragraph.runs  # direct runs; blockers() already refused links, fields and tracked changes
    texts = [run.text for run in runs]
    joined = "".join(texts)
    if old not in joined:
        raise Refused(f"paragraph {index}: {old!r} sits partly in content hub-doc can't edit safely (such as a "
                      "content control); nothing was written.")
    starts, position = [], 0
    for value in texts:
        starts.append(position)
        position += len(value)
    found, at = [], joined.find(old)
    while at != -1:
        found.append(at)
        at = joined.find(old, at + len(old))
    for begin in reversed(found):  # from the end, so earlier offsets stay right
        end = begin + len(old)
        first, last = _run_at(starts, texts, begin), _run_at(starts, texts, end - 1)
        for i in range(first, last + 1):
            extra = [child.tag for child in runs[i]._r if child.tag not in TEXT_PARTS]
            if extra:
                raise Refused(f"paragraph {index}: {old!r} runs through a picture, footnote or comment mark; "
                              "nothing was written.")
        head = texts[first][: begin - starts[first]]
        tail = texts[last][end - starts[last]:]
        if first == last:
            texts[first] = head + new + tail
        else:
            texts[first] = head + new
            for i in range(first + 1, last):
                texts[i] = ""
            texts[last] = tail
        for i in range(first, last + 1):
            if runs[i].text != texts[i]:
                runs[i].text = texts[i]
    return len(found)


def _run_at(starts: list[int], texts: list[str], offset: int) -> int:
    """The run holding the character at `offset` of the joined text."""
    return next(i for i, (start, value) in enumerate(zip(starts, texts)) if start <= offset < start + len(value))


def diff(original: str, edited: str) -> dict[str, Any]:
    before = [paragraph_text(p) for p in body_paragraphs(_open(original))]
    after = [paragraph_text(p) for p in body_paragraphs(_open(edited))]
    changes: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            continue
        pairs = max(a2 - a1, b2 - b1)
        for k in range(pairs):
            old = before[a1 + k] if a1 + k < a2 else None
            new = after[b1 + k] if b1 + k < b2 else None
            kind = "changed" if old is not None and new is not None else "removed" if new is None else "added"
            changes.append({"kind": kind, "before_index": a1 + k if old is not None else None,
                            "after_index": b1 + k if new is not None else None,
                            "before": old or "", "after": new or "", "words": _words(old or "", new or "")})
    return {"changed": changes[:CHANGES_LIMIT], "count": len(changes),
            "paragraphs": {"before": len(before), "after": len(after)}}


def _words(old: str, new: str) -> str:
    """The change word by word: [-removed-]{+added+}."""
    a, b = old.split(), new.split()
    out = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[a1:a2]))
            continue
        if a2 > a1:
            out.append("[-" + " ".join(a[a1:a2]) + "-]")
        if b2 > b1:
            out.append("{+" + " ".join(b[b1:b2]) + "+}")
    return " ".join(out)


def _preview(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return repr(text if len(text) <= limit else text[: limit - 1] + "…")
