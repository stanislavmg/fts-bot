from __future__ import annotations

import asyncio
from datetime import date
from functools import partial

from requests_oauthlib import OAuth1Session

import config


def _get_request_token() -> tuple[str, str]:
    oauth = OAuth1Session(
        config.FS_CONSUMER_KEY,
        client_secret=config.FS_CONSUMER_SECRET,
        callback_uri="oob",
    )
    resp = oauth.fetch_request_token(config.FS_REQUEST_TOKEN_URL)
    return resp["oauth_token"], resp["oauth_token_secret"]


def _get_authorize_url(request_token: str) -> str:
    return f"{config.FS_AUTHORIZE_URL}?oauth_token={request_token}"


def _get_access_token(
    request_token: str, request_secret: str, verifier: str
) -> tuple[str, str]:
    oauth = OAuth1Session(
        config.FS_CONSUMER_KEY,
        client_secret=config.FS_CONSUMER_SECRET,
        resource_owner_key=request_token,
        resource_owner_secret=request_secret,
        verifier=verifier,
    )
    resp = oauth.fetch_access_token(config.FS_ACCESS_TOKEN_URL)
    return resp["oauth_token"], resp["oauth_token_secret"]


def _api_call(
    access_token: str,
    access_secret: str,
    method: str,
    params: dict | None = None,
) -> dict:
    oauth = OAuth1Session(
        config.FS_CONSUMER_KEY,
        client_secret=config.FS_CONSUMER_SECRET,
        resource_owner_key=access_token,
        resource_owner_secret=access_secret,
    )
    req_params = {"method": method, "format": "json"}
    if params:
        req_params.update(params)
    resp = oauth.post(config.FS_API_URL, params=req_params)
    resp.raise_for_status()
    return resp.json()


def _server_api_call(method: str, params: dict | None = None) -> dict:
    """2-legged OAuth call (no user token) for public endpoints like foods.search."""
    oauth = OAuth1Session(
        config.FS_CONSUMER_KEY,
        client_secret=config.FS_CONSUMER_SECRET,
    )
    req_params = {"method": method, "format": "json"}
    if params:
        req_params.update(params)
    resp = oauth.get(config.FS_API_URL, params=req_params)
    resp.raise_for_status()
    return resp.json()


# ── Async wrappers ────────────────────────────────────────────────

async def get_request_token() -> tuple[str, str]:
    return await asyncio.get_running_loop().run_in_executor(
        None, _get_request_token
    )


def get_authorize_url(request_token: str) -> str:
    return _get_authorize_url(request_token)


async def get_access_token(
    request_token: str, request_secret: str, verifier: str
) -> tuple[str, str]:
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(_get_access_token, request_token, request_secret, verifier)
    )


async def search_food(query: str) -> list[dict]:
    data = await asyncio.get_running_loop().run_in_executor(
        None,
        partial(_server_api_call, "foods.search", {"search_expression": query, "max_results": "5"}),
    )
    foods = data.get("foods", {}).get("food", [])
    if isinstance(foods, dict):
        foods = [foods]
    return foods


async def get_food(food_id: int | str) -> dict:
    data = await asyncio.get_running_loop().run_in_executor(
        None,
        partial(_server_api_call, "food.get.v4", {"food_id": str(food_id)}),
    )
    return data.get("food", {})


async def create_food_entry(
    access_token: str,
    access_secret: str,
    *,
    food_id: int | str,
    food_entry_name: str,
    serving_id: int | str,
    number_of_units: float,
    meal: str = "other",
    entry_date: date | None = None,
) -> dict:
    if entry_date is None:
        entry_date = date.today()
    epoch = date(1970, 1, 1)
    date_int = (entry_date - epoch).days

    params = {
        "food_id": str(food_id),
        "food_entry_name": food_entry_name,
        "serving_id": str(serving_id),
        "number_of_units": f"{number_of_units:.3f}",
        "meal": meal,
        "date": str(date_int),
    }
    return await asyncio.get_running_loop().run_in_executor(
        None,
        partial(_api_call, access_token, access_secret, "food_entry.create", params),
    )


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
    access_token: str,
    access_secret: str,
    name: str,
    weight_g: float,
    meal: str = "other",
) -> dict | None:
    """Search for a food, pick the best match, and create a diary entry."""
    results = await search_food(name)
    if not results:
        return None

    food_id = results[0]["food_id"]
    food_detail = await get_food(food_id)

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
        access_token,
        access_secret,
        food_id=food_id,
        food_entry_name=name,
        serving_id=serving_id,
        number_of_units=units,
        meal=meal,
    )
