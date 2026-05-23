#!/bin/bash
# Doble clic en este archivo para actualizar el radar y publicarlo en línea.
cd "$(dirname "$0")"

echo "=============================="
echo "  Radar Inmobiliario · Nocnok"
echo "=============================="
echo ""
echo "1/3  Scrapeando propiedades…"

python3 scraper.py -u "diegoalonso15@hotmail.com" -p "Ducook1234"

if [ $? -ne 0 ]; then
  echo ""
  echo "ERROR: el scraper falló. Revisa la conexión a internet."
  read -p "Presiona Enter para cerrar..."
  exit 1
fi

echo ""
echo "2/3  Publicando en línea…"
git add index.html data/properties.json data/history.json data/weekly_stats.json 2>/dev/null
git commit -m "Update $(date '+%Y-%m-%d %H:%M')" --quiet 2>/dev/null
git push origin main --quiet 2>/dev/null && echo "     ✓ Publicado en Vercel" || echo "     (Sin conexión a GitHub — solo local)"

echo ""
echo "3/3  Abriendo reporte…"
open index.html
