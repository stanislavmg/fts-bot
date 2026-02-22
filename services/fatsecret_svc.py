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
        log.warning("FatSecret client created without session token")
    params = {
        "method": "foods.search",
        "search_expression": query,
        "page_number": str(page),
        "max_results": "50",
        "format": "json",
    }
    response = fs.session.get(fs.api_url, params=params)
    data = response.json()
    import json
    log.info("FatSecret raw response for '%s': %s", query, json.dumps(data, ensure_ascii=False, indent=2))
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
    query: str, session_token: tuple[str, str] | None = None, max_pages: int = 2
) -> list[dict]:
    """Search FatSecret for a query, fetching extra pages if the previous one was full."""
    loop = asyncio.get_running_loop()
    all_items: list[dict] = []
    for page in range(max_pages):
        try:
            items = await loop.run_in_executor(
                None, partial(_search_page_sync, query, page, session_token)
            )
        except (KeyError, TypeError):
            log.warning("FatSecret search returned no results for: %s (page %d)", query, page)
            break
        except Exception:
            log.exception("FatSecret search failed for: %s (page %d)", query, page)
            break
        all_items.extend(items)
        if len(items) < 50:
            break
    return all_items


async def search_food_multi(
    queries: list[str],
    session_token: tuple[str, str] | None = None,
) -> list[dict]:
    """Search multiple queries in parallel, deduplicate by food_id."""
    tasks = [search_food(q, session_token=session_token) for q in queries]
    all_results = await asyncio.gather(*tasks)

    for q, results in zip(queries, all_results):
        log.info("FatSecret search '%s' → %d results", q, len(results))

    seen: set[str] = set()
    combined: list[dict] = []
    for results in all_results:
        for item in results:
            fid = item.get("food_id")
            if fid not in seen:
                seen.add(fid)
                combined.append(item)
    log.info("FatSecret combined: %d unique results from %d queries", len(combined), len(queries))
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


import re

_NUTR_RE = re.compile(
    r"Calories:\s*([\d.]+)\s*kcal.*?"
    r"Fat:\s*([\d.]+)\s*g.*?"
    r"Carbs:\s*([\d.]+)\s*g.*?"
    r"Protein:\s*([\d.]+)\s*g",
    re.IGNORECASE | re.DOTALL,
)

_SERVING_G_RE = re.compile(
    r"Per\s+([\d.]+)\s*g",
    re.IGNORECASE,
)


def _parse_nutrition_from_desc(desc: str) -> tuple[dict[str, float], float] | None:
    """Parse KBJU from food_description.

    Returns (nutrition_per_100g, serving_grams) or None if unparseable.
    Nutrition is always normalized to per-100g.
    """
    if not desc:
        return None
    m = _NUTR_RE.search(desc)
    if not m:
        return None

    raw = {
        "calories": float(m.group(1)),
        "fat": float(m.group(2)),
        "carbs": float(m.group(3)),
        "protein": float(m.group(4)),
    }

    gm = _SERVING_G_RE.search(desc)
    serving_g = float(gm.group(1)) if gm else 100.0

    if serving_g > 0 and abs(serving_g - 100) > 0.5:
        factor = 100.0 / serving_g
        nutr = {k: v * factor for k, v in raw.items()}
    else:
        nutr = raw

    return nutr, serving_g


def _kbju_score(
    fs: dict[str, float],
    target: dict[str, float],
) -> float:
    """Weighted relative distance across all four KBJU values. Lower is better."""
    weights = {"calories": 2.0, "protein": 1.0, "fat": 1.0, "carbs": 1.0}
    score = 0.0
    for key, w in weights.items():
        t = target.get(key, 0)
        f = fs.get(key, 0)
        if t > 0:
            score += w * abs(f - t) / t
        elif f > 0:
            score += w * f / 100
    return score


async def match_food_top(
    search_queries: list[str],
    fallback_name: str,
    target: dict[str, float],
    top_n: int = 3,
    session_token: tuple[str, str] | None = None,
) -> list[dict]:
    """Search FatSecret with multiple queries in parallel, return top N matches by KBJU.

    target: {"calories": ..., "protein": ..., "fat": ..., "carbs": ...} per 100g.
    Each result dict has: food_id, food_name, cal_per_100g, description, nutrition, food_type, score.
    """
    queries = [q for q in search_queries if q]
    if not queries:
        queries = [fallback_name]
    results = await search_food_multi(queries, session_token=session_token)
    if not results:
        return []

    for i, item in enumerate(results[:10]):
        log.info(
            "FatSecret result #%d: id=%s name='%s' type=%s desc='%.150s'",
            i, item.get("food_id"), item.get("food_name"),
            item.get("food_type"), item.get("food_description", ""),
        )

    scored: list[tuple[float, dict]] = []
    seen_ids: set[str] = set()
    skipped = 0
    for item in results:
        fid = item.get("food_id")
        if fid in seen_ids:
            continue
        parsed = _parse_nutrition_from_desc(item.get("food_description", ""))
        if parsed is None:
            skipped += 1
            if skipped <= 5:
                log.info(
                    "Skipped unparseable: %s | desc: %.150s",
                    item.get("food_name", "?"),
                    item.get("food_description", ""),
                )
            continue
        nutr_per_100g, serving_g = parsed
        score = _kbju_score(nutr_per_100g, target)
        seen_ids.add(fid)
        scored.append((score, {
            "food_id": fid,
            "food_name": item.get("food_name", fallback_name),
            "cal_per_100g": nutr_per_100g["calories"],
            "description": item.get("food_description", ""),
            "nutrition": nutr_per_100g,
            "serving_g": serving_g,
            "food_type": item.get("food_type", ""),
        }))

    if skipped:
        log.info("Skipped %d items with unparseable descriptions (of %d total)", skipped, len(results))
    log.info("Scored %d items out of %d total results", len(scored), len(results))
    scored.sort(key=lambda x: x[0])
    return [entry for _, entry in scored[:top_n]]


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
