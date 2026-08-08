#!/usr/bin/env python3
import json, urllib.request, urllib.error, urllib.parse, webbrowser, os, sys, csv, math, uuid, base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

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
        if self.path.startswith("/gpx/"):
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
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.loads(body)
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
