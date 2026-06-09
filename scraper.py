#!/usr/bin/env python3
"""Nocnok Real Estate Scraper — Bolsa Inmobiliaria"""

import asyncio
import json
import os
import re
import sys
import time
import argparse
import smtplib
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

CST = timezone(timedelta(hours=-6))   # México / Zona Centro (UTC-6, sin horario de verano)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PROPERTIES_FILE = DATA_DIR / "properties.json"
HISTORY_FILE    = DATA_DIR / "history.json"
WEEKLY_FILE     = DATA_DIR / "weekly_stats.json"
ARCHIVE_FILE    = DATA_DIR / "archive.json"
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


GEO_PER_RUN = 60   # máximo de geocodificaciones por corrida (Nominatim: 1 req/s)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def geocode_ubicacion(ubicacion: str) -> tuple:
    """Geocodifica una colonia/zona usando Nominatim. Retorna (lat, lng) o (None, None)."""
    if not ubicacion:
        return None, None
    try:
        query = urllib.parse.urlencode({"q": ubicacion, "format": "json", "limit": "1", "countrycodes": "mx"})
        url   = f"https://nominatim.openstreetmap.org/search?{query}"
        req   = urllib.request.Request(url, headers={"User-Agent": "radar-inmobiliario/1.0 diegoalonso@reave.mx"})
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read())
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None, None


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

    link = f"https://app.nocnok.com/crm/154940/properties/{item.get('id','')}"

    status_date    = item.get("statusDate", "")
    fecha          = ""
    days_on_market = 0
    if status_date:
        try:
            if "Z" in status_date or "+" in status_date:
                pub_dt = datetime.fromisoformat(status_date.replace("Z", "+00:00"))
            else:
                # La API devuelve hora local CST sin indicador de zona
                pub_dt = datetime.fromisoformat(status_date).replace(tzinfo=CST)
            pub_cdt        = pub_dt.astimezone(CST)
            fecha          = pub_cdt.strftime("%Y-%m-%d")
            days_on_market = max(0, (datetime.now(CST).date() - pub_cdt.date()).days)
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
        for item in items:
            results.append(normalize_item(item))
        if page >= total_pages:
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


# ── Archivo histórico ─────────────────────────────────────────────────────────

def update_archive(current_props: list, run_ts: str) -> tuple:
    """Actualiza el archivo histórico. Retorna (new_ids, price_drops, delisted_pids)."""
    archive = load_json(ARCHIVE_FILE, {"properties": {}})
    arch    = archive.setdefault("properties", {})
    today   = run_ts[:10]

    current_ids = {p["id"] for p in current_props if p["id"]}
    new_ids: set      = set()
    price_drops: list = []

    # Retrocompatibilidad: asignar delisted_reason a registros viejos sin el campo
    for entry in arch.values():
        if entry.get("status") == "delisted" and not entry.get("delisted_reason"):
            entry["delisted_reason"] = "sold"

    for p in current_props:
        pid = p["id"]
        if not pid:
            continue
        if pid not in arch:
            arch[pid] = {
                **p,
                "first_seen":     today,
                "last_seen":      today,
                "status":         "active",
                "delisted_at":    None,
                "days_listed":    0,
                "precio_history": [{"date": today, "precio_num": p.get("precio_num"), "precio": p.get("precio")}],
            }
            new_ids.add(pid)
        else:
            entry = arch[pid]
            if entry.get("status") == "delisted":
                was_lapse = entry.get("delisted_reason") == "subscription_lapse"
                entry["status"]          = "active"
                entry["delisted_at"]     = None
                entry["delisted_reason"] = None
                if not was_lapse:
                    new_ids.add(pid)
            entry["last_seen"] = today

            old_num = entry.get("precio_num")
            new_num = p.get("precio_num")
            if old_num and new_num and new_num < old_num * 0.99:
                hist = entry.setdefault("precio_history", [])
                if not hist or hist[-1].get("precio_num") != new_num:
                    hist.append({"date": today, "precio_num": new_num, "precio": p.get("precio")})
                    orig = hist[0]
                    price_drops.append({
                        **p,
                        "first_seen":        entry.get("first_seen", today),
                        "original_precio":    orig.get("precio"),
                        "original_precio_num": orig.get("precio_num"),
                        "current_precio":     p.get("precio"),
                        "current_precio_num": new_num,
                        "total_drop_pct":    round((1 - new_num / orig["precio_num"]) * 100, 1) if orig.get("precio_num") else 0,
                        "last_drop_date":    today,
                    })

            for k in ["precio", "precio_num", "precio_m2", "pm2_num", "title", "ubicacion", "broker", "days_on_market"]:
                if k in p:
                    entry[k] = p[k]
            entry["status"] = "active"

    # Detectar deslistadas
    # Contar propiedades activas por broker ANTES del run (para detectar caída de suscripción)
    broker_active_before: dict = {}
    for entry in arch.values():
        if entry.get("status") == "active":
            b = entry.get("broker", "")
            broker_active_before[b] = broker_active_before.get(b, 0) + 1

    newly_delisted: list = []
    broker_newly_delisted: dict = {}  # broker -> count
    for pid, entry in arch.items():
        if entry.get("status") == "active" and pid not in current_ids:
            entry["status"]      = "delisted"
            entry["delisted_at"] = today
            try:
                d1 = datetime.fromisoformat(entry.get("first_seen", today))
                d2 = datetime.fromisoformat(today)
                entry["days_listed"] = (d2 - d1).days
            except Exception:
                entry["days_listed"] = 0
            b = entry.get("broker", "")
            broker_newly_delisted[b] = broker_newly_delisted.get(b, 0) + 1
            newly_delisted.append(pid)

    # Marcar como subscription_lapse si el broker perdió ≥90% de sus propiedades ese día
    lapse_brokers: set = set()
    for b, lost in broker_newly_delisted.items():
        total = broker_active_before.get(b, 0)
        if total > 0 and lost / total >= 0.90:
            lapse_brokers.add(b)

    if lapse_brokers:
        log(f"  Caída de suscripción detectada: {', '.join(lapse_brokers)}")
    for pid in newly_delisted:
        entry = arch[pid]
        b = entry.get("broker", "")
        entry["delisted_reason"] = "subscription_lapse" if b in lapse_brokers else "sold"

    # Geocodificar propiedades sin coordenadas (máx GEO_PER_RUN por corrida)
    geo_done = 0
    for pid, entry in arch.items():
        if geo_done >= GEO_PER_RUN:
            break
        if entry.get("lat") is None and entry.get("ubicacion"):
            lat, lng = geocode_ubicacion(entry["ubicacion"])
            entry["lat"] = lat
            entry["lng"] = lng
            geo_done += 1
            time.sleep(1.1)   # Nominatim: máx 1 req/s
    if geo_done:
        log(f"  Geocodificadas {geo_done} propiedades")

    save_json(ARCHIVE_FILE, archive)
    return new_ids, price_drops, newly_delisted


