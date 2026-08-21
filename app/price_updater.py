"""
Actualizador de precios en segundo plano.

Reparte las consultas de los 926 colgantes a lo largo de PRICE_CYCLE_HOURS
(6 horas por defecto) para no golpear el Mercado de Steam con muchas
peticiones seguidas. Termina un ciclo completo y arranca el siguiente.
"""

import asyncio
import logging
import os

from . import db, steam_client

logger = logging.getLogger("price_updater")

PRICE_CYCLE_HOURS = float(os.environ.get("PRICE_CYCLE_HOURS", "6"))


async def price_update_loop():
    while True:
        pending = db.get_charms_needing_update()
        total = len(pending)
        if total == 0:
            await asyncio.sleep(60)
            continue

        cycle_seconds = PRICE_CYCLE_HOURS * 3600
        # Cada colgante implica hasta 3 peticiones (precio, página del ítem
        # para el item_nameid la primera vez, y libro de órdenes). Además,
        # steam_client impone un espacio mínimo GLOBAL de ~3s entre
        # CUALQUIER petición a Steam (ver GLOBAL_MIN_INTERVAL), así que el
        # ciclo real puede tardar más que este objetivo si Steam está
        # limitando la tasa — eso es intencional y evita más bloqueos 429.
        per_item_delay = max(cycle_seconds / total, 0.5)

        logger.info(
            "Iniciando ciclo de actualización: %s colgantes, ~%.1fs entre cada uno "
            "(objetivo: %.1f horas por ciclo completo, incluye orden de compra)",
            total, per_item_delay, PRICE_CYCLE_HOURS,
        )

        successes = 0
        failures = 0
        loop = asyncio.get_event_loop()

        for i, charm in enumerate(pending, 1):
            name = charm["market_hash_name"]
            try:
                data = await loop.run_in_executor(
                    None,
                    steam_client.get_charm_market_data,
                    name,
                    charm["item_nameid"],
                    0.8,
                )
                db.update_market_data(
                    name, data["base_price"], data["item_nameid"], data["highest_buy_order"]
                )
                successes += 1
                if i % 25 == 0:
                    logger.info(
                        "Progreso: %s/%s (%s exitosos, %s fallidos) — último: %s = venta %s, compra %s",
                        i, total, successes, failures, name, data["base_price"], data["highest_buy_order"],
                    )
            except Exception as e:
                failures += 1
                logger.warning("Error actualizando %s: %s", name, e)
                # IMPORTANTE: marcamos el intento aunque haya fallado, para
                # que este colgante no bloquee el resto de la cola en el
                # próximo ciclo — su turno vuelve más adelante, no de
                # inmediato.
                try:
                    await loop.run_in_executor(None, db.mark_update_attempted, name)
                except Exception:
                    pass

            await asyncio.sleep(per_item_delay)

        logger.info(
            "Ciclo de actualización completo: %s exitosos, %s fallidos de %s totales.",
            successes, failures, total,
        )
