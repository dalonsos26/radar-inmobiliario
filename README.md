# Radar Inmobiliario — Nocnok Scraper

Extrae propiedades comerciales e industriales de la Bolsa Inmobiliaria de Nocnok para:
- Torreón, Coahuila
- Gómez Palacio, Durango
- Matamoros, Coahuila

## Instalación

```bash
pip3 install playwright
~/.local/bin/playwright install chromium   # o la ruta que indique pip
```

## Uso

### Paso 1 — Exploración (primera vez)
Corre esto para ver el DOM del portal y calibrar selectores:

```bash
python3 explore.py -u TU_EMAIL -p TU_PASSWORD
```
Genera screenshots en `data/` y dumps de HTML para inspeccionar.

### Paso 2 — Scraper completo

```bash
# Con credenciales directas
python3 scraper.py -u TU_EMAIL -p TU_PASSWORD

# Con navegador visible (útil para depurar)
python3 scraper.py -u TU_EMAIL -p TU_PASSWORD --visible

# Con variables de entorno
export NOCNOK_USER=tu@email.com
export NOCNOK_PASS=tu_password
python3 scraper.py

# Cambiar rango de días (default: 7)
python3 scraper.py -u EMAIL -p PASS --days 14
```

## Salidas

| Archivo | Descripción |
|---------|-------------|
| `data/properties.json` | Propiedades de la última corrida |
| `data/history.json` | Historial de IDs vistos |
| `report.html` | Reporte visual (abrir en navegador) |
| `data/debug_*.png` | Screenshots de depuración |

## Flujo de "NUEVAS"

- Primera corrida: todas las propiedades se marcan NUEVAS
- Corridas siguientes: solo las que no aparecieron antes se marcan NUEVAS
- El historial persiste en `data/history.json`

## Estructura del JSON de propiedades

```json
{
  "run_at": "2024-01-15 10:30:00",
  "total": 42,
  "new_count": 5,
  "properties": [
    {
      "id": "abc123",
      "title": "Bodega Industrial en Torreón",
      "tipo": "Bodega",
      "operacion": "Renta",
      "precio": "$25,000 MXN/mes",
      "superficie": "500 m²",
      "ubicacion": "Torreón, Coahuila",
      "broker": "Juan Pérez",
      "fecha_publicacion": "2024-01-14",
      "link": "https://app.nocnok.com/...",
      "fotos": ["data:image/jpeg;base64,..."]
    }
  ]
}
```
