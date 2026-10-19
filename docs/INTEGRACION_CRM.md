# Integración CRM ← Radar Inmobiliario (datos Nocnok)

Guía para consumir los datos del radar desde el CRM de NEAR. **No hagas otro
scraper de Nocnok**: el radar ya descarga, normaliza, deduplica y geocodifica
todo el catálogo Comercial + Industrial de Torreón, Gómez Palacio y Matamoros,
y lo publica como JSON estático. Scrapear por segunda vez duplicaría riesgo
(Cloudflare ya bloqueó el acceso automatizado una vez) y mantenimiento.

## Fuentes de datos (JSON públicos, sin autenticación)

Base: `https://raw.githubusercontent.com/dalonsos26/radar-inmobiliario/main/data/`

| Archivo | Contenido |
|---|---|
| `properties.json` | Catálogo activo completo (~4,700 propiedades). Objeto `{run_at, total, new_count, properties:[…]}` |
| `oportunidades.json` | `{delisted:[…], price_drops:[…]}` — dadas de baja y bajas de precio detectadas |
| `map_props.json` | Subconjunto con `lat`/`lng` geocodificados para mapa |
| `weekly_stats.json` | Series semanales (publicaciones, $/m² por tipo y ciudad) |
| `archive.json` | Histórico de propiedades que ya no están activas |

Alternativa con CDN: `https://radar-inmobiliario-eight.vercel.app/api/data?file=properties`
(mismo contenido; `file` ∈ properties, oportunidades, map_props, weekly_stats, denue).

## Cadencia de actualización

- **Diario ~9:00 am CDMX** (cron de Vercel) con respaldo ~3:00 pm (GitHub Actions).
- `run_at` en `properties.json` trae el timestamp de la última corrida — úsalo para
  saber si ya corrió hoy antes de sincronizar.

## Esquema de cada propiedad (`properties[]`)

| Campo | Ejemplo | Nota |
|---|---|---|
| `id` | `a46afcfd-…` | **UUID de Nocnok — úsalo como llave de upsert** |
| `code` | `NN-GWG081` | Folio corto visible |
| `title` | Terreno Comercial en Venta… | |
| `categoria` | Comercial / Industrial | |
| `tipo` | Terreno, Bodega, Local, Oficina… | |
| `operacion` / `op_key` | Venta / `venta` | `op_key` ∈ venta, renta |
| `precio` / `precio_num` | `5,100,000 MXN Venta` / `5100000.0` | usa el numérico |
| `superficie` / `area_num` | `315 m²` / `315.0` | |
| `precio_m2` / `pm2_num` | `$16,190/m²` / `16190.48` | |
| `ubicacion` / `municipio` / `estado` | | `municipio` ∈ Torreón, Gómez Palacio, Matamoros |
| `broker` | PEPE PROPIEDADES | |
| `fecha_publicacion` | `2026-10-19` | fecha de (re)publicación en Nocnok |
| `days_on_market` | `0` | días desde publicación |
| `link` | URL al listing en Nocnok | requiere sesión Nocnok para abrir |
| `fotos_local` | URLs S3 de Nocnok | hotlinkeables |
| `is_new` | bool | nueva en la corrida de hoy (global, no por usuario) |

## Reglas de sincronización sugeridas

1. **Upsert por `id`**: inserta si no existe, actualiza precio/fecha/DOM si cambió.
2. **Bajas**: una propiedad que desaparece de `properties.json` ya no está activa.
   Cruza con `oportunidades.json → delisted` (baja detectada hoy) y `archive.json`
   para marcar inactivas en el CRM en lugar de borrarlas.
3. **Cambios de precio**: `oportunidades.json → price_drops` trae
   `original_precio` vs `current_precio` ya calculados.
4. **Coordenadas**: toma `lat`/`lng` de `map_props.json` (unido por `id`); no todas
   las propiedades están geocodificadas.
5. Corre la sincronización **después de las 9:30 am CDMX** o compara `run_at`.

## Accesos

- Los JSON de arriba son públicos: el CRM no necesita credenciales de Nocnok
  ni del radar para leerlos.
- El **radar como app** (https://radar-inmobiliario-eight.vercel.app) sí pide
  usuario: Diego da de alta a cada persona en la variable `APP_USERS` de Vercel
  (rol `viewer` para solo consulta).
- Las credenciales de Nocnok viven únicamente en los secrets de GitHub Actions
  del radar; ningún otro sistema debe guardarlas.

## Reportes de ubicación (opcional para el CRM)

Cada propiedad puede enlazarse a un análisis de mercado interactivo:
`https://radar-inmobiliario-eight.vercel.app/reporte?lat=<lat>&lng=<lng>&nom=<nombre>&modo=driving-car&t=5,10,15`
— demografía INEGI, nivel socioeconómico y entorno comercial de la zona,
calculado en vivo. Útil como botón "Analizar zona" en la ficha del CRM.
