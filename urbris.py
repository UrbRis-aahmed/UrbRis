#!/usr/bin/env python3
import json, urllib.request, urllib.error, urllib.parse, webbrowser, os, sys, csv, math, uuid, base64, re, time, threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
CELL_CSV = os.path.join(BASE, "cell_towers.csv")
GRID_SIZE = 0.1  # degrees, roughly 11km - buckets towers for fast nearest-neighbor lookup
TOWER_GRID = {}

# ---- Saved-routes storage --------------------------------------------------
# Supabase (Postgres via its REST API), not a local JSON file - Render's free-tier
# filesystem is ephemeral and wipes on every restart/redeploy, which is a real risk
# once actual users are saving routes. Needs SUPABASE_URL and SUPABASE_KEY set as
# Render environment variables - never hardcoded, never sent to the client.
#
# Expected table (create once in the Supabase SQL editor):
#   create table routes (
#     id text primary key,
#     name text,
#     saved_at timestamptz default now(),
#     updated_at timestamptz default now(),
#     meta jsonb,
#     data jsonb
#   );
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def supabase_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)

def supabase_request(method, path, body=None, extra_headers=None):
    url = SUPABASE_URL + "/rest/v1/" + path
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def load_towers():
    if not os.path.exists(CELL_CSV):
        print("  No cell_towers.csv found next to urbris.py - cell coverage feature disabled")
        print("  (Download a tower CSV from opencellid.org and save it as cell_towers.csv to enable it)")
        return
    count = 0
    with open(CELL_CSV, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            # OpenCelliD's bulk export has no header row and a fixed column
            # order: radio,mcc,net,area,cell,unit,lon,lat,range,samples,...
            # Columns 6 and 7 (0-indexed) are lon and lat.
            if len(row) < 8:
                continue
            try:
                lon = float(row[6]); lat = float(row[7])
            except ValueError:
                continue  # a header row (if present) or a malformed line
            if lat == 0 and lon == 0:
                continue  # common bad-data sentinel in OpenCelliD exports
            key = (round(lat / GRID_SIZE), round(lon / GRID_SIZE))
            TOWER_GRID.setdefault(key, []).append((lat, lon))
            count += 1
    print(f"  Loaded {count} cell towers from cell_towers.csv")

def fetch_overpass_bbox_roads(minlat, minlon, maxlat, maxlon):
    query = (
        '[out:json][timeout:55];'
        'way["highway"]["highway"!~"^(path|track|footway|service|cycleway|steps|'
        'pedestrian|proposed|construction|bridleway|motorway_link)$"]'
        '(%f,%f,%f,%f);'
        'out geom;'
    ) % (minlat, minlon, maxlat, maxlon)
    return fetch_overpass(query)

def route_bbox(path, pad_m):
    lats = [p["lat"] for p in path]; lons = [p["lon"] for p in path]
    minlat, maxlat = min(lats), max(lats)
    minlon, maxlon = min(lons), max(lons)
    m_lat, m_lon = meters_per_deg((minlat + maxlat) / 2)
    return (minlat - pad_m / m_lat, minlon - pad_m / m_lon,
            maxlat + pad_m / m_lat, maxlon + pad_m / m_lon)

def nearest_route_point(lat, lon, path, cum_dist):
    # Linear scan against the route path is fine at route length scale (hundreds to
    # low thousands of points) - mirrors the same tradeoff already made client-side
    # in nearestCodedPoint() for route-safety comparison.
    best_i, best_d = None, None
    for i, p in enumerate(path):
        d = haversine_km(lat, lon, p["lat"], p["lon"]) * 1000
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    return best_i, best_d

def find_route_intersections(path, tolerance_m=50, pad_m=300):
    # Every road returned by Overpass carries its full vertex geometry (out geom).
    # A real intersection is simply a coordinate that two or more distinct ways both
    # contain - no separate node-graph query needed, just cross-referencing the
    # geometry already being fetched. Rounded to ~1cm to absorb float serialization
    # noise between ways while still requiring a genuinely shared point, not a
    # nearby-but-different one.
    minlat, minlon, maxlat, maxlon = route_bbox(path, pad_m)
    body, err = fetch_overpass_bbox_roads(minlat, minlon, maxlat, maxlon)
    if err:
        return None, err
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        return None, "Overpass returned malformed JSON: " + str(e)
    coord_ways = {}
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        wid = el.get("id")
        for node in el["geometry"]:
            key = (round(node["lat"], 7), round(node["lon"], 7))
            coord_ways.setdefault(key, set()).add(wid)

    # Precompute cumulative distance along the route so each intersection can be
    # placed at a real km marker, not just flagged as "somewhere near this route."
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + haversine_km(path[i-1]["lat"], path[i-1]["lon"], path[i]["lat"], path[i]["lon"]) * 1000)

    intersections = []
    seen_close = []  # dedup: two OSM nodes 5m apart shouldn't become two separate results
    for (lat, lon), way_ids in coord_ways.items():
        if len(way_ids) < 2:
            continue
        idx, dist_to_route = nearest_route_point(lat, lon, path, cum)
        if idx is None or dist_to_route > tolerance_m:
            continue
        if any(haversine_km(lat, lon, s[0], s[1]) * 1000 < 15 for s in seen_close):
            continue
        seen_close.append((lat, lon))
        intersections.append({
            "lat": lat, "lon": lon,
            "kmAlongRoute": round(cum[idx] / 1000, 3),
            "distFromRouteM": round(dist_to_route, 1),
            "approachRoads": len(way_ids)
        })
    intersections.sort(key=lambda x: x["kmAlongRoute"])
    return intersections, None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

# --- Route discovery: curvature math ported exactly from the same functions
# already used client-side (circumRadius/classifyRadius), so a road scored here
# would score identically if it were later drawn and analyzed the normal way. ---
def meters_per_deg(lat):
    r = math.radians(lat)
    return (111132.92 - 559.82 * math.cos(2 * r) + 1.175 * math.cos(4 * r),
            111412.84 * math.cos(r) - 93.5 * math.cos(3 * r))

def to_xy(pt, origin, m_lat, m_lon):
    return ((pt[1] - origin[1]) * m_lon, (pt[0] - origin[0]) * m_lat)

def circum_radius(p1, p2, p3):
    m_lat, m_lon = meters_per_deg(p2[0])
    a = to_xy(p1, p2, m_lat, m_lon)
    b = (0.0, 0.0)
    c = to_xy(p3, p2, m_lat, m_lon)
    ab = math.hypot(b[0] - a[0], b[1] - a[1])
    bc = math.hypot(c[0] - b[0], c[1] - b[1])
    ca = math.hypot(a[0] - c[0], a[1] - c[1])
    area = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2
    if area < 1e-4 or ab < 1e-3 or bc < 1e-3:
        return float("inf")
    return (ab * bc * ca) / (4 * area)

