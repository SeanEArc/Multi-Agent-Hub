"""`hub-doc`'s child process: caps its own memory, does one job and prints one JSON line.

Started by `app.doctool.cli.run_child` with `{command, memory_limit, output?, pdf?, staging?, job?}` on stdin.
The cap comes first, before any document is opened, through PR 1's job-object code (`extract_worker.cap_memory`,
one process, the limit on committed memory); if it can't be set nothing is opened (fail closed). When the work
outgrows the cap, BetterOffice aborts this process outright: the parent sees no reply and reports it.
"""

import json
import os
import sys
from typing import Any

from app.doctool.contract import Failed, HubDocError, Refused, short


def handle(request: dict[str, Any]) -> dict[str, Any]:
    from app.doctool.cli import Command

    command = Command.from_json(request["command"])
    if request.get("job") == "pdf-pages":
        from app.doctool import pdf_pages

        return pdf_pages.render(request["pdf"], request["staging"], command.max_pages, command.scale)
    output = request.get("output")
    match command.format, command.action:
        case "xlsx", action:
            from app.doctool import xlsx

            return xlsx.run(action, command, output)
        case "docx", "read":
            from app.doctool import docx_report

            return docx_report.read(command.files[0], command.view)
        case "docx", "report":
            from app.doctool import docx_report

            return docx_report.report(command.files[0])
        case "docx", "edit":
            from app.doctool import docx_edit

            return docx_edit.replace(command.files[0], output, command.replaces)
        case "docx", "diff":
            from app.doctool import docx_edit

            return docx_edit.diff(command.files[0], command.files[1])
    raise Failed(f"unknown command {command.format} {command.action}")


def main() -> None:
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    try:
        request = json.loads(sys.stdin.buffer.read())
        try:
            from app.extract_worker import cap_memory

            cap_memory(int(request["memory_limit"]))
        except (OSError, KeyError, ValueError) as exc:
            raise Failed(f"hub-doc couldn't give its worker a memory limit ({exc}), so it opened nothing.") from None
        reply: dict[str, Any] = {"ok": True, **handle(request)}
    except Refused as exc:
        reply = {"ok": False, "refused": True, "error": str(exc)}
    except HubDocError as exc:
        reply = {"ok": False, "error": str(exc)}
    except MemoryError:
        reply = {"ok": False, "error": "the file is too large: the work needed more memory than hub-doc allows."}
    except Exception as exc:
        reply = {"ok": False, "error": f"{_describe(exc)}: {short(exc)}"}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _describe(exc: Exception) -> str:
    """A short kind for a library error: BetterOffice's ParseError means the file isn't what it claims to be."""
    name = exc.__class__.__name__
    if name == "ParseError" or name in ("BadZipFile", "PackageNotFoundError", "XLRDError"):
        return "the file couldn't be read (it may be damaged or not really this type)"
    return name
