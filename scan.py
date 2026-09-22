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


# Country -> language for localized outreach (falls back to English).
LANG = {"Germany": "de", "Austria": "de", "Switzerland": "de", "France": "fr", "Belgium": "fr",
        "Spain": "es", "Italy": "it", "Netherlands": "nl", "Portugal": "pt"}
# Where cold B2B email is defensible (opt-out regimes). Germany/Austria (strict), Canada (CASL),
# France/Spain/Italy (stricter GDPR/ePrivacy) are deliberately excluded from the auto-campaign.
CAMPAIGN_OK = {"United Kingdom", "USA", "Ireland", "Australia", "Netherlands"}


def assess_site(site):
    """Fetch a lead's site: return (email, signals). Signals drive the redesign hook + score."""
    root = re.match(r"(https?://[^/]+)", site)
    root = root.group(1) if root else site
    dom = _host(site).split(":")[0].replace("www.", "")
    sig = {"https": site.lower().startswith("https"), "mobile": True, "year": None,
           "stack": "", "heavy": False, "reached": False}
    email = None
    for i, path in enumerate(CONTACT_PATHS):
        body = http(root + path, timeout=12, tries=1)
        if not body:
            continue
        sig["reached"] = True
        html = body.decode("utf-8", "ignore")
        if i == 0:  # assess the homepage
            low = html.lower()
            sig["mobile"] = 'name="viewport"' in low or "name='viewport'" in low
            sig["heavy"] = len(body) > 1_800_000
            yrs = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)[^\d]{0,12}(20\d\d)", html, re.I)]
            sig["year"] = max(yrs) if yrs else None
            if "wp-content" in low or "wordpress" in low:
                sig["stack"] = "wordpress"
            elif "wixsite" in low or "wix.com" in low:
                sig["stack"] = "wix"
            elif re.search(r"<table[^>]", low) and "grid" not in low and "flex" not in low:
                sig["stack"] = "table-layout"
        if not email:
            found = [e for e in EMAIL_RE.findall(html) if _good_email(e)]
            if found:
                same = [e for e in found if dom in e.lower()]
                role = [e for e in (same or found) if e.lower().split("@")[0] in
                        ("info", "contact", "sales", "hello", "enquiries", "enquiry", "admin", "office", "mail")]
                email = (role or same or found)[0]
        if email and i == 0:
            break  # got homepage signals + an email; no need for contact pages
    return email, sig


def site_score(sig):
    """0-100 redesign-worthiness + the single strongest hook code + issue list."""
    issues, score = [], 0
    if not sig.get("mobile"):
        issues.append("mobile"); score += 40
    if not sig.get("https"):
        issues.append("https"); score += 30
    yr = sig.get("year")
    if yr and yr <= NOW.year - 4:
        issues.append("outdated"); score += 25
    if sig.get("stack") in ("wix", "table-layout"):
        issues.append("dated_build"); score += 15
    if sig.get("heavy"):
        issues.append("slow"); score += 10
    if not issues:
        issues.append("generic"); score = 20
    order = ["mobile", "https", "outdated", "slow", "dated_build", "generic"]
    hook = min(issues, key=lambda x: order.index(x) if x in order else 9)
    return min(score, 100), hook, issues


def has_mx(email):
    """Deliverability check via DNS-over-HTTPS: does the email's domain accept mail?"""
    dom = email.split("@")[-1]
    try:
        body = http(f"https://dns.google/resolve?name={urllib.parse.quote(dom)}&type=MX", timeout=12, tries=1)
        d = json.loads(body)
        return any(a.get("type") == 15 for a in d.get("Answer", []))
    except Exception:  # noqa: BLE001
        return False


# ---------- prospect fit: favour small / starting-out agents; downrank big chains + pro sites ----------
COMPANY_WORDS = {"estate", "estates", "property", "properties", "lettings", "letting", "homes", "home",
                 "realty", "real", "group", "ltd", "limited", "llc", "inc", "associates", "partners",
                 "sales", "management", "residential", "agency", "agents", "agent", "co", "company",
                 "&", "and", "the", "international", "network", "services", "solutions",
                 "agence", "cabinet", "immobilier", "immobiliere", "groupe", "inmobiliaria",
                 "immobilien", "immobiliare", "imobiliaria", "makelaar", "makelaardij", "vastgoed",
                 "gestion", "conseil", "transactions", "patrimoine",
                 "gmbh", "sarl", "srl", "sas", "sa", "bv", "sl", "spa", "kg", "ug", "oü", "ohg",
                 "la", "le", "les", "il", "el", "lo", "de", "du", "van", "von", "del", "della", "los"}
