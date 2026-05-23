#!/usr/bin/env python3
"""Nocnok Real Estate Scraper — Bolsa Inmobiliaria"""

import asyncio
import json
import os
import re
import sys
import argparse
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PROPERTIES_FILE = DATA_DIR / "properties.json"
HISTORY_FILE    = DATA_DIR / "history.json"
WEEKLY_FILE     = DATA_DIR / "weekly_stats.json"
REPORT_FILE     = BASE_DIR / "index.html"

# ── Config ────────────────────────────────────────────────────────────────────
LOGIN_URL = "https://sso.nocnok.com/Login"
API_BASE  = "https://app.nocnok.com/api/v1"

TARGET_LOCATIONS = [
    "Torreón, Coahuila de Zaragoza",
    "Gómez Palacio, Durango",
    "Matamoros, Coahuila de Zaragoza",
]
TARGET_CATEGORIES = ["Commercial", "Industrial"]
DAYS_BACK = 7
PAGE_SIZE  = 36


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Auth ──────────────────────────────────────────────────────────────────────

async def login_and_get_session(username: str, password: str):
    pw  = await async_playwright().start()
    br  = await pw.chromium.launch(headless=True)
    ctx = await br.new_context(viewport={"width": 1440, "height": 900}, locale="es-MX")
    pg  = await ctx.new_page()
    token = None

    async def capture_auth(req):
        nonlocal token
        if "api/v1" in req.url and token is None:
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth
                log(f"  Token capturado: {auth[:30]}…")

    pg.on("request", capture_auth)
    log("Login en SSO…")
    await pg.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    await pg.locator("input[type='email']").first.fill(username)
    await pg.locator("input[type='password']").first.fill(password)
    await pg.locator("button[type='submit']").first.click()

    try:
        await pg.wait_for_url("**app.nocnok**", timeout=20000)
    except Exception:
        pass
    await pg.wait_for_load_state("networkidle", timeout=20000)
    log(f"  Post-login: {pg.url}")

    await pg.locator("a:has-text('Bolsa')").first.click()
    await pg.wait_for_load_state("networkidle", timeout=20000)
    await pg.wait_for_timeout(2000)

    all_cookies = await ctx.cookies()
    cookies_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)

    if not token:
        stored = await pg.evaluate("""() => {
            for (const k of Object.keys(localStorage)) {
                const v = localStorage.getItem(k) || '';
                if (v.startsWith('Bearer ')) return v;
                try {
                    const o = JSON.parse(v);
                    if (o.access_token) return 'Bearer ' + o.access_token;
                    if (o.token) return 'Bearer ' + o.token;
                } catch {}
            }
            return null;
        }""")
        if stored:
            token = stored

    return pw, br, ctx, pg, token or "", cookies_str


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _headers(token: str, cookies: str) -> dict:
    h = {
        "Accept": "application/json",
        "Accept-Language": "es-MX,es;q=0.9",
        "Referer": "https://app.nocnok.com/",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    }
    if token:
        h["Authorization"] = token
    if cookies:
        h["Cookie"] = cookies
    return h


def api_get(url: str, token: str, cookies: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers=_headers(token, cookies))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log(f"  HTTP {e.code}: {url[:80]}")
        return None
    except Exception as e:
        log(f"  Error: {e} | {url[:80]}")
        return None


# ── Precio por m² ─────────────────────────────────────────────────────────────

def parse_number(text: str) -> Optional[float]:
    clean = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def calc_precio_m2(precio_num: Optional[float], area_num: Optional[float]) -> str:
    if not precio_num or not area_num or area_num == 0:
        return ""
    val = precio_num / area_num
    return f"${val:,.0f}/m²" if val >= 1000 else f"${val:,.1f}/m²"


# ── Normalizar item ────────────────────────────────────────────────────────────

def normalize_item(item: dict) -> dict:
    account    = item.get("account", {})
    pics       = item.get("pictures", [])
    price_text = item.get("priceText", "")
    sale_price = item.get("salePrice")
    if not price_text and sale_price:
        price_text = f"${sale_price:,.0f}"

    precio_num = float(sale_price) if sale_price else parse_number(price_text)
    const_size = item.get("constructionSize")
    lot_size   = item.get("lotSize")
    area_num   = float(const_size or lot_size or 0) or None
    area_text  = item.get("constructionSizeText") or item.get("lotSizeText") or ""

    op_list = item.get("operation", [])
    op_text = item.get("operationText") or (", ".join(op_list) if op_list else "")
    op_raw  = op_text.lower()
    if "venta" in op_raw or "remate" in op_raw:
        op_key = "venta"
    elif "renta" in op_raw:
        op_key = "renta"
    else:
        op_key = "otro"

    link = (item.get("siteUrl") or item.get("sharedUrl") or item.get("marketplaceUrl") or
            f"https://app.nocnok.com/crm/154940/properties/{item.get('id','')}")

    status_date    = item.get("statusDate", "")
    fecha          = ""
    days_on_market = 0
    if status_date:
        try:
            pub_dt         = datetime.fromisoformat(status_date.replace("Z", ""))
            fecha          = pub_dt.strftime("%Y-%m-%d")
            days_on_market = max(0, (datetime.now() - pub_dt).days)
        except Exception:
            fecha = status_date[:10]

    def to_public(url: str) -> str:
        return url[:-5] + "s.webp" if url.endswith(".webp") else url

    pm2_num = round(precio_num / area_num, 2) if (precio_num and area_num and area_num > 0) else None

    return {
        "id":                item.get("id", ""),
        "code":              item.get("code", ""),
        "title":             item.get("title", "Sin título").strip(),
        "categoria":         item.get("categoryText", item.get("category", "")),
        "tipo":              item.get("typeText", item.get("type", "")),
        "operacion":         op_text,
        "op_key":            op_key,
        "precio":            price_text,
        "precio_num":        precio_num,
        "superficie":        area_text,
        "area_num":          area_num,
        "precio_m2":         calc_precio_m2(precio_num, area_num),
        "pm2_num":           pm2_num,
        "ubicacion":         item.get("location", ""),
        "municipio":         item.get("municipality", ""),
        "estado":            item.get("state", ""),
        "broker":            account.get("name", ""),
        "fecha_publicacion": fecha,
        "days_on_market":    days_on_market,
        "link":              link,
        "fotos_local":       [to_public(u) for u in pics[:3]],
    }