def get_oportunidades(archive: dict) -> tuple:
    """Retorna (delisted_list, price_drops_list) para el tab de Oportunidades."""
    arch = archive.get("properties", {})

    delisted:     list = []
    price_drops_ui: list = []

    for entry in arch.values():
        if entry.get("status") == "delisted" and entry.get("delisted_reason") != "subscription_lapse":
            delisted.append(entry)

        hist = entry.get("precio_history", [])
        if len(hist) >= 2:
            orig_num = hist[0].get("precio_num")
            last_num = hist[-1].get("precio_num")
            if orig_num and last_num and last_num < orig_num * 0.99:
                if not any(d["id"] == entry["id"] for d in price_drops_ui):
                    price_drops_ui.append({
                        **entry,
                        "original_precio":    hist[0].get("precio"),
                        "original_precio_num": orig_num,
                        "current_precio":     hist[-1].get("precio"),
                        "current_precio_num": last_num,
                        "total_drop_pct":    round((1 - last_num / orig_num) * 100, 1),
                        "last_drop_date":    hist[-1].get("date", ""),
                    })

    delisted.sort(key=lambda x: x.get("delisted_at", ""), reverse=True)
    price_drops_ui.sort(key=lambda x: x.get("total_drop_pct", 0), reverse=True)
    return delisted, price_drops_ui


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


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(total: int, new_count: int, run_ts: str):
    FROM = "diegoalonso15@hotmail.com"
    TO   = "diegoalonso@reave.mx"
    PASS = "Ducook1234"
    URL  = "https://radar-inmobiliario-eight.vercel.app"

    subject = f"Radar Inmobiliario — {new_count} nuevas propiedades"
    body = f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;color:#111">
  <h2 style="color:#4f46e5;margin-bottom:4px">Radar Inmobiliario actualizado</h2>
  <p style="color:#6b7280;font-size:13px;margin-top:0">{run_ts}</p>
  <table style="width:100%;border-collapse:collapse;margin:16px 0">
    <tr>
      <td style="padding:12px;background:#f0f2f5;border-radius:8px;text-align:center">
        <div style="font-size:2rem;font-weight:800;color:#4f46e5">{total}</div>
        <div style="font-size:11px;color:#6b7280;text-transform:uppercase">propiedades</div>
      </td>
      <td style="width:12px"></td>
      <td style="padding:12px;background:#ecfdf5;border-radius:8px;text-align:center">
        <div style="font-size:2rem;font-weight:800;color:#059669">{new_count}</div>
        <div style="font-size:11px;color:#6b7280;text-transform:uppercase">nuevas</div>
      </td>
    </tr>
  </table>
  <a href="{URL}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;
     padding:10px 20px;border-radius:8px;font-weight:700;font-size:14px">Ver radar →</a>
