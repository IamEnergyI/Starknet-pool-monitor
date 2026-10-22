"""FastAPI entry point for the Render web service.

Telegram updates are delivered to ``POST /webhook``.  An external cron service
calls ``GET /scan`` every 60 seconds.  No polling loop is used.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import CallbackContext

import bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Telegram and register the webhook while the service runs."""

    telegram_app = await bot.initialize()
    app.state.telegram_app = telegram_app
    logger.info("Application startup complete")

    try:
        yield
    finally:
        await bot.shutdown()
        logger.info("Application shutdown complete")


app = FastAPI(
    title="Crypto Scanner Bot",
    version="1.0.0",
    lifespan=lifespan,
)


def _telegram_application(request: Request) -> Any:
    telegram_app = getattr(request.app.state, "telegram_app", None)
    if telegram_app is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram application is not initialized",
        )
    return telegram_app


@app.get("/")
async def health_check() -> dict[str, str]:
    return {"status": "Bot is running"}


@app.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    """Receive one Telegram update through the webhook."""

    telegram_app = _telegram_application(request)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed client payload
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Telegram update must be a JSON object",
        )

    try:
        update = Update.de_json(payload, telegram_app.bot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not deserialize Telegram update: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Telegram update",
        ) from exc

    if update is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty Telegram update",
        )

    try:
        await telegram_app.process_update(update)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process Telegram update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process update",
        ) from exc

    return {"ok": True}


@app.get("/scan")
async def scan(request: Request) -> dict[str, Any]:
    """Cron endpoint: run one scan across every active user's filter."""

    telegram_app = _telegram_application(request)

    # Render's cron can overlap if a scan runs longer than 60 seconds.  Do not
    # queue duplicate scans; report a successful no-op instead.
    if _scan_lock.locked():
        logger.warning("Scan request skipped because a scan is already running")
        return {
            "status": "success",
            "skipped": True,
            "reason": "scan already in progress",
        }

    async with _scan_lock:
        try:
            # A generic context is sufficient: trigger_scan only needs context.bot.
            context = CallbackContext(application=telegram_app)
            stats = await bot.trigger_scan(context)
        except Exception:  # noqa: BLE001
            logger.exception("Scheduled scan failed")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "status": "error",
                    "detail": "Scan failed; check server logs.",
                },
            )

    return {"status": "success", **stats}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
