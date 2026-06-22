export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { lat, lng, mode, times } = req.body;
  if (!lat || !lng || !mode || !Array.isArray(times) || !times.length) {
    return res.status(400).json({ error: 'Faltan parámetros: lat, lng, mode, times[]' });
  }

  const costingMap = {
    'driving-car':  'auto',
    'foot-walking': 'pedestrian',
    'cycling-regular': 'bicycle',
  };
  const costing = costingMap[mode] || 'auto';

  const validMinutes = times.map(Number).filter(m => m > 0 && m <= 60);
  if (!validMinutes.length) {
    return res.status(400).json({ error: 'Tiempos inválidos' });
  }

  const body = {
    locations: [{ lat, lon: lng }],
    costing,
    contours: validMinutes.map(m => ({ time: m, color: 'ff7700' })),
    polygons: true,
    denoise: 1,
    generalize: 150,
  };

  try {
    const valRes = await fetch('https://valhalla1.openstreetmap.de/isochrone', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!valRes.ok) {
      const errText = await valRes.text();
      console.error('Valhalla error:', valRes.status, errText);
      return res.status(502).json({ error: 'Error del servidor de rutas: ' + valRes.status });
    }

    const geojson = await valRes.json();

    // Normalize to a property named "value" in seconds (like ORS) for frontend compatibility
    if (geojson.features) {
      geojson.features.forEach(feat => {
        const mins = feat.properties?.contour ?? feat.properties?.value;
        feat.properties.value = (mins || 0) * 60;
      });
    }

    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
    return res.status(200).json(geojson);
  } catch (err) {
    console.error('Isochrone fetch error:', err);
    return res.status(500).json({ error: 'Error interno: ' + err.message });
  }
}
