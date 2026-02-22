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

from models import MealResult, FoodItem
from services import db, openai_svc, fatsecret_svc

router = Router()
log = logging.getLogger(__name__)


class FoodStates(StatesGroup):
    waiting_for_meal_choice = State()
    picking_fs_items = State()
    waiting_custom_query = State()
    waiting_for_fs_confirm = State()


# ── Formatting helpers ────────────────────────────────────────────

def _format_kbju(result: MealResult) -> str:
    lines = ["<b>Результат расчёта КБЖУ:</b>\n"]
    for i, item in enumerate(result.items, 1):
        lines.append(
            f"{i}. <b>{item.name}</b> ({item.weight_g:.0f} г)\n"
            f"   {item.calories:.0f} ккал \u00b7 Б {item.protein:.1f} \u00b7 "
            f"Ж {item.fat:.1f} \u00b7 У {item.carbs:.1f}"
        )
    lines.append(
        f"\n<b>Итого:</b> {result.total_calories:.0f} ккал \u00b7 "
        f"Б {result.total_protein:.1f} \u00b7 "
        f"Ж {result.total_fat:.1f} \u00b7 "
        f"У {result.total_carbs:.1f}"
    )
    return "\n".join(lines)


def _match_percent(nutr: dict[str, float], gpt: dict[str, float]) -> int:
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


def _gpt_per_100(item: FoodItem) -> dict[str, float]:
    w = item.weight_g if item.weight_g else 1
    return {
        "calories": item.calories / w * 100,
        "protein": item.protein / w * 100,
        "fat": item.fat / w * 100,
        "carbs": item.carbs / w * 100,
    }


def _fmt_kbju(n: dict[str, float]) -> str:
    return f"{n['calories']:.0f} ккал \u00b7 Б {n['protein']:.1f} \u00b7 Ж {n['fat']:.1f} \u00b7 У {n['carbs']:.1f}"


def _format_pick_message(
    item: FoodItem,
    candidates: list[dict],
    item_idx: int,
    total_items: int,
) -> str:
    gpt = _gpt_per_100(item)
    labels = ["1", "2", "3"]
    lines = [
        f"<b>[{item_idx + 1}/{total_items}] {item.name}</b> ({item.weight_g:.0f}г)",
        f"\U0001f4ca GPT на 100г: {_fmt_kbju(gpt)}",
    ]
    if not candidates:
        lines.append("\n<i>Ничего не найдено в FatSecret.</i>")
        return "\n".join(lines)

    lines.append("")
    for j, c in enumerate(candidates):
        nutr = c.get("nutrition", {})
        if not nutr:
            continue
        pct = _match_percent(nutr, gpt)
        if pct >= 80:
            badge = f"\u2705 {pct}%"
        elif pct >= 50:
            badge = f"\u26a0\ufe0f {pct}%"
        else:
            badge = f"\u2757 {pct}%"
        lbl = labels[j] if j < len(labels) else str(j)
        lines.append(f"<b>{lbl})</b> {badge} {c['food_name']}")
        lines.append(f"     {_fmt_kbju(nutr)}")

    return "\n".join(lines)


def _pick_keyboard(num_candidates: int) -> InlineKeyboardMarkup:
    labels = ["1", "2", "3"]
    buttons_row = []
    for j in range(min(num_candidates, 3)):
        buttons_row.append(
            InlineKeyboardButton(text=labels[j], callback_data=f"pick_{j}")
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons_row,
            [
                InlineKeyboardButton(text="\U0001f50d Свой запрос", callback_data="pick_custom"),
                InlineKeyboardButton(text="\u23ed Пропустить", callback_data="pick_skip"),
            ],
        ]
    )


def _format_summary(selections: list[dict | None], result: MealResult) -> str:
    lines = ["<b>Выбранные продукты:</b>\n"]
    for i, (item, sel) in enumerate(zip(result.items, selections), 1):
        if sel is None:
            lines.append(f"{i}. {item.name} — <i>пропущено</i>")
            continue
        nutr = sel.get("nutrition", {})
        gpt = _gpt_per_100(item)
        pct = _match_percent(nutr, gpt) if nutr else 0
        lines.append(f'{i}. <b>{item.name}</b> → {sel["food_name"]}  ({pct}%)')
    return "\n".join(lines)


# ── Static keyboards ─────────────────────────────────────────────


CONFIRM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Сохранить", callback_data="meal_save"),
            InlineKeyboardButton(text="Пересчитать", callback_data="meal_retry"),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="meal_cancel")],
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
            InlineKeyboardButton(text="Записать в дневник", callback_data="fs_confirm"),
            InlineKeyboardButton(text="Отмена", callback_data="fs_cancel"),
        ],
    ]
)


