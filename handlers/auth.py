import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from services import db, fatsecret_svc

router = Router()
log = logging.getLogger(__name__)


class AuthStates(StatesGroup):
    waiting_for_pin = State()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    authorized = await db.is_user_authorized(message.from_user.id)
    if authorized:
        await message.answer(
            "Привет! Ты уже авторизован в FatSecret.\n"
            "Отправь мне текстовое или голосовое сообщение с описанием еды, "
            "и я посчитаю КБЖУ."
        )
    else:
        await message.answer(
            "Привет! Я бот для подсчёта КБЖУ и логирования еды в FatSecret.\n\n"
            "Для начала нужно авторизоваться — отправь /auth"
        )


@router.message(Command("auth"))
async def cmd_auth(message: Message, state: FSMContext) -> None:
    authorized = await db.is_user_authorized(message.from_user.id)
    if authorized:
        await message.answer("Ты уже авторизован! Можешь отправлять еду.")
        return

    try:
        auth_url = await fatsecret_svc.start_auth(message.from_user.id)
    except Exception:
        log.exception("Failed to get FatSecret auth URL")
        await message.answer("Ошибка при подключении к FatSecret. Попробуй позже.")
        return

    await message.answer(
        "Перейди по ссылке и авторизуйся в FatSecret:\n"
        f"{auth_url}\n\n"
        "После авторизации тебе покажут PIN-код. Отправь его мне."
    )
    await state.set_state(AuthStates.waiting_for_pin)


@router.message(AuthStates.waiting_for_pin, F.text)
async def process_pin(message: Message, state: FSMContext) -> None:
    pin = message.text.strip()

    try:
        session_token = await fatsecret_svc.complete_auth(
            message.from_user.id, pin
        )
    except ValueError:
        await message.answer("Сессия авторизации истекла. Отправь /auth ещё раз.")
        await state.clear()
        return
    except Exception:
        log.exception("Failed to exchange FatSecret tokens")
        await message.answer(
            "Неверный PIN или ошибка авторизации. Попробуй /auth заново."
        )
        await state.clear()
        return

    await db.save_access_tokens(
        message.from_user.id, session_token[0], session_token[1]
    )
    await state.clear()
    await message.answer(
        "Авторизация прошла успешно!\n"
        "Теперь отправляй описание еды текстом или голосовым, "
        "и я посчитаю КБЖУ."
    )
