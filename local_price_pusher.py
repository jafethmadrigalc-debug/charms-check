"""
Empujador de precios local — corre esto en TU COMPUTADORA, no en Fly.

QUÉ HACE
---------
Consulta el PRECIO DE VENTA de los 926 colgantes desde tu conexión (Steam
bloquea las IPs de datacenters como Fly, pero no la tuya) y los envía a tu
app en Fly.

Nota: la ORDEN DE COMPRA quedó fuera a propósito. Tras el rediseño del
Mercado de Steam (2026), ese dato solo aparece en una página que carga con
JavaScript y agrupa todos los colgantes de un evento en un desplegable —
demasiado frágil de automatizar. El precio de venta, en cambio, viene de un
endpoint JSON estable que no necesita navegador.

INSTALACIÓN
------------
    pip install requests

(Ya no hace falta Playwright.)

USO (PowerShell)
-----------------
    $env:FLY_APP_URL="https://charms-check.fly.dev"; $env:INGEST_TOKEN="tu-clave"; python local_price_pusher.py

Para probar con pocos primero:
    $env:LIMIT_FOR_TESTING="10"
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
FLY_APP_URL = os.environ.get("FLY_APP_URL", "https://charms-check.fly.dev")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "PON-AQUI-LA-MISMA-CLAVE-QUE-EN-FLY")
CHARMS_FILE = os.environ.get("CHARMS_FILE", "charms_database.json")

# Pausa entre consultas (segundos). No bajar mucho de 1.5 para no
# arriesgar que Steam limite tu IP.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.5"))

# Cada cuántos colgantes se envía el lote acumulado al servidor.
BATCH_SIZE = 20

# Cada cuánto repetir el ciclo completo (horas). 0 = correr una vez y salir.
REPEAT_EVERY_HOURS = float(os.environ.get("REPEAT_EVERY_HOURS", "0"))

# Para pruebas: procesar solo los primeros N colgantes. 0 = todos.
LIMIT_FOR_TESTING = int(os.environ.get("LIMIT_FOR_TESTING", "0"))

# Moneda de Steam. 1 = USD. Revisa el parámetro currency= en la URL del
# mercado en tu navegador si quieres otra.
CURRENCY = int(os.environ.get("STEAM_CURRENCY", "1"))
# ---------------------------------------------------------------------

STEAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(STEAM_HEADERS)


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def parse_price(price_str):
    """
    Convierte el precio devuelto por Steam a float.

    OJO CON LA MONEDA: este parseo asume formato con PUNTO DECIMAL
    ('$5.50'), que es lo que devuelve Steam con STEAM_CURRENCY=1 (USD).
    Si cambias a una moneda donde el punto es separador de miles
    (ej. colones: '₡2.235' = dos mil doscientos treinta y cinco), este
    parseo lo interpretaría mal como 2.235. Mantén USD, o ajusta esta
    función si cambias de moneda.
    """
    if not price_str:
        return None
    cleaned = "".join(c for c in price_str if c.isdigit() or c in ",.")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        derecha = cleaned.split(",")[-1]
        cleaned = cleaned.replace(",", "") if len(derecha) == 3 else cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def get_price_overview(market_hash_name, retries=4):
    """
    Endpoint JSON público de Steam. Devuelve el precio de venta.
    'median_price' refleja ventas recientes; si no está disponible,
    usamos 'lowest_price' (el listado activo más barato).
    """
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": 730,
        "currency": CURRENCY,
        "market_hash_name": market_hash_name,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
        except Exception as e:
            log(f"  Error de red: {e}")
            time.sleep(5)
            continue

        if resp.status_code == 429:
            wait = 20 * attempt
            log(f"  429 de Steam, esperando {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        if not data.get("success"):
            return None

        return parse_price(data.get("median_price") or data.get("lowest_price"))

    return None


def load_charms():
    with open(CHARMS_FILE, "r", encoding="utf-8") as f:
        charms = json.load(f)
    if LIMIT_FOR_TESTING > 0:
        charms = charms[:LIMIT_FOR_TESTING]
    return charms


def push_batch(batch):
    if not batch:
        return
    url = f"{FLY_APP_URL.rstrip('/')}/api/ingest"
    try:
        resp = requests.post(
            url,
            json={"items": batch},
            headers={"X-Ingest-Token": INGEST_TOKEN, "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        log(f"  !! Error de red enviando lote: {e}")
        return

    if resp.status_code != 200:
        log(f"  !! Error enviando lote: {resp.status_code} {resp.text[:200]}")
    else:
        log(f"  Lote de {len(batch)} enviado al servidor OK.")


def run_cycle():
    charms = load_charms()
    total = len(charms)
    log(f"Cargados {total} colgantes desde {CHARMS_FILE}. Empezando ciclo...")

    batch = []
    conseguidos = 0
    sin_datos = 0

    for i, charm in enumerate(charms, 1):
        name = charm["market_hash_name"]
        try:
            base_price = get_price_overview(name)

            if base_price is not None:
                conseguidos += 1
            else:
                sin_datos += 1

            batch.append({
                "market_hash_name": name,
                "base_price": base_price,
                "item_nameid": None,
                "highest_buy_order": None,
            })

            if i % 25 == 0:
                log(f"Progreso: {i}/{total} ({conseguidos} con precio, {sin_datos} sin datos) — último: {name} = {base_price}")

        except Exception as e:
            log(f"Error con {name}: {e}")

        if len(batch) >= BATCH_SIZE:
            push_batch(batch)
            batch = []

        time.sleep(REQUEST_DELAY)

    push_batch(batch)
    log(f"Ciclo completo: {conseguidos} con precio, {sin_datos} sin datos, de {total} totales.")


def main():
    if INGEST_TOKEN == "PON-AQUI-LA-MISMA-CLAVE-QUE-EN-FLY":
        log("!! Configura INGEST_TOKEN antes de correr esto.")
        sys.exit(1)

    while True:
        run_cycle()
        if REPEAT_EVERY_HOURS <= 0:
            break
        log(f"Esperando {REPEAT_EVERY_HOURS} horas para el próximo ciclo...")
        time.sleep(REPEAT_EVERY_HOURS * 3600)


if __name__ == "__main__":
    main()
