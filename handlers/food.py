from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from models import MealResult
from services import db, openai_svc, fatsecret_svc

router = Router()
log = logging.getLogger(__name__)


class FoodStates(StatesGroup):
    waiting_for_meal_choice = State()


def _format_kbju(result: MealResult) -> str:
    lines = ["<b>Результат расчёта КБЖУ:</b>\n"]
    for i, item in enumerate(result.items, 1):
        lines.append(
            f"{i}. <b>{item.name}</b> ({item.weight_g:.0f} г)\n"
            f"   К: {item.calories:.0f} | Б: {item.protein:.1f} | "
            f"Ж: {item.fat:.1f} | У: {item.carbs:.1f}"
        )
    lines.append(
        f"\n<b>Итого:</b> {result.total_calories:.0f} ккал | "
        f"Б: {result.total_protein:.1f} | "
        f"Ж: {result.total_fat:.1f} | "
        f"У: {result.total_carbs:.1f}"
    )
    return "\n".join(lines)


CONFIRM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Сохранить", callback_data="meal_save"),
            InlineKeyboardButton(text="Пересчитать", callback_data="meal_retry"),
        ],
        [
            InlineKeyboardButton(text="Отмена", callback_data="meal_cancel"),
        ],
    ]
)

MEAL_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Завтрак", callback_data="mt_breakfast"),
            InlineKeyboardButton(text="Обед", callback_data="mt_lunch"),
        ],
        [
            InlineKeyboardButton(text="Ужин", callback_data="mt_dinner"),
            InlineKeyboardButton(text="Другое", callback_data="mt_other"),
        ],
    ]
)


async def _ensure_authorized(message: Message) -> bool:
    if not await db.is_user_authorized(message.from_user.id):
        await message.answer(
            "Сначала авторизуйся в FatSecret: /auth"
        )
        return False
    return True


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await _ensure_authorized(message):
        return

    wait_msg = await message.answer("Распознаю голос...")

    file = await bot.get_file(message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    await bot.download_file(file.file_path, destination=tmp_path)

    try:
        text = await openai_svc.transcribe_voice(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    await wait_msg.edit_text(f"Распознано: <i>{text}</i>\n\nСчитаю КБЖУ...", parse_mode="HTML")

    try:
        result = await openai_svc.calculate_kbju(text)
    except Exception:
        log.exception("GPT KBJU calculation failed")
        await wait_msg.edit_text("Ошибка при расчёте КБЖУ. Попробуй ещё раз.")
        return

    await state.update_data(
        original_text=text,
        meal_result=result.model_dump(),
    )
    await wait_msg.edit_text(
        _format_kbju(result),
        reply_markup=CONFIRM_KB,
        parse_mode="HTML",
    )


@router.message(F.text, ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

    if not await _ensure_authorized(message):
        return

    wait_msg = await message.answer("Считаю КБЖУ...")

    try:
        result = await openai_svc.calculate_kbju(message.text)
    except Exception:
        log.exception("GPT KBJU calculation failed")
        await wait_msg.edit_text("Ошибка при расчёте КБЖУ. Попробуй ещё раз.")
        return

    await state.update_data(
        original_text=message.text,
        meal_result=result.model_dump(),
    )
    await wait_msg.edit_text(
        _format_kbju(result),
        reply_markup=CONFIRM_KB,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "meal_retry")
async def on_retry(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    original_text = data.get("original_text")
    if not original_text:
        await callback.answer("Нет данных для пересчёта.")
        return

    await callback.answer("Пересчитываю...")
    await callback.message.edit_text("Пересчитываю КБЖУ...")

    try:
        result = await openai_svc.calculate_kbju(original_text)
    except Exception:
        log.exception("GPT retry failed")
        await callback.message.edit_text("Ошибка. Попробуй отправить сообщение заново.")
        await state.clear()
        return

    await state.update_data(meal_result=result.model_dump())
    await callback.message.edit_text(
        _format_kbju(result),
        reply_markup=CONFIRM_KB,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "meal_save")
async def on_save(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    meal_data = data.get("meal_result")
    if not meal_data:
        await callback.message.edit_text("Нет данных для сохранения.")
        await state.clear()
        return

    result = MealResult.model_validate(meal_data)
    text = _format_kbju(result) + "\n\nВыбери тип приёма пищи:"
    await state.set_state(FoodStates.waiting_for_meal_choice)
    await callback.message.edit_text(text, reply_markup=MEAL_KB, parse_mode="HTML")


@router.callback_query(F.data.startswith("mt_"))
async def on_meal_type(callback: CallbackQuery, state: FSMContext) -> None:
    meal_type = callback.data.removeprefix("mt_")
    meal_labels = {
        "breakfast": "Завтрак",
        "lunch": "Обед",
        "dinner": "Ужин",
        "other": "Другое",
    }

    data = await state.get_data()
    meal_data = data.get("meal_result")
    if not meal_data:
        await callback.answer("Нет данных.")
        await state.clear()
        return

    result = MealResult.model_validate(meal_data)
    tokens = await db.get_access_tokens(callback.from_user.id)
    if not tokens:
        await callback.answer("Не авторизован в FatSecret.")
        await state.clear()
        return

    session_token = tokens
    await callback.answer("Сохраняю в FatSecret...")
    await callback.message.edit_text(
        _format_kbju(result) + "\n\nСохраняю в дневник FatSecret...",
        parse_mode="HTML",
    )

    errors = []
    for item in result.items:
        try:
            await fatsecret_svc.log_food_item(
                session_token,
                name=item.name,
                weight_g=item.weight_g,
                meal=meal_type,
            )
        except Exception:
            log.exception("Failed to log %s to FatSecret", item.name)
            errors.append(item.name)

    await db.save_meal(callback.from_user.id, meal_data, logged_to_fs=len(errors) == 0)
    await state.clear()

    if errors:
        await callback.message.edit_text(
            _format_kbju(result)
            + f"\n\nЗаписано в дневник ({meal_labels.get(meal_type, meal_type)})."
            + f"\nНе удалось найти в FatSecret: {', '.join(errors)}",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            _format_kbju(result)
            + f"\n\nУспешно записано в дневник ({meal_labels.get(meal_type, meal_type)})!",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "meal_cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено.")
    await state.clear()
    await callback.message.edit_text("Расчёт отменён.")