# ── Handlers ──────────────────────────────────────────────────────


async def _ensure_authorized(message: Message) -> bool:
    if not await db.is_user_authorized(message.from_user.id):
        await message.answer("Сначала авторизуйся в FatSecret: /auth")
        return False
    return True


async def _process_food_text(
    text: str, message: Message, state: FSMContext, wait_msg: Message
) -> None:
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


@router.message(F.text, FoodStates.waiting_custom_query)
async def on_custom_query(message: Message, state: FSMContext) -> None:
    """User typed a custom search query for the current food item."""
    query = message.text.strip()
    if not query:
        await message.answer("Пустой запрос. Попробуй ещё раз:")
        return

    data = await state.get_data()
    idx = data["current_item_idx"]
    meal_data = data["meal_result"]
    meal_data["items"][idx]["search_queries"] = [query]
    await state.update_data(meal_result=meal_data)
    await state.set_state(FoodStates.picking_fs_items)

    wait_msg = await message.answer("Ищу...")

    result = MealResult.model_validate(meal_data)
    item = result.items[idx]
    target = _gpt_per_100(item)

    tokens = data.get("fs_tokens")
    log.info("Custom search for '%s', query='%s'", item.name, query)
    candidates = []
    try:
        candidates = await fatsecret_svc.match_food_top(
            search_queries=[query],
            fallback_name=item.name,
            target=target,
            top_n=6,
            session_token=tokens,
        )
    except Exception:
        log.exception("FatSecret custom search failed for %s", item.name)
    log.info("Found %d candidates for '%s' (custom)", len(candidates), item.name)

    used = data.get("used_queries", {})
    exclude_ids = set(used.get(str(idx), {}).get("seen_food_ids", []))
    candidates = [c for c in candidates if c["food_id"] not in exclude_ids][:3]
    candidates.sort(
        key=lambda c: _match_percent(c.get("nutrition", {}), target), reverse=True
    )

    item_used = used.get(str(idx), {"seen_food_ids": [], "queries": []})
    for c in candidates:
        if c["food_id"] not in item_used["seen_food_ids"]:
            item_used["seen_food_ids"].append(c["food_id"])
    item_used["queries"] = list(set(item_used.get("queries", []) + [query]))
    used[str(idx)] = item_used
    await state.update_data(current_candidates=candidates, used_queries=used)

    text = _format_pick_message(item, candidates, idx, len(result.items))
    kb = _pick_keyboard(len(candidates)) if candidates else InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="\U0001f50d Свой запрос", callback_data="pick_custom"),
            InlineKeyboardButton(text="\u23ed Пропустить", callback_data="pick_skip"),
        ]]
    )
    await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


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
        await callback.message.edit_text("Ошибка. Попробуй отправить сообщение заново.")
        await state.clear()
        return

    await state.update_data(meal_result=result.model_dump())
    await callback.message.edit_text(
        _format_kbju(result), reply_markup=CONFIRM_KB, parse_mode="HTML"
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


# ── Meal type → start item-by-item picking ────────────────────────


async def _search_and_show_item(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Search FatSecret for the current item and show top-3 candidates."""
    data = await state.get_data()
    result = MealResult.model_validate(data["meal_result"])
    idx = data["current_item_idx"]
    item = result.items[idx]
    used = data.get("used_queries", {})

    await callback.message.edit_text(
        f"<b>[{idx + 1}/{len(result.items)}] {item.name}</b>\n\nИщу в FatSecret...",
        parse_mode="HTML",
    )

    target = _gpt_per_100(item)
    log.info(
        "Searching FatSecret for '%s', queries=%s", item.name, item.search_queries
    )
    tokens = data.get("fs_tokens")
    candidates = []
    try:
        candidates = await fatsecret_svc.match_food_top(
            search_queries=item.search_queries,
            fallback_name=item.name,
            target=target,
            top_n=6,
            session_token=tokens,
        )
    except Exception:
        log.exception("FatSecret search failed for %s", item.name)
    log.info("Found %d candidates for '%s'", len(candidates), item.name)

    exclude_ids = set(used.get(str(idx), {}).get("seen_food_ids", []))
    candidates = [c for c in candidates if c["food_id"] not in exclude_ids][:3]
    candidates.sort(
        key=lambda c: _match_percent(c.get("nutrition", {}), target), reverse=True
    )

    item_used = used.get(str(idx), {"seen_food_ids": [], "queries": []})
    for c in candidates:
        if c["food_id"] not in item_used["seen_food_ids"]:
            item_used["seen_food_ids"].append(c["food_id"])
    item_used["queries"] = list(set(item_used.get("queries", []) + item.search_queries))
    used[str(idx)] = item_used
    await state.update_data(
        current_candidates=candidates,
        used_queries=used,
    )

    text = _format_pick_message(item, candidates, idx, len(result.items))
    kb = _pick_keyboard(len(candidates)) if candidates else InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="\U0001f50d Свой запрос", callback_data="pick_custom"),
            InlineKeyboardButton(text="\u23ed Пропустить", callback_data="pick_skip"),
        ]]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("mt_"))
async def on_meal_type(callback: CallbackQuery, state: FSMContext) -> None:
    meal_type = callback.data.removeprefix("mt_")
    data = await state.get_data()
    meal_data = data.get("meal_result")
    if not meal_data:
        await callback.answer("Нет данных.")
        await state.clear()
        return

    tokens = await db.get_access_tokens(callback.from_user.id)
    if not tokens:
        await callback.answer("Не авторизован в FatSecret.")
        await state.clear()
        return

    result = MealResult.model_validate(meal_data)
    await callback.answer("Ищу продукты...")
    await state.update_data(
        meal_type=meal_type,
        fs_tokens=tokens,
        current_item_idx=0,
        item_selections=[None] * len(result.items),
        used_queries={},
    )
    await state.set_state(FoodStates.picking_fs_items)
    await _search_and_show_item(callback, state)


# ── Pick handlers ─────────────────────────────────────────────────


async def _advance_to_next_item(callback: CallbackQuery, state: FSMContext) -> None:
    """Move to the next item or show final summary."""
    data = await state.get_data()
    result = MealResult.model_validate(data["meal_result"])
    idx = data["current_item_idx"] + 1

    if idx >= len(result.items):
        selections = data["item_selections"]
        await state.update_data(item_selections=selections)
        await state.set_state(FoodStates.waiting_for_fs_confirm)

        text = _format_summary(selections, result)
        selected = sum(1 for s in selections if s is not None)
        text += f"\n\nВыбрано: {selected} из {len(result.items)}"
        await callback.message.edit_text(
            text, reply_markup=FS_CONFIRM_KB, parse_mode="HTML"
        )
        return

    await state.update_data(current_item_idx=idx)
    await _search_and_show_item(callback, state)


@router.callback_query(F.data.startswith("pick_"), FoodStates.picking_fs_items)
async def on_pick(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.removeprefix("pick_")
    data = await state.get_data()
    idx = data["current_item_idx"]
    selections = data["item_selections"]
    candidates = data.get("current_candidates", [])

    if action == "skip":
        await callback.answer("Пропущено")
        selections[idx] = None
        await state.update_data(item_selections=selections)
        await _advance_to_next_item(callback, state)
        return

    if action == "custom":
        await callback.answer()
        result = MealResult.model_validate(data["meal_result"])
        item = result.items[idx]
        await state.set_state(FoodStates.waiting_custom_query)
        await callback.message.answer(
            f"Введи поисковый запрос для <b>{item.name}</b> (на английском лучше):",
            parse_mode="HTML",
        )
        return

    try:
        pick_idx = int(action)
    except ValueError:
        await callback.answer("Ошибка.")
        return

    if pick_idx >= len(candidates):
        await callback.answer("Вариант недоступен.")
        return

    await callback.answer("Выбрано!")
    selections[idx] = candidates[pick_idx]
    await state.update_data(item_selections=selections)
    await _advance_to_next_item(callback, state)


# ── Final confirm / cancel ────────────────────────────────────────


@router.callback_query(F.data == "fs_confirm")
async def on_fs_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    meal_labels = {
        "breakfast": "Завтрак", "lunch": "Обед",
        "dinner": "Ужин", "other": "Другое",
    }

    data = await state.get_data()
    meal_data = data.get("meal_result")
    selections = data.get("item_selections", [])
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
        _format_summary(selections, result) + "\n\nЗаписываю в дневник...",
        parse_mode="HTML",
    )

    errors = []
    skipped = []
    for item, sel in zip(result.items, selections):
        if sel is None:
            skipped.append(item.name)
            continue
        try:
            await fatsecret_svc.log_matched_food(
                session_token=tokens,
                food_id=sel["food_id"],
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
        status_parts.append(f"Пропущено: {', '.join(skipped)}")
    if errors:
        status_parts.append(f"Ошибка записи: {', '.join(errors)}")

    await callback.message.edit_text(
        _format_summary(selections, result) + "\n".join(status_parts),
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
