from __future__ import annotations

import logging
import os
import tempfile

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


def _match_percent(nutr: dict[str, float], gpt: dict[str, float]) -> int:
    """Calculate how closely FS nutrition matches GPT estimate (0-100%)."""
    total_w = 0.0
    similarity = 0.0
    for key, w in (("calories", 2.0), ("protein", 1.0), ("fat", 1.0), ("carbs", 1.0)):
        total_w += w
        t = gpt.get(key, 0)
        f = nutr.get(key, 0)
        if t > 0:
            similarity += w * max(0, 1 - abs(f - t) / t)
        elif f == 0:
            similarity += w
    return round(similarity / total_w * 100)


def _format_matches(matches: list[dict], result: MealResult) -> str:
    lines = ["<b>Найдено в FatSecret:</b>\n"]
    for i, (item, m) in enumerate(zip(result.items, matches), 1):
        if m is None:
            lines.append(f"{i}. {item.name} — <i>не найдено</i>")
            continue
        nutr = m.get("nutrition")
        w = item.weight_g if item.weight_g else 1
        if nutr:
            gpt = {
                "calories": item.calories / w * 100,
                "protein": item.protein / w * 100,
                "fat": item.fat / w * 100,
                "carbs": item.carbs / w * 100,
            }
            pct = _match_percent(nutr, gpt)
            header = f'{i}. <b>{item.name}</b> → {m["food_name"]}  ({pct}%)'
            table = (
                f"<pre>"
                f"       Ккал   Б      Ж      У\n"
                f" FS:   {nutr['calories']:>5.0f}  {nutr['protein']:>5.1f}  {nutr['fat']:>5.1f}  {nutr['carbs']:>5.1f}\n"
                f" GPT:  {gpt['calories']:>5.0f}  {gpt['protein']:>5.1f}  {gpt['fat']:>5.1f}  {gpt['carbs']:>5.1f}"
                f"</pre>"
            )
            lines.append(f"{header}\n{table}")
        else:
            lines.append(f'{i}. <b>{item.name}</b> → {m["food_name"]}')
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


async def _process_food_text(
    text: str, message: Message, state: FSMContext, wait_msg: Message
) -> None:
    """Common logic: send text to LLM, show KBJU result."""
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


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

    if not await _ensure_authorized(message):
        return

    wait_msg = await message.answer("Распознаю голосовое...")

    tmp_path = None
    try:
        file = await bot.get_file(message.voice.file_id)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".oga")
        os.close(tmp_fd)
        await bot.download_file(file.file_path, tmp_path)

        text = await openai_svc.transcribe_voice(tmp_path)
    except Exception:
        log.exception("Voice transcription failed")
        await wait_msg.edit_text(
            "Не удалось распознать голосовое. Попробуй ещё раз или отправь текстом."
        )
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    await wait_msg.edit_text(f"Распознано: <i>{text}</i>\n\nСчитаю КБЖУ...")
    await _process_food_text(text, message, state, wait_msg)


@router.message(F.text, ~F.text.startswith("/"))
async def handle_text(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

    if not await _ensure_authorized(message):
        return

    wait_msg = await message.answer("Считаю КБЖУ...")
    await _process_food_text(message.text, message, state, wait_msg)


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
        w = item.weight_g if item.weight_g else 1
        target_per_100 = {
            "calories": item.calories / w * 100,
            "protein": item.protein / w * 100,
            "fat": item.fat / w * 100,
            "carbs": item.carbs / w * 100,
        }
        try:
            m = await fatsecret_svc.match_food(
                search_name=item.search_name,
                fallback_name=item.name,
                target=target_per_100,
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
