// Disparo puntual del scraper vía Vercel Cron (GitHub Actions retrasa sus
// schedules horas en horario pico). Protegido con CRON_SECRET.
export default async function handler(req, res) {
  const auth = req.headers.authorization || '';
  if (!process.env.CRON_SECRET || auth !== `Bearer ${process.env.CRON_SECRET}`) {
    return res.status(401).json({ error: 'no autorizado' });
  }
  const token = process.env.GITHUB_PAT;
  if (!token) return res.status(500).json({ error: 'no configurado' });
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
    return res.status(r.status === 204 ? 200 : 502).json({ ok: r.status === 204 });
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
}
