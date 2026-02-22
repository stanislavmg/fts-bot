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
        token, secret = await fatsecret_svc.get_request_token()
    except Exception:
        log.exception("Failed to get FatSecret request token")
        await message.answer("Ошибка при подключении к FatSecret. Попробуй позже.")
        return

    await db.save_request_tokens(message.from_user.id, token, secret)

    auth_url = fatsecret_svc.get_authorize_url(token)
    await message.answer(
        "Перейди по ссылке и авторизуйся в FatSecret:\n"
        f"{auth_url}\n\n"
        "После авторизации тебе покажут PIN-код. Отправь его мне."
    )
    await state.set_state(AuthStates.waiting_for_pin)


@router.message(AuthStates.waiting_for_pin, F.text)
async def process_pin(message: Message, state: FSMContext) -> None:
    pin = message.text.strip()
    tokens = await db.get_request_tokens(message.from_user.id)
    if not tokens:
        await message.answer("Сессия авторизации истекла. Отправь /auth ещё раз.")
        await state.clear()
        return

    request_token, request_secret = tokens
    try:
        access_token, access_secret = await fatsecret_svc.get_access_token(
            request_token, request_secret, pin
        )
    except Exception:
        log.exception("Failed to exchange FatSecret tokens")
        await message.answer(
            "Неверный PIN или ошибка авторизации. Попробуй /auth заново."
        )
        await state.clear()
        return

    await db.save_access_tokens(message.from_user.id, access_token, access_secret)
    await state.clear()
    await message.answer(
        "Авторизация прошла успешно!\n"
        "Теперь отправляй описание еды текстом или голосовым, "
        "и я посчитаю КБЖУ."
    )
