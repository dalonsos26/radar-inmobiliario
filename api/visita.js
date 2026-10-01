// Registra visitas a reportes compartidos. Guarda contadores en la rama
// "visitas" (no dispara deploys de Vercel, que solo observa main).
const REPO = 'dalonsos26/radar-inmobiliario';
const BRANCH = 'visitas';
const FILE = 'visitas.json';

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });

  const token = process.env.GITHUB_PAT;
  if (!token) return res.status(500).json({ error: 'no configurado' });

  const { id, nom } = req.body || {};
  if (!id || !/^[A-Za-z0-9_-]{4,40}$/.test(id)) return res.status(400).json({ error: 'id inválido' });

  const gh = (path, opts) => fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    ...opts,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
      'X-GitHub-Api-Version': '2022-11-28',
      ...(opts && opts.headers),
    },
  });

  try {
    // Leer el archivo actual (si existe)
    let sha = null, data = {};
    const r = await gh(`contents/${FILE}?ref=${BRANCH}`);
    if (r.status === 200) {
      const j = await r.json();
      sha = j.sha;
      try { data = JSON.parse(Buffer.from(j.content, 'base64').toString('utf8')); } catch (e) {}
    }
    const now = new Date().toISOString();
    if (!data[id]) data[id] = { n: 0, nom: String(nom || '').slice(0, 60), creado: now };
    data[id].n += 1;
    data[id].ultima = now;

    const put = await gh(`contents/${FILE}`, {
      method: 'PUT',
      body: JSON.stringify({
        message: `visita ${id}`,
        content: Buffer.from(JSON.stringify(data, null, 1)).toString('base64'),
        branch: BRANCH,
        ...(sha ? { sha } : {}),
      }),
    });
    if (!put.ok) return res.status(502).json({ error: 'GitHub ' + put.status });
    return res.status(200).json({ ok: true, n: data[id].n });
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
}
