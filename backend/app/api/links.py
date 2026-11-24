"""Opening a web link from a previewed document in the user's default browser (revision 4, attachments PR 3).

The desktop app's window must never navigate away from the hub, and the shell has no opener plugin, so the preview
pane sends a document's link here and the backend (on the user's PC, loopback only, behind the token) hands it to
Windows. Only http, https and mailto links: never a file, a UNC path or another program's protocol.
"""

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.attachments import FileOpener

router = APIRouter(prefix="/links", tags=["links"])

SCHEMES = {"http", "https", "mailto"}
MAX_LENGTH = 2048


class LinkRequest(BaseModel):
    url: str


def checked_link(url: str) -> str:
    """The link as given, or HTTPException 400 for anything but a plain http(s) or mailto link. Windows gets it
    through `os.startfile` (no shell), so characters such as `|`, `^` or `{` are fine once the scheme and host are;
    control characters, spaces and quotes are not part of a real link and are refused."""
    url = url.strip()
    refused = HTTPException(status.HTTP_400_BAD_REQUEST, "Only http, https and mailto links can be opened.")
    if len(url) > MAX_LENGTH or any(ord(c) < 0x21 or ord(c) == 0x7F or c in '"<>' for c in url):
        raise refused
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname
    except ValueError:  # such as an unclosed IPv6 bracket, http://[abc]/
        raise refused from None
    if scheme not in SCHEMES or (scheme != "mailto" and not host):
        raise refused
    return url


@router.post("/open", status_code=status.HTTP_204_NO_CONTENT)
def open_link(body: LinkRequest, request: Request) -> None:
    opener: FileOpener = request.app.state.open_file
    opener(checked_link(body.url), False)
