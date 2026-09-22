"""leadscout: find real-estate agents/agencies WITHOUT a website, with their contact
details and the channels they advertise on. Data from OpenStreetMap via the Overpass API
(free, no key). Stdlib only. Runs in batches so no single run hammers the public APIs.
"""
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

REGIONS = "regions.json"
STATE = "data/state.json"          # full lead store + scan progress
OUT = "docs/data/leads.json"       # slim feed for the dashboard
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
BATCH = int(os.environ.get("LS_BATCH", "6"))   # regions per run
UA = {"User-Agent": "leadscout/0.1 (real-estate lead research; +https://github.com/abdulsalam-create/leadscout)"}
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
CTX = ssl.create_default_context()


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj, pretty=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1 if pretty else None, separators=None if pretty else (",", ":"), sort_keys=pretty)


def http(url, data=None, timeout=90, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print(f"[!] {url[:48]} try {i + 1}: {e}")
            time.sleep(5 + i * 5)
    return None


def bbox(region):
    """Resolve a 'City, Country' string to an OSM bounding box via Nominatim."""
    q = urllib.parse.urlencode({"q": region, "format": "json", "limit": 1})
    body = http(f"{NOMINATIM}?{q}", timeout=30)
    time.sleep(1.5)  # Nominatim etiquette: <=1 req/sec
    try:
        b = json.loads(body)[0]["boundingbox"]  # [south, north, west, east]
        return float(b[0]), float(b[2]), float(b[1]), float(b[3])  # -> S, W, N, E
    except Exception:  # noqa: BLE001
        return None


def overpass(s, w, n, e):
    q = f"""[out:json][timeout:150];
(
 nwr["office"="estate_agent"]({s},{w},{n},{e});
 nwr["shop"="estate_agent"]({s},{w},{n},{e});
 nwr["office"="property_management"]({s},{w},{n},{e});
);
out center tags;"""
    body = http(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(), timeout=180)
    try:
        return json.loads(body).get("elements", [])
    except Exception:  # noqa: BLE001
        return []


CHANNEL_TAGS = {
    "facebook": ("contact:facebook", "facebook"),
    "instagram": ("contact:instagram", "instagram"),
    "whatsapp": ("contact:whatsapp", "whatsapp"),
    "linkedin": ("contact:linkedin", "linkedin"),
    "twitter": ("contact:twitter", "twitter", "contact:x"),
    "tiktok": ("contact:tiktok", "tiktok"),
    "youtube": ("contact:youtube", "youtube"),
    "telegram": ("contact:telegram", "telegram"),
}


def first(t, *keys):
    for k in keys:
        if t.get(k):
            return str(t[k]).strip()
    return ""


# A "website" that is really just a social/link page is NOT a proper site -> still a build-from-scratch lead.
SOCIAL_HOSTS = ("facebook.", "fb.com", "fb.me", "instagram.", "linktr.ee", "linktree", "wa.me",
                "t.me", "twitter.", "x.com", "tiktok.", "wixsite.com", "wordpress.com", "blogspot.",
                "business.site", "sites.google.", "carrd.co", "beacons.ai", "bit.ly", "google.com/maps")


def _host(u):
    m = re.match(r"https?://([^/]+)", u.strip(), re.I)
    return (m.group(1).lower() if m else u.lower())


def to_lead(el, region, country):
    t = el.get("tags", {})
    name = first(t, "name", "brand", "operator")
    if not name:
        return None
    website = first(t, "website", "contact:website", "url")
    social_site = website and any(s in _host(website) for s in SOCIAL_HOSTS)
    seg = "site" if (website and not social_site) else "nosite"   # site=has real website (redesign), nosite=build
    channels = {c: first(t, *tags) for c, tags in CHANNEL_TAGS.items()}
    channels = {c: v for c, v in channels.items() if v}
    if social_site:  # fold the social "website" into channels as its network
        for c in CHANNEL_TAGS:
            if c in _host(website):
                channels.setdefault(c, website)
    email = first(t, "email", "contact:email")
    phone = first(t, "phone", "contact:phone", "contact:mobile")
    if seg == "nosite" and not (email or phone or channels):
        return None  # a no-site name with no way to reach it is not a usable lead
    addr = " ".join(x for x in (first(t, "addr:housenumber"), first(t, "addr:street"),
                                first(t, "addr:city"), first(t, "addr:postcode")) if x)
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    return {
        "id": f"{el['type']}/{el['id']}",
        "name": name,
        "brand": first(t, "brand", "operator"),
        "seg": seg,
        "web": website if seg == "site" else "",
        "email": email,
        "email_src": "osm" if email else "",
        "phone": phone,
        "region": region,
        "country": country,
        "city": first(t, "addr:city") or region.split(",")[0],
        "addr": addr,
        "channels": channels,
        "lat": lat, "lon": lon,
        "first": TODAY,
    }


# ---------- email enrichment (scrape has-website leads' sites for a contact email) ----------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = ("example.", "sentry", "wixpress", "@2x", "noreply", "no-reply", ".png", ".jpg",
             ".gif", ".svg", "@sentry", "godaddy", "@email", "yourdomain", "domain.com")
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contact.html", "/about", "/about-us")
ENRICH_BATCH = int(os.environ.get("LS_ENRICH", "60"))


