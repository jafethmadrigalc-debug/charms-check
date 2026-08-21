"""
Empujador de precios local — corre esto en TU COMPUTADORA, no en Fly.

Por qué: Steam bloquea agresivamente las IPs de datacenters/cloud (como las
de Fly) al consultar el mercado, sin importar qué tan despacio vayas. Tu
conexión de internet normal (residencial) no tiene ese problema. Este
script consulta los precios desde aquí y se los manda al servidor de Fly,
que solo se encarga de guardarlos y mostrar la interfaz.

CONFIGURACIÓN
--------------
1. En Fly, define el secret INGEST_TOKEN (una contraseña que tú inventes):

    flyctl secrets set INGEST_TOKEN=alguna-clave-larga-y-dificil-de-adivinar

2. Aquí abajo, pon esa misma clave y la URL de tu app.

3. Instala dependencias:

    pip install requests

4. Corre el script. Puedes dejarlo corriendo indefinidamente (hace ciclos
   solo) o programarlo con el Programador de tareas de Windows / cron de
   Mac-Linux para que corra, por ejemplo, cada 6 horas.

    python local_price_pusher.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------
# CONFIGURA ESTO
# ---------------------------------------------------------------------
FLY_APP_URL = os.environ.get("FLY_APP_URL", "https://charms-check-a.fly.dev")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "PON-AQUI-LA-MISMA-CLAVE-QUE-EN-FLY")
CHARMS_FILE = os.environ.get("CHARMS_FILE", "charms_database.json")

# Cada cuánto pausar entre colgantes consultados (segundos). Con IP
# residencial normal esto puede ser bastante más rápido que en un
# datacenter, pero sigue siendo respetuoso con Steam.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.5"))

# Cada cuántos colgantes se envía el lote acumulado al servidor (para no
# hacer una petición HTTP por cada uno).
BATCH_SIZE = 20

# Cada cuánto volver a correr un ciclo completo (horas). Pon 0 para correr
# una sola vez y salir (útil si lo programas con cron/Task Scheduler).
REPEAT_EVERY_HOURS = float(os.environ.get("REPEAT_EVERY_HOURS", "0"))
# ---------------------------------------------------------------------

STEAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
CURRENCY = int(os.environ.get("STEAM_CURRENCY", "1"))
COUNTRY = os.environ.get("STEAM_COUNTRY", "CR")

import re
ITEM_NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def steam_get(url, params, retries=4):
    for attempt in range(1, retries + 1):
        resp = requests.get(url, params=params, headers=STEAM_HEADERS, timeout=15)
        if resp.status_code == 429:
            wait = 15 * attempt
            log(f"  429 de Steam, esperando {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}
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


def get_price_overview(market_hash_name):
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": 730, "currency": CURRENCY, "market_hash_name": market_hash_name}
    data = steam_get(url, params)
    if not data.get("success"):
        return None
    return parse_price(data.get("median_price") or data.get("lowest_price"))


def get_item_nameid(market_hash_name):
    url = f"https://steamcommunity.com/market/listings/730/{requests.utils.quote(market_hash_name)}"
    for attempt in range(1, 4):
        resp = requests.get(url, headers=STEAM_HEADERS, timeout=15)
        if resp.status_code == 429:
            time.sleep(15 * attempt)
            continue
        resp.raise_for_status()
        match = ITEM_NAMEID_RE.search(resp.text)
        return match.group(1) if match else None
    return None


def get_order_histogram(item_nameid):
    url = "https://steamcommunity.com/market/itemordershistogram"
    params = {
        "country": COUNTRY, "language": "english", "currency": CURRENCY,
        "item_nameid": item_nameid, "norender": 1,
    }
    try:
        data = steam_get(url, params)
    except Exception:
        return None
    if not data.get("success"):
        return None
    highest_buy = data.get("highest_buy_order")
    return float(highest_buy) / 100.0 if highest_buy else None


def load_charms():
    with open(CHARMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def push_batch(batch):
    if not batch:
        return
    url = f"{FLY_APP_URL.rstrip('/')}/api/ingest"
    resp = requests.post(
        url,
        json={"items": batch},
        headers={"X-Ingest-Token": INGEST_TOKEN, "Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"  !! Error enviando lote al servidor: {resp.status_code} {resp.text[:200]}")
    else:
        log(f"  Lote de {len(batch)} enviado al servidor OK.")


def run_cycle():
    charms = load_charms()
    total = len(charms)
    log(f"Cargados {total} colgantes desde {CHARMS_FILE}. Empezando ciclo...")

    batch = []
    for i, charm in enumerate(charms, 1):
        name = charm["market_hash_name"]
        try:
            base_price = get_price_overview(name)
            time.sleep(REQUEST_DELAY)

            item_nameid = get_item_nameid(name)
            time.sleep(REQUEST_DELAY)

            highest_buy_order = None
            if item_nameid:
                highest_buy_order = get_order_histogram(item_nameid)
                time.sleep(REQUEST_DELAY)

            batch.append({
                "market_hash_name": name,
                "base_price": base_price,
                "item_nameid": item_nameid,
                "highest_buy_order": highest_buy_order,
            })

            if i % 25 == 0:
                log(f"Progreso: {i}/{total} — último: {name} = venta {base_price}, compra {highest_buy_order}")

        except Exception as e:
            log(f"Error con {name}: {e}")

        if len(batch) >= BATCH_SIZE:
            push_batch(batch)
            batch = []

    push_batch(batch)
    log("Ciclo completo.")


def main():
    if INGEST_TOKEN == "PON-AQUI-LA-MISMA-CLAVE-QUE-EN-FLY":
        log("!! Configura INGEST_TOKEN (variable de entorno o edita el script) antes de correr esto.")
        sys.exit(1)

    while True:
        run_cycle()
        if REPEAT_EVERY_HOURS <= 0:
            break
        log(f"Esperando {REPEAT_EVERY_HOURS} horas para el próximo ciclo...")
        time.sleep(REPEAT_EVERY_HOURS * 3600)


if __name__ == "__main__":
    main()
