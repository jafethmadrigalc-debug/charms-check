"""
Funciones para hablar con el Mercado de la Comunidad de Steam.
Solo lectura de datos públicos (priceoverview y listings/render). No inicia
sesión, no compra, no vende.
"""

import os
import time
import requests

CURRENCY = int(os.environ.get("STEAM_CURRENCY", "1"))  # 1 = USD
COUNTRY = os.environ.get("STEAM_COUNTRY", "CR")
REMOVE_KEYCHAIN_COST = float(os.environ.get("REMOVE_KEYCHAIN_COST", "150"))
WEAR_CONDITIONS = os.environ.get(
    "WEAR_CONDITIONS", "Field-Tested"
).split(",")

STEAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def steam_get(url: str, params: dict, retries: int = 3, min_delay: float = 1.5):
    for attempt in range(1, retries + 1):
        resp = requests.get(url, params=params, headers=STEAM_HEADERS, timeout=15)
        if resp.status_code == 429:
            wait = 10 * attempt
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(min_delay)
        return resp.json()
    raise RuntimeError(f"No se pudo obtener {url} tras {retries} intentos")


def parse_price(price_str):
    if not price_str:
        return None
    cleaned = "".join(c for c in price_str if c.isdigit() or c in ",.")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def get_price_overview(market_hash_name: str, min_delay: float = 1.5):
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": 730,
        "currency": CURRENCY,
        "market_hash_name": market_hash_name,
    }
    data = steam_get(url, params, min_delay=min_delay)
    if not data.get("success"):
        return None
    return parse_price(data.get("median_price") or data.get("lowest_price"))


def find_all_attached_keychain_listings(market_hash_name: str, keychain_name: str, min_delay: float = 2.0):
    """
    Revisa los listados activos de un arma y devuelve TODOS los que tienen
    puesto el colgante indicado (no solo el más barato), como lista de
    dicts: [{"price": float, "listing_id": str}, ...] ordenada de menor a
    mayor precio.
    """
    url = f"https://steamcommunity.com/market/listings/730/{requests.utils.quote(market_hash_name)}/render/"
    params = {
        "query": "",
        "start": 0,
        "count": 100,
        "country": COUNTRY,
        "language": "english",
        "currency": CURRENCY,
    }
    data = steam_get(url, params, min_delay=min_delay)

    assets = data.get("assets", {}).get("730", {})
    listinginfo = data.get("listinginfo", {})

    found = []

    for listing_id, info in listinginfo.items():
        asset_info = info.get("asset", {})
        contextid = asset_info.get("contextid")
        assetid = asset_info.get("id")
        if not contextid or not assetid:
            continue
        asset_desc = assets.get(str(contextid), {}).get(str(assetid))
        if not asset_desc:
            continue

        descriptions = asset_desc.get("descriptions", [])
        has_keychain = any(
            keychain_name.lower() in (d.get("value") or "").lower()
            for d in descriptions
        )
        if not has_keychain:
            continue

        converted_price = info.get("converted_price", 0) + info.get("converted_fee", 0)
        price = converted_price / 100.0
        found.append({"price": price, "listing_id": listing_id})

    found.sort(key=lambda f: f["price"])
    return found
