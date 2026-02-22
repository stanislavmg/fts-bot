from __future__ import annotations

import asyncio
import logging
from functools import partial

from fatsecret import Fatsecret

import config

log = logging.getLogger(__name__)

# In-memory storage for Fatsecret instances during auth flow.
# Key: telegram_id, Value: Fatsecret instance (holds request_token between steps).
_auth_sessions: dict[int, Fatsecret] = {}


def _start_auth() -> tuple[Fatsecret, str]:
    fs = Fatsecret(config.FS_CONSUMER_KEY, config.FS_CONSUMER_SECRET)
    url = fs.get_authorize_url()
    return fs, url


def _complete_auth(fs: Fatsecret, pin: str) -> tuple[str, str]:
    session_token = fs.authenticate(pin)
    return session_token


def _get_client(session_token: tuple[str, str]) -> Fatsecret:
    return Fatsecret(
        config.FS_CONSUMER_KEY,
        config.FS_CONSUMER_SECRET,
        session_token=session_token,
    )


# ── Async wrappers for auth ──────────────────────────────────────

async def start_auth(telegram_id: int) -> str:
    fs, url = await asyncio.get_running_loop().run_in_executor(
        None, _start_auth
    )
    _auth_sessions[telegram_id] = fs
    return url


async def complete_auth(telegram_id: int, pin: str) -> tuple[str, str]:
    fs = _auth_sessions.pop(telegram_id, None)
    if fs is None:
        raise ValueError("No pending auth session")
    session_token = await asyncio.get_running_loop().run_in_executor(
        None, partial(_complete_auth, fs, pin)
    )
    return session_token


# ── Food search & diary ──────────────────────────────────────────

async def search_food(query: str) -> list[dict]:
    fs = Fatsecret(config.FS_CONSUMER_KEY, config.FS_CONSUMER_SECRET)
    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None, partial(fs.foods_search, query)
        )
    except (KeyError, TypeError):
        log.warning("FatSecret search returned no results for: %s", query)
        return []
    if results is None:
        return []
    if isinstance(results, dict):
        return [results]
    return results


async def get_food(session_token: tuple[str, str], food_id: str) -> dict:
    fs = _get_client(session_token)
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(fs.food_get, food_id)
    )


async def create_food_entry(
    session_token: tuple[str, str],
    *,
    food_id: str,
    food_entry_name: str,
    serving_id: str,
    number_of_units: float,
    meal: str = "other",
) -> str | None:
    fs = _get_client(session_token)
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        partial(
            fs.food_entry_create,
            food_id=food_id,
            food_entry_name=food_entry_name,
            serving_id=serving_id,
            number_of_units=number_of_units,
            meal=meal,
        ),
    )
    return result


def _find_gram_serving(servings: list[dict]) -> dict | None:
    """Find 100g or 1g serving among available servings."""
    for s in servings:
        desc = s.get("serving_description", "").lower()
        if "100" in desc and ("g" in desc or "gram" in desc):
            return s
    for s in servings:
        desc = s.get("serving_description", "").lower()
        if desc.startswith("1 g") or desc == "1g":
            return s
    return servings[0] if servings else None


async def log_food_item(
    session_token: tuple[str, str],
    name: str,
    weight_g: float,
    meal: str = "other",
    search_name: str = "",
) -> str | None:
    """Search for a food, pick the best match, and create a diary entry."""
    query = search_name or name
    results = await search_food(query)
    if not results and search_name:
        results = await search_food(name)
    if not results:
        return None

    food_id = results[0]["food_id"]
    food_detail = await get_food(session_token, food_id)

    servings_raw = food_detail.get("servings", {}).get("serving", [])
    if isinstance(servings_raw, dict):
        servings_raw = [servings_raw]

    serving = _find_gram_serving(servings_raw)
    if not serving:
        return None

    serving_id = serving["serving_id"]
    metric_amount = float(serving.get("metric_serving_amount", 100))
    units = weight_g / metric_amount

    return await create_food_entry(
        session_token,
        food_id=food_id,
        food_entry_name=name,
        serving_id=serving_id,
        number_of_units=units,
        meal=meal,
    )
