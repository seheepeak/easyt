"""Files sent to the Telegram chat the bot answers."""

import os
from pathlib import Path

from telegram import Bot

from easyt import log


async def send_document(path: Path, caption: str) -> None:
    """Send `path` to TELEGRAM_CHAT_ID with `caption`, unless the bot is not set up."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        return

    async with Bot(token=token) as bot:
        result = await bot.send_document(chat_id=chat_id, document=path, caption=caption)
    log.success(f"Sent document to telegram: {result.message_id}")
