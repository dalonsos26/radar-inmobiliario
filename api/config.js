export default function handler(req, res) {
  res.setHeader('Cache-Control', 'public, s-maxage=3600');
  // El token pk. de Mapbox es público por diseño (se usa en el navegador)
  res.status(200).json({ mapboxToken: process.env.MAPBOX_TOKEN || '' });
}
