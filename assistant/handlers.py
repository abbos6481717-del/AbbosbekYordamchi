"""aiogram router: business updates go to the Assistant, owner commands in the bot chat."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .config import Settings
from .service import Assistant

log = logging.getLogger("assistant.handlers")

ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

STRANGER_START_TEXT = (
    "Salom! Bu shaxsiy yordamchi bot, u faqat egasi uchun ishlaydi. "
    "Bu yerda suhbatlashib bo'lmaydi — iltimos, egasiga to'g'ridan-to'g'ri yozing."
)


def build_router(assistant: Assistant, settings: Settings) -> Router:
    router = Router(name="assistant")
    owner_id = settings.owner_id
    private = F.chat.type == ChatType.PRIVATE
    is_owner = F.from_user.id == owner_id

    @router.business_connection()
    async def on_business_connection(business_connection: BusinessConnection) -> None:
        await assistant.on_business_connection(business_connection)

    @router.business_message()
    async def on_business_message(message: Message, bot: Bot) -> None:
        outcome = await assistant.on_business_message(bot, message)
        log.debug("Business message %s in chat %s: %s", message.message_id, message.chat.id, outcome.value)

    async def answer(message: Message, text: str) -> None:
        try:
            await message.answer(text)
        except TelegramAPIError as exc:
            log.warning("Could not answer in the bot chat: %s", exc)

    @router.message(CommandStart(), private, is_owner)
    async def owner_start(message: Message) -> None:
        await answer(message, assistant.start_text())

    @router.message(CommandStart(), private)
    async def stranger_start(message: Message) -> None:
        await answer(message, STRANGER_START_TEXT)

    @router.message(Command("pause"), private, is_owner)
    async def owner_pause(message: Message) -> None:
        assistant.pause()
        log.info("Paused by the owner")
        await answer(message, "Paused. Use /resume to continue.")

    @router.message(Command("resume"), private, is_owner)
    async def owner_resume(message: Message) -> None:
        assistant.resume()
        log.info("Resumed by the owner")
        await answer(message, "Resumed.\n\n" + assistant.status_text())

    @router.message(Command("status"), private, is_owner)
    async def owner_status(message: Message) -> None:
        await answer(message, assistant.status_text())

    @router.message()
    async def ignore_everything_else(message: Message) -> None:
        # Other direct messages (strangers, groups, owner's plain text) are ignored.
        return None

    return router
