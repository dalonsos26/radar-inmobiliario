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

// km per minute approximations for fallback circles
const SPEED_KM_MIN = {
  'driving-car':     0.55, // ~33 km/h city average
  'foot-walking':    0.083, // ~5 km/h
  'cycling-regular': 0.25,  // ~15 km/h
};

function circleFeature(lat, lon, radiusKm, mins) {
  const pts = 72;
  const coords = [];
  for (let i = 0; i <= pts; i++) {
    const a = (i / pts) * 2 * Math.PI;
    const dLat = (radiusKm / 111.32) * Math.cos(a);
    const dLon = (radiusKm / (111.32 * Math.cos(lat * Math.PI / 180))) * Math.sin(a);
    coords.push([lon + dLon, lat + dLat]);
  }
  return {
    type: 'Feature',
    properties: { contour: mins, value: mins * 60, fallback: true },
    geometry: { type: 'Polygon', coordinates: [coords] },
  };
}

function fallbackGeoJSON(lat, lon, mode, minutes) {
  const speed = SPEED_KM_MIN[mode] || SPEED_KM_MIN['driving-car'];
  return {
    type: 'FeatureCollection',
    features: minutes.map(m => circleFeature(lat, lon, m * speed, m)),
  };
}

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

  // Try each Valhalla host
  for (const host of VALHALLA_HOSTS) {
    try {
      const valRes = await fetch(`${host}/isochrone`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: AbortSignal.timeout(8000),
      });

      if (!valRes.ok) continue;

      const geojson = await valRes.json();
      if (geojson.features) {
        geojson.features.forEach(feat => {
          feat.properties.value = (feat.properties.contour || 0) * 60;
        });
      }

      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json(geojson);
    } catch (_) {
      // try next host
    }
  }

  // All Valhalla hosts failed — return circle approximations
  console.log('Valhalla unavailable, using circle fallback');
  const fallback = fallbackGeoJSON(lat, lng, mode, validMinutes);
  return res.status(200).json(fallback);
}
