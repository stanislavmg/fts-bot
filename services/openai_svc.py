from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from pathlib import Path

from openai import AsyncOpenAI

import config
from models import MealResult

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
_whisper_model = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            max_retries=2,
            timeout=30.0,
        )
    return _client


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model (base)...")
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        log.info("Whisper model loaded")
    return _whisper_model


SYSTEM_PROMPT = """\
Ты диетолог-нутрициолог. Пользователь описывает, что он съел.
Верни JSON со списком продуктов и их КБЖУ на указанный вес.
Если вес не указан, оцени стандартную порцию и укажи её вес.
Используй данные из общепринятых таблиц калорийности.

Ответь ТОЛЬКО валидным JSON без markdown-обёртки, без ```json```, без пояснений:
{
  "items": [
    {
      "name": "Название продукта",
      "search_queries": ["query1", "query2", "query3"],
      "weight_g": 200,
      "calories": 330,
      "protein": 62,
      "fat": 7.2,
      "carbs": 0
    }
  ],
  "total_calories": 330,
  "total_protein": 62,
  "total_fat": 7.2,
  "total_carbs": 0
}

Totals — это сумма по всем items. Все числовые значения — float.
name — название на русском языке.
search_queries — ровно 3 варианта названия на английском для поиска в базе данных продуктов. От конкретного к общему.
Примеры:
- Сывороточный протеин: ["whey protein isolate", "whey protein powder", "protein powder"]
- Гречка варёная: ["buckwheat cooked", "buckwheat groats boiled", "buckwheat"]
- Сыр чеддер: ["cheddar cheese", "cheddar", "hard cheese"]\
"""


def _extract_json(text: str) -> dict:
    """Extract JSON from model response, stripping markdown fences if present."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


async def calculate_kbju(text: str) -> MealResult:
    client = _get_client()
    response = await client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        timeout=30.0,
    )
    if not response.choices:
        log.error("LLM returned no choices: %s", response)
        raise ValueError("LLM returned empty response")
    raw = response.choices[0].message.content
    log.info("LLM raw response: %s", raw[:500] if raw else "<None>")
    if not raw:
        raise ValueError("LLM returned empty content")
    data = _extract_json(raw)
    return MealResult.model_validate(data)


def _transcribe_sync(file_path: str) -> str:
    model = _get_whisper()
    segments, info = model.transcribe(file_path, language="ru", beam_size=3)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


async def transcribe_voice(file_path: str | Path) -> str:
    """Transcribe an audio file using local faster-whisper."""
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(None, partial(_transcribe_sync, str(file_path)))
    log.info("Whisper transcription: %s", text[:200])
    if not text:
        raise ValueError("Whisper returned empty transcription")
    return text


MATCH_PROMPT = """\
Пользователь ищет продукт "{name}" в базе данных FatSecret.
Ориентировочное КБЖУ на 100г (от первичной оценки):
  Ккал: {cal}, Белки: {prot}г, Жиры: {fat}г, Углеводы: {carbs}г

Ниже список найденных позиций из базы (food_id, название, описание с КБЖУ порции).
Выбери 3 наиболее подходящих по смыслу и КБЖУ. Для каждого пересчитай КБЖУ на 100 грамм.

Ответь ТОЛЬКО валидным JSON-массивом без markdown-обёртки, без пояснений:
[
  {{"food_id": "12345", "food_name": "Название", "calories": 377.0, "protein": 83.0, "fat": 3.3, "carbs": 6.7}},
  ...
]

Все числа — float, КБЖУ строго на 100г. Если подходящих меньше 3, верни сколько есть.
Позиции из базы:\n{items}\
"""


async def pick_best_matches(
    item_name: str,
    gpt_per_100g: dict[str, float],
    fs_results: list[dict],
) -> list[dict]:
    """Ask LLM to pick top-3 matches from FatSecret search results."""
    compact = "\n".join(
        f"- food_id={r['food_id']} | {r.get('food_name', '?')} | {r.get('food_description', '')}"
        for r in fs_results
    )
    prompt = MATCH_PROMPT.format(
        name=item_name,
        cal=gpt_per_100g.get("calories", 0),
        prot=gpt_per_100g.get("protein", 0),
        fat=gpt_per_100g.get("fat", 0),
        carbs=gpt_per_100g.get("carbs", 0),
        items=compact,
    )
    client = _get_client()
    response = await client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        timeout=30.0,
    )
    if not response.choices:
        log.warning("LLM returned no choices for match picking")
        return []
    raw = response.choices[0].message.content
    log.info("LLM match response for '%s': %s", item_name, raw[:500] if raw else "<None>")
    if not raw:
        return []
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        log.exception("Failed to parse LLM match response")
        return []
    if not isinstance(data, list):
        return []
    results = []
    for entry in data[:3]:
        if not isinstance(entry, dict) or "food_id" not in entry:
            continue
        results.append({
            "food_id": str(entry["food_id"]),
            "food_name": entry.get("food_name", ""),
            "nutrition": {
                "calories": float(entry.get("calories", 0)),
                "protein": float(entry.get("protein", 0)),
                "fat": float(entry.get("fat", 0)),
                "carbs": float(entry.get("carbs", 0)),
            },
        })
    return results