# ── Scraping paginado ─────────────────────────────────────────────────────────

def is_within_days(date_str: Optional[str], days: int = DAYS_BACK) -> bool:
    if not date_str:
        return True
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", ""))
        return dt >= datetime.now() - timedelta(days=days)
    except Exception:
        return True


def build_api_url(location: str, category: str, page: int) -> str:
    params = [
        ("pageNumber", str(page)), ("pageSize", str(PAGE_SIZE)),
        ("sortBy", "StatusDate"), ("sortDirection", "Descending"),
        ("locations", location), ("categories", category),
    ]
    return f"{API_BASE}/search/properties/all?" + urllib.parse.urlencode(params)


def fetch_one_stream(location: str, category: str, token: str, cookies: str) -> list:
    results, page, total_pages = [], 1, None
    while True:
        url  = build_api_url(location, category, page)
        data = api_get(url, token, cookies)
        if not data or not data.get("success"):
            log(f"    ⚠ Respuesta inválida (pág {page})")
            break
        items       = data["data"].get("items", [])
        total_pages = total_pages or data["data"].get("pageCount", 1)
        if page == 1:
            log(f"    {data['data'].get('totalItems',0):,} items / {total_pages} páginas")
        stop = False
        for item in items:
            if not is_within_days(item.get("statusDate", ""), DAYS_BACK):
                stop = True
                break
            results.append(normalize_item(item))
        if stop or page >= total_pages:
            break
        page += 1
    return results


def fetch_all_properties(token: str, cookies: str) -> list:
    seen: set = set()
    all_props  = []
    for loc in TARGET_LOCATIONS:
        for cat in TARGET_CATEGORIES:
            log(f"  → {loc} | {cat}")
            batch = fetch_one_stream(loc, cat, token, cookies)
            added = 0
            for p in batch:
                if p["id"] and p["id"] not in seen:
                    seen.add(p["id"])
                    all_props.append(p)
                    added += 1
            log(f"    {added} nuevas en rango de {DAYS_BACK}d")
    return all_props


# ── Historia ──────────────────────────────────────────────────────────────────

def merge_with_history(props: list) -> set:
    history = load_json(HISTORY_FILE, {"seen_ids": [], "first_seen": {}})
    seen    = set(history.get("seen_ids", []))
    first   = history.get("first_seen", {})
    now     = datetime.now().isoformat()
    new_ids: set = set()
    for p in props:
        pid = p["id"]
        if pid and pid not in seen:
            seen.add(pid)
            first[pid] = now
            new_ids.add(pid)
    save_json(HISTORY_FILE, {"seen_ids": list(seen), "first_seen": first})
    return new_ids


# ── Estadísticas semanales ────────────────────────────────────────────────────

def update_weekly_stats(props: list, new_ids: set) -> dict:
    stats = load_json(WEEKLY_FILE, {"weeks": {}})
    weeks = stats.setdefault("weeks", {})

    new_props = [p for p in props if p["id"] in new_ids]
    week_buckets: dict = defaultdict(list)
    for p in new_props:
        fecha = p.get("fecha_publicacion", "")
        if not fecha:
            continue
        try:
            dt       = datetime.fromisoformat(fecha)
            week_key = dt.strftime("%G-W%V")
            week_buckets[week_key].append(p)
        except Exception:
            continue

    for week_key, wprops in week_buckets.items():
        wk = weeks.setdefault(week_key, {"week": week_key, "total": 0, "cities": {}})
        wk["total"] += len(wprops)
        for p in wprops:
            city    = p.get("municipio", "Otro")
            city_wk = wk["cities"].setdefault(city, {"total": 0, "tipos_m2": {}})
            city_wk["total"] += 1
            tipo = p.get("tipo", "")
            pn   = p.get("precio_num")
            an   = p.get("area_num")
            if tipo and pn and an and an > 0:
                tm = city_wk["tipos_m2"].setdefault(tipo, {"sum": 0.0, "count": 0})
                tm["sum"]   += pn / an
                tm["count"] += 1

    save_json(WEEKLY_FILE, stats)
    return stats


# ── Reporte HTML ──────────────────────────────────────────────────────────────