def curviness_score(points):
    """Average of (1 / radius) across every interior point - tighter curves
    contribute more, straight stretches (radius -> infinity) contribute ~0.
    Real number, not a classification label, so segments can be ranked directly."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(1, len(points) - 1):
        r = circum_radius(points[i - 1], points[i], points[i + 1])
        total += (1.0 / r) if r < 2000 else 0.0
    return total / (len(points) - 2)

def fetch_overpass_roads(lat, lon, radius_m):
    query = (
        '[out:json][timeout:55];'
        'way["highway"]["highway"!~"^(path|track|footway|service|cycleway|steps|'
        'pedestrian|proposed|construction|bridleway|motorway|motorway_link)$"]'
        '(around:%d,%f,%f);'
        'out geom;'
    ) % (radius_m, lat, lon)
    req = urllib.request.Request(
        # overpass-api.de's main public instance has been broadly rejecting
        # requests with this exact 406 recently - confirmed by multiple independent
        # reports of unrelated tools (QGIS, other scripts) hitting the identical
        # error, not something specific to this request. Using a mirror instead,
        # confirmed by another user's report to resolve the exact same symptom.
        "https://overpass.private.coffee/api/interpreter",
        data=("data=" + urllib.parse.quote(query)).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "*/*",
                 "User-Agent": "Mozilla/5.0 (compatible; UrbRis/1.0; +https://urbris.com)"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def discover_windy_routes(lat, lon, radius_km, gkey):
    data = fetch_overpass_roads(lat, lon, radius_km * 1000)
    candidates = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
        if len(pts) < 5:
            continue
        length_km = sum(haversine_km(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
                         for i in range(len(pts) - 1))
        if length_km < 0.5:  # too short to be a real "route" worth riding on its own
            continue
        curv = curviness_score(pts)
        if curv <= 0:
            continue
        candidates.append({
            "id": el.get("id"), "name": el.get("tags", {}).get("name", "Unnamed road"),
            "points": pts, "length_km": round(length_km, 2), "curviness_raw": curv
        })

    if not candidates:
        return []

    # Narrow hard before spending anything on Elevation - the actual cost control.
    candidates.sort(key=lambda c: c["curviness_raw"], reverse=True)
    shortlist = candidates[:40]

    # Elevation-based scenic scoring, batched into as few requests as possible,
    # only ever run against the already-narrowed shortlist, never the full pull.
    if gkey:
        for c in shortlist:
            sample = c["points"][::max(1, len(c["points"]) // 10)][:10]
            locs = "|".join("%f,%f" % (p[0], p[1]) for p in sample)
            try:
                url = "https://maps.googleapis.com/maps/api/elevation/json?locations=" + urllib.parse.quote(locs) + "&key=" + gkey
                with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
                    elev_data = json.loads(r.read())
                elevs = [res["elevation"] for res in elev_data.get("results", [])]
                c["elev_variation"] = (max(elevs) - min(elevs)) if len(elevs) >= 2 else 0.0
            except Exception:
                c["elev_variation"] = 0.0
    else:
        for c in shortlist:
            c["elev_variation"] = 0.0

    max_curv = max(c["curviness_raw"] for c in shortlist) or 1.0
    max_elev = max(c["elev_variation"] for c in shortlist) or 1.0
    for c in shortlist:
        curv_norm = c["curviness_raw"] / max_curv
        scenic_norm = c["elev_variation"] / max_elev
        c["score"] = round(0.5 * curv_norm + 0.5 * scenic_norm, 4)
        del c["curviness_raw"]

    shortlist.sort(key=lambda c: c["score"], reverse=True)
    return shortlist[:5]

def fetch_log_data():
    rows = supabase_request("GET", "routes?select=data")
    all_pts = []
    total_km = 0.0
    for row in rows:
        pts = ((row.get("data") or {}).get("res")) or []
        if len(pts) < 2:
            continue
        # Only the fields actually needed to draw the map - not full route objects
        # (images, addresses, etc.), keeping every /log-data poll lightweight.
        slim = [
            {"lat": p.get("lat"), "lon": p.get("lon"), "irap": p.get("irap"),
             "imageSource": p.get("imageSource"), "hasCoverage": p.get("hasCoverage")}
            for p in pts
        ]
        all_pts.append(slim)
        total_km += pts[-1].get("km", 0) - pts[0].get("km", 0)
    return total_km, all_pts

def call_claude_web_search(prompt, akey, model="claude-sonnet-5"):
    payload = json.dumps({
        "model": model, "max_tokens": 2000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": akey, "anthropic-version": "2023-06-01"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read())
    texts = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
    return "\n".join(texts)

BRIEFING_WHY_NOTES = {
    # Grounded in the same research already curated on /research - not invented for
    # this feature. Handed to Claude as real context it may draw on, never as something
    # to embellish beyond.
    "curvature": "curvature is one of the most decisive infrastructure factors in motorcycle risk scoring, per real road-attribute research",
    "safety_barrier": "iRAP's own Motorcycle Safety Review Panel found barrier design matters enormously for injury severity on a slide - a missing or damaged barrier is a real gap in what would catch you",
    "delineation": "poor or missing lane markings make it harder to read a curve's true shape at speed, before you're already committed to it",
    "road_surface": "unpaved or mixed surface changes available traction under lean angle, differently than it would for a car",
    "road_condition": "poor surface condition affects traction and shock loading through the suspension",
    "shoulder_type": "no shoulder means less room to recover if you're forced off your line",
    "roadside_distance": "how close a fixed hazard sits to the road edge determines what's actually there if you do go off",
    "street_lighting": "no lighting matters specifically for visibility if riding this after dark",
}

def extract_json_array(text):
    clean = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    # fallback: Claude may add a stray sentence despite instructions - pull the
    # outermost [ ... ] rather than fail the whole scan over it
    start, end = clean.find("["), clean.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(clean[start:end + 1])
        except Exception:
            pass
    return []

def extract_json_object(text):
    clean = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    start, end = clean.find("{"), clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(clean[start:end + 1])
        except Exception:
            pass
    return {}

RESEARCH_SEARCH_PROMPT = (
    "Search the web for motorcycle safety or road infrastructure research relevant to "
    "motorcyclists, published in roughly the last 7 days. Focus on peer-reviewed studies, "
    "government or industry road-safety reports (e.g. iRAP, NHTSA, Transport Canada, MTO), "
    "and credible news coverage of genuinely new research findings. Do not include anything "
    "already well established or widely known (do not re-report the Hurt Report, MAIDS, MCCS, "
    "or iRAP's general methodology - only newly published material).\n\n"
    "Respond with ONLY a JSON array, no other text, no markdown fences, no preamble. Each "
    'item: {"title": "...", "url": "...", "summary": "one or two factual sentences"}. If '
    "nothing new was found, respond with exactly []."
)

INCIDENT_SEARCH_PROMPT = (
    "Search the web for Ontario, Canada motorcycle collisions reported in roughly the last "
    "3 days - news coverage, OPP news releases, or local police reports. Only include "
    "incidents with a specific location (a named road, highway, or intersection) - exclude "
    "anything with only a city or region-level location. Be factual and respectful in "
    "summaries; do not speculate or editorialize about a real person's death.\n\n"
    "Respond with ONLY a JSON array, no other text, no markdown fences, no preamble. Each "
    'item: {"title": "...", "url": "...", "summary": "one or two factual sentences '
    'including date, specific location, and outcome"}. If nothing new was found, respond '
    "with exactly []."
)

def send_research_email(new_research, new_incidents):
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_email = os.environ.get("RESEARCH_EMAIL_TO", "")
    from_email = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
    if not (api_key and to_email):
        print("  [research-scan] RESEND_API_KEY or RESEARCH_EMAIL_TO not set - skipping email")
        return

    def section(title, items):
        if not items:
            return ""
        rows = "".join(
            '<li style="margin-bottom:12px"><a href="%s">%s</a><br>'
            '<span style="color:#666;font-size:13px">%s</span></li>'
            % (i.get("url", ""), i.get("title", "Untitled"), i.get("summary", ""))
            for i in items
        )
        return "<h3>%s</h3><ul>%s</ul>" % (title, rows)

    html = section("New research", new_research) + section("New Ontario incidents", new_incidents)
    if not html:
        return
    body = json.dumps({
        "from": from_email, "to": [to_email],
        "subject": "Urbris research update - %d new studies, %d new incidents" % (len(new_research), len(new_incidents)),
        "html": html
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        print("  [research-scan] email sent")
    except urllib.error.HTTPError as e:
        print("  [research-scan] email send failed:", e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        print("  [research-scan] email send failed:", e)

def should_run_daily_research_scan():
    if not supabase_configured():
        return False
    try:
        rows = supabase_request("GET", "research_feed?type=eq._scan_marker&select=found_at&order=found_at.desc&limit=1")
        if not rows:
            return True
        last = (rows[0] or {}).get("found_at")
        if not last:
            return True
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > 20 * 3600
    except Exception as e:
        print("  [research-scan] could not check last scan time:", e)
        return False

def run_daily_research_scan():
    # Reuses ANTHROPIC_API_KEY (already required for the batch-completion poller) and
    # Claude's own server-side web_search tool - one Messages API call executes the
    # search and returns results in the same response, no separate search API/key
    # needed. Marker row is written up front so a zero-result scan still counts as
    # "ran today" and the 3-min poller doesn't retry it forever.
    if not supabase_configured():
        return
    akey = os.environ.get("ANTHROPIC_API_KEY", "")
    if not akey:
        return

    try:
        supabase_request("POST", "research_feed", body={
            "type": "_scan_marker", "title": "scan", "url": "_meta:scan:" + str(uuid.uuid4()),
            "summary": "", "found_at": now_iso()
        })
    except Exception as e:
        print("  [research-scan] could not write scan marker - aborting to avoid retry storm:", e)
        return

    try:
        existing = supabase_request("GET", "research_feed?select=url") or []
        existing_urls = {r.get("url") for r in existing}
    except Exception as e:
        print("  [research-scan] could not fetch existing items:", e)
        existing_urls = set()

    new_research, new_incidents = [], []
    try:
        raw = call_claude_web_search(RESEARCH_SEARCH_PROMPT, akey)
        for item in extract_json_array(raw):
            if item.get("url") and item["url"] not in existing_urls:
                new_research.append(item)
    except Exception as e:
        print("  [research-scan] research search failed:", e)

    try:
        raw = call_claude_web_search(INCIDENT_SEARCH_PROMPT, akey)
        for item in extract_json_array(raw):
            if item.get("url") and item["url"] not in existing_urls:
                new_incidents.append(item)
    except Exception as e:
        print("  [research-scan] incident search failed:", e)

    for kind, items in (("research", new_research), ("incident", new_incidents)):
        for item in items:
            try:
                supabase_request("POST", "research_feed", body={
                    "type": kind, "title": (item.get("title") or "")[:300],
                    "url": item["url"], "summary": (item.get("summary") or "")[:600],
                    "found_at": now_iso()
                })
            except Exception as e:
                print("  [research-scan] insert failed:", e)

    if new_research or new_incidents:
        send_research_email(new_research, new_incidents)
    print("  [research-scan] found %d new research item(s), %d new incident(s)" % (len(new_research), len(new_incidents)))

def render_research_page(feed_items=None, search_q="", search_type=""):
    feed_items = feed_items or []
    is_search = bool(search_q or search_type)

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def render_feed_paper(item, meta_label):
        return (
            '<div class="paper"><div class="meta">%s</div><h3><a href="%s" target="_blank" '
            'rel="noopener">%s</a></h3><p>%s</p></div>'
        ) % (esc(meta_label), esc(item.get("url", "")), esc(item.get("title", "Untitled")), esc(item.get("summary", "")))

    search_form = """
  <form method="get" action="/research" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:28px">
    <input type="text" name="q" value="%s" placeholder="Search titles and summaries..." style="flex:1;min-width:200px;background:var(--sf2);border:1px solid var(--b2);border-radius:8px;padding:10px 14px;color:var(--tx);font-size:14px">
    <select name="type" style="background:var(--sf2);border:1px solid var(--b2);border-radius:8px;padding:10px 14px;color:var(--tx);font-size:14px">
      <option value="">All</option>
      <option value="research"%s>Research only</option>
      <option value="incident"%s>Incidents only</option>
    </select>
    <button type="submit" style="background:var(--ac);border:none;border-radius:8px;padding:10px 20px;color:#fff;font-size:14px;font-weight:600;cursor:pointer">Search</button>
    %s
  </form>""" % (
        esc(search_q),
        ' selected' if search_type == 'research' else '',
        ' selected' if search_type == 'incident' else '',
        '<a href="/research" style="align-self:center;color:var(--mu);font-size:13px">Clear</a>' if is_search else ''
    )

    if is_search:
        results_html = "".join(
            render_feed_paper(f, ("Research" if f.get("type") == "research" else "Incident") + " · Found " + (f.get("found_at") or "")[:10])
            for f in feed_items
        ) or '<p class="section-sub" style="margin:0">No matches.</p>'
        results_label = "%d result(s)" % len(feed_items)
        if search_q:
            results_label += ' for "%s"' % esc(search_q)
        dynamic_sections = """
<section class="wrap">
  <h2>Search results</h2>
  <p class="section-sub">""" + results_label + """</p>
  """ + results_html + """
</section>"""
    else:
        live_research = [f for f in feed_items if f.get("type") == "research"]
        live_incidents = [f for f in feed_items if f.get("type") == "incident"]
        live_research_html = "".join(
            render_feed_paper(f, "Found " + (f.get("found_at") or "")[:10]) for f in live_research
        ) or '<p class="section-sub" style="margin:0">No new research found yet - this section fills in as the daily scan runs.</p>'
        live_incidents_html = "".join(
            render_feed_paper(f, "Found " + (f.get("found_at") or "")[:10]) for f in live_incidents
        ) or '<p class="section-sub" style="margin:0">No new incidents found yet - this section fills in as the daily scan runs.</p>'
        dynamic_sections = """
<section class="wrap">
  <h2>Recent research</h2>
  <p class="section-sub">Automatically checked daily for newly published motorcycle safety and road infrastructure research.</p>
  """ + live_research_html + """
</section>

<section class="wrap">
  <h2>Recent Ontario incidents</h2>
  <p class="section-sub">Automatically checked daily for newly reported Ontario motorcycle collisions with a specific, named location.</p>
  """ + live_incidents_html + """