def _good_email(e):
    e = e.lower()
    return not any(b in e for b in BAD_EMAIL) and not re.fullmatch(r"[0-9a-f]{20,}@.*", e)


def scrape_email(site):
    root = re.match(r"(https?://[^/]+)", site)
    root = root.group(1) if root else site
    dom = _host(site).split(":")[0].replace("www.", "")
    for path in CONTACT_PATHS:
        body = http(root + path, timeout=12, tries=1)
        if not body:
            continue
        html = body.decode("utf-8", "ignore")
        found = [e for e in EMAIL_RE.findall(html) if _good_email(e)]
        if found:
            same = [e for e in found if dom in e.lower()]          # prefer an email on the site's own domain
            role = [e for e in (same or found) if e.lower().split("@")[0] in
                    ("info", "contact", "sales", "hello", "enquiries", "enquiry", "admin", "office", "mail")]
            return (role or same or found)[0]
    return None


def enrich_sites(state):
    """For a batch of has-website leads with no email yet, scrape their site for a contact email."""
    todo = [ld for ld in state["leads"].values()
            if ld.get("seg") == "site" and not ld.get("email") and ld.get("email_src") != "tried"]
    todo = todo[:ENRICH_BATCH]
    if not todo:
        return 0
    got = 0
    with ThreadPoolExecutor(10) as ex:
        for ld, em in zip(todo, ex.map(lambda l: scrape_email(l["web"]), todo)):
            if em:
                ld["email"], ld["email_src"] = em, "scraped"
                got += 1
            else:
                ld["email_src"] = "tried"  # don't refetch every run
    print(f"[+] enriched {got}/{len(todo)} site leads with an email")
    return got


def scan_region(region):
    country = region.split(",")[-1].strip()
    box = bbox(region)
    if not box:
        print(f"[!] no bbox for {region}")
        return []
    els = overpass(*box)
    leads = [ld for ld in (to_lead(e, region, country) for e in els) if ld]
    print(f"[+] {region}: {len(els)} agents, {len(leads)} without a website")
    time.sleep(3)  # be gentle with Overpass between regions
    return leads


def main():
    regions = load(REGIONS, [])
    if not regions:
        raise SystemExit("regions.json is empty")
    state = load(STATE, {"leads": {}, "offset": 0, "done": {}})
    state.setdefault("leads", {}); state.setdefault("offset", 0); state.setdefault("done", {})

    off = state["offset"] % len(regions)
    batch = regions[off:off + BATCH] or regions[:BATCH]
    state["offset"] = (off + BATCH) % len(regions)
    print(f"[i] scanning regions [{off}:{off + len(batch)}] of {len(regions)}")

    new_count = 0
    for region in batch:
        for ld in scan_region(region):
            old = state["leads"].get(ld["id"])
            ld["first"] = old["first"] if old else ld["first"]
            if not old:
                new_count += 1
            state["leads"][ld["id"]] = ld
        state["done"][region] = TODAY

    enrich_sites(state)  # scrape a batch of has-website leads for emails

    leads = list(state["leads"].values())
    leads.sort(key=lambda x: (x["country"], x["city"], x["name"]))
    by_country = {}
    for ld in leads:
        by_country[ld["country"]] = by_country.get(ld["country"], 0) + 1
    save(OUT, {
        "updated": NOW.isoformat(timespec="minutes"),
        "count": len(leads),
        "nosite": sum(1 for x in leads if x["seg"] == "nosite"),
        "site": sum(1 for x in leads if x["seg"] == "site"),
        "with_email": sum(1 for x in leads if x["email"]),
        "with_channel": sum(1 for x in leads if x["channels"]),
        "by_country": by_country,
        "regions_done": len(state["done"]),
        "regions_total": len(regions),
        "leads": leads,
    })
    save(STATE, state)
    print(f"[=] total {len(leads)} (+{new_count} new) | nosite {sum(1 for x in leads if x['seg']=='nosite')} "
          f"| site {sum(1 for x in leads if x['seg']=='site')} | emails {sum(1 for x in leads if x['email'])} "
          f"| regions {len(state['done'])}/{len(regions)}")


if __name__ == "__main__":
    main()