def build_report(props: list, new_ids: set, run_ts: str, weekly_stats: dict) -> str:
    total   = len(props)
    num_new = sum(1 for p in props if p["id"] in new_ids)

    def esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")

    def field(label, val, cls=""):
        v = esc(val)
        if not v or v == "None":
            v = "—"
        return (f'<div class="field"><span class="lbl">{label}</span>'
                f'<span class="val {cls}">{v}</span></div>')

    # Sort: new first (desc fecha), then seen (desc fecha)
    new_props  = sorted([p for p in props if p["id"] in new_ids],
                        key=lambda p: p.get("fecha_publicacion",""), reverse=True)
    seen_props = sorted([p for p in props if p["id"] not in new_ids],
                        key=lambda p: p.get("fecha_publicacion",""), reverse=True)
    ordered = new_props + seen_props

    cards_html = ""
    for p in ordered:
        pid    = p.get("id", "")
        is_new = pid in new_ids
        op_key = p.get("op_key", "otro")
        fecha  = p.get("fecha_publicacion") or ""
        dom    = p.get("days_on_market", 0)
        municipio = p.get("municipio", "")
        pm2_num   = p.get("pm2_num") or 0
        tipo_val  = p.get("tipo", "")

        dom_cls = "dom-old" if dom > 30 else ("dom-mid" if dom > 7 else "dom-new")
        dom_tip = f"{dom} días en mercado" + (" — candidato a negociar" if dom > 30 else "")

        foto_srcs  = p.get("fotos_local") or []
        fotos_html = "".join(
            f'<img src="{esc(src)}" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
            for src in foto_srcs[:3]
        )
        fotos_block = (f'<div class="fotos">{fotos_html}</div>'
                       if fotos_html else '<div class="no-foto">Sin fotos</div>')

        link    = esc(p.get("link", ""))
        btn     = (f'<a href="{link}" target="_blank" rel="noopener" class="btn-ver">Ver listing →</a>'
                   if link else "")
        precio_m2 = p.get("precio_m2", "")
        pid_short = pid[:12]
        card_id   = f"c-{pid_short}"

        new_badge = '<span class="badge-new">★ NUEVA</span>' if is_new else ""
        cls       = "card new" if is_new else "card"

        cards_html += f"""
<div class="{cls}" id="{card_id}"
  data-id="{esc(pid)}"
  data-new="{'1' if is_new else '0'}"
  data-op="{op_key}"
  data-fecha="{fecha}"
  data-city="{esc(municipio)}"
  data-tipo="{esc(tipo_val)}"
  data-dom="{dom}"
  data-pm2="{pm2_num}">
  <div class="card-top">
    <div class="card-badges">
      {new_badge}
      <span class="badge-opp" id="opp-{pid_short}" style="display:none">💡 OPORTUNIDAD</span>
      <span class="dom-badge {dom_cls}" title="{esc(dom_tip)}">⏱ {dom}d</span>
    </div>
    <button class="fav-btn" id="fav-{pid_short}" onclick="toggleFav('{esc(pid)}',this)" title="Guardar en favoritos">☆</button>
    <span class="code">{esc(p.get("code",""))}</span>
    <h3>{esc(p.get("title","Sin título"))}</h3>
  </div>
  {fotos_block}
  <div class="card-body">
    {field("Tipo", tipo_val)}
    {field("Categoría", p.get("categoria"))}
    {field("Operación", p.get("operacion"))}
    {field("Precio", p.get("precio"), "price")}
    {field("$/m²", precio_m2, "price-m2") if precio_m2 else ""}
    {field("Superficie", p.get("superficie"))}
    {field("Ubicación", p.get("ubicacion"))}
    {field("Broker", p.get("broker"))}
    {field("Publicado", fecha)}
  </div>
  <div class="card-foot">{btn}</div>
</div>"""

    grid_html = f'<div class="grid" id="grid">{cards_html}</div>' if props else "<p class='empty'>No se encontraron propiedades.</p>"

    props_json  = json.dumps([{
        "id": p.get("id",""), "code": p.get("code",""), "title": p.get("title",""),
        "categoria": p.get("categoria",""), "tipo": p.get("tipo",""),
        "operacion": p.get("operacion",""), "op_key": p.get("op_key","otro"),
        "precio": p.get("precio",""), "precio_m2": p.get("precio_m2",""),
        "pm2_num": p.get("pm2_num") or 0, "superficie": p.get("superficie",""),
        "ubicacion": p.get("ubicacion",""), "municipio": p.get("municipio",""),
        "broker": p.get("broker",""), "fecha_publicacion": p.get("fecha_publicacion",""),
        "days_on_market": p.get("days_on_market",0),
        "link": p.get("link",""), "fotos_local": p.get("fotos_local",[]),
        "is_new": 1 if p.get("id","") in new_ids else 0,
    } for p in props], ensure_ascii=False)

    weekly_json = json.dumps(weekly_stats, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar Inmobiliario · Nocnok</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f4f6f9;--surface:#fff;--surface2:#f0f2f5;--border:#dde1ea;--border2:#c8cedd;
  --accent:#4f46e5;--accent-h:#3e38c4;--new:#059669;--new-bg:#ecfdf5;--new-bd:#6ee7b7;
  --text:#111827;--muted:#6b7280;--price:#b45309;--price2:#1d4ed8;
  --amber:#d97706;--red:#dc2626;--teal:#0f766e;
  --sh:0 1px 3px rgba(0,0,0,.08);--sh2:0 10px 25px rgba(0,0,0,.12);
}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;padding:1.5rem 1rem;min-height:100vh}}
header{{max-width:1420px;margin:0 auto 1rem;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:1.2rem 1.6rem;box-shadow:var(--sh)}}
h1{{font-size:1.5rem;font-weight:800;color:var(--accent);margin-bottom:.15rem}}
.sub{{color:var(--muted);font-size:.8rem;line-height:1.6}}
.stats{{display:flex;gap:.7rem;margin-top:1rem;flex-wrap:wrap}}
.stat{{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.5rem 1rem}}
.stat .n{{font-size:1.6rem;font-weight:800;line-height:1;color:var(--accent)}}
.stat .n.g{{color:var(--new)}}.stat .n.m{{color:var(--muted)}}.stat .n.f{{color:var(--amber)}}
.stat .d{{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:.1rem}}
.tab-bar{{max-width:1420px;margin:0 auto .7rem;display:flex;gap:.35rem;flex-wrap:wrap}}
.tab-btn{{background:var(--surface);border:1.5px solid var(--border);color:var(--muted);font-size:.8rem;font-weight:600;padding:.46rem 1rem;border-radius:10px;cursor:pointer;transition:all .15s;white-space:nowrap}}
.tab-btn:hover{{border-color:var(--accent);color:var(--accent)}}
.tab-btn.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.toolbar{{max-width:1420px;margin:0 auto .85rem;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.7rem 1.1rem;box-shadow:var(--sh);display:flex;flex-wrap:wrap;gap:.8rem;align-items:center}}
.tb-group{{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap}}
.tb-sep{{width:1px;height:22px;background:var(--border);align-self:center}}
.tb-lbl{{font-size:.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}}
.btn{{border:1.5px solid var(--border2);background:var(--surface);color:var(--text);font-size:.77rem;font-weight:600;padding:.34rem .85rem;border-radius:20px;cursor:pointer;transition:all .15s;white-space:nowrap;line-height:1}}
.btn:hover{{border-color:var(--accent);color:var(--accent)}}
.btn.on{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.btn.on-new{{background:var(--new);color:#fff;border-color:var(--new)}}
.btn.on-renta{{background:#0369a1;color:#fff;border-color:#0369a1}}
.btn.on-venta{{background:#7c3aed;color:#fff;border-color:#7c3aed}}
.btn.on-city{{background:var(--teal);color:#fff;border-color:var(--teal)}}
.sort-btn.on{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.count{{font-size:.78rem;color:var(--muted);margin-left:auto;white-space:nowrap}}
.grid{{max-width:1420px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(295px,1fr));gap:1rem}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;display:flex;flex-direction:column;box-shadow:var(--sh);transition:transform .15s,box-shadow .15s}}
.card:hover{{transform:translateY(-2px);box-shadow:var(--sh2)}}
.card.new{{border-color:var(--new-bd);background:var(--new-bg)}}
.card.hidden{{display:none!important}}
.card-top{{padding:.75rem .85rem .3rem;position:relative}}
.card-badges{{display:flex;flex-wrap:wrap;gap:.28rem;margin-bottom:.28rem;padding-right:2.2rem}}
.badge-new{{display:inline-block;background:var(--new);color:#fff;font-size:.6rem;font-weight:800;letter-spacing:.08em;padding:.15rem .48rem;border-radius:20px}}
.badge-opp{{display:inline-block;background:var(--amber);color:#fff;font-size:.6rem;font-weight:800;padding:.15rem .48rem;border-radius:20px}}
.dom-badge{{display:inline-block;font-size:.6rem;font-weight:700;padding:.15rem .45rem;border-radius:20px}}
.dom-new{{background:#dcfce7;color:#166534}}
.dom-mid{{background:#fef9c3;color:#854d0e}}
.dom-old{{background:#fee2e2;color:#991b1b}}
.fav-btn{{position:absolute;top:.65rem;right:.65rem;background:none;border:none;font-size:1.15rem;cursor:pointer;line-height:1;padding:.05rem;color:var(--muted);transition:color .15s}}
.fav-btn.active,.fav-btn:hover{{color:#f59e0b}}
.code{{font-size:.67rem;color:var(--muted);font-family:ui-monospace,monospace}}
.card-top h3{{font-size:.84rem;font-weight:600;line-height:1.35;margin-top:.12rem}}
.fotos{{display:flex;gap:2px;height:152px;overflow:hidden;background:var(--surface2)}}
.fotos img{{flex:1;object-fit:cover;min-width:0}}
.no-foto{{height:80px;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.74rem;background:var(--surface2)}}
.card-body{{padding:.62rem .85rem;display:flex;flex-direction:column;gap:.28rem;flex:1}}
.field{{display:flex;gap:.35rem;font-size:.77rem;line-height:1.35}}
.lbl{{color:var(--muted);min-width:68px;flex-shrink:0;font-weight:500}}
.val{{color:var(--text);font-weight:500}}
.val.price{{color:var(--price);font-weight:700}}
.val.price-m2{{color:var(--price2);font-weight:700;font-size:.74rem}}
.card-foot{{padding:.55rem .85rem;border-top:1px solid var(--border)}}
.card.new .card-foot{{border-top-color:var(--new-bd)}}
.btn-ver{{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;font-size:.74rem;font-weight:700;padding:.32rem .78rem;border-radius:7px;transition:background .15s}}
.btn-ver:hover{{background:var(--accent-h)}}
.empty{{max-width:1420px;margin:4rem auto;text-align:center;color:var(--muted)}}
.section{{max-width:1420px;margin:0 auto}}
.section-card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:1.4rem;box-shadow:var(--sh);margin-bottom:1rem}}
.section-card h2{{font-size:1rem;font-weight:700;color:var(--text);margin-bottom:.9rem;padding-bottom:.55rem;border-bottom:1px solid var(--border)}}
.broker-table{{width:100%;border-collapse:collapse;font-size:.8rem}}
.broker-table th{{text-align:left;padding:.45rem .65rem;background:var(--surface2);color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid var(--border)}}
.broker-table td{{padding:.5rem .65rem;border-bottom:1px solid var(--border);vertical-align:middle}}
.broker-table tr:hover td{{background:var(--surface2)}}
.broker-rank{{font-weight:800;color:var(--muted);font-size:.78rem}}
.broker-bar-wrap{{height:5px;background:var(--surface2);border-radius:3px;margin-top:3px;overflow:hidden}}
.broker-bar{{height:100%;background:var(--accent);border-radius:3px;opacity:.7}}
.chart-wrap{{position:relative;height:300px}}
.chart-empty{{display:flex;align-items:center;justify-content:center;height:300px;color:var(--muted);font-size:.85rem;text-align:center}}
.alerts-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.75rem;margin-bottom:1rem}}
.alert-item{{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.85rem}}
.alert-tipo{{font-size:.88rem;font-weight:700;color:var(--text);margin-bottom:.45rem}}
.alert-lbl{{font-size:.74rem;color:var(--muted);font-weight:600;display:block;margin-bottom:.3rem}}
.alert-input{{width:100%;border:1.5px solid var(--border2);border-radius:7px;padding:.38rem .65rem;font-size:.88rem;background:var(--surface);color:var(--text);outline:none}}
.alert-input:focus{{border-color:var(--accent)}}
.save-btn{{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.5rem 1.2rem;font-size:.82rem;font-weight:700;cursor:pointer}}
.save-btn:hover{{background:var(--accent-h)}}
.save-ok{{color:var(--new);font-size:.8rem;margin-left:.8rem;display:none}}
.favs-empty{{text-align:center;color:var(--muted);padding:4rem 2rem}}
.favs-empty .icon{{font-size:2.5rem;margin-bottom:.7rem}}
</style>
</head>
<body>

<header>
  <h1>Radar Inmobiliario · Nocnok</h1>
  <p class="sub">Bolsa Inmobiliaria &nbsp;·&nbsp; Torreón &nbsp;·&nbsp; Gómez Palacio &nbsp;·&nbsp; Matamoros &nbsp;·&nbsp; Comercial &amp; Industrial</p>
  <p class="sub">Actualizado: <strong>{run_ts}</strong> &nbsp;·&nbsp; Últimos <strong>{DAYS_BACK} días</strong></p>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="d">propiedades</div></div>
    <div class="stat"><div class="n g">{num_new}</div><div class="d">nuevas hoy</div></div>
    <div class="stat"><div class="n m">{total - num_new}</div><div class="d">ya vistas</div></div>
    <div class="stat"><div class="n f" id="hdr-favs">0</div><div class="d">favoritos</div></div>
  </div>
</header>

<div class="tab-bar">
  <button class="tab-btn active" onclick="showTab('props',this)">🏠 Propiedades</button>
  <button class="tab-btn"        onclick="showTab('favs',this)">⭐ Favoritos</button>
  <button class="tab-btn"        onclick="showTab('brokers',this)">🏆 Brokers</button>
  <button class="tab-btn"        onclick="showTab('stats',this)">📊 Estadísticas</button>
  <button class="tab-btn"        onclick="showTab('alerts',this)">⚙️ Alertas</button>
</div>

<!-- TAB: Propiedades -->
<div id="tab-props">
  <div class="toolbar">
    <div class="tb-group">
      <span class="tb-lbl">Ciudad</span>
      <button class="btn on-city on" id="cy-all"  onclick="applyCity('all',this)">Todas</button>
      <button class="btn"            id="cy-tor"  onclick="applyCity('Torreón',this)">Torreón</button>
      <button class="btn"            id="cy-gp"   onclick="applyCity('Gómez Palacio',this)">Gómez Palacio</button>
      <button class="btn"            id="cy-mat"  onclick="applyCity('Matamoros',this)">Matamoros</button>
    </div>
    <div class="tb-sep"></div>
    <div class="tb-group">
      <span class="tb-lbl">Novedad</span>
      <button class="btn on" id="f-all"  onclick="applyFilter('status','all',this)">Todas</button>
      <button class="btn"    id="f-new"  onclick="applyFilter('status','new',this)">★ Nuevas</button>
      <button class="btn"    id="f-seen" onclick="applyFilter('status','seen',this)">Ya vistas</button>
    </div>
    <div class="tb-sep"></div>
    <div class="tb-group">
      <span class="tb-lbl">Operación</span>
      <button class="btn on"  id="o-all"   onclick="applyFilter('op','all',this)">Todas</button>
      <button class="btn"     id="o-renta" onclick="applyFilter('op','renta',this)">Renta</button>
      <button class="btn"     id="o-venta" onclick="applyFilter('op','venta',this)">Venta</button>
    </div>
    <div class="tb-sep"></div>
    <div class="tb-group">
      <span class="tb-lbl">Ordenar</span>
      <button class="btn sort-btn on" id="s-desc" onclick="applySort('desc',this)">Más reciente</button>
      <button class="btn sort-btn"    id="s-asc"  onclick="applySort('asc',this)">Más antiguo</button>
      <button class="btn sort-btn"    id="s-dom"  onclick="applySort('dom',this)">Más tiempo</button>
    </div>
    <span class="count" id="count">{total} propiedades</span>
  </div>
  {grid_html}
</div>

<!-- TAB: Favoritos -->
<div id="tab-favs" style="display:none">
  <div id="favs-content"></div>
</div>

<!-- TAB: Brokers -->
<div id="tab-brokers" style="display:none">
  <div class="section">
    <div class="toolbar" style="margin-bottom:.85rem">
      <div class="tb-group">
        <span class="tb-lbl">Ciudad</span>
        <button class="btn on-city on" id="bk-all" onclick="applyBkCity('all',this)">Todas</button>
        <button class="btn"            id="bk-tor" onclick="applyBkCity('Torreón',this)">Torreón</button>
        <button class="btn"            id="bk-gp"  onclick="applyBkCity('Gómez Palacio',this)">Gómez Palacio</button>
        <button class="btn"            id="bk-mat" onclick="applyBkCity('Matamoros',this)">Matamoros</button>
      </div>
    </div>
    <div class="section-card">
      <h2>🏆 Ranking de Brokers / Agencias</h2>
      <div id="broker-wrap"></div>
    </div>
  </div>
</div>

<!-- TAB: Estadísticas -->
<div id="tab-stats" style="display:none">
  <div class="section">
    <div class="toolbar" style="margin-bottom:.85rem">
      <div class="tb-group">
        <span class="tb-lbl">Ciudad</span>
        <button class="btn on-city on" id="st-all" onclick="applyStCity('all',this)">Todas</button>
        <button class="btn"            id="st-tor" onclick="applyStCity('Torreón',this)">Torreón</button>
        <button class="btn"            id="st-gp"  onclick="applyStCity('Gómez Palacio',this)">Gómez Palacio</button>
        <button class="btn"            id="st-mat" onclick="applyStCity('Matamoros',this)">Matamoros</button>
      </div>
    </div>
    <div class="section-card">
      <h2>📊 Publicaciones nuevas por semana</h2>
      <div id="weekly-wrap"><div class="chart-wrap"><canvas id="chart-weekly"></canvas></div></div>
    </div>
    <div class="section-card">
      <h2>💰 Precio/m² promedio por semana y tipo</h2>
      <div id="pm2-wrap"><div class="chart-wrap"><canvas id="chart-pm2"></canvas></div></div>
    </div>
  </div>
</div>

<!-- TAB: Alertas -->
<div id="tab-alerts" style="display:none">
  <div class="section">
    <div class="section-card">
      <h2>⚙️ Umbrales de Precio/m² por tipo de propiedad</h2>
      <p style="font-size:.8rem;color:var(--muted);margin-bottom:1.1rem">
        Define el precio máximo/m² aceptable. Las propiedades <em>por debajo</em> del umbral se marcan <strong>💡 OPORTUNIDAD</strong>. Los valores se guardan en tu navegador.
      </p>
      <div class="alerts-grid" id="alerts-grid"></div>
      <button class="save-btn" onclick="saveThresholds()">Guardar umbrales</button>
      <span class="save-ok" id="save-ok">✓ Guardado</span>
    </div>
  </div>
</div>

<script>
var PROPS  = {props_json};
var WEEKLY = {weekly_json};

var PROPS_MAP = {{}};
PROPS.forEach(function(p) {{ PROPS_MAP[p.id] = p; }});

var fCity   = 'all';
var fStatus = 'all';
var fOp     = 'all';
var fSort   = 'desc';
var fBkCity = 'all';
var fStCity = 'all';

var FAVS       = new Set(JSON.parse(localStorage.getItem('radar_favs') || '[]'));
var THRESHOLDS = JSON.parse(localStorage.getItem('radar_thresholds') || '{{}}');
var chartW = null;
var chartP = null;

// ── Tabs ──────────────────────────────────────────────────────────────────────
function showTab(name, btn) {{
  ['props','favs','brokers','stats','alerts'].forEach(function(t) {{
    document.getElementById('tab-'+t).style.display = t===name ? '' : 'none';
  }});
  document.querySelectorAll('.tab-btn').forEach(function(b) {{
    b.classList.toggle('active', b===btn);
  }});
  if (name==='favs')    renderFavs();
  if (name==='brokers') renderBrokers();
  if (name==='stats')   renderCharts();
  if (name==='alerts')  renderAlerts();
}}

// ── City filter (props) ───────────────────────────────────────────────────────
function applyCity(city, btn) {{
  fCity = city;
  document.querySelectorAll('#cy-all,#cy-tor,#cy-gp,#cy-mat').forEach(function(b) {{
    b.className = 'btn' + (b===btn ? ' on-city on' : '');
  }});
  render();
}}

function applyFilter(type, val, btn) {{
  if (type==='status') {{
    fStatus = val;
    document.querySelectorAll('#f-all,#f-new,#f-seen').forEach(function(b) {{
      b.className = 'btn' + (b===btn ? ' on'+(val==='new'?' on-new':'') : '');
    }});
  }} else {{
    fOp = val;
    document.querySelectorAll('#o-all,#o-renta,#o-venta').forEach(function(b) {{
      var c = 'btn';
      if (b===btn) c += val==='renta' ? ' on-renta' : val==='venta' ? ' on-venta' : ' on';
      b.className = c;
    }});
  }}
  render();
}}

function applySort(dir, btn) {{
  fSort = dir;
  document.querySelectorAll('.sort-btn').forEach(function(b) {{
    b.className = 'btn sort-btn' + (b===btn ? ' on' : '');
  }});
  render();
}}

function render() {{
  var grid = document.getElementById('grid');
  if (!grid) return;
  var cards = Array.from(grid.querySelectorAll('.card'));
  var visible = 0;
  cards.forEach(function(c) {{
    var isNew = c.dataset.new==='1';
    var okS = fStatus==='all' || (fStatus==='new'&&isNew) || (fStatus==='seen'&&!isNew);
    var okO = fOp==='all' || c.dataset.op===fOp;
    var okC = fCity==='all' || c.dataset.city===fCity;
    var show = okS && okO && okC;
    c.classList.toggle('hidden', !show);
    if (show) {{ visible++; checkOpp(c); }}
  }});
  document.getElementById('count').textContent = visible + ' propiedades';
  var vis = cards.filter(function(c) {{ return !c.classList.contains('hidden'); }});
  vis.sort(function(a,b) {{
    if (fSort==='dom') return parseInt(b.dataset.dom||0)-parseInt(a.dataset.dom||0);
    var fa=a.dataset.fecha||'', fb=b.dataset.fecha||'';
    return fSort==='asc' ? fa.localeCompare(fb) : fb.localeCompare(fa);
  }});
  vis.forEach(function(c) {{ grid.appendChild(c); }});
  updateFavBtns();
}}

// ── Oportunidades ─────────────────────────────────────────────────────────────
function checkOpp(card) {{
  var tipo  = card.dataset.tipo || '';
  var pm2   = parseFloat(card.dataset.pm2 || 0);
  var pid   = card.dataset.id || '';
  var el    = document.getElementById('opp-' + pid.substring(0,12));
  if (!el) return;
  var thr = THRESHOLDS[tipo];
  el.style.display = (thr && pm2>0 && pm2<parseFloat(thr)) ? 'inline-block' : 'none';
}}

// ── Favoritos ─────────────────────────────────────────────────────────────────
function toggleFav(pid, btn) {{
  if (FAVS.has(pid)) {{
    FAVS.delete(pid);
    if (btn) {{ btn.textContent='☆'; btn.classList.remove('active'); }}
  }} else {{
    FAVS.add(pid);
    if (btn) {{ btn.textContent='★'; btn.classList.add('active'); }}
  }}
  localStorage.setItem('radar_favs', JSON.stringify(Array.from(FAVS)));
  document.getElementById('hdr-favs').textContent = FAVS.size;
}}

function updateFavBtns() {{
  document.querySelectorAll('.fav-btn').forEach(function(btn) {{
    var card = btn.closest('.card');
    if (!card) return;
    var pid = card.dataset.id || '';
    btn.textContent = FAVS.has(pid) ? '★' : '☆';
    btn.classList.toggle('active', FAVS.has(pid));
  }});
}}

function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
}}
function fldH(lbl,val,cls) {{
  return '<div class="field"><span class="lbl">'+lbl+'</span><span class="val'+(cls?' '+cls:'')+'">'+esc(val||'—')+'</span></div>';
}}

function buildFavCard(p) {{
  var fotos = (p.fotos_local||[]).slice(0,3).map(function(src) {{
    return '<img src="'+esc(src)+'" alt="" loading="lazy" onerror="this.style.display=\'none\'">';
  }}).join('');
  var fotosB = fotos ? '<div class="fotos">'+fotos+'</div>' : '<div class="no-foto">Sin fotos</div>';
  var dom = p.days_on_market||0;
  var domCls = dom>30 ? 'dom-old' : dom>7 ? 'dom-mid' : 'dom-new';
  var thr = THRESHOLDS[p.tipo];
  var oppB = (thr && p.pm2_num>0 && p.pm2_num<parseFloat(thr)) ? '<span class="badge-opp">💡 OPORTUNIDAD</span>' : '';
  return '<div class="card" data-id="'+esc(p.id)+'">'+
    '<div class="card-top">'+
      '<div class="card-badges">'+
        (p.is_new?'<span class="badge-new">★ NUEVA</span>':'')+
        oppB+
        '<span class="dom-badge '+domCls+'">⏱ '+dom+'d</span>'+
      '</div>'+
      '<button class="fav-btn active" onclick="removeFav(\''+esc(p.id)+'\')">★</button>'+
      '<span class="code">'+esc(p.code)+'</span>'+
      '<h3>'+esc(p.title)+'</h3>'+
    '</div>'+
    fotosB+
    '<div class="card-body">'+
      fldH('Tipo',p.tipo)+fldH('Operación',p.operacion)+
      fldH('Precio',p.precio,'price')+(p.precio_m2?fldH('$/m²',p.precio_m2,'price-m2'):'')+
      fldH('Superficie',p.superficie)+fldH('Ubicación',p.ubicacion)+
      fldH('Broker',p.broker)+fldH('Publicado',p.fecha_publicacion)+
    '</div>'+
    '<div class="card-foot">'+(p.link?'<a href="'+esc(p.link)+'" target="_blank" rel="noopener" class="btn-ver">Ver listing →</a>':'')+
    '</div>'+
  '</div>';
}}

function removeFav(pid) {{
  FAVS.delete(pid);
  localStorage.setItem('radar_favs', JSON.stringify(Array.from(FAVS)));
  document.getElementById('hdr-favs').textContent = FAVS.size;
  renderFavs();
  updateFavBtns();
}}

function renderFavs() {{
  var el = document.getElementById('favs-content');
  if (FAVS.size===0) {{
    el.innerHTML='<div class="favs-empty"><div class="icon">☆</div><p>No tienes propiedades guardadas.<br>Haz clic en ☆ en cualquier tarjeta para guardarla aquí.</p></div>';
    return;
  }}
  var html = '';
  FAVS.forEach(function(pid) {{ var p=PROPS_MAP[pid]; if(p) html+=buildFavCard(p); }});
  el.innerHTML = '<div class="grid" style="max-width:1420px;margin:0 auto">'+html+'</div>';
}}

// ── Brokers ───────────────────────────────────────────────────────────────────
function applyBkCity(city, btn) {{
  fBkCity = city;
  document.querySelectorAll('#bk-all,#bk-tor,#bk-gp,#bk-mat').forEach(function(b) {{
    b.className = 'btn' + (b===btn ? ' on-city on' : '');
  }});
  renderBrokers();
}}

function renderBrokers() {{
  var list = fBkCity==='all' ? PROPS : PROPS.filter(function(p) {{ return p.municipio===fBkCity; }});
  var bk = {{}};
  list.forEach(function(p) {{
    var name = p.broker || 'Sin nombre';
    if (!bk[name]) bk[name] = {{name:name,total:0,comercial:0,industrial:0,renta:0,venta:0}};
    var b = bk[name]; b.total++;
    if ((p.categoria||'').toLowerCase().includes('industrial')) b.industrial++; else b.comercial++;
    if (p.op_key==='renta') b.renta++; else if (p.op_key==='venta') b.venta++;
  }});
  var sorted = Object.values(bk).sort(function(a,b) {{ return b.total-a.total; }});
  var maxT = sorted.length ? sorted[0].total : 1;
  if (!sorted.length) {{
    document.getElementById('broker-wrap').innerHTML='<p style="color:var(--muted);padding:1rem">Sin datos.</p>';
    return;
  }}
  var medals = ['🥇','🥈','🥉'];
  var html = '<table class="broker-table"><thead><tr><th>#</th><th>Broker / Agencia</th><th>Total</th><th>Comercial</th><th>Industrial</th><th>Renta</th><th>Venta</th></tr></thead><tbody>';
  sorted.slice(0,50).forEach(function(b,i) {{
    var r = i+1;
    var barW = Math.round(b.total/maxT*180);
    html += '<tr><td class="broker-rank">'+( r<=3 ? medals[r-1] : r )+'</td>'+
      '<td><strong>'+esc(b.name)+'</strong><div class="broker-bar-wrap"><div class="broker-bar" style="width:'+barW+'px"></div></div></td>'+
      '<td><strong>'+b.total+'</strong></td><td>'+b.comercial+'</td><td>'+b.industrial+'</td><td>'+b.renta+'</td><td>'+b.venta+'</td></tr>';
  }});
  html += '</tbody></table>';
  document.getElementById('broker-wrap').innerHTML = html;
}}

// ── Estadísticas ──────────────────────────────────────────────────────────────
function applyStCity(city, btn) {{
  fStCity = city;
  document.querySelectorAll('#st-all,#st-tor,#st-gp,#st-mat').forEach(function(b) {{
    b.className = 'btn' + (b===btn ? ' on-city on' : '');
  }});
  renderCharts();
}}

function mkCanvas(wrapId, canvasId) {{
  var wrap = document.getElementById(wrapId);
  wrap.innerHTML = '<div class="chart-wrap"><canvas id="'+canvasId+'"></canvas></div>';
  return document.getElementById(canvasId);
}}

function renderCharts() {{
  var weeks = WEEKLY.weeks || {{}};
  var wkeys = Object.keys(weeks).sort();

  if (!wkeys.length) {{
    document.getElementById('weekly-wrap').innerHTML='<div class="chart-empty">Aún no hay datos históricos. Se acumulan con cada ejecución diaria del scraper.</div>';
    document.getElementById('pm2-wrap').innerHTML='';
    return;
  }}

  var labels = wkeys.map(function(k) {{
    var p = k.split('-W'); return 'Sem '+p[1]+' ('+p[0].slice(2)+')';
  }});

  // Weekly new props
  var wData = wkeys.map(function(k) {{
    var wk = weeks[k];
    if (fStCity==='all') return wk.total||0;
    var cd = (wk.cities||{{}})[fStCity];
    return cd ? (cd.total||0) : 0;
  }});

  if (chartW) chartW.destroy();
  var canvasW = mkCanvas('weekly-wrap','chart-weekly');
  chartW = new Chart(canvasW, {{
    type:'bar',
    data:{{ labels:labels, datasets:[{{
      label:'Publicaciones nuevas',
      data:wData,
      backgroundColor:'rgba(79,70,229,.7)',
      borderColor:'rgba(79,70,229,1)',
      borderWidth:1,
      borderRadius:5
    }}]}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{ legend:{{display:false}}, tooltip:{{callbacks:{{label:function(ctx){{return ctx.parsed.y+' propiedades';}}}}}} }},
      scales:{{ y:{{beginAtZero:true, ticks:{{precision:0}}}} }}
    }}
  }});

  // Price/m² by tipo
  var tipoColors = {{
    'Bodega':'rgba(14,165,233,.85)',
    'Nave Industrial':'rgba(168,85,247,.85)',
    'Terreno':'rgba(34,197,94,.85)',
    'Local':'rgba(249,115,22,.85)',
    'Local Comercial':'rgba(249,115,22,.85)',
    'Oficina':'rgba(236,72,153,.85)'
  }};
  var tipoAcc = {{}};
  wkeys.forEach(function(k) {{
    var cities = (weeks[k].cities||{{}});
    var toCheck = fStCity==='all' ? Object.keys(cities) : [fStCity];
    toCheck.forEach(function(city) {{
      if (!cities[city]) return;
      var tm = cities[city].tipos_m2||{{}};
      Object.keys(tm).forEach(function(tipo) {{
        if (!tipoAcc[tipo]) tipoAcc[tipo]={{}};
        if (!tipoAcc[tipo][k]) tipoAcc[tipo][k]={{sum:0,count:0}};
        tipoAcc[tipo][k].sum   += tm[tipo].sum;
        tipoAcc[tipo][k].count += tm[tipo].count;
      }});
    }});
  }});

  var pm2DS = Object.keys(tipoAcc).slice(0,6).map(function(tipo) {{
    var data = wkeys.map(function(k) {{
      var d = tipoAcc[tipo][k];
      return (d&&d.count) ? Math.round(d.sum/d.count) : null;
    }});
    var color = tipoColors[tipo]||'rgba(107,114,128,.8)';
    return {{label:tipo, data:data, borderColor:color,
      backgroundColor:color.replace('.85','.12'), fill:true,
      tension:.3, spanGaps:true, pointRadius:4}};
  }});

  if (chartP) chartP.destroy();
  if (!pm2DS.length) {{
    document.getElementById('pm2-wrap').innerHTML='<div class="chart-empty">Sin datos de precio/m² aún.</div>';
    return;
  }}
  var canvasP = mkCanvas('pm2-wrap','chart-pm2');
  chartP = new Chart(canvasP, {{
    type:'line',
    data:{{labels:labels, datasets:pm2DS}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:'bottom'}}, tooltip:{{callbacks:{{label:function(ctx){{return ctx.dataset.label+': $'+ctx.parsed.y+'/m²';}}}}}} }},
      scales:{{y:{{beginAtZero:false, ticks:{{callback:function(v){{return '$'+v+'/m²';}}}}}}}}
    }}
  }});
}}

// ── Alertas ───────────────────────────────────────────────────────────────────
var TIPOS_ALERT = ['Bodega','Nave Industrial','Terreno','Local','Local Comercial','Oficina'];

function renderAlerts() {{
  var html = '';
  TIPOS_ALERT.forEach(function(tipo) {{
    var val = THRESHOLDS[tipo]||'';
    html += '<div class="alert-item">'+
      '<div class="alert-tipo">'+esc(tipo)+'</div>'+
      '<label class="alert-lbl">Máx $/m² (umbral de oportunidad)</label>'+
      '<input class="alert-input" type="number" data-tipo="'+esc(tipo)+'" placeholder="Ej: 150" value="'+esc(String(val))+'">'+
    '</div>';
  }});
  document.getElementById('alerts-grid').innerHTML = html;
}}

function saveThresholds() {{
  document.querySelectorAll('.alert-input').forEach(function(inp) {{
    var tipo = inp.dataset.tipo;
    var val  = inp.value.trim();
    if (val) THRESHOLDS[tipo] = parseFloat(val);
    else delete THRESHOLDS[tipo];
  }});
  localStorage.setItem('radar_thresholds', JSON.stringify(THRESHOLDS));
  var ok = document.getElementById('save-ok');
  ok.style.display='inline';
  setTimeout(function(){{ ok.style.display='none'; }}, 2000);
  render();
}}

// ── Init ──────────────────────────────────────────────────────────────────────
render();
document.getElementById('hdr-favs').textContent = FAVS.size;
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(username: str, password: str):
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"=== Nocnok Scraper · {run_ts} ===")

    log("\n[1/5] Login y captura de sesión…")
    pw, br, ctx, pg, token, cookies = await login_and_get_session(username, password)

    try:
        if not token and not cookies:
            log("ERROR: No se pudo obtener credenciales.")
            sys.exit(1)

        log(f"\n[2/5] Scraping ({DAYS_BACK}d)…")
        props = fetch_all_properties(token, cookies)
        log(f"\n  → {len(props)} propiedades extraídas")

        log("\n[3/5] Historia…")
        new_ids = merge_with_history(props)
        log(f"  {len(new_ids)} NUEVAS / {len(props)} total")

        log("\n[4/5] Estadísticas semanales…")
        weekly_stats = update_weekly_stats(props, new_ids)
        weeks_count  = len(weekly_stats.get("weeks", {}))
        log(f"  {weeks_count} semanas acumuladas")

        log("\n[5/5] Guardando…")
        save_json(PROPERTIES_FILE, {
            "run_at": run_ts, "total": len(props),
            "new_count": len(new_ids), "properties": props,
        })
        log(f"  ✓ {PROPERTIES_FILE}")

        html = build_report(props, new_ids, run_ts, weekly_stats)
        REPORT_FILE.write_text(html, encoding="utf-8")
        log(f"  ✓ {REPORT_FILE}")

        log(f"\n{'='*54}")
        log(f"  TOTAL: {len(props)} propiedades | {len(new_ids)} NUEVAS")
        log(f"  Reporte: {REPORT_FILE}")
        log(f"{'='*54}")

    finally:
        await br.close()
        await pw.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", "-u", default=os.environ.get("NOCNOK_USER"))
    parser.add_argument("--password", "-p", default=os.environ.get("NOCNOK_PASS"))
    parser.add_argument("--days",     "-d", type=int, default=7)
    args = parser.parse_args()

    if not args.username or not args.password:
        print("Uso: python3 scraper.py -u EMAIL -p CONTRASEÑA")
        sys.exit(1)

    DAYS_BACK = args.days
    asyncio.run(main(args.username, args.password))