</section>"""

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Research — Urbris</title>
<style>
:root{
  --bg:#0b0e0d; --sf:#121715; --sf2:#18201d;
  --b:rgba(229,235,229,.08); --b2:rgba(229,235,229,.16);
  --tx:#f0f2ec; --mu:#8d9890; --mu2:#69736c;
  --ac:#dd6a32; --ac2:#f1844e; --ac-soft:rgba(221,106,50,.12);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;letter-spacing:-.005em;line-height:1.6}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:880px;margin:0 auto;padding:0 28px}
a{color:var(--ac2)}
nav{position:sticky;top:0;z-index:40;backdrop-filter:blur(10px);background:rgba(11,14,13,.82);border-bottom:1px solid var(--b)}
nav .wrap{display:flex;align-items:center;justify-content:space-between;height:56px}
.logo{font-weight:760;letter-spacing:-.045em;font-size:19px}
.logo span{color:var(--ac2)}
.navlinks{display:flex;gap:22px;font-size:13px;color:var(--mu)}
.navlinks a{color:var(--mu);text-decoration:none}
.navlinks a:hover{color:var(--tx)}
header{padding:72px 0 48px}
.eyebrow{display:inline-block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--ac2);font-family:ui-monospace,monospace;margin-bottom:18px}
h1{font-size:38px;line-height:1.1;letter-spacing:-.02em;font-weight:760;margin:0 0 18px}
.lede{font-size:16.5px;color:var(--mu);max-width:640px;line-height:1.65;margin:0}
.disclosure{border:1px solid var(--b);border-left:2px solid var(--ac2);border-radius:8px;padding:16px 20px;background:var(--ac-soft);font-size:13.5px;margin:32px 0 0}
section{padding:52px 0;border-top:1px solid var(--b)}
h2{font-size:22px;letter-spacing:-.015em;margin:0 0 8px}
.section-sub{color:var(--mu);font-size:14px;max-width:600px;margin:0 0 32px}
.paper{border:1px solid var(--b);border-radius:10px;padding:20px 22px;margin-bottom:14px;background:rgba(17,22,20,.4)}
.paper .meta{font-family:ui-monospace,monospace;font-size:10.5px;color:var(--mu2);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.paper h3{font-size:15.5px;margin:0 0 8px}
.paper h3 a{text-decoration:none;color:var(--tx)}
.paper h3 a:hover{color:var(--ac2)}
.paper p{font-size:13.5px;color:var(--mu);margin:0}
footer{padding:56px 0 80px;text-align:center;color:var(--mu2);font-size:12.5px;border-top:1px solid var(--b)}
@media(max-width:640px){h1{font-size:28px}}
</style>
</head>
<body>
<nav><div class="wrap">
  <div class="logo">Urb<span>ris</span></div>
  <div class="navlinks"><a href="/">App</a><a href="/log">Log</a></div>
</div></nav>

<header class="wrap">
  <div class="eyebrow">Research</div>
  <h1>What the evidence actually says about why riders crash — and how road design changes it.</h1>
  <p class="lede">Urbris exists as a research and training effort as much as a tool: coding roads against a real standard only means something if the standard itself is grounded in real crash causation research, not intuition. This page collects the studies Urbris's own methodology is built on and checked against.</p>
  <div class="disclosure">This is a curated reading list, not original Urbris research (yet) — every study below is independent, peer-reviewed or government/industry-published work, linked directly to its source. Where a finding directly shapes how Urbris codes a road, that connection is called out explicitly. The "Recent" sections below refresh daily via an automated search - unlike the core list above, those entries have not been individually reviewed before appearing here.</div>
  """ + search_form + """
</header>
""" + dynamic_sections + """
<section class="wrap">
  <h2>Human factors: why crashes happen</h2>
  <p class="section-sub">The major case-control studies of real motorcycle crashes, in rough chronological order. All four use a similar method: investigate real crash scenes, then compare against similar riders on the same road who didn't crash, to isolate what actually elevated risk.</p>

  <div class="paper">
    <div class="meta">FHWA · 2019 · United States</div>
    <h3><a href="https://www.fhwa.dot.gov/publications/research/safety/18064/18064.pdf" target="_blank" rel="noopener">Motorcycle Crash Causation Study (MCCS)</a></h3>
    <p>The modern successor to the Hurt Report and the current gold standard for US causation research — over 1,900 data elements analyzed per crash, with a 14-volume supplemental series covering individual factors in depth.</p>
  </div>

  <div class="paper">
    <div class="meta">NTSB · 2018 · United States</div>
    <h3><a href="https://www.ntsb.gov/safety/safety-studies/Documents/SR1801.pdf" target="_blank" rel="noopener">Safety Report on Motorcycle Crash Risk Factors</a></h3>
    <p>A policy-focused read of the MCCS data, organized around four issue areas: crash warning and prevention, braking and stability, alcohol and drug use, and licensing.</p>
  </div>

  <div class="paper">
    <div class="meta">ACEM / MAIDS · 2000 (v2.0 update) · Five EU countries</div>
    <h3><a href="https://en.wikipedia.org/wiki/MAIDS_report" target="_blank" rel="noopener">MAIDS — Motorcycle Accidents In Depth Study</a></h3>
    <p>The European equivalent to the Hurt Report — around 2,000 variables coded per crash, including full on-scene reconstructions, compared against matched non-crash exposure riders.</p>
  </div>

  <div class="paper">
    <div class="meta">USC / Harry Hurt · 1981 · Los Angeles, US</div>
    <h3><a href="https://en.wikipedia.org/wiki/Hurt_Report" target="_blank" rel="noopener">Motorcycle Accident Cause Factors and Identification of Countermeasures ("The Hurt Report")</a></h3>
    <p>The foundational study — on-scene investigation of over 900 real crashes. Some individual findings have since been superseded, but it remains the field's most-cited baseline, and the method every study since has followed.</p>
  </div>
</section>

<section class="wrap">
  <h2>Infrastructure: what the road itself changes</h2>
  <p class="section-sub">Most causation research above skews toward rider and driver behavior. This is the research that isolates road design specifically — the part Urbris actually measures.</p>

  <div class="paper">
    <div class="meta">iRAP · ongoing</div>
    <h3><a href="https://irap.org/research-and-technical-papers/" target="_blank" rel="noopener">iRAP Research &amp; Technical Papers</a></h3>
    <p>Includes iRAP's own commissioned Motorcycle Safety Review Panel report, finding that crash barriers can be specifically designed to give riders real protection against the features that cause the most devastating injuries — directly informing how Urbris weights barrier condition.</p>
  </div>

  <div class="paper">
    <div class="meta">Journal of Railway Transportation and Technology</div>
    <h3><a href="https://mail.jrtt.org/jrtt/article/view/77" target="_blank" rel="noopener">Road Attributes and Traffic Characteristics Effects on Motorcycle Safety</a></h3>
    <p>Identifies curvature, median transversability, and operating speed as the most decisive infrastructure factors in motorcycle risk scoring — the direct research basis for why Urbris measures real curve radius as a primary signal, not a secondary one.</p>
  </div>

  <div class="paper">
    <div class="meta">FEMA (Federation of European Motorcyclists) × iRAP · 2023</div>
    <h3><a href="https://www.femamotorcycling.eu/wp-content/uploads/systematic_approach_mc_safety_2023_WT_V4.pdf" target="_blank" rel="noopener">Moving Towards a Systematic Approach for Motorcycle Safety</a></h3>
    <p>A joint framing of the "safe system" approach specifically for motorcyclists — human behaviour, vehicle design, and road infrastructure treated as one system rather than three separate problems.</p>
  </div>

  <div class="paper">
    <div class="meta">iRAP</div>
    <h3><a href="https://irap.org/" target="_blank" rel="noopener">iRAP Star Rating Methodology</a></h3>
    <p>The infrastructure-safety standard Urbris codes every route against — Star Ratings for vehicle occupants, motorcyclists, cyclists, and pedestrians, adopted by the WHO as a global road safety benchmark.</p>
  </div>
</section>

<section class="wrap">
  <h2>Riding abroad on a Canadian license</h2>
  <p class="section-sub">General guidance for Canadian-licensed riders, not a definitive per-country list — requirements vary by destination, change over time, and sometimes depend on length of stay or the rental company itself. Always verify with your destination's embassy/consulate or the official Government of Canada travel advisory before you go.</p>

  <div class="disclosure"><b>The one detail worth knowing before anything else:</b> an International Driving Permit only carries the vehicle classes already on your underlying Canadian license. If your license doesn't have a motorcycle endorsement, the IDP issued from it will not authorize you to ride a motorcycle abroad — it inherits your license's restrictions, it doesn't expand them.</div>

  <div class="paper">
    <div class="meta">What it is</div>
    <h3>International Driving Permit (IDP)</h3>
    <p>Not a license on its own — a standardized translation of your existing Canadian license, valid only alongside it. Recognized in roughly 186 countries that are party to the 1949 Convention on Road Traffic, plus some non-signatory countries that honor it anyway. Valid for one year from issue date.</p>
  </div>

  <div class="paper">
    <div class="meta">How to get one, in Canada</div>
    <h3>CAA is the only authorized issuer</h3>
    <p>Issued exclusively by the Canadian Automobile Association under a UN-approved mandate — any IDP not issued by CAA is not genuine and will not be accepted abroad. $32 CAD as of December 2025. Requires a valid Canadian provincial/territorial license (learner's and suspended licenses don't qualify) plus two passport-style photos.</p>
  </div>

  <div class="paper">
    <div class="meta">United States</div>
    <h3>No IDP needed for a Canadian license</h3>
    <p>Canadians with a provincial or territorial driver's license (including a motorcycle endorsement) can ride in the US on that license directly — no IDP required.</p>
  </div>

  <div class="paper">
    <div class="meta">Worth knowing requirements do change</div>
    <h3>Vietnam stopped recognizing the Canadian IDP</h3>
    <p>Effective 2025, Vietnam no longer accepts the Canadian-issued IDP — a concrete example of why checking your specific destination before departure matters, not a one-time check you can rely on indefinitely.</p>
  </div>

  <div class="paper">
    <div class="meta">Always check the actual current requirement</div>
    <h3><a href="https://travel.gc.ca/travelling/documents/international-driving-permit" target="_blank" rel="noopener">Government of Canada — International Driving Permit guidance</a></h3>
    <p>The authoritative, kept-current source. Look under the "Laws and culture" tab of the Travel Advice and Advisory for your specific destination, or contact that country's embassy/consulate in Canada directly.</p>
  </div>
</section>

<footer class="wrap">Urbris — road risk, measured against real research, not intuition.</footer>
</body>
</html>"""

