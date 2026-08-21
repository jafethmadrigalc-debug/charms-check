import asyncio
import json
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, steam_client
from .price_updater import price_update_loop
from .alerter import alert_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("main")

app = FastAPI(title="CS2 Colgantes Monitor")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# El actualizador automático DENTRO de Fly suele quedar bloqueado por Steam
# (Steam es agresivo bloqueando IPs de datacenters). Por defecto queda
# apagado; en su lugar usa local_price_pusher.py desde tu propia
# computadora, que empuja los precios al endpoint /api/ingest de abajo.
# Si quieres intentarlo de nuevo desde el servidor, pon
# ENABLE_SERVER_PRICE_UPDATES=true como variable de entorno.
ENABLE_SERVER_PRICE_UPDATES = os.environ.get("ENABLE_SERVER_PRICE_UPDATES", "false") == "true"
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")


@app.on_event("startup")
async def startup():
    db.init_db()
    if ENABLE_SERVER_PRICE_UPDATES:
        asyncio.create_task(price_update_loop())
        logger.info("Actualizador de precios DEL SERVIDOR activado (ENABLE_SERVER_PRICE_UPDATES=true).")
    else:
        logger.info(
            "Actualizador de precios del servidor DESACTIVADO. Usa local_price_pusher.py "
            "desde tu computadora para alimentar /api/ingest."
        )
    asyncio.create_task(alert_loop())
    logger.info("App iniciada. Alertador corriendo en segundo plano.")


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
                "highest_buy_order": c["highest_buy_order"],
                "last_updated": c["last_updated"],
                "weapons_count": len(json.loads(c["weapons_json"] or "[]")),
                "watched": bool(c["watched"]),
            }
        )
    return out


class WatchRequest(BaseModel):
    market_hash_names: list[str]
    watched: bool


@app.post("/api/watch")
async def api_watch(req: WatchRequest):
    """Marca o desmarca colgantes como 'vigilados' — los vigilados son los
    que el alertador de fondo revisa para mandar notificaciones a Discord."""
    loop = asyncio.get_event_loop()
    for name in req.market_hash_names:
        await loop.run_in_executor(None, db.set_watch, name, req.watched)
    return {"ok": True, "updated": len(req.market_hash_names), "watched": req.watched}


class SettingsModel(BaseModel):
    discord_webhook_url: str = None
    remove_keychain_cost: float = None
    alerts_enabled: bool = None
    alert_interval_minutes: float = None


@app.get("/api/settings")
async def api_get_settings():
    loop = asyncio.get_event_loop()
    settings = await loop.run_in_executor(None, db.get_settings)
    return {
        "discord_webhook_url": settings["discord_webhook_url"],
        "remove_keychain_cost": float(settings["remove_keychain_cost"]),
        "alerts_enabled": settings["alerts_enabled"] == "true",
        "alert_interval_minutes": float(settings["alert_interval_minutes"]),
    }


@app.post("/api/settings")
async def api_save_settings(req: SettingsModel):
    updates = {}
    if req.discord_webhook_url is not None:
        updates["discord_webhook_url"] = req.discord_webhook_url
    if req.remove_keychain_cost is not None:
        updates["remove_keychain_cost"] = req.remove_keychain_cost
    if req.alerts_enabled is not None:
        updates["alerts_enabled"] = "true" if req.alerts_enabled else "false"
    if req.alert_interval_minutes is not None:
        updates["alert_interval_minutes"] = req.alert_interval_minutes

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, db.save_settings, updates)
    return await api_get_settings()


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
                    "highest_buy_order": charm["highest_buy_order"],
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
                "highest_buy_order": charm["highest_buy_order"],
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


class IngestItem(BaseModel):
    market_hash_name: str
    base_price: float = None
    item_nameid: str = None
    highest_buy_order: float = None


class IngestRequest(BaseModel):
    items: list[IngestItem]


@app.post("/api/ingest")
async def api_ingest(req: IngestRequest, x_ingest_token: str = Header(default="")):
    """
    Recibe precios ya consultados por local_price_pusher.py (corriendo en
    tu propia computadora, con una IP residencial que Steam no bloquea) y
    los guarda en la base de datos del servidor. Protegido con un token
    simple para que no cualquiera pueda escribir en tu base de datos.
    """
    if not INGEST_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="INGEST_TOKEN no está configurado en el servidor. Defínelo como secret en Fly.",
        )
    if x_ingest_token != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido.")

    loop = asyncio.get_event_loop()
    updated = 0
    for item in req.items:
        await loop.run_in_executor(
            None, db.update_market_data, item.market_hash_name,
            item.base_price, item.item_nameid, item.highest_buy_order,
        )
        updated += 1

    return {"ok": True, "updated": updated}
