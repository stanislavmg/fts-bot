import json
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from services import db

router = Router()
log = logging.getLogger(__name__)


@router.message(Command("diary"))
async def cmd_diary(message: Message) -> None:
    """Placeholder for viewing today's logged meals from local DB."""
    if not await db.is_user_authorized(message.from_user.id):
        await message.answer("Сначала авторизуйся: /auth")
        return

    await message.answer(
        "Дневник пока доступен в приложении FatSecret.\n"
        "Все записи, сохранённые через бота, попадают туда автоматически."
    )