def render_public_log_page(gkey, total_km, all_routes_pts):
    data_json = json.dumps(all_routes_pts)

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UrbRis - Road Log</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { width:100%; height:100%; background:#0e0f11; overflow:hidden; font-family:-apple-system,Segoe UI,sans-serif; }
  #map { width:100vw; height:100vh; }
  /* Floating overlay, not a header - the map fills the entire screen underneath it,
     built for a TV/billboard running unattended, not a page someone scrolls. */
  #overlay {
    position:fixed; top:32px; left:32px; z-index:10;
    background:rgba(14,15,17,0.72); backdrop-filter:blur(6px);
    border:1px solid rgba(255,255,255,0.08); border-radius:16px;
    padding:24px 32px; color:#e8e8ec;
  }
  #overlay .title { font-size:18px; color:#9a9da4; letter-spacing:0.06em; text-transform:uppercase; margin-bottom:6px; }
  #overlay .km { font-size:64px; font-weight:600; line-height:1; }
  #overlay .km span { font-size:28px; font-weight:400; color:#9a9da4; margin-left:8px; }
  /* Same dark panel treatment as the km overlay, mirrored to the opposite corner. */
  #clockOverlay {
    position:fixed; top:32px; right:32px; z-index:10;
    background:rgba(14,15,17,0.72); backdrop-filter:blur(6px);
    border:1px solid rgba(255,255,255,0.08); border-radius:50%;
    width:140px; height:140px;
  }
</style></head>
<body>
<div id="map"></div>
<div id="overlay">
  <div class="title">UrbRis Road Log</div>
  <div class="km" id="kmStat">""" + f"{total_km:.1f}" + """<span>km verified</span></div>
</div>
<div id="clockOverlay">
  <svg id="clockSvg" viewBox="0 0 140 140" width="140" height="140"></svg>
</div>
<script>
let ROUTES = """ + data_json + """;
let map, polylines = [];

const RF = {
  road_surface: ['unpaved', 'mixed'], road_condition: ['poor', 'very poor'],
  delineation: ['poor', 'none'], safety_barrier: ['none', 'damaged'],
  shoulder_type: ['none'], roadside_distance: ['0-1m', '1-5m'],
  curvature: ['sharp'], street_lighting: ['no']
};
function riskScore(irap) {
  const keys = Object.keys(RF);
  let hit = 0;
  keys.forEach(k => {
    const v = irap[k];
    if (v != null && RF[k].some(f => String(v).toLowerCase().includes(f))) hit++;
  });
  return hit / keys.length;
}
const VIRIDIS_STOPS = [
  [0.00, 68, 1, 84], [0.13, 72, 40, 120], [0.25, 62, 74, 137], [0.38, 49, 104, 142],
  [0.50, 38, 130, 142], [0.63, 31, 158, 137], [0.75, 53, 183, 121], [0.88, 109, 205, 89],
  [1.00, 253, 231, 37]
];
function viridisRGB(score) {
  const s = Math.max(0, Math.min(1, score));
  for (let i = 0; i < VIRIDIS_STOPS.length - 1; i++) {
    const [t0, r0, g0, b0] = VIRIDIS_STOPS[i], [t1, r1, g1, b1] = VIRIDIS_STOPS[i + 1];
    if (s >= t0 && s <= t1) {
      const f = (s - t0) / (t1 - t0);
      return [Math.round(r0 + f * (r1 - r0)), Math.round(g0 + f * (g1 - g0)), Math.round(b0 + f * (b1 - b0))];
    }
  }
  return VIRIDIS_STOPS[VIRIDIS_STOPS.length - 1].slice(1);
}
function riskColor(score, verified) {
  const [r, g, b] = viridisRGB(score);
  if (verified === false) {
    const gray = (r + g + b) / 3, mix = 0.55;
    return 'rgb(' + Math.round(r + (gray - r) * mix) + ',' + Math.round(g + (gray - g) * mix) + ',' + Math.round(b + (gray - b) * mix) + ')';
  }
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

function havM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
  const dp = (lat2 - lat1) * Math.PI / 180, dl = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

let animSegments = [];
let animFrameId = null;

// Each segment gets a 0-1 "progress" value based on its position along its OWN
// route, not distance from any external point - every route reveals from its own
// real starting point simultaneously, following the actual path order (like
// watching each ride happen), not a circular bloom from one shared anchor.
function buildAnimSegments(routes) {
  const segs = [];
  routes.forEach(pts => {
    const routeSegs = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      if (a.lat == null || b.lat == null) continue;
      const hasCov = a.imageSource || a.hasCoverage;
      if (!hasCov) continue;
      let color;
      if (a.irap && b.irap) {
        color = riskColor((riskScore(a.irap) + riskScore(b.irap)) / 2, !!(a.imageSource && b.imageSource));
      } else {
        color = a.imageSource ? '#5b6470' : '#3a3d42';
      }
      routeSegs.push({ a, b, color });
    }
    routeSegs.forEach((s, idx) => {
      s.progress = routeSegs.length > 1 ? idx / (routeSegs.length - 1) : 0;
    });
    segs.push(...routeSegs);
  });
  return segs;
}

// Box breathing timing (4-4-4-4, scaled up) - inhale/hold/exhale/hold, all equal,
// mapped onto four real visual states rather than a plain grow/retract oscillation.
// Chosen over 4-7-8 deliberately: that pattern's asymmetry is what makes it feel
// like a small effort, which is the point for active anxiety relief - not what you
// want in something meant to be pleasant to glance at passively for hours.
const PHASE_MS = 4000; // the actual, literal 4-second count used in real box
                        // breathing practice - this can genuinely be followed
                        // along with as an exercise, not just watched ambiently
const GROW_MS = PHASE_MS, HOLD_FULL_MS = PHASE_MS, RETRACT_MS = PHASE_MS, HOLD_EMPTY_MS = PHASE_MS;
const CYCLE_MS = GROW_MS + HOLD_FULL_MS + RETRACT_MS + HOLD_EMPTY_MS;

function runBreathingCycle(routes, fitBoundsOnce) {
  animSegments = buildAnimSegments(routes);
  polylines.forEach(p => p.setMap(null));
  polylines = animSegments.map(s => {
    const poly = new google.maps.Polyline({
      path: [{ lat: s.a.lat, lng: s.a.lon }, { lat: s.b.lat, lng: s.b.lon }],
      strokeColor: s.color, strokeWeight: 4, strokeOpacity: 0.9, map: null
    });
    poly._visible = false; // tracked explicitly so tick() only touches lines whose
                            // state actually changes, not all of them every frame
    return poly;
  });

  if (fitBoundsOnce && animSegments.length) {
    const bounds = new google.maps.LatLngBounds();
    animSegments.forEach(s => {
      bounds.extend({ lat: s.a.lat, lng: s.a.lon });
      bounds.extend({ lat: s.b.lat, lng: s.b.lon });
    });
    map.fitBounds(bounds);
  }

  const cycleStart = performance.now();
  if (animFrameId) cancelAnimationFrame(animFrameId);

  function tick(now) {
    const elapsed = (now - cycleStart) % CYCLE_MS;
    let progressFrac;
    if (elapsed < GROW_MS) {
      progressFrac = elapsed / GROW_MS; // inhale - growing outward
    } else if (elapsed < GROW_MS + HOLD_FULL_MS) {
      progressFrac = 1; // hold - full coverage, held steady
    } else if (elapsed < GROW_MS + HOLD_FULL_MS + RETRACT_MS) {
      progressFrac = 1 - (elapsed - GROW_MS - HOLD_FULL_MS) / RETRACT_MS; // exhale
    } else {
      progressFrac = 0; // hold - empty, held steady
    }

    // Only calls setMap on a line when its visibility genuinely flips - the actual
    // fix for the jank, since the old version touched every single polyline on
    // every single frame regardless of whether anything about it had changed.
    for (let i = 0; i < polylines.length; i++) {
      const shouldShow = animSegments[i].progress <= progressFrac;
      if (shouldShow !== polylines[i]._visible) {
        polylines[i].setMap(shouldShow ? map : null);
        polylines[i]._visible = shouldShow;
      }
    }
    animFrameId = requestAnimationFrame(tick);
  }
  animFrameId = requestAnimationFrame(tick);
}

function drawRoutes(routes, fitBounds) {
  polylines.forEach(p => p.setMap(null));
  polylines = [];
  const bounds = new google.maps.LatLngBounds();
  routes.forEach(pts => {
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      if (a.lat == null || b.lat == null) continue;
      const hasCov = a.imageSource || a.hasCoverage;
      if (!hasCov) continue;
      let color;
      if (a.irap && b.irap) {
        color = riskColor((riskScore(a.irap) + riskScore(b.irap)) / 2, !!(a.imageSource && b.imageSource));
      } else {
        color = a.imageSource ? '#5b6470' : '#3a3d42';
      }
      polylines.push(new google.maps.Polyline({
        path: [{ lat: a.lat, lng: a.lon }, { lat: b.lat, lng: b.lon }],
        strokeColor: color, strokeWeight: 4, strokeOpacity: 0.9, map: map
      }));
      bounds.extend({ lat: a.lat, lng: a.lon });
      bounds.extend({ lat: b.lat, lng: b.lon });
    }
  });
  if (fitBounds && !bounds.isEmpty()) map.fitBounds(bounds);
}

// Polls for fresh data periodically instead of a full page reload - meant to run
// unattended on a screen for hours, so no flash/flicker every refresh cycle.
const POLL_MS = 5 * 60 * 1000;
function pollForUpdates() {
  fetch('/log-data').then(r => r.json()).then(d => {
    if (d.error) return;
    document.getElementById('kmStat').innerHTML = d.total_km.toFixed(1) + '<span>km verified</span>';
    runBreathingCycle(d.routes, false);
  }).catch(() => {});
}

// Analog clock, drawn fresh every second - simple SVG geometry, no external
// dependency, styled to match the same dark panels used elsewhere on this page.
const CLOCK_R = 70, CLOCK_CX = 70, CLOCK_CY = 70;
function handPoint(angleDeg, length) {
  const rad = (angleDeg - 90) * Math.PI / 180;
  return [CLOCK_CX + Math.cos(rad) * length, CLOCK_CY + Math.sin(rad) * length];
}
function drawClock() {
  const now = new Date();
  const h = now.getHours() % 12, m = now.getMinutes(), s = now.getSeconds();
  const hAngle = h * 30 + m * 0.5;
  const mAngle = m * 6 + s * 0.1;
  const sAngle = s * 6;

  let ticks = '';
  for (let i = 0; i < 12; i++) {
    const a = i * 30;
    const [x1, y1] = handPoint(a, CLOCK_R - 10);
    const [x2, y2] = handPoint(a, CLOCK_R - 4);
    ticks += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#9a9da4" stroke-width="2"/>`;
  }
  const [hx, hy] = handPoint(hAngle, 36);
  const [mx, my] = handPoint(mAngle, 52);
  const [sx, sy] = handPoint(sAngle, 58);

  document.getElementById('clockSvg').innerHTML = `
    <circle cx="${CLOCK_CX}" cy="${CLOCK_CY}" r="${CLOCK_R - 3}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="2"/>
    ${ticks}
    <line x1="${CLOCK_CX}" y1="${CLOCK_CY}" x2="${hx}" y2="${hy}" stroke="#e8e8ec" stroke-width="4" stroke-linecap="round"/>
    <line x1="${CLOCK_CX}" y1="${CLOCK_CY}" x2="${mx}" y2="${my}" stroke="#e8e8ec" stroke-width="3" stroke-linecap="round"/>
    <line x1="${CLOCK_CX}" y1="${CLOCK_CY}" x2="${sx}" y2="${sy}" stroke="#5b9dd9" stroke-width="1.5" stroke-linecap="round"/>
    <circle cx="${CLOCK_CX}" cy="${CLOCK_CY}" r="3.5" fill="#e8e8ec"/>
  `;
}

