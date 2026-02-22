from __future__ import annotations

import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

import config
from models import MealResult

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _client


SYSTEM_PROMPT = """\
Ты диетолог-нутрициолог. Пользователь описывает, что он съел.
Верни JSON со списком продуктов и их КБЖУ на указанный вес.
Если вес не указан, оцени стандартную порцию и укажи её вес.
Используй данные из общепринятых таблиц калорийности.

Формат ответа (строго JSON, без markdown):
{
  "items": [
    {
      "name": "Название продукта",
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
Названия продуктов — на русском языке.\
"""

MEAL_RESULT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "meal_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "weight_g": {"type": "number"},
                            "calories": {"type": "number"},
                            "protein": {"type": "number"},
                            "fat": {"type": "number"},
                            "carbs": {"type": "number"},
                        },
                        "required": ["name", "weight_g", "calories", "protein", "fat", "carbs"],
                        "additionalProperties": False,
                    },
                },
                "total_calories": {"type": "number"},
                "total_protein": {"type": "number"},
                "total_fat": {"type": "number"},
                "total_carbs": {"type": "number"},
            },
            "required": ["items", "total_calories", "total_protein", "total_fat", "total_carbs"],
            "additionalProperties": False,
        },
    },
}


async def transcribe_voice(file_path: Path) -> str:
    client = _get_client()
    with open(file_path, "rb") as f:
        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru",
        )
    return transcription.text


async def calculate_kbju(text: str) -> MealResult:
    client = _get_client()
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format=MEAL_RESULT_SCHEMA,
        temperature=0.3,
    )
    raw = response.choices[0].message.content
    data = json.loads(raw)
    return MealResult.model_validate(data)
