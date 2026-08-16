#!/usr/bin/env python3
import json, urllib.request, urllib.error, urllib.parse, webbrowser, os, sys, csv, math, uuid, base64, re, time
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

if __name__ == "__main__":
    load_towers()
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "localhost"
    print(f"\n  Urbris running at http://{host}:{port}")
    if host == "localhost":
        print("  Opening browser...")
        webbrowser.open(f"http://{host}:{port}")
    print("  Ctrl+C to stop\n")
    HTTPServer((host, port), H).serve_forever()
