import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject, Update

import config
from handlers import auth, food, diary
from services.db import init_db

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not config.ALLOWED_USERS:
            return await handler(event, data)

        user_id = None
        if isinstance(event, Update):
            if event.message:
                user_id = event.message.from_user.id
            elif event.callback_query:
                user_id = event.callback_query.from_user.id

        if user_id and user_id not in config.ALLOWED_USERS:
            log.warning("Access denied for user %s", user_id)
            return None

        return await handler(event, data)


RESTART_DELAY = 5


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    await init_db()

    bot = Bot(
        token=config.API_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AccessMiddleware())

    dp.include_router(auth.router)
    dp.include_router(food.router)
    dp.include_router(diary.router)

    log.info("Bot started, allowed users: %s", config.ALLOWED_USERS or "all")
    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            log.exception("Polling crashed, restarting in %ds...", RESTART_DELAY)
            await asyncio.sleep(RESTART_DELAY)
        else:
            log.info("Polling stopped gracefully, shutting down")
            break


if __name__ == "__main__":
    asyncio.run(main())
