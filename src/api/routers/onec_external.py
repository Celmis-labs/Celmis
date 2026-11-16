"""Upload of a 1C file for `inspect_external`.

The MCP tool hands its caller a one-time URL; the caller's agent posts the
file there (`curl -F file=@<path> <url>`). The ticket in the URL is the whole
credential: random, single use, short-lived and bound to the caller it was
issued to, so the upload needs no session and the file is readable by that
caller alone. The answer is plain text for the agent that ran the command.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import PlainTextResponse

from src.onec import store
from src.onec.container import is_container

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/onec/external/{ticket}", response_class=PlainTextResponse)
async def upload(ticket: str, file: UploadFile = File(...)) -> PlainTextResponse:
    data = await file.read(store.MAX_BYTES + 1)
    if len(data) > store.MAX_BYTES:
        return PlainTextResponse(
            f"refused: the file is larger than {store.MAX_BYTES // (1024 * 1024)} MB\n",
            status_code=413)
    if not is_container(data):
        return PlainTextResponse(
            "refused: not a 1C file (.erf, .epf or Form.bin); the link is still valid\n",
            status_code=415)
    try:
        saved = store.redeem(ticket, data, file.filename or "upload")
    except store.StoreError as exc:
        return PlainTextResponse(f"refused: {exc}\n", status_code=404)
    logger.info("onec_external_upload handle=%s bytes=%d", saved.handle, saved.size)
    return PlainTextResponse(
        f"stored {saved.file_name} ({saved.size} bytes) as {saved.handle}, kept 24 h\n"
        f'next: inspect_external(file="{saved.handle}", repo="<configuration repository>")\n',
        status_code=201)
