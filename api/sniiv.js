// Proxy a la API de cubos del SNIIV (SEDATU) — evita problemas de CORS
// y cachea 1 día. Docs: https://sniiv.sedatu.gob.mx/Reporte/Datos_abiertos
const CUBOS = {
  registro:       (y, e, m) => `GetRegistro/${y}/${e}/${m}/tipo_vivienda`,
  financiamiento: (y, e, m) => `GetFinanciamiento/${y}/${e}/${m}/destino_credito`,
};

export default async function handler(req, res) {
  const { cubo, ent, mun, years } = req.query;
  if (!CUBOS[cubo] || !/^\d{2}$/.test(ent || '') || !/^\d{3}$/.test(mun || '')) {
    return res.status(400).json({ error: 'params: cubo=registro|financiamiento, ent=05, mun=035, years=2025,2026' });
  }
  const y = (years || String(new Date().getFullYear())).replace(/[^0-9,]/g, '');
  try {
    const r = await fetch(`https://sniiv.sedatu.gob.mx/api/CuboAPI/${CUBOS[cubo](y, ent, mun)}`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!r.ok) return res.status(502).json({ error: 'SNIIV ' + r.status });
    const data = await r.json();
    res.setHeader('Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=172800');
    return res.status(200).json(data);
  } catch (e) {
    return res.status(502).json({ error: e.message });
  }
}
