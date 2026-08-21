"""
Capa de datos (SQLite). Guarda los 926 colgantes con su precio base más
reciente. Se usa un archivo SQLite simple porque el volumen de datos es
pequeño (menos de 1000 filas) y no necesitamos nada más sofisticado.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/charms.db")
CHARMS_SEED_FILE = os.environ.get(
    "CHARMS_SEED_FILE",
    os.path.join(os.path.dirname(__file__), "..", "charms_database.json"),
)


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS charms (
                market_hash_name TEXT PRIMARY KEY,
                event TEXT,
                map TEXT,
                team0 TEXT,
                team1 TEXT,
                stage TEXT,
                player TEXT,
                description TEXT,
                weapons_json TEXT,
                base_price REAL,
                last_updated TEXT
            )
            """
        )
        _add_column_if_missing(conn, "charms", "item_nameid", "TEXT")
        _add_column_if_missing(conn, "charms", "highest_buy_order", "REAL")
        _add_column_if_missing(conn, "charms", "buy_order_updated", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watches (
                market_hash_name TEXT PRIMARY KEY,
                watched_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_log (
                listing_id TEXT PRIMARY KEY,
                market_hash_name TEXT,
                notified_at TEXT
            )
            """
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM charms").fetchone()["c"]
        if count == 0:
            seed_from_json(conn)


def _add_column_if_missing(conn, table, column, coltype):
    """Migración segura: agrega la columna si no existe (para no romper una
    base de datos que ya venía de una versión anterior de la app)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def seed_from_json(conn):
    if not os.path.exists(CHARMS_SEED_FILE):
        return
    with open(CHARMS_SEED_FILE, "r", encoding="utf-8") as f:
        charms = json.load(f)
    for c in charms:
        conn.execute(
            """
            INSERT OR IGNORE INTO charms
                (market_hash_name, event, map, team0, team1, stage, player,
                 description, weapons_json, base_price, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                c["market_hash_name"],
                c.get("event"),
                c.get("map"),
                c.get("team0"),
                c.get("team1"),
                c.get("stage"),
                c.get("player"),
                c.get("description"),
                json.dumps(c.get("weapons", [])),
            ),
        )


def get_all_charms():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, CASE WHEN w.market_hash_name IS NULL THEN 0 ELSE 1 END AS watched
            FROM charms c
            LEFT JOIN watches w ON w.market_hash_name = c.market_hash_name
            ORDER BY
                (COALESCE(c.highest_buy_order, c.base_price) IS NULL),
                COALESCE(c.highest_buy_order, c.base_price) DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_charms_by_names(names):
    if not names:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT * FROM charms WHERE market_hash_name IN ({placeholders})",
            names,
        ).fetchall()
        return [dict(r) for r in rows]


def get_charms_needing_update(limit=None):
    """Charms ordenados por los que llevan más tiempo sin actualizarse
    (o nunca se han actualizado) primero. Devuelve también el item_nameid
    ya cacheado (si existe) para ahorrar una petición extra a Steam."""
    with get_conn() as conn:
        query = (
            "SELECT market_hash_name, item_nameid FROM charms "
            "ORDER BY (last_updated IS NULL) DESC, last_updated ASC"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        return [{"market_hash_name": r["market_hash_name"], "item_nameid": r["item_nameid"]} for r in rows]


def update_price(market_hash_name, price):
    with get_conn() as conn:
        conn.execute(
            "UPDATE charms SET base_price = ?, last_updated = ? WHERE market_hash_name = ?",
            (price, datetime.now(timezone.utc).isoformat(), market_hash_name),
        )


def mark_update_attempted(market_hash_name):
    """
    Marca que se INTENTÓ actualizar este colgante (sin importar si falló),
    actualizando solo 'last_updated' y sin tocar el precio que ya tuviera
    guardado. Esto es clave para que el ciclo avance: si un colgante falla
    (por ejemplo por un 429 de Steam) y nunca se marca como intentado,
    siempre vuelve a aparecer PRIMERO en la cola la próxima vuelta —
    dejando el ciclo atascado en los mismos primeros ítems sin nunca
    avanzar al resto.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE charms SET last_updated = ? WHERE market_hash_name = ?",
            (datetime.now(timezone.utc).isoformat(), market_hash_name),
        )


def update_market_data(market_hash_name, base_price, item_nameid, highest_buy_order):
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE charms
            SET base_price = ?, last_updated = ?, item_nameid = ?,
                highest_buy_order = ?, buy_order_updated = ?
            WHERE market_hash_name = ?
            """,
            (base_price, now, item_nameid, highest_buy_order, now, market_hash_name),
        )


def get_stats():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM charms").fetchone()["c"]
        priced = conn.execute(
            "SELECT COUNT(*) AS c FROM charms WHERE base_price IS NOT NULL"
        ).fetchone()["c"]
        oldest = conn.execute(
            "SELECT MIN(last_updated) AS m FROM charms WHERE last_updated IS NOT NULL"
        ).fetchone()["m"]
        newest = conn.execute(
            "SELECT MAX(last_updated) AS m FROM charms WHERE last_updated IS NOT NULL"
        ).fetchone()["m"]
        watched = conn.execute("SELECT COUNT(*) AS c FROM watches").fetchone()["c"]
        return {
            "total": total,
            "priced": priced,
            "oldest_update": oldest,
            "newest_update": newest,
            "watched": watched,
        }


# --- Vigilancia (watchlist para alertas de Discord) -----------------------

def set_watch(market_hash_name: str, watched: bool):
    with get_conn() as conn:
        if watched:
            conn.execute(
                "INSERT OR REPLACE INTO watches (market_hash_name, watched_at) VALUES (?, ?)",
                (market_hash_name, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute(
                "DELETE FROM watches WHERE market_hash_name = ?", (market_hash_name,)
            )


def get_watched_names():
    with get_conn() as conn:
        rows = conn.execute("SELECT market_hash_name FROM watches").fetchall()
        return [r["market_hash_name"] for r in rows]


# --- Configuración (webhook de Discord, costo de quitar, alertas on/off) --

DEFAULT_SETTINGS = {
    "discord_webhook_url": "",
    "remove_keychain_cost": "150",
    "alerts_enabled": "false",
    "alert_interval_minutes": "15",
}


def get_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        values = {r["key"]: r["value"] for r in rows}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(values)
    return merged


def save_settings(new_values: dict):
    with get_conn() as conn:
        for key, value in new_values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )


# --- Registro de alertas ya enviadas (para no repetir la misma) -----------

def was_already_notified(listing_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alert_log WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None


def mark_notified(listing_id: str, market_hash_name: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO alert_log (listing_id, market_hash_name, notified_at) VALUES (?, ?, ?)",
            (listing_id, market_hash_name, datetime.now(timezone.utc).isoformat()),
        )
