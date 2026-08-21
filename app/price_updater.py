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
        names = db.get_charms_needing_update()
        total = len(names)
        if total == 0:
            await asyncio.sleep(60)
            continue

        cycle_seconds = PRICE_CYCLE_HOURS * 3600
        per_item_delay = max(cycle_seconds / total, 1.0)

        logger.info(
            "Iniciando ciclo de actualización: %s colgantes, ~%.1fs entre cada uno "
            "(objetivo: %.1f horas por ciclo completo)",
            total, per_item_delay, PRICE_CYCLE_HOURS,
        )

        for i, name in enumerate(names, 1):
            try:
                loop = asyncio.get_event_loop()
                price = await loop.run_in_executor(
                    None, steam_client.get_price_overview, name, 1.0
                )
                db.update_price(name, price)
                if i % 25 == 0:
                    logger.info("Progreso: %s/%s (%s = %s)", i, total, name, price)
            except Exception as e:
                logger.warning("Error actualizando %s: %s", name, e)

            await asyncio.sleep(per_item_delay)

        logger.info("Ciclo de actualización completo.")
