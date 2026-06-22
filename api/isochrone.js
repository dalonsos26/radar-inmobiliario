const VALHALLA_HOSTS = [
  'https://valhalla1.openstreetmap.de',
  'https://valhalla2.openstreetmap.de',
  'https://valhalla3.openstreetmap.de',
];

const costingMap = {
  'driving-car':     'auto',
  'foot-walking':    'pedestrian',
  'cycling-regular': 'bicycle',
};

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { lat, lng, mode, times } = req.body;
  if (!lat || !lng || !mode || !Array.isArray(times) || !times.length) {
    return res.status(400).json({ error: 'Faltan parámetros: lat, lng, mode, times[]' });
  }

  const costing = costingMap[mode] || 'auto';
  const validMinutes = times.map(Number).filter(m => m > 0 && m <= 60);
  if (!validMinutes.length) {
    return res.status(400).json({ error: 'Tiempos inválidos' });
  }

  const body = JSON.stringify({
    locations: [{ lat, lon: lng }],
    costing,
    contours: validMinutes.map(m => ({ time: m })),
    polygons: true,
    denoise: 1,
    generalize: 150,
  });

  let lastErr = null;
  for (const host of VALHALLA_HOSTS) {
    try {
      const valRes = await fetch(`${host}/isochrone`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: AbortSignal.timeout(12000),
      });

      if (!valRes.ok) {
        lastErr = `HTTP ${valRes.status} from ${host}`;
        continue;
      }

      const geojson = await valRes.json();

      if (geojson.features) {
        geojson.features.forEach(feat => {
          feat.properties.value = (feat.properties.contour || 0) * 60;
        });
      }

      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json(geojson);
    } catch (err) {
      lastErr = err.message;
    }
  }

  console.error('All Valhalla hosts failed:', lastErr);
  return res.status(502).json({ error: 'Servicio de isócronas no disponible: ' + lastErr });
}