</div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = FROM
        msg["To"]      = TO
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP("smtp-mail.outlook.com", 587, timeout=20) as srv:
            srv.starttls()
            srv.login(FROM, PASS)
            srv.send_message(msg)
        log("  ✓ Email enviado a " + TO)
    except Exception as e:
        log(f"  ⚠ Email no enviado: {e}")


# ── Reporte HTML ──────────────────────────────────────────────────────────────

def build_report(props: list, new_ids: set, run_ts: str, weekly_stats: dict,
                 delisted: list = None, price_drops: list = None) -> str:
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

        dom_cls   = "dom-old" if dom > 30 else ("dom-mid" if dom > 7 else "dom-new")
        show_hoy  = dom == 0 or (is_new and dom <= 1)
        dom_tip   = ("Publicado hoy" if show_hoy else f"{dom} días en mercado") + (" — candidato a negociar" if dom > 30 else "")
        dom_label = "Hoy" if show_hoy else f"{dom}d"

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
      <span class="dom-badge {dom_cls}" title="{esc(dom_tip)}">⏱ {dom_label}</span>
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

    _delisted    = delisted or []
    _price_drops = price_drops or []

    def _oport_fields(p):
        return {
            "id": p.get("id",""), "title": p.get("title",""), "tipo": p.get("tipo",""),
            "municipio": p.get("municipio",""), "superficie": p.get("superficie",""),
            "precio": p.get("precio",""), "broker": p.get("broker",""), "link": p.get("link",""),
            "fotos_local": (p.get("fotos_local") or [])[:3],
            "first_seen": p.get("first_seen",""), "last_seen": p.get("last_seen",""),
            "delisted_at": p.get("delisted_at",""), "days_listed": p.get("days_listed",0),
            "precio_history": p.get("precio_history",[]),
            "original_precio": p.get("original_precio",""),
            "current_precio": p.get("current_precio",""),
            "total_drop_pct": p.get("total_drop_pct",0),
            "last_drop_date": p.get("last_drop_date",""),
            "lat": p.get("lat"), "lng": p.get("lng"),
        }

    delisted_json    = json.dumps([_oport_fields(p) for p in _delisted], ensure_ascii=False)
    price_drops_json = json.dumps([_oport_fields(p) for p in _price_drops], ensure_ascii=False)

    # Propiedades con coordenadas para el mapa
    archive   = load_json(ARCHIVE_FILE, {"properties": {}})
    map_props = [
        {"id": e.get("id",""), "title": e.get("title",""), "tipo": e.get("tipo",""),
         "municipio": e.get("municipio",""), "superficie": e.get("superficie",""),
         "precio": e.get("precio",""), "precio_m2": e.get("precio_m2",""),
         "op_key": e.get("op_key","otro"), "link": e.get("link",""),
         "fotos_local": (e.get("fotos_local") or [])[:1],
         "lat": e.get("lat"), "lng": e.get("lng"),
         "status": e.get("status","active"),
        }
        for e in archive.get("properties", {}).values()
        if e.get("lat") and e.get("lng")
    ]
    map_json = json.dumps(map_props, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Near Real Estate · Radar Inmobiliario</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f5f6f8;--surface:#fff;--surface2:#f0f2f5;--border:#e2e5ea;--border2:#d1d5db;
  --accent:#ea580c;--accent-h:#c2410c;--new:#059669;--new-bg:#ecfdf5;--new-bd:#6ee7b7;
  --text:#111827;--muted:#6b7280;--price:#b45309;--price2:#1d4ed8;
  --amber:#d97706;--red:#dc2626;--teal:#0f766e;
  --sh:0 1px 4px rgba(0,0,0,.06);--sh2:0 10px 25px rgba(0,0,0,.1);
}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;padding:1.5rem 1rem;min-height:100vh}}
header{{max-width:1420px;margin:0 auto 1rem;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:1.2rem 1.6rem;box-shadow:var(--sh)}}
h1{{font-size:1.5rem;font-weight:800;margin-bottom:.15rem}}
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
.update-btn{{background:none;border:1.5px solid var(--border2);border-radius:20px;
             color:var(--muted);font-size:.75rem;font-weight:600;padding:.25rem .7rem;
             cursor:pointer;transition:all .15s;vertical-align:middle}}