BIG_BRANDS = {"foxtons", "connells", "savills", "knight frank", "winkworth", "hamptons", "chestertons",
              "countrywide", "bairstow eves", "william h brown", "your move", "reeds rains", "haart",
              "barnard marcus", "dexters", "chancellors", "leaders", "romans", "purplebricks", "kfh",
              "kinleigh", "marsh & parsons", "strutt & parker", "carter jonas", "fine & country",
              "jackson-stops", "john d wood", "belvoir", "martin & co", "hunters", "yopa", "openrent",
              "century 21", "century21", "re/max", "remax", "keller williams", "coldwell banker",
              "sotheby", "compass", "redfin", "berkshire hathaway", "douglas elliman", "exp realty",
              "engel & völkers", "engel & volkers", "ray white", "lj hooker", "harcourts", "barfoot",
              "royal lepage", "orpi", "laforêt", "laforet", "guy hoquet", "stéphane plaza", "stephane plaza",
              "era immobilier", "nestenn", "iad", "foncia", "citya", "square habitat", "l'adresse",
              "avis immobilier", "von poll", "tecnocasa", "don piso", "redpiso", "look & find",
              "gabetti", "tecnorete", "grimaldi", "toscano", "remo", "century 21"}


def is_person(name):
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    if not (2 <= len(toks) <= 3):
        return False
    if any(t.lower().strip(".,&") in COMPANY_WORDS for t in toks):
        return False
    return all(re.match(r"^[A-Z][a-zA-Z'’.-]+$", t) for t in toks)


def prospect_fit(ld):
    """0-100: how much this looks like a small / starting-out agent worth pitching."""
    score, why = 0, []
    name = ld.get("name", ""); brand = ld.get("brand", "")
    blob = (name + " " + brand).lower()
    seg = ld.get("seg")

    if seg == "nosite":
        score += 46; why.append("no website")
    else:
        iss = ld.get("issues") or []
        if any(i in iss for i in ("mobile", "https", "outdated", "dated_build", "slow")):
            score += 34; why.append("weak / dated site")
        elif iss == ["generic"] or not iss:
            score -= 12; why.append("already a solid site")

    if any(b in blob for b in BIG_BRANDS):
        score -= 55; why.append("major brand")
    elif brand:
        score -= 26; why.append("part of a chain")
    elif is_person(name):
        score += 30; why.append("individual agent")
    else:
        score += 12; why.append("small independent")

    if ld.get("email"):
        score += 8
    if ld.get("channels"):
        score += 6; why.append("advertises on social")

    ld["fit"] = max(0, min(100, score))
    ld["fit_why"] = why
    ld["prospect"] = ld["fit"] >= 55


def enrich_sites(state):
    """Batch: scrape has-website leads for email + assess site quality; verify email deliverability."""
    todo = [ld for ld in state["leads"].values()
            if ld.get("seg") == "site" and ld.get("email_src") != "tried" and not ld.get("qscore")]
    todo = todo[:ENRICH_BATCH]
    if not todo:
        return 0
    got = 0
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(lambda l: (l, *assess_site(l["web"])), todo))
    for ld, email, sig in results:
        score, hook, issues = site_score(sig)
        ld["qscore"], ld["hook"], ld["issues"] = score, hook, issues
        ld["lang"] = LANG.get(ld["country"], "en")
        if email and not ld.get("email"):
            ld["email"], ld["email_src"] = email, "scraped"
            got += 1
        if not ld.get("email"):
            ld["email_src"] = "tried"
        # deliverability + campaign eligibility
        ld["email_ok"] = bool(ld.get("email")) and has_mx(ld["email"])
        ld["campaign"] = bool(ld["email_ok"] and ld["country"] in CAMPAIGN_OK)
    print(f"[+] assessed {len(todo)} sites | {got} new emails | "
          f"{sum(1 for l,_ ,_ in results if l.get('campaign'))} campaign-ready")
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
    for ld in leads:
        prospect_fit(ld)  # recomputed every run (cheap, no network)
    leads.sort(key=lambda x: (-x.get("fit", 0), x["country"], x["city"], x["name"]))
    by_country = {}
    for ld in leads:
        by_country[ld["country"]] = by_country.get(ld["country"], 0) + 1
    save(OUT, {
        "updated": NOW.isoformat(timespec="minutes"),
        "count": len(leads),
        "nosite": sum(1 for x in leads if x["seg"] == "nosite"),
        "site": sum(1 for x in leads if x["seg"] == "site"),
        "with_email": sum(1 for x in leads if x["email"]),
        "verified": sum(1 for x in leads if x.get("email_ok")),
        "campaign": sum(1 for x in leads if x.get("campaign")),
        "prospects": sum(1 for x in leads if x.get("prospect")),
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
