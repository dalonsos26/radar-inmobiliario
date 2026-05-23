#!/bin/bash
# Script llamado por launchd cada día a las 9:45 AM.
# Corre el scraper y publica el resultado en GitHub → Vercel.

cd "$(dirname "$0")"

echo "[$(date '+%H:%M:%S')] === Auto-deploy inicio ==="

# 1. Correr scraper
/usr/bin/python3 scraper.py \
  --username "diegoalonso15@hotmail.com" \
  --password "Ducook1234"

if [ $? -ne 0 ]; then
  echo "[$(date '+%H:%M:%S')] ERROR: scraper falló, abortando deploy"
  exit 1
fi

# 2. Publicar en GitHub (Vercel se actualiza automáticamente)
git add index.html data/properties.json data/history.json data/weekly_stats.json
git commit -m "Update $(date '+%Y-%m-%d %H:%M')" --quiet
git push origin main --quiet

echo "[$(date '+%H:%M:%S')] === Deploy completado ==="
