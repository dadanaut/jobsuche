#!/usr/bin/env python3
import os
import re
import json
import time
import html
import requests
from pathlib import Path
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # Fallback, wenn nicht verfügbar

COUNTRY = "de"
ADZUNA_BASE = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search"

def getenv(name, default=None):
    v = os.getenv(name)
    return v if v is not None and str(v).strip() != "" else default

def parse_list_semicolons(s, default=None):
    if not s:
        return default or []
    return [x.strip() for x in s.split(";") if x.strip()]

def parse_int_flexible(value, default):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(float(value))
    s = str(value).strip()
    # entferne alles außer Ziffern
    s = re.sub(r"[^0-9]", "", s)
    if not s:
        return default
    try:
        return int(s)
    except Exception:
        return default

def normalize(s):
    return (s or "").strip()

def contains_any(text, terms):
    if not text:
        return False
    t = text.casefold()
    return any(term.casefold() in t for term in terms)

def clean_text(s, max_len=350):
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    s = html.escape(s)
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s

def ensure_dir(p):
    Path(p).mkdir(parents=True, exist_ok=True)

def get_berlin_tz():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo("Europe/Berlin")
    except Exception:
        return None

def fetch_adzuna_page(keyword, page, cfg):
    params = {
        "app_id": cfg["ADZUNA_APP_ID"],
        "app_key": cfg["ADZUNA_APP_KEY"],
        "what": keyword,
        "where": cfg["HOME_CITY"],
        "distance": cfg["RADIUS_KM"],
        "sort_by": "date",
        "results_per_page": cfg["RESULTS_PER_PAGE"],
        "what_exclude": "Zeitarbeit",
        # Adzuna nutzt die Seitenzahl im Pfad (…/search/{page}), daher kein zusätzliches "page" hier nötig.
    }
    url = f"{ADZUNA_BASE}/{page}"
    headers = {"User-Agent": "MarcoJobBot/1.0 (+GitHub Actions)"}
    resp = requests.get(url, params=params, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.json()

def job_city_mentions_excluded(job, exclude_city):
    if not exclude_city:
        return False
    loc = job.get("location") or {}
    disp = normalize(loc.get("display_name"))
    if exclude_city.casefold() in disp.casefold():
        return True
    area = loc.get("area") or []
    for a in area:
        if exclude_city.casefold() in str(a).casefold():
            return True
    # Fallback: Titel/Beschreibung
    title = normalize(job.get("title"))
    desc = normalize(job.get("description"))
    if exclude_city.casefold() in title.casefold() or exclude_city.casefold() in desc.casefold():
        return True
    return False

def extract_annual_salary(job):
    smin = job.get("salary_min")
    smax = job.get("salary_max")
    return (smin, smax)

def meets_min_salary(job, min_year):
    smin, smax = extract_annual_salary(job)
    if smin is None and smax is None:
        return None  # unbekannt
    # wenn nur eines vorhanden ist
    val = smax if smax is not None else smin
    try:
        return float(val) >= float(min_year)
    except Exception:
        return None

def build_job_obj(raw, cfg):
    loc = raw.get("location") or {}
    comp = raw.get("company") or {}
    smin, smax = extract_annual_salary(raw)
    created_iso = normalize(raw.get("created"))

    # ISO -> datetime
    created_dt = None
    if created_iso:
        try:
            created_dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        except Exception:
            created_dt = None

    job = {
        "id": raw.get("id"),
        "title": normalize(raw.get("title")),
        "company": normalize(comp.get("display_name")),
        "location": normalize(loc.get("display_name")),
        "areas": loc.get("area") or [],
        "created": created_iso,
        "created_local": None,
        "redirect_url": raw.get("redirect_url"),
        "description": normalize(raw.get("description")),
        "contract_type": normalize(raw.get("contract_type")),
        "contract_time": normalize(raw.get("contract_time")),
        "category": (raw.get("category") or {}).get("label"),
        "salary_min": smin,
        "salary_max": smax,
        "salary_is_predicted": normalize(raw.get("salary_is_predicted")),
        "source": "Adzuna",
    }

    if created_dt:
        berlin = get_berlin_tz()
        try:
            if berlin:
                job["created_local"] = created_dt.astimezone(berlin).strftime("%d.%m.%Y %H:%M")
            else:
                job["created_local"] = created_dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            job["created_local"] = created_dt.strftime("%Y-%m-%d %H:%M UTC")

    # Heuristik Remote
    job["is_remote_guess"] = contains_any(
        " ".join([job["title"], job["description"]]),
        ["remote", "homeoffice", "home office", "hybrid", "teil-remote"]
    )

    job["meets_salary"] = meets_min_salary(job, cfg["SALARY_MIN_YEAR"])
    return job

def should_exclude(job, cfg):
    # Stadt-Ausschluss
    if cfg["EXCLUDE_CITY"] and job_city_mentions_excluded(job, cfg["EXCLUDE_CITY"]):
        return True, "excluded_city"
    # Zeitarbeit & Synonyme
    hay = " ".join([job["title"], job["company"], job["description"]]).casefold()
    for term in cfg["EXCLUDE_TERMS"]:
        if term.casefold() in hay:
            return True, f"excluded_term:{term}"
    return False, None

def rank_job(job):
    # 2 (Gehalt ok) > 1 (unbekannt) > 0 (unter Minimum)
    if job["meets_salary"] is True:
        return 2
    if job["meets_salary"] is None:
        return 1
    return 0

def main():
    cfg = {
        "ADZUNA_APP_ID": getenv("ADZUNA_APP_ID"),
        "ADZUNA_APP_KEY": getenv("ADZUNA_APP_KEY"),
        "HOME_CITY": getenv("HOME_CITY", "Lauffen am Neckar"),
        "EXCLUDE_CITY": getenv("EXCLUDE_CITY", "Stuttgart"),
        "RADIUS_KM": parse_int_flexible(getenv("RADIUS_KM", "30"), 30),
        "SALARY_MIN_YEAR": parse_int_flexible(getenv("SALARY_MIN_YEAR", "54000"), 54000),
        "KEYWORDS": parse_list_semicolons(getenv("KEYWORDS"), [
            "Mediengestalter", "Webdesigner", "WordPress", "TYPO3", "SEO", "Content Manager", "Social Media", "Digital Marketing"
        ]),
        "EXCLUDE_TERMS": parse_list_semicolons(getenv("EXCLUDE_TERMS"), [
            "Zeitarbeit", "Leiharbeit", "Arbeitnehmerüberlassung", "Personaldienstleister", "Personalleasing"
        ]),
        "ADZUNA_MAX_PAGES": parse_int_flexible(getenv("ADZUNA_MAX_PAGES", "2"), 2),
        "RESULTS_PER_PAGE": parse_int_flexible(getenv("RESULTS_PER_PAGE", "50"), 50),
    }

    # Minimal-Config-Log (ohne Secrets)
    print("[INFO] Effektive Konfiguration:")
    print(f"  HOME_CITY={cfg['HOME_CITY']}, EXCLUDE_CITY={cfg['EXCLUDE_CITY']}, RADIUS_KM={cfg['RADIUS_KM']}")
    print(f"  SALARY_MIN_YEAR={cfg['SALARY_MIN_YEAR']}, ADZUNA_MAX_PAGES={cfg['ADZUNA_MAX_PAGES']}, RESULTS_PER_PAGE={cfg['RESULTS_PER_PAGE']}")
    print(f"  KEYWORDS={cfg['KEYWORDS']}")
    print(f"  EXCLUDE_TERMS={cfg['EXCLUDE_TERMS']}")

    if not cfg["ADZUNA_APP_ID"] or not cfg["ADZUNA_APP_KEY"]:
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY fehlen als Secrets/Env.")

    all_raw = []
    per_keyword_count = {}
    seen_ids = set()

    for kw in cfg["KEYWORDS"]:
        kw = kw.strip()
        if not kw:
            continue
        total_for_kw = 0
        for page in range(1, cfg["ADZUNA_MAX_PAGES"] + 1):
            try:
                data = fetch_adzuna_page(kw, page, cfg)
            except Exception as e:
                print(f"[WARN] Adzuna-Request fehlgeschlagen für '{kw}' Seite {page}: {e}")
                break
            results = data.get("results", [])
            if not results:
                break
            for r in results:
                rid = r.get("id") or r.get("redirect_url")
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                all_raw.append((kw, r))
                total_for_kw += 1
            time.sleep(0.3)  # Rate-Limit-Schonung
        per_keyword_count[kw] = total_for_kw

    kept = []
    excluded = []
    for kw, raw in all_raw:
        job = build_job_obj(raw, cfg)
        ex, reason = should_exclude(job, cfg)
        job["keyword"] = kw
        if ex:
            job["exclude_reason"] = reason
            excluded.append(job)
        else:
            kept.append(job)

    kept.sort(key=lambda j: (-rank_job(j), j["company"].casefold(), j["title"].casefold()))

    ensure_dir("site")
    ensure_dir("site/data")

    output = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "home_city": cfg["HOME_CITY"],
        "exclude_city": cfg["EXCLUDE_CITY"],
        "radius_km": cfg["RADIUS_KM"],
        "salary_min_year": cfg["SALARY_MIN_YEAR"],
        "keywords": cfg["KEYWORDS"],
        "exclude_terms": cfg["EXCLUDE_TERMS"],
        "sources": ["Adzuna"],
        "counts": {
            "fetched_total": len(all_raw),
            "kept": len(kept),
            "excluded": len(excluded),
            "per_keyword": per_keyword_count,
        },
        "jobs": kept,
    }
    with open("site/data/jobs.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # HTML bauen
    try:
        berlin = get_berlin_tz()
        now_dt = datetime.now(timezone.utc)
        if berlin:
            now_local = now_dt.astimezone(berlin).strftime("%d.%m.%Y %H:%M")
        else:
            now_local = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        now_local = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    total = len(kept)
    kept_cards = []
    for j in kept:
        salary_txt = "Gehaltsangabe: unbekannt"
        try:
            if j["salary_min"] or j["salary_max"]:
                parts = []
                if j["salary_min"]:
                    parts.append(f"min €{int(float(j['salary_min'])):,}".replace(",", "."))
                if j["salary_max"]:
                    parts.append(f"max €{int(float(j['salary_max'])):,}".replace(",", "."))
                salary_txt = " / ".join(parts) + " p.a."
        except Exception:
            pass

        badge = ""
        if j["meets_salary"] is True:
            badge = '<span class="badge good">≥ Mindestgehalt</span>'
        elif j["meets_salary"] is None:
            badge = '<span class="badge neutral">Gehalt unbekannt</span>'
        else:
            badge = '<span class="badge warn">unter Mindestgehalt</span>'

        remote = ' <span class="pill">Remote/HYB</span>' if j.get("is_remote_guess") else ""
        created = j.get("created_local") or (j.get("created") or "")[:16]
        desc = clean_text(j.get("description"), max_len=360)
        title = html.escape(j.get("title") or "")
        company = html.escape(j.get("company") or "")
        location = html.escape(j.get("location") or "")
        redirect = html.escape(j.get("redirect_url") or "#")
        keyword = html.escape(j.get("keyword") or "")

        card = f"""
        <article class="card">
          <h3>{title}</h3>
          <p class="meta">{company} · {location}{remote}</p>
          <p class="meta">Quelle: Adzuna · Erstellt: {html.escape(created)} · Schlagwort: {keyword}</p>
          <p class="salary">{salary_txt} {badge}</p>
          <p class="desc">{desc}</p>
          <p><a class="btn" href="{redirect}" target="_blank" rel="noopener">Zur Ausschreibung</a></p>
        </article>
        """
        kept_cards.append(card)

    keyword_summary = "".join(
        f"<li><strong>{html.escape(k)}</strong>: {v}</li>" for k, v in per_keyword_count.items()
    )
    exclude_terms_html = ", ".join(html.escape(t) for t in cfg["EXCLUDE_TERMS"])

    html_doc = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jobs – Auto-Suche für {html.escape(cfg['HOME_CITY'])}</title>
  <style>
    :root {{
      --bg: #0b0f14; --fg: #e6edf3; --muted:#9fb1c3; --card:#121821; --pri:#2ea043; --warn:#d29922; --bad:#f85149; --pill:#3b82f6;
    }}
    body {{ background: var(--bg); color: var(--fg); font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; }}
    header {{ padding: 24px 16px; border-bottom: 1px solid #1f2937; }}
    header h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
    header p {{ margin: 2px 0; color: var(--muted); }}
    main {{ padding: 16px; max-width: 1100px; margin: 0 auto; }}
    .stats, .legend {{ color: var(--muted); margin-bottom: 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }}
    .card {{ background: var(--card); border: 1px solid #1f2937; border-radius: 10px; padding: 14px; }}
    .card h3 {{ margin: 0 0 6px 0; font-size: 18px; }}
    .meta {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .salary {{ margin: 8px 0; }}
    .desc {{ color: var(--fg); }}
    .btn {{ display:inline-block; background: #2563eb; color: white; padding: 8px 12px; border-radius: 8px; text-decoration: none; }}
    .badge {{ margin-left: 8px; padding: 2px 8px; border-radius: 999px; font-size: 12px; vertical-align: middle; }}
    .badge.good {{ background: rgba(46,160,67,.18); color: #3fb950; }}
    .badge.neutral {{ background: rgba(210,153,34,.18); color: #d29922; }}
    .badge.warn {{ background: rgba(248,81,73,.18); color: #f85149; }}
    .pill {{ margin-left: 6px; background: rgba(59,130,246,.18); color: #60a5fa; padding: 2px 6px; border-radius: 999px; font-size: 12px; }}
    footer {{ padding: 20px 16px; color: var(--muted); text-align: center; }}
    ul.inline {{ padding-left: 16px; }}
    a, a:visited {{ color: #7aa2ff; }}
  </style>
</head>
<body>
  <header>
    <h1>Jobsuche: {html.escape(cfg['HOME_CITY'])} (+{cfg['RADIUS_KM']} km) – ohne {html.escape(cfg['EXCLUDE_CITY'])}</h1>
    <p>Letzte Aktualisierung: {html.escape(now_local)} · Mindestgehalt: €{cfg['SALARY_MIN_YEAR']:,} p.a. · Quellen: Adzuna</p>
    <p>Keywords: {", ".join(html.escape(k) for k in cfg["KEYWORDS"])} · Ausschlüsse: {html.escape(exclude_terms_html)}</p>
  </header>
  <main>
    <div class="stats">
      <p>Gefunden (nach Filter): <strong>{total}</strong> · Rohdaten: site/data/jobs.json</p>
      <p>Treffer pro Keyword:</p>
      <ul class="inline">{keyword_summary}</ul>
      <p class="legend">Hinweis: Anzeigen ohne Gehaltsangabe werden angezeigt und entsprechend gekennzeichnet.</p>
    </div>
    <section class="grid">
      {''.join(kept_cards) if kept_cards else '<p>Keine passenden Anzeigen gefunden.</p>'}
    </section>
  </main>
  <footer>
    Generiert automatisch (GitHub Actions). © {datetime.now().year} · Profil von Marco Dinkel
  </footer>
</body>
</html>
"""
    with open("site/index.html", "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"[OK] Rohanzeigen: {len(all_raw)}, nach Filtern: {len(kept)}. HTML & JSON erzeugt.")

if __name__ == "__main__":
    main()
