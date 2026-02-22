from __future__ import annotations

import logging

from aiogram import Router, F
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
    waiting_for_fs_confirm = State()


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


def _format_matches(matches: list[dict], result: MealResult) -> str:
    lines = ["<b>Найдено в FatSecret:</b>\n"]
    for i, (item, m) in enumerate(zip(result.items, matches), 1):
        if m is None:
            lines.append(f"{i}. {item.name} — <i>не найдено</i>")
            continue
        fs_cal = m.get("cal_per_100g")
        gpt_cal_100 = (item.calories / item.weight_g * 100) if item.weight_g else 0
        cal_str = f"{fs_cal:.0f}" if fs_cal else "?"
        lines.append(
            f'{i}. <b>{item.name}</b> → {m["food_name"]}\n'
            f"   FS: {cal_str} ккал/100г | GPT: {gpt_cal_100:.0f} ккал/100г"
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

FS_CONFIRM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Записать в дневник", callback_data="fs_confirm"
            ),
            InlineKeyboardButton(text="Отмена", callback_data="fs_cancel"),
        ],
    ]
)


async def _ensure_authorized(message: Message) -> bool:
    if not await db.is_user_authorized(message.from_user.id):
        await message.answer("Сначала авторизуйся в FatSecret: /auth")
        return False
    return True


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    await message.answer(
        "Голосовые сообщения пока не поддерживаются. Отправь текстом."
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
        await callback.message.edit_text(
            "Ошибка. Попробуй отправить сообщение заново."
        )
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

    await callback.answer("Ищу продукты в FatSecret...")
    await callback.message.edit_text(
        _format_kbju(result) + "\n\nИщу продукты в базе FatSecret...",
        parse_mode="HTML",
    )

    matches: list[dict | None] = []
    for item in result.items:
        cal_per_100 = (item.calories / item.weight_g * 100) if item.weight_g else 0
        try:
            m = await fatsecret_svc.match_food(
                search_name=item.search_name,
                fallback_name=item.name,
                target_cal_per_100g=cal_per_100,
            )
        except Exception:
            log.exception("FatSecret match failed for %s", item.name)
            m = None
        matches.append(m)

    matches_serializable = [m if m else None for m in matches]
    await state.update_data(
        fs_matches=matches_serializable,
        meal_type=meal_type,
    )
    await state.set_state(FoodStates.waiting_for_fs_confirm)

    text = _format_matches(matches, result)
    found = sum(1 for m in matches if m is not None)
    text += f"\n\nНайдено: {found} из {len(result.items)}"

    await callback.message.edit_text(
        text, reply_markup=FS_CONFIRM_KB, parse_mode="HTML"
    )


@router.callback_query(F.data == "fs_confirm")
async def on_fs_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    meal_labels = {
        "breakfast": "Завтрак",
        "lunch": "Обед",
        "dinner": "Ужин",
        "other": "Другое",
    }

    data = await state.get_data()
    meal_data = data.get("meal_result")
    matches = data.get("fs_matches", [])
    meal_type = data.get("meal_type", "other")

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

    await callback.answer("Сохраняю...")
    await callback.message.edit_text(
        _format_matches(matches, result) + "\n\nЗаписываю в дневник...",
        parse_mode="HTML",
    )

    errors = []
    skipped = []
    for item, m in zip(result.items, matches):
        if m is None:
            skipped.append(item.name)
            continue
        try:
            await fatsecret_svc.log_matched_food(
                session_token=tokens,
                food_id=m["food_id"],
                entry_name=item.name,
                weight_g=item.weight_g,
                meal=meal_type,
            )
        except Exception:
            log.exception("Failed to log %s to FatSecret", item.name)
            errors.append(item.name)

    await db.save_meal(
        callback.from_user.id, meal_data, logged_to_fs=len(errors) == 0
    )
    await state.clear()

    label = meal_labels.get(meal_type, meal_type)
    status_parts = [f"\n\nЗаписано в дневник ({label})!"]
    if skipped:
        status_parts.append(f"Не найдено в FatSecret: {', '.join(skipped)}")
    if errors:
        status_parts.append(f"Ошибка записи: {', '.join(errors)}")

    await callback.message.edit_text(
        _format_matches(matches, result) + "\n".join(status_parts),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "fs_cancel")
async def on_fs_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено.")
    await state.clear()
    await callback.message.edit_text("Запись в дневник отменена.")


@router.callback_query(F.data == "meal_cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено.")
    await state.clear()
    await callback.message.edit_text("Расчёт отменён.")
