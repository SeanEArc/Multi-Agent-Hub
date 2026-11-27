"""`hub-doc`: the hub's document toolkit, one command for every agent (revision 4, attachments PR 2).

The hub puts `hub-doc` on each agent's PATH (`app.files.documents`) and tells the agent about it. It is the same
program from source (`[project.scripts] hub-doc = "app.doctool.cli:main"`, copied into a bin folder of its own)
and in the bundle (a second exe beside `hub-backend.exe`, sharing `_internal`).

Layout:
- `cli`: the entry point and the supervising parent. It parses the command, checks every path it would write
  (`outputs`), runs the work in a capped child and answers with one JSON object.
- `worker`: the child. It caps its own memory before opening any file, does the work and prints one JSON line.
- `xlsx` (BetterOffice, xlrd for `.xls`), `docx_report` (read and the formatting report: BetterOffice plus
  python-docx), `docx_edit` (the careful replace and the paragraph diff), `wordxml` (shared Word XML helpers),
  `pdf_pages` (pypdfium2 page images), `office` and `winproc` (Word through PowerShell COM scripts, and the
  cleanup that kills only Office processes automation started).

The contract (agents and tests rely on it): stdout is one JSON object with `ok` (and `error` when false); exit
code 0 ok, 1 refused (a write outside the outputs folder, over an input or a read-only file, a Word paragraph it
won't edit), 2 failed (missing or unreadable file, bad arguments, out of memory). The outputs folder comes from
`HUB_DOC_OUTPUTS`; `HUB_DOC_MEMORY_LIMIT` (bytes) can only lower the memory cap.
"""
