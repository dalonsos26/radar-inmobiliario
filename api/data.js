import { readFileSync } from 'fs';
import { join } from 'path';

const ALLOWED = new Set(['properties', 'oportunidades', 'map_props', 'weekly_stats']);

export default function handler(req, res) {
  const { file } = req.query;
  if (!file || !ALLOWED.has(file)) {
    return res.status(400).json({ error: 'file param required: properties | oportunidades | map_props | weekly_stats' });
  }
  try {
    const filePath = join(process.cwd(), 'data', `${file}.json`);
    const content = readFileSync(filePath, 'utf-8');
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'public, s-maxage=300, stale-while-revalidate=600');
    return res.status(200).send(content);
  } catch (e) {
    return res.status(404).json({ error: 'not found' });
  }
}
