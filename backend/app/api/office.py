"""Microsoft Office status for Settings (`app.office`): "found", "not found" or "checking"."""

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/office", tags=["office"])


@router.get("")
def office_status(request: Request) -> dict[str, Any]:
    return request.app.state.office.snapshot()


@router.post("/check")
async def check_office(request: Request) -> dict[str, Any]:
    """Run the test conversion now (or join the one running) and answer when it has finished."""
    return await request.app.state.office.check()
