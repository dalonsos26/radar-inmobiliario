import { createHmac, timingSafeEqual } from 'crypto';

// APP_USERS env var format: "usuario:contraseña:rol,usuario2:contraseña2:rol"
// Roles: admin (puede actualizar) | viewer (solo lectura)

function parseUsers() {
  return (process.env.APP_USERS || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
    .map(s => {
      const [user, pass, role] = s.split(':');
      return { user, pass, role: role || 'viewer' };
    });
}

export function signToken(user, role) {
  const key = process.env.APP_USERS || '';
  const payload = `${user}|${role}`;
  const sig = createHmac('sha256', key).update(payload).digest('hex');
  return Buffer.from(`${payload}|${sig}`).toString('base64');
}

export function verifyToken(token) {
  try {
    const raw = Buffer.from(token, 'base64').toString('utf8');
    const [user, role, sig] = raw.split('|');
    const key = process.env.APP_USERS || '';
    const expected = createHmac('sha256', key).update(`${user}|${role}`).digest('hex');
    const a = Buffer.from(sig, 'hex');
    const b = Buffer.from(expected, 'hex');
    if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
    return { user, role };
  } catch (e) {
    return null;
  }
}

export default function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  const users = parseUsers();
  if (!users.length) return res.status(500).json({ error: 'No hay usuarios configurados (APP_USERS)' });

  const { user, pass } = req.body || {};
  const match = users.find(u => u.user === user && u.pass === pass);
  if (!match) return res.status(401).json({ error: 'Usuario o contraseña incorrectos' });

  return res.status(200).json({ ok: true, user: match.user, role: match.role, token: signToken(match.user, match.role) });
}
