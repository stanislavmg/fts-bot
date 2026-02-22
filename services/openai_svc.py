from __future__ import annotations

import json
import logging
import re

from openai import AsyncOpenAI

import config
from models import MealResult

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


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
      "search_name": "product name in English",
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
search_name — название на английском для поиска в базе данных (например: "lentils", "cheddar cheese", "butter").\
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
