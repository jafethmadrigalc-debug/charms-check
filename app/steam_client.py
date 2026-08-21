"""
Funciones para hablar con el Mercado de la Comunidad de Steam.
Solo lectura de datos públicos (priceoverview y listings/render). No inicia
sesión, no compra, no vende.
"""

import os
import re
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


ITEM_NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")


def get_item_nameid(market_hash_name: str, min_delay: float = 1.5):
    """
    El 'item_nameid' es un ID numérico interno que Steam usa para el libro
    de órdenes de compra/venta de un ítem "commodity" (ítems idénticos entre
    sí, como colgantes sueltos, cromos, etc — no aplica a armas con
    desgaste/float variable). Solo se puede obtener leyendo el HTML de la
    página del ítem; no hay endpoint directo para convertir el nombre en
    este ID. Una vez obtenido, conviene guardarlo (se cachea en la base de
    datos) para no tener que volver a pedirlo.
    """
    url = f"https://steamcommunity.com/market/listings/730/{requests.utils.quote(market_hash_name)}"
    for attempt in range(1, 4):
        resp = requests.get(url, headers=STEAM_HEADERS, timeout=15)
        if resp.status_code == 429:
            time.sleep(10 * attempt)
            continue
        resp.raise_for_status()
        time.sleep(min_delay)
        match = ITEM_NAMEID_RE.search(resp.text)
        return match.group(1) if match else None
    return None


def get_order_histogram(item_nameid: str, min_delay: float = 1.5):
    """
    Libro de órdenes de compra/venta público de un ítem "commodity"
    (la misma gráfica de oferta/demanda que se ve en el navegador sin
    necesidad de tener cuenta). Devuelve (highest_buy_order, lowest_sell_order)
    en la moneda configurada, o (None, None) si no se pudo obtener.
    """
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": COUNTRY,
        "language": "english",
        "currency": CURRENCY,
        "item_nameid": item_nameid,
        "norender": 1,
    }
    try:
        data = steam_get(url, params, min_delay=min_delay)
    except Exception:
        return None, None

    if not data.get("success"):
        return None, None

    highest_buy = data.get("highest_buy_order")
    lowest_sell = data.get("lowest_sell_order")
    highest_buy = float(highest_buy) / 100.0 if highest_buy else None
    lowest_sell = float(lowest_sell) / 100.0 if lowest_sell else None
    return highest_buy, lowest_sell


def get_charm_market_data(market_hash_name: str, cached_item_nameid: str = None, min_delay: float = 1.5):
    """
    Combina precio base (priceoverview) + orden de compra más alta
    (itemordershistogram) para un colgante SUELTO. Reutiliza el
    item_nameid si ya se tenía guardado, para ahorrar una petición.
    Devuelve dict: {base_price, item_nameid, highest_buy_order, lowest_sell_order}
    """
    base_price = get_price_overview(market_hash_name, min_delay=min_delay)

    item_nameid = cached_item_nameid
    if not item_nameid:
        item_nameid = get_item_nameid(market_hash_name, min_delay=min_delay)

    highest_buy_order, lowest_sell_order = (None, None)
    if item_nameid:
        highest_buy_order, lowest_sell_order = get_order_histogram(item_nameid, min_delay=min_delay)

    return {
        "base_price": base_price,
        "item_nameid": item_nameid,
        "highest_buy_order": highest_buy_order,
        "lowest_sell_order": lowest_sell_order,
    }


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
