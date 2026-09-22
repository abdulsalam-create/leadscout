# leadscout

**Phase 1 of a freelancing outreach pipeline.** Finds real-estate agents and agencies that have **no website** (the opportunity), across the UK, US, Canada, Australia and Europe, with whatever contact detail and advertising channel is publicly available. It is a bounty-watch-style tool: a stdlib Python scanner on GitHub Actions writing JSON, plus a static dashboard on GitHub Pages.

**[Dashboard](https://abdulsalam-create.github.io/leadscout/)** · filter, search, and export leads to CSV.

## Where the data comes from
[OpenStreetMap](https://www.openstreetmap.org) via the free Overpass API (no key), resolving cities to bounding boxes with Nominatim. It queries `office=estate_agent` / `shop=estate_agent`, keeps only those **without** a `website` tag, and records name, brand/brokerage, phone, email, address, map location, and social channels (Facebook, Instagram, WhatsApp, etc.). Only leads with **at least one contact method** are kept.

## Honest data reality (read this)
Businesses without a website usually do not publish an email in open data either. In testing, of ~2,000 no-website agents in three UK cities, ~8% had any contact detail at all and almost all of those were a **phone number**, not an email. So Phase 1 reliably gives you:
- the **target list** of no-website agents at scale, and
- a **phone or social channel** for each (often the very channel they advertise on),

but only a handful of emails. **Harvesting emails for cold outreach is Phase 2** (enrichment from directories, Google Business profiles, their Facebook page, or licensing records). This tool is built so that enrichment can be layered on later without changing the schema.

## Running
- Automatic: `.github/workflows/scan.yml` runs every 4 hours, scanning a batch of cities each time so the list builds up and the public APIs are never hammered.
- Manual: **Actions → scan-leads → Run workflow**, or `python scan.py` locally (`LS_BATCH=3 python scan.py` to scan fewer cities).
- Add target cities by editing `regions.json` (`"City, Country"` strings).

## The bigger plan
1. **This tool**: see the leads. ← you are here
2. Email enrichment + verification.
3. Cold email at scale from a custom domain with tailored drafts per business.
4. A separate portfolio site (custom domain) with demo builds as proof of concept.
