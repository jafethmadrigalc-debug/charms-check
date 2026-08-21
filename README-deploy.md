# CS2 Colgantes Monitor — App Web (Fly.io)

App que:
1. Mantiene una base de datos con los **926 colgantes de highlight** de
   Austin 2025, Budapest 2025 y Cologne 2026.
2. Actualiza sus precios en segundo plano, repartidos a lo largo de
   **6 horas por ciclo completo** (configurable) para no golpear a Steam.
3. Te muestra una tabla con checkboxes para elegir cuáles te interesan.
4. Al presionar "Comparar seleccionados", solo en ese momento consulta el
   Mercado de Steam en vivo para ver si hay armas con ese colgante puesto
   vendiéndose por menos que el valor del colgante.

## Estructura del proyecto

```
steam_webapp/
├── app/
│   ├── main.py           # FastAPI: rutas /, /api/charms, /api/compare
│   ├── db.py              # SQLite: guarda los 926 colgantes y sus precios
│   ├── steam_client.py    # Llamadas a Steam (solo lectura pública)
│   ├── price_updater.py   # Tarea de fondo: actualiza precios cada 6h
│   └── static/index.html  # Frontend (tabla + checkboxes + resultados)
├── charms_database.json   # Datos semilla (926 colgantes)
├── requirements.txt
├── Dockerfile
└── fly.toml
```

## Probarlo en tu computadora primero (recomendado)

```bash
pip install -r requirements.txt
export DB_PATH=./charms.db
uvicorn app.main:app --reload --port 8080
```

Abre `http://localhost:8080` en el navegador. Deberías ver la tabla con los
926 colgantes (precios en `None`/"sin datos" al inicio — se van llenando
solos en segundo plano, repartidos en 6 horas).

## Desplegar en Fly.io

1. Instala `flyctl` si no lo tienes:
   - Mac: `brew install flyctl`
   - Windows (PowerShell): `iwr https://fly.io/install.ps1 -useb | iex`
   - Linux: `curl -L https://fly.io/install.sh | sh`

2. Inicia sesión:
   ```bash
   flyctl auth login
   ```

3. Desde la carpeta `steam_webapp/`, lanza la app (usa el `fly.toml` que ya
   está incluido; te preguntará algunas cosas, puedes aceptar los valores
   por defecto salvo el nombre de la app si ya está tomado):
   ```bash
   flyctl launch --no-deploy
   ```
   Si te pide sobreescribir `fly.toml`, di que **no** (ya está configurado).

4. Crea el volumen persistente donde vive la base de datos SQLite (si no
   se crea solo desde `fly.toml`):
   ```bash
   flyctl volumes create charms_data --size 1 --region mia
   ```
   (usa la misma región que pusiste en `fly.toml`, `mia` = Miami por
   defecto — puedes usar `flyctl platform regions` para ver otras).

5. Despliega:
   ```bash
   flyctl deploy
   ```

6. Abre la app:
   ```bash
   flyctl open
   ```

## Variables de entorno ajustables (en `fly.toml`, sección `[env]`)

- `PRICE_CYCLE_HOURS` — cuántas horas debe tardar un ciclo completo
  actualizando los 926 precios (default 6).
- `REMOVE_KEYCHAIN_COST` — costo de "Quitar colgante" en el juego.
- `STEAM_CURRENCY` — código de moneda de Steam (1 = USD por defecto).
- `WEAR_CONDITIONS` — desgastes a revisar al comparar (por defecto solo
  `Field-Tested`; puedes poner `Factory New,Minimal Wear,Field-Tested` si
  quieres cubrir más, pero cada uno multiplica las peticiones al comparar).

Para cambiar algo, edita `fly.toml` y vuelve a correr `flyctl deploy`.

## Notas importantes

- **No pude probar la app contra el Mercado de Steam real** porque mi
  entorno de trabajo no tiene salida de red hacia `steamcommunity.com`
  (solo probé que el servidor arranca, sirve la tabla, guarda datos en
  SQLite y no se cae si una consulta falla). En Fly.io sí vas a tener
  acceso real, así que ahí sí debería traer precios de verdad — pero
  revisa los primeros logs (`flyctl logs`) para confirmar que las
  consultas a Steam funcionan y no te está bloqueando el user-agent u otra
  cosa.
- El botón "Comparar seleccionados" hace peticiones en vivo mientras
  esperas — si marcas muchos colgantes con muchas armas cada uno, puede
  tardar bastante (cada arma+desgaste es una petición con ~2s de pausa).
  Empieza marcando pocos para probar.
- Sigue siendo solo lectura de datos públicos — no inicia sesión, no
  compra, no vende. Eso lo sigues haciendo tú manualmente.
- Fly.io no es gratis indefinidamente; revisa su tabla de precios actual
  antes de dejarlo corriendo, especialmente con `min_machines_running = 1`
  (la máquina queda encendida todo el tiempo para que el actualizador de
  fondo siga corriendo).