function initMap() {
  map = new google.maps.Map(document.getElementById('map'), {
    zoom: 10, center: { lat: 43.25, lng: -79.87 },
    disableDefaultUI: true, // no zoom/pan controls, gesture handling, etc. - nothing
                             // to click on a TV with no mouse, just an ambient display
    gestureHandling: 'none', keyboardShortcuts: false,
    // Exact same style array as the main app's mapStyles(false).
    styles: [
      { elementType: 'geometry', stylers: [{ color: '#0e1012' }] },
      { elementType: 'labels.text.fill', stylers: [{ color: '#888680' }] },
      { featureType: 'road', elementType: 'geometry', stylers: [{ color: '#1f2124' }] },
      { featureType: 'water', elementType: 'geometry', stylers: [{ color: '#08090a' }] },
      { featureType: 'poi', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative.land_parcel', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative', elementType: 'geometry', stylers: [{ visibility: 'off' }] },
      // Base 'administrative' labels-off catches subtypes not explicitly listed below
      // (e.g. Indian reserves, which Google doesn't file under country/province/
      // locality/neighborhood) - the specific rules stay as belt-and-suspenders.
      { featureType: 'administrative', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative.country', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative.province', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative.locality', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'water', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'road.highway', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
      { featureType: 'road.highway', elementType: 'labels.text', stylers: [{ visibility: 'off' }] },
      { featureType: 'road.arterial', elementType: 'labels.icon', stylers: [{ visibility: 'off' }] },
      { featureType: 'road', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'administrative.neighborhood', elementType: 'labels', stylers: [{ visibility: 'off' }] },
      { featureType: 'transit', stylers: [{ visibility: 'off' }] }
    ]
  });
  runBreathingCycle(ROUTES, true);
  setInterval(pollForUpdates, POLL_MS);
  drawClock();
  setInterval(drawClock, 1000);
}
</script>
<script async src="https://maps.googleapis.com/maps/api/js?key=""" + gkey + """&callback=initMap"></script>
</body></html>"""

def nearest_tower_km(lat, lon):
    if not TOWER_GRID:
        return None
    gk = (round(lat / GRID_SIZE), round(lon / GRID_SIZE))
    best = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for t_lat, t_lon in TOWER_GRID.get((gk[0] + dx, gk[1] + dy), []):
                d = haversine_km(lat, lon, t_lat, t_lon)
                if best is None or d < best:
                    best = d
    return round(best, 2) if best is not None else None

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

def fetch_overpass(query):
    payload = urllib.parse.urlencode({"data": query}).encode()
    last_err = "no mirrors tried"
    for mirror in OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(
                mirror, data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            try:
                json.loads(body)  # confirm it's actually complete, parseable JSON before
                # forwarding it on - a truncated/cut-off response can still start with "{"
                # but fail to parse, which used to slip through and break the browser with
                # a confusing "Unexpected end of JSON input" error instead of a clear one here.
            except json.JSONDecodeError as e:
                last_err = f"{mirror} returned malformed/incomplete JSON: {e}"
                print(f"  {last_err} - trying next mirror")
                continue
            print(f"  Overpass query succeeded via {mirror}")
            return body, None
        except urllib.error.HTTPError as e:
            last_err = f"{mirror} returned HTTP {e.code}"
            print(f"  {last_err} - trying next mirror")
        except Exception as e:
            last_err = f"{mirror} failed: {e}"
            print(f"  {last_err} - trying next mirror")
    return None, last_err

class H(BaseHTTPRequestHandler):
    def _parse_multipart(self, body_bytes, content_type):
        # Direct byte-boundary splitting instead of the email module. The email
        # module builds an internal MIME tree and applies text-oriented transfer-
        # encoding handling even to binary parts - for a request full of JPEG images
        # this can genuinely use many times the raw body size in memory, which is
        # very likely what actually caused the free-tier 512MB OOM at 244 points
        # (the raw request itself was only ~6MB), not the upload concurrency.
        m = re.search(rb'boundary=([^;\r\n]+)', content_type.encode())
        if not m:
            return {}, {}
        boundary = b'--' + m.group(1).strip(b'"')
        fields, files = {}, {}
        for part in body_bytes.split(boundary):
            part = part.strip(b'\r\n')
            if not part or part == b'--':
                continue
            if b'\r\n\r\n' not in part:
                continue
            header_block, content = part.split(b'\r\n\r\n', 1)
            content = content[:-2] if content.endswith(b'\r\n') else content  # trailing CRLF before next boundary
            headers = header_block.decode('utf-8', 'ignore')
            name_m = re.search(r'name="([^"]+)"', headers)
            if not name_m:
                continue
            name = name_m.group(1)
            filename_m = re.search(r'filename="([^"]+)"', headers)
            if filename_m:
                files[name] = content
            else:
                fields[name] = content.decode("utf-8", "ignore")
        return fields, files

    def _snap_points_serverside(self, points, gkey):
        """Same Roads API + chunking-by-100 approach the frontend's /snap
        endpoint uses, just called directly from here since the desktop tool
        doesn't do its own browser-side snapping."""
        out = []
        for i in range(0, len(points), 100):
            chunk = points[i:i + 100]
            path = "|".join(str(p["lat"]) + "," + str(p["lon"]) for p in chunk)
            url = ("https://roads.googleapis.com/v1/snapToRoads?interpolate=true&path="
                   + urllib.parse.quote(path) + "&key=" + gkey)
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as r:
                j = json.loads(r.read())
            for s in j.get("snappedPoints", []):
                out.append({"lat": s["location"]["latitude"], "lon": s["location"]["longitude"]})
        return out

    def _upload_to_storage(self, bucket, path, file_bytes, content_type):
        """Individual image upload to Supabase's actual file storage - not the
        database. Confirmed via real prior reports that this endpoint needs raw
        binary bytes in the request body, not a base64 string (a base64 body here
        produces a corrupted file on Supabase's end). Returns the public URL if the
        bucket is set to public, matching how Street View URLs already work
        elsewhere - a short reference, not embedded image data."""
        url = SUPABASE_URL + "/storage/v1/object/" + bucket + "/" + urllib.parse.quote(path)
        req = urllib.request.Request(
            url, data=file_bytes, method="POST",
            headers={"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
                     "Content-Type": content_type, "x-upsert": "true"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return SUPABASE_URL + "/storage/v1/object/public/" + bucket + "/" + urllib.parse.quote(path)

    def _handle_import_local(self, body, content_type):
        t_start = time.time()
        try:
            print("[import-local] body received: %.2fMB" % (len(body) / (1024 * 1024)))
            fields, files = self._parse_multipart(body, content_type)
            manifest = json.loads(fields.get("manifest", "{}"))
            pts = manifest.get("points", [])
            print("[import-local] parsed: %d points, %d files (%.1fs elapsed)" % (len(pts), len(files), time.time() - t_start))
            if not pts:
                self._json({"error": "No points in manifest"}, code=400)
                return

            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return

            gkey = manifest.get("gkey", "")
            if gkey:
                try:
                    snapped = self._snap_points_serverside(
                        [{"lat": p["lat"], "lon": p["lon"]} for p in pts], gkey
                    )
                    if snapped:
                        # Road-snapped for shape accuracy, but keep the original
                        # per-point images/headings paired by nearest index -
                        # snapping can return a different point count than went in.
                        for i, p in enumerate(pts):
                            src = snapped[min(i, len(snapped) - 1)]
                            p["lat"], p["lon"] = src["lat"], src["lon"]
                except Exception:
                    pass  # snapping is a nice-to-have here - fall back to raw GPS points

            rid = uuid.uuid4().hex[:12]

            # km/order still computed sequentially first (each depends on the point
            # before it), but the actual uploads - the genuinely slow part - run
            # concurrently. Sequential uploads worked fine for 42 points; a 244-point
            # ride doing 244 uploads one at a time was very likely long enough to hit
            # Render's own gateway timeout before the response could even be sent.
            km_running = 0.0
            for i, p in enumerate(pts):
                if i > 0:
                    km_running += haversine_km(pts[i - 1]["lat"], pts[i - 1]["lon"], p["lat"], p["lon"])
                p["_km"] = round(km_running, 4)

            def upload_one(item):
                i, p = item
                img_file = p.get("image_file")
                img_bytes = files.get(img_file) if img_file else None
                if not img_bytes:
                    return i, ""
                try:
                    return i, self._upload_to_storage(
                        "route-images", rid + "/" + ("pt_%04d.jpg" % i), img_bytes, "image/jpeg"
                    )
                except Exception as e:
                    print("[import-local] upload failed for point %d: %s" % (i, e))
                    return i, None  # None = attempted but failed, distinct from "no image"

            print("[import-local] starting %d uploads (%.1fs elapsed)" % (len(pts), time.time() - t_start))
            urls_by_index = {}
            upload_failures = 0
            with ThreadPoolExecutor(max_workers=5) as pool:
                for i, url in pool.map(upload_one, enumerate(pts)):
                    if url is None:
                        upload_failures += 1
                        urls_by_index[i] = ""
                    else:
                        urls_by_index[i] = url

            print("[import-local] uploads done: %d failed of %d (%.1fs elapsed)" % (upload_failures, len(pts), time.time() - t_start))

            res = []
            for i, p in enumerate(pts):
                image_url = urls_by_index.get(i, "")
                res.append({
                    "km": p["_km"], "lat": p["lat"], "lon": p["lon"],
                    "heading": p.get("heading", 0), "imageUrl": image_url,
                    "hasCoverage": bool(image_url), "imageSource": "local-desktop-extract" if image_url else None,
                    "etaSec": p.get("eta_sec", 0) or 0
                })
            km = res[-1]["km"] if res else 0.0

            saved_at = now_iso()
            row = {
                "id": rid, "name": manifest.get("name") or ("Route " + saved_at),
                "saved_at": saved_at, "updated_at": saved_at,
                "meta": {"totalKm": round(km, 2), "points": len(res)},
                "data": {"res": res},
            }
            try:
                print("[import-local] saving route row to database (%.1fs elapsed)" % (time.time() - t_start))
                supabase_request("POST", "routes", body=row, extra_headers={"Prefer": "resolution=merge-duplicates"})
                result = {"id": rid, "name": row["name"], "points": len(res), "km": round(km, 2)}
                if upload_failures:
                    result["warning"] = str(upload_failures) + " image(s) failed to upload - route saved without them"
                print("[import-local] DONE - responding to client (%.1fs total)" % (time.time() - t_start))
                self._json(result)
            except urllib.error.HTTPError as e:
                # str(e) alone only gives the generic "HTTP Error 500: Internal Server
                # Error" - the actual reason Supabase rejected this lives in the
                # response body, which has to be read explicitly to see it.
                body_text = e.read().decode("utf-8", "ignore")
                self._json({"error": "Supabase rejected the save: " + body_text}, code=e.code)
        except Exception as e:
            self._json({"error": "Local import failed: " + str(e)}, code=500)

    def log_message(self, fmt, *args):
        print(" ", args[0], self.path)
    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
    def do_GET(self):
        if self.path == "/gopro-telemetry.bundle.js":
            bundle_path = os.path.join(BASE, "gopro-telemetry.bundle.js")
            if os.path.exists(bundle_path):
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "public, max-age=604800")  # vendored, rarely changes
                self.end_headers()
                with open(bundle_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
            return
        if self.path == "/log-data":
            # Polled periodically by the /log page itself so it can refresh live
            # without a jarring full-page reload - the actual point of "running live
            # on a TV," not something meant to be visited directly.
            if not supabase_configured():
                self._json({"error": "Supabase not configured"}, code=500)
                return
            try:
                total_km, all_pts = fetch_log_data()
                self._json({"total_km": total_km, "routes": all_pts})
            except Exception as e:
                self._json({"error": "Could not load routes: " + str(e)}, code=500)
            return

        if self.path == "/research" or self.path.startswith("/research?"):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            search_q = (qs.get("q", [""])[0] or "").strip()
            search_type = (qs.get("type", [""])[0] or "").strip()
            if search_type not in ("research", "incident"):
                search_type = ""

            feed_items = []
            if supabase_configured():
                try:
                    type_filter = "type=eq." + search_type if search_type else "type=in.(research,incident)"
                    query_parts = [type_filter, "order=found_at.desc", "limit=100"]
                    if search_q:
                        # Strip characters that would break PostgREST's or()/ilike syntax
                        # (commas and parens are the list/grouping delimiters) rather than
                        # attempt full escaping - a small loss of search flexibility for a
                        # guarantee the query string can never come out malformed.
                        safe_q = re.sub(r"[,()]", " ", search_q).strip()
                        if safe_q:
                            pattern = urllib.parse.quote("*" + safe_q + "*")
                            query_parts.append("or=(title.ilike." + pattern + ",summary.ilike." + pattern + ")")
                    feed_items = supabase_request("GET", "research_feed?" + "&".join(query_parts)) or []
                except Exception as e:
                    print("  [research] could not fetch feed:", e)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_research_page(feed_items, search_q=search_q, search_type=search_type).encode("utf-8"))
            return

        if self.path == "/log":
            gkey = os.environ.get("GOOGLE_MAPS_PUBLIC_KEY", "")
            if not gkey:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"GOOGLE_MAPS_PUBLIC_KEY not set on the server - this is a separate, "
                                  b"domain-restricted key for the public log page, not the personal key "
                                  b"used in the main app.")
                return
            if not supabase_configured():
                self.send_response(500)
                self.end_headers()
                return
            try:
                total_km, all_pts = fetch_log_data()
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(("Could not load routes: " + str(e)).encode())
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(render_public_log_page(gkey, total_km, all_pts).encode())
            return

            # A stable, live URL for one saved route's GPX - unlike the client-side
            # download, Scenic's own servers can actually fetch this directly, which is
            # what their documented "import GPX from a provided URL" method requires.
            route_id = self.path[len("/gpx/"):].split("?")[0]
            if not supabase_configured():
                self.send_response(500)
                self.end_headers()
                return
            try:
                rows = supabase_request("GET", "routes?id=eq." + urllib.parse.quote(route_id) + "&select=name,data")
                if not rows:
                    self.send_response(404)
                    self.end_headers()
                    return
                route = rows[0]
                pts = (route.get("data") or {}).get("res", [])
                name = route.get("name") or "UrbRis Route"
                esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<gpx version="1.1" creator="UrbRis" xmlns="http://www.topografix.com/GPX/1/1">',
                    "  <metadata><name>" + esc(name) + "</name></metadata>",
                    "  <trk>", "    <name>" + esc(name) + "</name>", "    <trkseg>",
                ]
                for p in pts:
                    lat, lon = p.get("lat"), p.get("lon")
                    if lat is not None and lon is not None:
                        lines.append('      <trkpt lat="%.6f" lon="%.6f"></trkpt>' % (lat, lon))
                lines += ["    </trkseg>", "  </trk>", "</gpx>"]
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/gpx+xml")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Disposition", 'inline; filename="' + route_id + '.gpx"')
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(500)
                self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        with open(os.path.join(BASE, "urbris.html"), "rb") as f:
            self.wfile.write(f.read())
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))
        if self.path == "/routes/import-local":
            # The local desktop extraction tool uploads a small JSON manifest
            # plus a handful of already-extracted images as a normal multipart
            # form upload (like a plain HTML file input), not JSON - so this
            # has to be intercepted before the json.loads() below, which would
            # otherwise crash trying to parse multipart data as JSON.
            body = self.rfile.read(content_length)
            self._handle_import_local(body, content_type)
            return
        body = self.rfile.read(content_length)
        data = json.loads(body)
        if self.path == "/discover-routes":
            lat = data.get("lat")
            lon = data.get("lon")
            radius_km = data.get("radius_km", 50)
            gkey = data.get("gkey", "")
            if lat is None or lon is None:
                self._json({"error": "lat and lon required"}, code=400)
                return
            try:
                results = discover_windy_routes(float(lat), float(lon), float(radius_km), gkey)
                self._json({"routes": results})
            except urllib.error.HTTPError as e:
                # Same lesson as earlier tonight elsewhere - the status code alone
                # hides the real reason. Overpass's own error text (often explaining
                # exactly what it rejected and why) lives in the response body.
                body_text = ""
                try:
                    body_text = e.read().decode("utf-8", "ignore")[:1000]
                except Exception:
                    pass
                self._json({"error": "Overpass request failed: HTTP %d - %s" % (e.code, body_text or "(no body)")}, code=502)
            except Exception as e:
                self._json({"error": "Discovery failed: " + str(e)}, code=500)
            return
        if self.path == "/cellcoverage":
            points = data.get("points", [])
            distances = [nearest_tower_km(p.get("lat"), p.get("lon")) for p in points]
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"distances": distances}).encode())
            return
        if self.path == "/routes/save":
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            try:
                rid = data.get("id") or uuid.uuid4().hex[:12]
                is_update = bool(data.get("id"))
                saved_at = now_iso()
                existing_name = None
                if is_update:
                    existing = supabase_request("GET", "routes?id=eq." + urllib.parse.quote(rid) + "&select=saved_at,name")
                    if existing:
                        saved_at = existing[0]["saved_at"]
                        existing_name = existing[0]["name"]
                row = {
                    "id": rid,
                    "name": data.get("name") or existing_name or ("Route " + now_iso()),
                    "saved_at": saved_at,
                    "updated_at": now_iso(),
                    "meta": data.get("meta", {}),
                    "data": data.get("data", {}),
                }
                supabase_request("POST", "routes", body=row, extra_headers={"Prefer": "resolution=merge-duplicates"})
                self._json({"id": rid, "name": row["name"], "savedAt": row["saved_at"]})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase save failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Save failed: " + str(e)}, code=500)
            return
        if self.path == "/research/run-scan":
            # Manual trigger for testing - the real scan runs automatically once daily
            # via the poller thread. Runs synchronously and returns what it found, so
            # this can be verified without waiting up to 24h for the automatic run.
            try:
                akey = os.environ.get("ANTHROPIC_API_KEY", "")
                if not akey:
                    self._json({"error": "ANTHROPIC_API_KEY not set on the server"}, code=500)
                    return
                if not supabase_configured():
                    self._json({"error": "Supabase not configured"}, code=500)
                    return
                run_daily_research_scan()
                self._json({"ok": True, "message": "Scan ran - check /research or your email"})
            except Exception as e:
                self._json({"error": "Scan failed: " + str(e)}, code=500)
            return

        if self.path == "/routes/coded-points":
            # Feeds Tier 1 of "safest route" comparison: every already-iRAP-coded point
            # across every saved route, stripped down to just lat/lon/irap (no image
            # data) so the client can spatially match Google's route alternatives
            # against real coded risk without pulling full route payloads.
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            try:
                rows = supabase_request("GET", "routes?select=data") or []
                points = []
                for row in rows:
                    for p in (row.get("data") or {}).get("res") or []:
                        if p.get("irap") and p.get("lat") is not None and p.get("lon") is not None:
                            points.append({"lat": p["lat"], "lon": p["lon"], "irap": p["irap"]})
                self._json({"points": points})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase fetch failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Fetch failed: " + str(e)}, code=500)
            return
        if self.path == "/routes/list":
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            try:
                # Only the light columns here, not the full route data - listing
                # shouldn't pull every point/image for every saved route.
                rows = supabase_request("GET", "routes?select=id,name,saved_at,meta&order=saved_at.desc")
                items = [{"id": r["id"], "name": r["name"], "savedAt": r.get("saved_at"), "meta": r.get("meta") or {}} for r in (rows or [])]
                self._json({"routes": items})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase list failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "List failed: " + str(e)}, code=500)
            return
        if self.path == "/routes/load":
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            rid = data.get("id", "")
            try:
                rows = supabase_request("GET", "routes?id=eq." + urllib.parse.quote(rid) + "&select=*")
                if not rows:
                    self._json({"error": "Route not found"}, code=404)
                    return
                r = rows[0]
                self._json({"id": r["id"], "name": r["name"], "savedAt": r.get("saved_at"), "updatedAt": r.get("updated_at"), "meta": r.get("meta") or {}, "data": r.get("data") or {}})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase load failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Load failed: " + str(e)}, code=500)
            return
        if self.path == "/routes/rename":
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            rid = data.get("id", "")
            try:
                supabase_request("PATCH", "routes?id=eq." + urllib.parse.quote(rid), body={"name": data.get("name", "")})
                self._json({"ok": True})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase rename failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Rename failed: " + str(e)}, code=500)
            return
        if self.path == "/routes/delete":
            if not supabase_configured():
                self._json({"error": "Supabase not configured - set SUPABASE_URL and SUPABASE_KEY on the server"}, code=500)
                return
            rid = data.get("id", "")
            try:
                supabase_request("DELETE", "routes?id=eq." + urllib.parse.quote(rid))
                self._json({"ok": True})
            except urllib.error.HTTPError as e:
                self._json({"error": "Supabase delete failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Delete failed: " + str(e)}, code=500)
            return
        if self.path == "/briefing-audio":
            # Real audio, not browser speechSynthesis - a genuine improvement in voice
            # quality now, and also what unlocks a real downloadable video export later
            # (browser TTS can't be reliably captured into a recording; a real audio
            # file can be). Not building the export pipeline itself tonight - just the
            # audio piece, as its own complete, testable unit.
            openai_key = data.get("openaiKey", "")
            text = data.get("text", "")
            if not openai_key:
                self._json({"error": "No OpenAI API key"}, code=400)
                return
            if not text:
                self._json({"error": "No text to speak"}, code=400)
                return
            try:
                payload = json.dumps({
                    "model": "gpt-4o-mini-tts",
                    "input": text[:6000],  # stay well under the model's ~2000-token input limit
                    "voice": "onyx",
                    "instructions": "Warm, calm, conversational tone - like a knowledgeable riding "
                                     "buddy giving a pre-ride briefing, not a robotic announcement.",
                    "response_format": "mp3"
                }).encode()
                req = urllib.request.Request(
                    "https://api.openai.com/v1/audio/speech", data=payload,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + openai_key}
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    audio_bytes = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(audio_bytes)))
                self.end_headers()
                self.wfile.write(audio_bytes)
            except urllib.error.HTTPError as e:
                self._json({"error": "OpenAI TTS failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "OpenAI TTS failed: " + str(e)}, code=500)
            return

        if self.path == "/briefing-script":
            # Turns real route/weather/risk data into a short, natural narration script
            # for the in-app Route Briefing - Claude's job here is wording and weaving
            # real "why it matters" context together, never inventing a fact. Every flag,
            # number, and why-note handed to it below is real (the flags come from the
            # actual iRAP coding; the why-notes are the same research basis already
            # curated on /research) - Claude is only asked to phrase it naturally, not
            # to originate any of the substance.
            akey = data.get("akey", "")
            if not akey:
                self._json({"error": "No Anthropic API key"}, code=400)
                return
            route_name = data.get("routeName", "This route")
            total_km = data.get("totalKm", 0)
            weather = data.get("weather", [])
            segments = data.get("segments", [])
            for seg in segments:
                seg["whyNotes"] = [BRIEFING_WHY_NOTES[f] for f in seg.get("flags", []) if f in BRIEFING_WHY_NOTES]
            prompt = (
                "You are writing a short spoken pre-ride briefing for a motorcyclist - think "
                "of a knowledgeable riding buddy giving you the rundown before you head out, "
                "not a robotic checklist. Warm, direct, conversational - but every fact must "
                "come from the data below. Never invent road conditions, weather, or risks not "
                "present in this data. When a segment includes 'whyNotes', you may use that "
                "real context to explain why a flag matters, in your own natural phrasing - "
                "but don't state anything as fact that isn't grounded in the given data.\n\n"
                "Route: " + str(route_name) + ", " + str(total_km) + " km total, "
                + str(len(segments)) + " notable segment(s) flagged.\n"
                "Weather along the way: " + json.dumps(weather) + "\n"
                "Flagged segments (already coded from real imagery), in route order: "
                + json.dumps(segments) + "\n\n"
                "Respond with ONLY a JSON object, no other text, no markdown fences: "
                '{"intro": "two to three warm, natural sentences introducing the ride - '
                'distance, general character, how many notable spots to watch for", '
                '"weather": "one or two sentence weather summary, only mention genuinely '
                'notable conditions - if nothing stands out, say so briefly and move on", '
                '"overview": "one or two sentences setting up that we will walk through '
                'the flagged segments in order along the route", '
                '"segments": ["two to four natural spoken sentences per segment, same order '
                'as given - mention the segment\'s flags together naturally, and where '
                'whyNotes exist, weave in why it actually matters, in your own words"]}'
            )
            try:
                payload = json.dumps({
                    "model": "claude-sonnet-5", "max_tokens": 1800,
                    "messages": [{"role": "user", "content": prompt}]
                }).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages", data=payload,
                    headers={"Content-Type": "application/json", "x-api-key": akey, "anthropic-version": "2023-06-01"}
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    result = json.loads(r.read())
                texts = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
                script = extract_json_object("\n".join(texts))
                self._json({"script": script})
            except urllib.error.HTTPError as e:
                self._json({"error": "Briefing script generation failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Briefing script generation failed: " + str(e)}, code=500)
            return

        if self.path == "/route-intersections":
            # Detects real intersections along a route independent of the fixed 100m
            # sampling grid the rest of the pipeline uses - an intersection is a
            # discrete event, not something that needs even spacing. See the shared-
            # coordinate approach in find_route_intersections() for how.
            path = data.get("path", [])
            tolerance_m = data.get("toleranceM", 50)
            if not path or len(path) < 2:
                self._json({"error": "No route path provided"}, code=400)
                return
            try:
                intersections, err = find_route_intersections(path, tolerance_m=tolerance_m)
                if err:
                    self._json({"error": "Overpass request failed: " + str(err)}, code=502)
                    return
                self._json({"intersections": intersections, "count": len(intersections)})
            except Exception as e:
                self._json({"error": "Intersection detection failed: " + str(e)}, code=500)
            return

        if self.path == "/roadsinarea":
            bbox = data.get("bbox", "")  # "south,west,north,east"
            query = (
                '[out:json][timeout:25];'
                'way["highway"~"^(motorway|trunk|primary|secondary|tertiary|'
                'unclassified|residential|track|motorway_link|trunk_link|'
                'primary_link|secondary_link|tertiary_link)$"]'
                '["access"!~"^(no|private)$"](' + bbox + ');'
                'out geom;'
            )
            body_result, err = fetch_overpass(query)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body_result is not None:
                self.wfile.write(body_result)
            else:
                self.wfile.write(json.dumps({
                    "error": "All Overpass mirrors were busy or unreachable (" + str(err) + "). "
                             "This is a shared free service so it happens sometimes - wait a minute "
                             "and try again, or try a smaller area."
                }).encode())
            return
        if self.path == "/batch-submit":
            # Batch mode for Pull Risk: same model, same prompt, same iRAP schema as the
            # live /analyse path - just submitted as one Anthropic Message Batch instead of
            # one live call per point. Half the token cost, results land within a few
            # minutes to 24h. Deliberately NOT a cheaper/weaker model - this feeds a safety
            # index, so live and batch modes must produce comparable coding.
            akey = data.get("akey", "")
            items = data.get("items", [])
            prompt = data.get("prompt", "")
            model = data.get("model", "claude-opus-4-8")
            if not akey:
                self._json({"error": "No Anthropic API key"}, code=400)
                return
            if not items:
                self._json({"error": "No points to submit"}, code=400)
                return

            def fetch_img(item):
                try:
                    if item.get("imageBase64"):
                        return item["idx"], item["imageBase64"], item.get("mediaType", "image/jpeg"), None
                    img_req = urllib.request.Request(item["imageUrl"])
                    with urllib.request.urlopen(img_req, timeout=20) as img_resp:
                        img_data = img_resp.read()
                        content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    return item["idx"], base64.b64encode(img_data).decode("utf-8"), content_type, None
                except Exception as e:
                    return item["idx"], None, None, str(e)

            with ThreadPoolExecutor(max_workers=10) as pool:
                fetched = list(pool.map(fetch_img, items))

            fetch_errors = {idx: err for idx, b64, ct, err in fetched if err}
            requests_list = [
                {
                    "custom_id": str(idx),
                    "params": {
                        "model": model, "max_tokens": 800,
                        "messages": [{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}},
                            {"type": "text", "text": prompt}
                        ]}]
                    }
                }
                for idx, b64, ct, err in fetched if not err
            ]
            if not requests_list:
                self._json({"error": "All images failed to fetch: " + json.dumps(fetch_errors)}, code=502)
                return
            try:
                payload = json.dumps({"requests": requests_list}).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages/batches", data=payload,
                    headers={"Content-Type": "application/json",
                             "x-api-key": akey,
                             "anthropic-version": "2023-06-01"}
                )
                with urllib.request.urlopen(req) as r:
                    batch = json.loads(r.read())
                self._json({
                    "batchId": batch.get("id"), "processingStatus": batch.get("processing_status"),
                    "submitted": len(requests_list), "failed": len(fetch_errors),
                    "fetchErrors": fetch_errors
                })
            except urllib.error.HTTPError as e:
                self._json({"error": "Batch submit failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Batch submit failed: " + str(e)}, code=500)
            return

        if self.path == "/batch-status":
            akey = data.get("akey", "")
            batch_id = data.get("batchId", "")
            if not akey or not batch_id:
                self._json({"error": "Missing akey or batchId"}, code=400)
                return
            try:
                batch = anthropic_batch_status(batch_id, akey)
                self._json({
                    "processingStatus": batch.get("processing_status"),
                    "requestCounts": batch.get("request_counts"),
                    "resultsUrl": batch.get("results_url"),
                    "endedAt": batch.get("ended_at"),
                    "expiresAt": batch.get("expires_at")
                })
            except urllib.error.HTTPError as e:
                self._json({"error": "Batch status check failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Batch status check failed: " + str(e)}, code=500)
            return

        if self.path == "/batch-results":
            akey = data.get("akey", "")
            batch_id = data.get("batchId", "")
            if not akey or not batch_id:
                self._json({"error": "Missing akey or batchId"}, code=400)
                return
            try:
                batch = anthropic_batch_status(batch_id, akey)
                if batch.get("processing_status") != "ended" or not batch.get("results_url"):
                    self._json({"error": "Batch not ready yet", "processingStatus": batch.get("processing_status")}, code=409)
                    return
                results = anthropic_batch_results(batch, akey)
                self._json({"results": results, "requestCounts": batch.get("request_counts")})
            except urllib.error.HTTPError as e:
                self._json({"error": "Fetching batch results failed: " + e.read().decode("utf-8", "ignore")}, code=e.code)
            except Exception as e:
                self._json({"error": "Fetching batch results failed: " + str(e)}, code=500)
            return

        try:
            if self.path == "/analyse":
                akey = data.get("akey", "")
                if not akey:
                    raise ValueError("No Anthropic API key")
                if data.get("imageBase64"):
                    # Drone photo (or any locally-supplied image) sent straight from
                    # the browser - no fetch needed, unlike the Street View path below.
                    img_b64 = data["imageBase64"]
                    content_type = data.get("mediaType", "image/jpeg")
                else:
                    # Fetch the Street View image and convert to base64
                    # (Anthropic cannot access Google Maps URLs directly)
                    img_url = data["imageUrl"]
                    img_req = urllib.request.Request(img_url)
                    with urllib.request.urlopen(img_req) as img_resp:
                        img_data = img_resp.read()
                        content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                    img_b64 = base64.b64encode(img_data).decode("utf-8")
                payload = json.dumps({
                    "model": "claude-opus-4-8", "max_tokens": 800,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": img_b64}},
                        {"type": "text", "text": data["prompt"]}
                    ]}]
                }).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages", data=payload,
                    headers={"Content-Type": "application/json",
                             "x-api-key": akey,
                             "anthropic-version": "2023-06-01"}
                )
            elif self.path == "/snap":
                gkey = data.get("gkey", "")
                path = data.get("path", "")
                interpolate = "true" if data.get("interpolate") else "false"
                url = ("https://roads.googleapis.com/v1/snapToRoads"
                       "?interpolate=" + interpolate + "&path="
                       + urllib.parse.quote(path) + "&key=" + gkey)
                req = urllib.request.Request(url)
            elif self.path == "/speedlimits":
                gkey = data.get("gkey", "")
                place_ids = data.get("placeIds", [])
                params = "&".join("placeId=" + urllib.parse.quote(str(pid)) for pid in place_ids)
                url = "https://roads.googleapis.com/v1/speedLimits?" + params + "&units=KPH&key=" + gkey
                req = urllib.request.Request(url)
            elif self.path == "/elevation":
                gkey = data.get("gkey", "")
                locations = data.get("locations", [])
                locs_str = "|".join(locations)
                url = "https://maps.googleapis.com/maps/api/elevation/json?locations=" + urllib.parse.quote(locs_str) + "&key=" + gkey
                req = urllib.request.Request(url)
            elif self.path == "/streetviewdates":
                # Street View's Metadata endpoint - separate from the image itself, and
                # genuinely free (no quota consumed). Only accepts one location per
                # request, unlike Elevation's batching, so points are fetched in
                # parallel here rather than one at a time, to keep this reasonably fast
                # for a route with many points.
                gkey = data.get("gkey", "")
                locations = data.get("locations", [])
                if not gkey or not locations:
                    self._json({"dates": []})
                    return

                def fetch_one(loc):
                    try:
                        url = "https://maps.googleapis.com/maps/api/streetview/metadata?location=" + urllib.parse.quote(loc) + "&key=" + gkey
                        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as r:
                            j = json.loads(r.read())
                        return j.get("date")  # e.g. "2018-10", or None if unavailable
                    except Exception:
                        return None

                try:
                    with ThreadPoolExecutor(max_workers=10) as pool:
                        dates = list(pool.map(fetch_one, locations))
                    self._json({"dates": dates})
                except Exception as e:
                    self._json({"error": "Street View date lookup failed: " + str(e)}, code=502)
                return
            elif self.path == "/roadconditions511":
                # Ontario 511's own official events feed (MTO-sourced construction,
                # closures, incidents) - confirmed genuinely keyless and open. Unlike
                # Google's crowd-inferred traffic, this doesn't need other vehicles
                # present to generate a signal, so it doesn't inherit Google's specific
                # weakness on quiet rural roads. Throttled by 511 to 10 calls/60s.
                # Handled fully here (not via the shared fall-through below) because the
                # first version returned an empty body - Python's default User-Agent
                # ("Python-urllib/x.x") looks script-like enough that some servers,
                # including government ones, silently reject or empty-respond to it
                # rather than returning a normal error status.
                try:
                    r511 = urllib.request.Request(
                        "https://511on.ca/api/v2/get/event?format=json",
                        headers={"User-Agent": "Mozilla/5.0 (compatible; UrbRis/1.0; +https://urbris.com)",
                                 "Accept": "application/json"}
                    )
                    with urllib.request.urlopen(r511, timeout=15) as resp511:
                        body511 = resp511.read()
                    if not body511:
                        self._json({"error": "511 returned an empty response - their service may be down or throttling this request"}, code=502)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body511)
                except urllib.error.HTTPError as e:
                    self._json({"error": "511 request failed: HTTP " + str(e.code)}, code=e.code)
                except Exception as e:
                    self._json({"error": "511 request failed: " + str(e)}, code=502)
                return
            elif self.path == "/chat":
                akey = data.get("akey", "")
                if not akey:
                    raise ValueError("No Anthropic API key")
                question = data.get("question", "")
                context = data.get("context", "")
                prompt = ("You are analyzing a rider's saved route database from UrbRis, a "
                           "road-risk verification app for motorcyclists. Here is the current "
                           "data (route names, distances, risk scores, verification status):\n\n"
                           + context + "\n\nQuestion: " + question +
                           "\n\nAnswer concisely and specifically, referencing actual route "
                           "names from the data above. If the data doesn't contain enough to "
                           "answer, say so plainly rather than guessing.")
                payload = json.dumps({
                    "model": "claude-opus-4-8", "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}]
                }).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages", data=payload,
                    headers={"Content-Type": "application/json",
                             "x-api-key": akey,
                             "anthropic-version": "2023-06-01"}
                )
            else:
                self.send_response(404)
                self.end_headers()
                return
            with urllib.request.urlopen(req) as r:
                result = r.read()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(result)
        except urllib.error.HTTPError as e:
            body = e.read()
            print("  Anthropic error", e.code, body[:200])
            self.send_response(e.code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

def anthropic_batch_status(batch_id, akey):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages/batches/" + urllib.parse.quote(batch_id),
        headers={"x-api-key": akey, "anthropic-version": "2023-06-01"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def anthropic_batch_results(batch, akey):
    # batch is the dict already fetched by anthropic_batch_status - caller checks
    # processing_status == "ended" and results_url is set before calling this.
    results_req = urllib.request.Request(
        batch["results_url"],
        headers={"x-api-key": akey, "anthropic-version": "2023-06-01"}
    )
    with urllib.request.urlopen(results_req) as r:
        raw_lines = r.read().decode("utf-8").strip().split("\n")
    results = {}
    for line in raw_lines:
        if not line.strip():
            continue
        row = json.loads(line)
        cid = row.get("custom_id")
        result = row.get("result", {})
        if result.get("type") == "succeeded":
            msg = result.get("message", {})
            text = ""
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    break
            clean = re.sub(r"```json|```", "", text).strip()
            try:
                results[cid] = {"irap": json.loads(clean)}
            except Exception:
                results[cid] = {"error": "Could not parse iRAP JSON: " + clean[:150]}
        else:
            err = result.get("error", {})
            results[cid] = {"error": err.get("message") or result.get("type") or "Batch item failed"}
    return results

def _twilio_send(sid, token, from_num, to_num, body, label):
    payload = urllib.parse.urlencode({"From": from_num, "To": to_num, "Body": body}).encode()
    req = urllib.request.Request(
        "https://api.twilio.com/2010-04-01/Accounts/" + sid + "/Messages.json",
        data=payload, method="POST"
    )
    auth = base64.b64encode((sid + ":" + token).encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
        print("  [batch-poller] " + label + " notification sent")
    except urllib.error.HTTPError as e:
        print("  [batch-poller] Twilio " + label + " send failed:", e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        print("  [batch-poller] Twilio " + label + " send failed:", e)

def send_notifications(body):
    # Sends via whichever channel(s) have their env vars set - SMS and WhatsApp
    # can both be configured at once, or just one, or neither (silent no-op).
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not (sid and token):
        print("  [batch-poller] TWILIO_ACCOUNT_SID/AUTH_TOKEN not set - skipping notifications")
        return
    sms_from = os.environ.get("TWILIO_SMS_FROM", "")   # e.g. "+15557654321" (a Twilio number)
    sms_to = os.environ.get("TWILIO_SMS_TO", "")        # e.g. "+15551234567" (your phone)
    if sms_from and sms_to:
        _twilio_send(sid, token, sms_from, sms_to, body, "SMS")
    wa_from = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. "whatsapp:+14155238886"
    wa_to = os.environ.get("TWILIO_WHATSAPP_TO", "")      # e.g. "whatsapp:+15551234567"
    if wa_from and wa_to:
        _twilio_send(sid, token, wa_from, wa_to, body, "WhatsApp")
    if not (sms_from and sms_to) and not (wa_from and wa_to):
        print("  [batch-poller] no TWILIO_SMS_* or TWILIO_WHATSAPP_* pair set - skipping notifications")

def poll_pending_batches_once():
    if not supabase_configured():
        return
    akey = os.environ.get("ANTHROPIC_API_KEY", "")
    if not akey:
        return  # background notifications are opt-in via this env var - silently no-op without it
    try:
        rows = supabase_request("GET", "routes?select=id,name,data") or []
    except Exception as e:
        print("  [batch-poller] could not list routes:", e)
        return
    for row in rows:
        rdata = row.get("data") or {}
        pb = rdata.get("pendingBatch")
        if not pb or not pb.get("id"):
            continue
        try:
            batch = anthropic_batch_status(pb["id"], akey)
            if batch.get("processing_status") != "ended" or not batch.get("results_url"):
                continue
            results = anthropic_batch_results(batch, akey)
            res_list = rdata.get("res") or []
            coded, failed = 0, 0
            for cid, entry in results.items():
                try:
                    idx = int(cid)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(res_list) and entry.get("irap"):
                    res_list[idx]["irap"] = entry["irap"]
                    coded += 1
                else:
                    failed += 1
            rdata["res"] = res_list
            rdata["pendingBatch"] = None
            supabase_request(
                "PATCH", "routes?id=eq." + urllib.parse.quote(row["id"]),
                body={"data": rdata, "updated_at": now_iso()}
            )
            name = row.get("name") or row["id"]
            print(f"  [batch-poller] applied batch results for '{name}': {coded} coded, {failed} failed")
            send_notifications(
                "UrbRis: risk analysis batch done for \"" + name + "\" - "
                + str(coded) + "/" + str(coded + failed) + " points coded. Open the app to review."
            )
        except Exception as e:
            print("  [batch-poller] error processing route", row.get("id"), ":", e)

def poll_pending_batches_loop():
    while True:
        time.sleep(180)  # 3 minutes - batches rarely finish faster than this anyway
        try:
            poll_pending_batches_once()
        except Exception as e:
            print("  [batch-poller] loop error:", e)
        try:
            if should_run_daily_research_scan():
                run_daily_research_scan()
        except Exception as e:
            print("  [research-scan] loop error:", e)

if __name__ == "__main__":
    load_towers()
    threading.Thread(target=poll_pending_batches_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "localhost"
    print(f"\n  Urbris running at http://{host}:{port}")
    if host == "localhost":
        print("  Opening browser...")
        webbrowser.open(f"http://{host}:{port}")
    print("  Ctrl+C to stop\n")
    HTTPServer((host, port), H).serve_forever()
