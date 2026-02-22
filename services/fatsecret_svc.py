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


def _parse_calories_from_desc(desc: str) -> float | None:
    """Extract 'Per 100g' calories from food_description like 'Per 100g - Calories: 116kcal | ...'."""
    if not desc:
        return None
    import re
    m = re.search(r"Calories:\s*([\d.]+)\s*kcal", desc, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


async def match_food(
    search_name: str,
    fallback_name: str,
    target_cal_per_100g: float,
) -> dict | None:
    """Search FatSecret, return the best match by calories proximity.

    Returns dict with keys: food_id, food_name, cal_per_100g, description.
    """
    query = search_name or fallback_name
    results = await search_food(query)
    if not results and search_name:
        results = await search_food(fallback_name)
    if not results:
        return None

    best = None
    best_diff = float("inf")
    for item in results[:10]:
        cal = _parse_calories_from_desc(item.get("food_description", ""))
        if cal is None:
            continue
        diff = abs(cal - target_cal_per_100g)
        if diff < best_diff:
            best_diff = diff
            best = {
                "food_id": item["food_id"],
                "food_name": item.get("food_name", query),
                "cal_per_100g": cal,
                "description": item.get("food_description", ""),
            }

    if best is None and results:
        best = {
            "food_id": results[0]["food_id"],
            "food_name": results[0].get("food_name", query),
            "cal_per_100g": None,
            "description": results[0].get("food_description", ""),
        }

    return best


async def log_matched_food(
    session_token: tuple[str, str],
    food_id: str,
    entry_name: str,
    weight_g: float,
    meal: str = "other",
) -> str | None:
    """Create a diary entry for an already-matched food_id."""
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
        food_entry_name=entry_name,
        serving_id=serving_id,
        number_of_units=units,
        meal=meal,
    )
