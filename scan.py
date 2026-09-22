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
    q = f"""[out:json][timeout:120];
(
 node["office"="estate_agent"]({s},{w},{n},{e});
 way["office"="estate_agent"]({s},{w},{n},{e});
 node["shop"="estate_agent"]({s},{w},{n},{e});
 way["shop"="estate_agent"]({s},{w},{n},{e});
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


def to_lead(el, region, country):
    t = el.get("tags", {})
    if first(t, "website", "contact:website", "url"):
        return None  # only keep agents WITHOUT a website
    name = first(t, "name", "brand", "operator")
    if not name:
        return None
    channels = {c: first(t, *tags) for c, tags in CHANNEL_TAGS.items()}
    channels = {c: v for c, v in channels.items() if v}
    addr = " ".join(x for x in (first(t, "addr:housenumber"), first(t, "addr:street"),
                                first(t, "addr:city"), first(t, "addr:postcode")) if x)
    email = first(t, "email", "contact:email")
    phone = first(t, "phone", "contact:phone", "contact:mobile")
    if not (email or phone or channels):
        return None  # a name with no way to reach it is not a usable lead
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    return {
        "id": f"{el['type']}/{el['id']}",
        "name": name,
        "brand": first(t, "brand", "operator"),
        "email": email,
        "phone": phone,
        "region": region,
        "country": country,
        "city": first(t, "addr:city") or region.split(",")[0],
        "addr": addr,
        "channels": channels,
        "lat": lat, "lon": lon,
        "first": TODAY,
    }


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

    leads = list(state["leads"].values())
    leads.sort(key=lambda x: (x["country"], x["city"], x["name"]))
    by_country = {}
    for ld in leads:
        by_country[ld["country"]] = by_country.get(ld["country"], 0) + 1
    save(OUT, {
        "updated": NOW.isoformat(timespec="minutes"),
        "count": len(leads),
        "with_email": sum(1 for x in leads if x["email"]),
        "with_channel": sum(1 for x in leads if x["channels"]),
        "by_country": by_country,
        "regions_done": len(state["done"]),
        "regions_total": len(regions),
        "leads": leads,
    })
    save(STATE, state)
    print(f"[=] total leads {len(leads)} (+{new_count} new) | with email "
          f"{sum(1 for x in leads if x['email'])} | regions done {len(state['done'])}/{len(regions)}")


if __name__ == "__main__":
    main()
