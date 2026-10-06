"""The shop service entry point."""

import logging

from fastapi import FastAPI

from app.api import orders
from app.config import get_settings

logging.basicConfig(level=get_settings().log_level)
app = FastAPI(title="acme shop")
app.include_router(orders.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
