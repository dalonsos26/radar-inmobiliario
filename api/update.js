import { verifyToken } from './login.js';

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  // Solo admins pueden disparar la actualización
  const auth = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  const session = verifyToken(auth);
  if (!session || session.role !== 'admin') {
    return res.status(403).json({ error: 'Solo administradores pueden actualizar' });
  }

  const token = process.env.GITHUB_PAT;
  if (!token) return res.status(500).json({ error: 'No configurado' });

  try {
    const r = await fetch(
      'https://api.github.com/repos/dalonsos26/radar-inmobiliario/actions/workflows/update.yml/dispatches',
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'application/vnd.github.v3+json',
          'Content-Type': 'application/json',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
    if (r.status === 204) {
      res.status(200).json({ ok: true });
    } else {
      const txt = await r.text();
      res.status(500).json({ ok: false, error: txt });
    }
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
}
