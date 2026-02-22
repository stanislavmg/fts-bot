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

def _search_page_sync(
    query: str, page: int, session_token: tuple[str, str] | None = None
) -> list[dict]:
    if session_token:
        fs = _get_client(session_token)
    else:
        fs = Fatsecret(config.FS_CONSUMER_KEY, config.FS_CONSUMER_SECRET)
    params = {
        "method": "foods.search",
        "search_expression": query,
        "page_number": str(page),
        "max_results": "50",
        "region": "RU",
        "language": "ru",
        "format": "json",
    }
    response = fs.session.get(fs.api_url, params=params)
    data = response.json()
    if "error" in data:
        log.warning("FatSecret API error: %s", data["error"])
        return []
    foods = data.get("foods", {})
    food_list = foods.get("food", [])
    if food_list is None:
        return []
    if isinstance(food_list, dict):
        return [food_list]
    return food_list


async def search_food(
    query: str, session_token: tuple[str, str] | None = None
) -> list[dict]:
    """Search FatSecret for a query (up to 50 results, region=RU)."""
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None, partial(_search_page_sync, query, 0, session_token)
        )
    except (KeyError, TypeError):
        log.warning("FatSecret search returned no results for: %s", query)
        return []
    except Exception:
        log.exception("FatSecret search failed for: %s", query)
        return []


def _clean_russian_query(name: str) -> str:
    """Strip parenthetical parts and extra whitespace for better FatSecret search."""
    import re
    cleaned = re.sub(r"\s*\([^)]*\)", "", name).strip()
    return cleaned if cleaned else name


async def search_food_multi(
    queries: list[str],
    fallback_name: str = "",
    session_token: tuple[str, str] | None = None,
) -> list[dict]:
    """Search multiple queries in parallel, deduplicate by food_id."""
    ru_query = ""
    if fallback_name:
        cleaned = _clean_russian_query(fallback_name)
        if cleaned not in queries:
            ru_query = cleaned

    tasks = [search_food(q, session_token=session_token) for q in queries]
    if ru_query:
        tasks.append(search_food(ru_query, session_token=session_token))

    all_queries = list(queries) + ([ru_query] if ru_query else [])
    all_results = await asyncio.gather(*tasks)

    for q, results in zip(all_queries, all_results):
        log.info("FatSecret search '%s' → %d results", q, len(results))

    seen: set[str] = set()
    combined: list[dict] = []
    for results in all_results:
        for item in results:
            fid = item.get("food_id")
            if fid not in seen:
                seen.add(fid)
                combined.append(item)
    log.info("FatSecret combined: %d unique results from %d queries", len(combined), len(all_queries))
    return combined


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


async def search_and_pick(
    search_queries: list[str],
    fallback_name: str,
    gpt_per_100g: dict[str, float],
    session_token: tuple[str, str] | None = None,
) -> list[dict]:
    """Search FatSecret, then ask LLM to pick top-3 matches.

    Returns list of dicts with: food_id, food_name, nutrition (per 100g).
    """
    from services import openai_svc

    queries = [q for q in search_queries if q]
    if not queries:
        queries = [fallback_name]
    results = await search_food_multi(
        queries, fallback_name=fallback_name, session_token=session_token
    )
    if not results:
        return []

    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        fid = item.get("food_id")
        if fid not in seen:
            seen.add(fid)
            unique.append(item)

    log.info("Sending %d unique FatSecret results to LLM for '%s'", len(unique), fallback_name)
    return await openai_svc.pick_best_matches(fallback_name, gpt_per_100g, unique)


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
    units = weight_g

    return await create_food_entry(
        session_token,
        food_id=food_id,
        food_entry_name=entry_name,
        serving_id=serving_id,
        number_of_units=units,
        meal=meal,
    )
