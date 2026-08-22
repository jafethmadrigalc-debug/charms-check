"""
Alertador en segundo plano.

Cada ALERT_INTERVAL_MINUTES (configurable desde la interfaz), revisa SOLO
los colgantes que el usuario marcó como "vigilados" (watchlist), busca
listados activos del arma con ese colgante puesto, y si el webhook de
Discord está configurado y las alertas están activadas, avisa cuando
encuentra una oportunidad — sin repetir la misma alerta dos veces (se
recuerda por listing_id).
"""

import asyncio
import logging
import requests

from . import db, steam_client

logger = logging.getLogger("alerter")


def send_discord(webhook_url: str, message: str):
    try:
        requests.post(webhook_url, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        logger.warning("No se pudo enviar a Discord: %s", e)


async def alert_loop():
    while True:
        settings = db.get_settings()
        interval_minutes = float(settings.get("alert_interval_minutes") or 15)

        alerts_enabled = settings.get("alerts_enabled") == "true"
        webhook_url = settings.get("discord_webhook_url") or ""
        remove_cost = float(settings.get("remove_keychain_cost") or 150)

        if not alerts_enabled or not webhook_url:
            await asyncio.sleep(60)
            continue

        watched_names = db.get_watched_names()
        if not watched_names:
            await asyncio.sleep(60)
            continue

        charms = db.get_charms_by_names(watched_names)
        logger.info("Revisando %s colgantes vigilados...", len(charms))

        loop = asyncio.get_event_loop()

        for charm in charms:
            keychain = charm["market_hash_name"]
            base_price = charm["base_price"]
            weapons = __import__("json").loads(charm["weapons_json"] or "[]")

            # La ganancia se calcula contra el precio de venta del colgante.
            # Es una referencia (lo que otros están pidiendo), no una venta
            # garantizada.
            reference_price = base_price

            if reference_price is None or not weapons:
                continue

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

                    for listing in found:
                        listing_id = listing["listing_id"]
                        listing_price = listing["price"]

                        if db.was_already_notified(listing_id):
                            continue

                        profit = reference_price - remove_cost - listing_price
                        if profit > 0:
                            price_source = "precio de venta (referencia)"
                            send_discord(
                                webhook_url,
                                f"🎯 Oportunidad: **{weapon_full}**\n"
                                f"Colgante: {keychain}\n"
                                f"Precio del arma (con colgante puesto): {listing_price}\n"
                                f"Valor de referencia del colgante ({price_source}): {reference_price}\n"
                                f"Costo de quitar: {remove_cost}\n"
                                f"**Ganancia estimada: {profit:.2f}**\n"
                                f"Listing ID: {listing_id}",
                            )
                            db.mark_notified(listing_id, keychain)

        await asyncio.sleep(interval_minutes * 60)
