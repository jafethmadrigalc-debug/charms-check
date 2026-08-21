import asyncio
import json
import logging
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, steam_client
from .price_updater import price_update_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("main")

app = FastAPI(title="CS2 Colgantes Monitor")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup():
    db.init_db()
    asyncio.create_task(price_update_loop())
    logger.info("App iniciada. Actualizador de precios corriendo en segundo plano.")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/stats")
async def api_stats():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, db.get_stats)


@app.get("/api/charms")
async def api_charms(event: str = None):
    loop = asyncio.get_event_loop()
    charms = await loop.run_in_executor(None, db.get_all_charms)
    out = []
    for c in charms:
        if event and c["event"] != event:
            continue
        out.append(
            {
                "market_hash_name": c["market_hash_name"],
                "description": c["description"],
                "event": c["event"],
                "map": c["map"],
                "team0": c["team0"],
                "team1": c["team1"],
                "stage": c["stage"],
                "player": c["player"],
                "base_price": c["base_price"],
                "last_updated": c["last_updated"],
                "weapons_count": len(json.loads(c["weapons_json"] or "[]")),
            }
        )
    return out


class ListingsRequest(BaseModel):
    market_hash_names: list[str]


@app.post("/api/listings")
async def api_listings(req: ListingsRequest):
    """
    NOTA: el cálculo automático de "ganancia" está en pausa por ahora.
    Este endpoint solo MUESTRA los listados activos del Mercado de Steam
    para las armas que pueden traer el/los colgante(s) marcados, para que
    tú decidas manualmente comparando contra el precio base. Es a demanda
    (no automático), así que puede tardar según cuántos elijas.
    """
    loop = asyncio.get_event_loop()
    charms = await loop.run_in_executor(None, db.get_charms_by_names, req.market_hash_names)
    results = []

    for charm in charms:
        keychain = charm["market_hash_name"]
        base_price = charm["base_price"]
        weapons = json.loads(charm["weapons_json"] or "[]")

        if not weapons:
            results.append(
                {
                    "keychain": keychain,
                    "description": charm.get("description"),
                    "base_price": base_price,
                    "status": "sin_armas",
                    "message": "No hay lista de armas asociada a este colgante todavía.",
                    "listings": [],
                }
            )
            continue

        all_listings = []
        for weapon_base in weapons:
            for wear in steam_client.WEAR_CONDITIONS:
                weapon_full = f"Souvenir {weapon_base} ({wear})"
                try:
                    found = await loop.run_in_executor(
                        None,
                        steam_client.find_all_attached_keychain_listings,
                        weapon_full,
                        keychain,
                        2.0,
                    )
                except Exception as e:
                    logger.warning("Error revisando %s: %s", weapon_full, e)
                    continue

                for f in found:
                    all_listings.append(
                        {
                            "weapon": weapon_full,
                            "listing_price": f["price"],
                            "listing_id": f["listing_id"],
                        }
                    )

        all_listings.sort(key=lambda x: x["listing_price"])
        results.append(
            {
                "keychain": keychain,
                "description": charm.get("description"),
                "base_price": base_price,
                "status": "ok",
                "listings": all_listings,
            }
        )

    return results


@app.get("/api/export")
async def api_export():
    """Respaldo de la lista completa (con los precios actualizados) para
    descargar como JSON, por si quieres guardarla aparte."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, db.get_all_charms)