.update-btn:hover{{border-color:var(--accent);color:var(--accent)}}
.update-btn:disabled{{opacity:.5;cursor:default}}
#update-status{{font-size:.75rem;margin-left:.5rem;vertical-align:middle}}
.oport-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem;margin-top:.75rem}}
.oport-card{{background:var(--surface);border:1.5px solid var(--border);border-radius:12px;padding:1rem}}
.oport-card.delisted{{border-color:#fca5a5;background:#fff8f8}}
.oport-card.price-drop{{border-color:#86efac;background:#f0fdf4}}
.oport-badge{{font-size:.65rem;font-weight:700;padding:.2rem .5rem;border-radius:20px;display:inline-block;margin-bottom:.5rem}}
.badge-delisted{{background:#fee2e2;color:#991b1b}}
.badge-drop{{background:#dcfce7;color:#166534}}
.oport-title{{font-size:.88rem;font-weight:700;color:var(--text);margin:.2rem 0}}
.oport-detail{{font-size:.78rem;color:var(--muted);margin:.1rem 0}}
.oport-price{{font-size:.85rem;font-weight:700;color:var(--price);margin:.35rem 0}}
.price-old{{text-decoration:line-through;color:var(--muted);font-weight:400;margin-right:.3rem}}
.price-arrow{{color:#16a34a;font-weight:700;margin:0 .3rem}}
.oport-meta{{font-size:.72rem;color:var(--muted);margin-top:.6rem;padding-top:.5rem;border-top:1px solid var(--border);line-height:1.6}}
#map-container{{height:520px;border-radius:12px;overflow:hidden;border:1.5px solid var(--border);margin-top:.75rem}}
.map-toolbar{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.75rem}}
.leaflet-popup-content{{min-width:200px;font-family:inherit}}
.map-popup-foto{{width:100%;height:110px;object-fit:cover;border-radius:6px;margin-bottom:.5rem;display:block}}
.map-popup-title{{font-size:.85rem;font-weight:700;margin-bottom:.25rem}}
.map-popup-detail{{font-size:.75rem;color:#6b7280;margin:.1rem 0}}
.map-popup-price{{font-size:.82rem;font-weight:700;color:#b45309;margin:.3rem 0}}
.map-popup-link{{display:inline-block;margin-top:.4rem;font-size:.75rem;font-weight:600;color:#ea580c;text-decoration:none}}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
</head>
<body>

<header>
  <h1><span style="color:#ea580c;font-weight:900;letter-spacing:-.01em">NEAR</span><span style="font-weight:300;font-size:1.1rem;color:#374151;letter-spacing:.03em"> Real Estate</span></h1>
  <p class="sub" style="margin-top:.15rem">Radar Inmobiliario &nbsp;·&nbsp; Torreón &nbsp;·&nbsp; Gómez Palacio &nbsp;·&nbsp; Matamoros &nbsp;·&nbsp; Comercial &amp; Industrial</p>
  <p class="sub">Actualizado: <strong id="update-label">{run_ts}</strong>
    &nbsp;·&nbsp; <button class="update-btn" id="update-btn" onclick="triggerUpdate()">🔄 Actualizar</button>
    <span id="update-status"></span>
  </p>
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
  <button class="tab-btn"        onclick="showTab('oportunidades',this)">🔍 Oportunidades <span id="oport-count" style="background:#dc2626;color:#fff;border-radius:20px;font-size:.65rem;padding:.1rem .4rem;margin-left:.2rem;display:none"></span></button>
  <button class="tab-btn"        onclick="showTab('mapa',this)">🗺️ Mapa</button>
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

<!-- TAB: Mapa -->
<div id="tab-mapa" style="display:none">
  <div class="section">
    <div class="section-card">
      <div class="map-toolbar">
        <div class="tb-group">
          <span class="tb-lbl">Ciudad</span>
          <button class="btn on-city on" id="mp-all" onclick="applyMapCity('all',this)">Todas</button>
          <button class="btn"            id="mp-tor" onclick="applyMapCity('Torreón',this)">Torreón</button>
          <button class="btn"            id="mp-gp"  onclick="applyMapCity('Gómez Palacio',this)">Gómez Palacio</button>
          <button class="btn"            id="mp-mat" onclick="applyMapCity('Matamoros',this)">Matamoros</button>
        </div>
        <div class="tb-sep"></div>
        <div class="tb-group">
          <span class="tb-lbl">Vista</span>
          <button class="btn on" id="mp-pins"  onclick="applyMapView('pins',this)">📍 Pins</button>
          <button class="btn"    id="mp-heat"  onclick="applyMapView('heat',this)">🔥 Calor</button>
        </div>
        <div class="tb-sep"></div>
        <div class="tb-group">
          <span class="tb-lbl">Operación</span>
          <button class="btn on" id="mp-op-all"   onclick="applyMapOp('all',this)">Todas</button>
          <button class="btn"    id="mp-op-renta" onclick="applyMapOp('renta',this)">Renta</button>
          <button class="btn"    id="mp-op-venta" onclick="applyMapOp('venta',this)">Venta</button>
        </div>
        <span class="count" id="map-count" style="margin-left:auto"></span>
      </div>
      <div id="map-container"></div>
      <p style="font-size:.72rem;color:var(--muted);margin-top:.5rem">Las ubicaciones corresponden al centroide de la colonia o zona indicada en el listing.</p>
    </div>
  </div>
</div>

<!-- TAB: Oportunidades -->
<div id="tab-oportunidades" style="display:none">
  <div class="section">
    <div class="toolbar" style="margin-bottom:.85rem">
      <div class="tb-group">
        <span class="tb-lbl">Mostrar</span>
        <button class="btn on" id="op-tab-all"    onclick="applyOpTab('all',this)">Todas</button>
        <button class="btn"    id="op-tab-delist" onclick="applyOpTab('delisted',this)">🔴 Deslistadas</button>
        <button class="btn"    id="op-tab-drop"   onclick="applyOpTab('drops',this)">💚 Bajaron precio</button>
      </div>
    </div>
    <div class="section-card" id="oport-delisted-wrap">
      <h2>🔴 Deslistadas <span style="font-size:.8rem;font-weight:400;color:var(--muted)">— posiblemente vendidas o rentadas</span></h2>
      <p style="font-size:.78rem;color:var(--muted);margin-bottom:.5rem">Se detectan cuando desaparecen del portal. El tiempo en mercado te ayuda a analizar qué tan rápido se mueve el mercado.</p>
      <div class="oport-grid" id="oport-delisted"></div>
    </div>
    <div class="section-card" id="oport-drops-wrap" style="margin-top:1.2rem">
      <h2>💚 Bajaron de Precio <span style="font-size:.8rem;font-weight:400;color:var(--muted)">— posible urgencia de venta</span></h2>
      <p style="font-size:.78rem;color:var(--muted);margin-bottom:.5rem">Propiedades donde el precio bajó más del 1% respecto a cuando se detectaron por primera vez.</p>
      <div class="oport-grid" id="oport-drops"></div>
    </div>
  </div>
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
var PROPS       = {props_json};
var WEEKLY      = {weekly_json};
var DELISTED    = {delisted_json};
var PRICE_DROPS = {price_drops_json};
var MAP_PROPS   = {map_json};

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
  ['props','favs','mapa','oportunidades','brokers','stats','alerts'].forEach(function(t) {{
    document.getElementById('tab-'+t).style.display = t===name ? '' : 'none';
  }});
  document.querySelectorAll('.tab-btn').forEach(function(b) {{
    b.classList.toggle('active', b===btn);
  }});
  if (name==='favs')          renderFavs();
  if (name==='brokers')       renderBrokers();
  if (name==='stats')         renderCharts();
  if (name==='alerts')        renderAlerts();
  if (name==='oportunidades') renderOportunidades();
  if (name==='mapa')          initMap();
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

// ── Mapa ──────────────────────────────────────────────────────────────────────
var _map = null, _clusterLayer = null, _heatLayer = null;
var mapCity = 'all', mapOp = 'all', mapView = 'pins';

var TIPO_COLORS = {{
  'Bodega':'#f97316','Nave':'#3b82f6','Terreno':'#22c55e',
  'Local':'#a855f7','Oficina':'#06b6d4','Edificio':'#ec4899',
  'Casa':'#84cc16','Consultorio':'#f59e0b','Hotel':'#64748b'
}};

function mapIcon(tipo) {{
  var c = TIPO_COLORS[tipo] || '#6b7280';
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="36" viewBox="0 0 28 36">'+
    '<path d="M14 0C6.3 0 0 6.3 0 14c0 9.9 14 22 14 22s14-12.1 14-22C28 6.3 21.7 0 14 0z" fill="'+c+'"/>'+
    '<circle cx="14" cy="14" r="6" fill="white"/></svg>';
  return L.divIcon({{
    html: svg, className: '', iconSize: [28,36], iconAnchor: [14,36], popupAnchor: [0,-36]
  }});
}}

function buildMapPopup(p) {{
  var foto = (p.fotos_local||[])[0];
  return '<div style="min-width:190px">'+
    (foto ? '<img src="'+esc(foto)+'" class="map-popup-foto" onerror="this.style.display=\\'none\\'">' : '')+
    '<div class="map-popup-title">'+esc(p.title||'')+'</div>'+
    '<div class="map-popup-detail">'+esc(p.tipo||'')+' · '+esc(p.municipio||'')+'</div>'+
    '<div class="map-popup-detail">'+esc(p.superficie||'')+'</div>'+
    '<div class="map-popup-price">'+esc(p.precio||'')+'</div>'+
    (p.precio_m2 ? '<div class="map-popup-detail">'+esc(p.precio_m2)+'</div>' : '')+
    (p.link ? '<a href="'+esc(p.link)+'" target="_blank" rel="noopener" class="map-popup-link">Ver listing →</a>' : '')+
  '</div>';
}}

function getFilteredMapProps() {{
  return MAP_PROPS.filter(function(p) {{
    var okC = mapCity==='all' || p.municipio===mapCity;
    var okO = mapOp==='all' || p.op_key===mapOp;
    return okC && okO && p.lat && p.lng;
  }});
}}

function applyMapCity(city, btn) {{
  mapCity = city;
  document.querySelectorAll('#mp-all,#mp-tor,#mp-gp,#mp-mat').forEach(function(b){{
    b.className = 'btn'+(b===btn?' on-city on':'');
  }});
  refreshMap();
}}

function applyMapOp(op, btn) {{
  mapOp = op;
  document.querySelectorAll('#mp-op-all,#mp-op-renta,#mp-op-venta').forEach(function(b){{
    b.className = 'btn'+(b===btn?' on':'');
  }});
  refreshMap();
}}

function applyMapView(view, btn) {{
  mapView = view;
  document.querySelectorAll('#mp-pins,#mp-heat').forEach(function(b){{
    b.className = 'btn'+(b===btn?' on':'');
  }});
  refreshMap();
}}

function refreshMap() {{
  if (!_map) return;
  if (_clusterLayer) {{ _map.removeLayer(_clusterLayer); _clusterLayer = null; }}
  if (_heatLayer)    {{ _map.removeLayer(_heatLayer);    _heatLayer    = null; }}

  var filtered = getFilteredMapProps();
  document.getElementById('map-count').textContent = filtered.length + ' propiedades';

  if (mapView === 'pins') {{
    _clusterLayer = L.markerClusterGroup({{maxClusterRadius:50}});
    filtered.forEach(function(p) {{
      var m = L.marker([p.lat, p.lng], {{icon: mapIcon(p.tipo)}});
      m.bindPopup(buildMapPopup(p), {{maxWidth:220}});
      _clusterLayer.addLayer(m);
    }});
    _map.addLayer(_clusterLayer);
  }} else {{
    var pts = filtered.map(function(p){{ return [p.lat, p.lng, 1]; }});
    _heatLayer = L.heatLayer(pts, {{radius:35, blur:25, maxZoom:14,
      gradient:{{0.2:'#3b82f6', 0.5:'#f59e0b', 0.8:'#ef4444'}}
    }});
    _map.addLayer(_heatLayer);
  }}
}}

function initMap() {{
  if (_map) {{ refreshMap(); return; }}
  // Cargar scripts Leaflet dinámicamente
  function loadScript(src, cb) {{
    var s = document.createElement('script'); s.src = src; s.onload = cb; document.head.appendChild(s);
  }}
  loadScript('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', function() {{
    loadScript('https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js', function() {{
      loadScript('https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js', function() {{
        _map = L.map('map-container').setView([25.543, -103.428], 12);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
          attribution: '© OpenStreetMap contributors', maxZoom: 19
        }}).addTo(_map);
        refreshMap();
      }});
    }});
  }});
}}

// ── Tab Oportunidades ─────────────────────────────────────────────────────────
var opTabFilter = 'all';
function applyOpTab(val, btn) {{
  opTabFilter = val;
  document.querySelectorAll('#op-tab-all,#op-tab-delist,#op-tab-drop').forEach(function(b) {{
    b.className = 'btn' + (b===btn ? ' on' : '');
  }});
  document.getElementById('oport-delisted-wrap').style.display = (val==='all'||val==='delisted') ? '' : 'none';
  document.getElementById('oport-drops-wrap').style.display    = (val==='all'||val==='drops')    ? '' : 'none';
}}

var oportunidadesRendered = false;
function renderOportunidades() {{
  if (oportunidadesRendered) return;
  oportunidadesRendered = true;

  // Contador en el tab
  var total = DELISTED.length + PRICE_DROPS.length;
  var badge = document.getElementById('oport-count');
  if (badge && total > 0) {{ badge.textContent = total; badge.style.display='inline'; }}

  // Deslistadas
  var delEl = document.getElementById('oport-delisted');
  if (!DELISTED.length) {{
    delEl.innerHTML = '<p style="color:var(--muted);font-size:.85rem;padding:1rem 0">Sin propiedades deslistadas registradas aún. Se irán acumulando con cada actualización.</p>';
  }} else {{
    delEl.innerHTML = DELISTED.map(function(p) {{
      var hist = p.precio_history || [];
      var priceChg = '';
      if (hist.length >= 2 && hist[0].precio !== hist[hist.length-1].precio) {{
        priceChg = '<div class="oport-detail" style="margin-top:.3rem">Precios: <span class="price-old">'+esc(hist[0].precio||'')+'</span> → '+esc(hist[hist.length-1].precio||'')+'</div>';
      }}
      var fotos = (p.fotos_local||[]).map(function(s){{
        return '<img src="'+esc(s)+'" alt="" loading="lazy" onerror="this.style.display=\\'none\\'">';
      }}).join('');
      return '<div class="oport-card delisted">'+
        '<span class="oport-badge badge-delisted">🔴 DESLISTADA</span>'+
        (fotos ? '<div class="fotos" style="margin:.4rem 0 .5rem">'+fotos+'</div>' : '')+
        '<div class="oport-title">'+esc(p.title||'')+'</div>'+
        '<div class="oport-detail">'+esc(p.tipo||'')+' · '+esc(p.municipio||'')+'</div>'+
        '<div class="oport-detail">'+esc(p.superficie||'')+'</div>'+
        '<div class="oport-price">'+esc(p.precio||'')+'</div>'+
        priceChg+
        '<div class="oport-meta">'+
          '🏢 Broker: <b>'+esc(p.broker||'—')+'</b><br>'+
          '📅 Publicado: <b>'+esc(p.first_seen||'')+'</b><br>'+
          '❌ Deslistado: <b>'+esc(p.delisted_at||'')+'</b><br>'+
          '⏱ Tiempo en mercado: <b>'+p.days_listed+' días</b>'+
        '</div>'+
        (p.link ? '<div style="margin-top:.6rem"><a href="'+esc(p.link)+'" target="_blank" rel="noopener" class="btn-ver" style="font-size:.72rem;padding:.28rem .7rem">Ver listing →</a></div>' : '')+
      '</div>';
    }}).join('');
  }}

  // Bajaron de precio
  var dropEl = document.getElementById('oport-drops');
  if (!PRICE_DROPS.length) {{
    dropEl.innerHTML = '<p style="color:var(--muted);font-size:.85rem;padding:1rem 0">Sin bajas de precio detectadas aún. Se registran al comparar cada actualización con la anterior.</p>';
  }} else {{
    dropEl.innerHTML = PRICE_DROPS.map(function(p) {{
      var fotos = (p.fotos_local||[]).map(function(s){{
        return '<img src="'+esc(s)+'" alt="" loading="lazy" onerror="this.style.display=\\'none\\'">';
      }}).join('');
      return '<div class="oport-card price-drop">'+
        '<span class="oport-badge badge-drop">💚 -'+p.total_drop_pct+'% PRECIO</span>'+
        (fotos ? '<div class="fotos" style="margin:.4rem 0 .5rem">'+fotos+'</div>' : '')+
        '<div class="oport-title">'+esc(p.title||'')+'</div>'+
        '<div class="oport-detail">'+esc(p.tipo||'')+' · '+esc(p.municipio||'')+'</div>'+
        '<div class="oport-detail">'+esc(p.superficie||'')+'</div>'+
        '<div class="oport-price">'+
          '<span class="price-old">'+esc(p.original_precio||'')+'</span>'+
          '<span class="price-arrow">→</span>'+
          esc(p.current_precio||'')+
        '</div>'+
        '<div class="oport-meta">'+
          '📅 Detectado desde: <b>'+esc(p.first_seen||'')+'</b><br>'+
          '📉 Última baja: <b>'+esc(p.last_drop_date||'')+'</b>'+
        '</div>'+
        (p.link ? '<div style="margin-top:.6rem"><a href="'+esc(p.link)+'" target="_blank" rel="noopener" class="btn-ver" style="font-size:.72rem;padding:.28rem .7rem">Ver listing →</a></div>' : '')+
      '</div>';
    }}).join('');
  }}
}}

// ── Oportunidades (alertas precio) ────────────────────────────────────────────
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
    return '<img src="'+esc(src)+'" alt="" loading="lazy" onerror="this.hidden=true">';
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
        '<span class="dom-badge '+domCls+'">⏱ '+(dom===0?'Hoy':dom+'d')+'</span>'+
      '</div>'+
      '<button class="fav-btn active" data-pid="'+esc(p.id)+'" onclick="removeFav(this.dataset.pid)">★</button>'+
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

// ── Fecha "Hoy / Ayer" ───────────────────────────────────────────────────────
(function() {{
  var ts    = "{run_ts}";
  var date  = ts.substring(0,10);
  var time  = ts.substring(11,16);
  var now   = new Date();
  var pad   = function(n){{ return String(n).padStart(2,'0'); }};
  var toStr = function(d){{ return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()); }};
  var yest  = new Date(now); yest.setDate(yest.getDate()-1);
  var months = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
  var day   = parseInt(date.substring(8,10));
  var month = months[parseInt(date.substring(5,7))-1];
  var label = date===toStr(now) ? 'Hoy' : date===toStr(yest) ? 'Ayer' : date;
  var el = document.getElementById('update-label');
  if (el) el.textContent = label+' '+day+' '+month+', '+time;
}})();

// ── Botón Actualizar ─────────────────────────────────────────────────────────
function triggerUpdate() {{
  var btn = document.getElementById('update-btn');
  var st  = document.getElementById('update-status');
  btn.disabled = true;
  btn.textContent = '⏳ Iniciando...';
  st.textContent  = '';
  st.style.color  = 'var(--muted)';
  fetch('/api/update', {{method:'POST'}})
    .then(function(r){{ return r.json(); }})
    .then(function(d) {{
      if (d.ok) {{
        btn.textContent = '✓ Actualizando';
        st.style.color  = 'var(--new)';
        var secs = 240;
        var fmtTime = function(s) {{ var m=Math.floor(s/60); var ss=s%60; return m+':'+(ss<10?'0':'')+ss; }};
        st.textContent = '⏱ Recarga en ' + fmtTime(secs);
        var iv = setInterval(function() {{
          secs--;
          if (secs <= 0) {{
            clearInterval(iv);
            btn.disabled=false; btn.textContent='🔄 Actualizar';
            st.textContent = '¡Listo! Recarga la página.';
            setTimeout(function(){{ st.textContent=''; }}, 10000);
          }} else {{
            st.textContent = '⏱ Recarga en ' + fmtTime(secs);
          }}
        }}, 1000);
      }} else {{
        btn.disabled=false; btn.textContent='🔄 Actualizar';
        st.textContent='Error: '+(d.error||'intenta de nuevo');
        st.style.color='var(--red)';
      }}
    }})
    .catch(function(){{
      btn.disabled=false; btn.textContent='🔄 Actualizar';
      st.textContent='Sin conexión'; st.style.color='var(--red)';
    }});
}}

// ── Init ──────────────────────────────────────────────────────────────────────
render();
document.getElementById('hdr-favs').textContent = FAVS.size;
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(username: str, password: str):
    run_ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    log(f"=== Nocnok Scraper · {run_ts} ===")

    log("\n[1/5] Login y captura de sesión…")
    pw, br, ctx, pg, token, cookies = await login_and_get_session(username, password)

    try:
        if not token and not cookies:
            log("ERROR: No se pudo obtener credenciales.")
            sys.exit(1)

        log("\n[2/5] Scraping (todas las activas)…")
        props = fetch_all_properties(token, cookies)
        log(f"\n  → {len(props)} propiedades extraídas")

        log("\n[3/5] Archivo histórico…")
        new_ids, price_drops, newly_delisted = update_archive(props, run_ts)
        merge_with_history(props)  # mantiene history.json para compatibilidad
        log(f"  {len(new_ids)} NUEVAS | {len(newly_delisted)} deslistadas | {len(price_drops)} bajaron precio")

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

        archive      = load_json(ARCHIVE_FILE, {"properties": {}})
        delisted, price_drops_ui = get_oportunidades(archive)
        log(f"  {len(delisted)} deslistadas acumuladas | {len(price_drops_ui)} con baja de precio")

        html = build_report(props, new_ids, run_ts, weekly_stats, delisted, price_drops_ui)
        REPORT_FILE.write_text(html, encoding="utf-8")
        log(f"  ✓ {REPORT_FILE}")

        log("\nEnviando email…")
        send_email(len(props), len(new_ids), run_ts)

        log(f"\n{'='*54}")
        log(f"  TOTAL: {len(props)} propiedades | {len(new_ids)} NUEVAS | {len(delisted)} deslistadas")
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
