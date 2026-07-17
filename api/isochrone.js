const MAPBOX_TOKEN = process.env.MAPBOX_TOKEN || '';

const mapboxProfileMap = {
  'driving-car':     'mapbox/driving-traffic', // typical traffic profiles
  'foot-walking':    'mapbox/walking',
  'cycling-regular': 'mapbox/cycling',
};

async function tryMapbox(lat, lng, mode, validMinutes) {
  if (!MAPBOX_TOKEN) return null;
  const profile = mapboxProfileMap[mode] || 'mapbox/driving-traffic';
  const sorted = [...validMinutes].sort((a, b) => a - b);

  // Mapbox allows max 4 contours per request — chunk if needed
  const chunks = [];
  for (let i = 0; i < sorted.length; i += 4) chunks.push(sorted.slice(i, i + 4));

  const features = [];
  for (const chunk of chunks) {
    const url = `https://api.mapbox.com/isochrone/v1/${profile}/${lng},${lat}` +
      `?contours_minutes=${chunk.join(',')}&polygons=true&denoise=1&access_token=${MAPBOX_TOKEN}`;
    const res = await fetch(url, { signal: AbortSignal.timeout(10000) });
    if (!res.ok) {
      console.error('Mapbox error:', res.status, await res.text());
      return null;
    }
    const gj = await res.json();
    if (!gj.features) return null;
    gj.features.forEach(f => {
      f.properties.value = (f.properties.contour || 0) * 60;
      features.push(f);
    });
  }
  return { type: 'FeatureCollection', features };
}

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

const orsProfileMap = {
  'driving-car':     'driving-car',
  'foot-walking':    'foot-walking',
  'cycling-regular': 'cycling-regular',
};

const SPEED_KM_MIN = {
  'driving-car':     0.55,
  'foot-walking':    0.083,
  'cycling-regular': 0.25,
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

async function tryORS(lat, lng, mode, validMinutes) {
  const apiKey = process.env.ORS_API_KEY;
  if (!apiKey) return null;

  const profile = orsProfileMap[mode] || 'driving-car';
  const url = `https://api.openrouteservice.org/v2/isochrones/${profile}`;

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': apiKey,
    },
    body: JSON.stringify({
      locations: [[lng, lat]],
      range: validMinutes.map(m => m * 60), // ORS uses seconds; road-network based, no real-time traffic
      range_type: 'time',
      smoothing: 0.25,
    }),
    signal: AbortSignal.timeout(10000),
  });

  if (!res.ok) {
    const txt = await res.text();
    console.error('ORS error:', res.status, txt);
    return null;
  }

  const geojson = await res.json();

  // Normalize: ORS returns "value" in seconds (already scaled by 0.5).
  // Map each feature back to the original user-requested minutes by rank.
  if (geojson.features) {
    geojson.features.sort((a, b) => a.properties.value - b.properties.value);
    geojson.features.forEach((feat, i) => {
      feat.properties.contour = validMinutes[i] || Math.round((feat.properties.value || 0) / 60 / 0.5);
    });
  }

  return geojson;
}

async function tryValhalla(lat, lng, mode, validMinutes) {
  const costing = costingMap[mode] || 'auto';
  const body = JSON.stringify({
    locations: [{ lat, lon: lng }],
    costing,
    contours: validMinutes.map(m => ({ time: m })),
    polygons: true,
    denoise: 1,
    generalize: 150,
  });

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
      return geojson;
    } catch (_) { }
  }
  return null;
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { lat, lng, mode, times } = req.body;
  if (!lat || !lng || !mode || !Array.isArray(times) || !times.length) {
    return res.status(400).json({ error: 'Faltan parámetros: lat, lng, mode, times[]' });
  }

  const validMinutes = times.map(Number).filter(m => m > 0 && m <= 60);
  if (!validMinutes.length) {
    return res.status(400).json({ error: 'Tiempos inválidos' });
  }

  // 1. Try Mapbox (traffic-aware profiles)
  try {
    const mb = await tryMapbox(lat, lng, mode, validMinutes);
    if (mb) {
      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json(mb);
    }
  } catch (e) {
    console.error('Mapbox failed:', e.message);
  }

  // 2. Try ORS (if API key configured)
  try {
    const ors = await tryORS(lat, lng, mode, validMinutes);
    if (ors) {
      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json(ors);
    }
  } catch (e) {
    console.error('ORS failed:', e.message);
  }

  // 2. Try Valhalla public servers
  try {
    const val = await tryValhalla(lat, lng, mode, validMinutes);
    if (val) {
      res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
      return res.status(200).json(val);
    }
  } catch (e) {
    console.error('Valhalla failed:', e.message);
  }

  // 3. Fallback: circle approximation
  console.log('Using circle fallback');
  return res.status(200).json(fallbackGeoJSON(lat, lng, mode, validMinutes));
}
