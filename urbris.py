#!/usr/bin/env python3
import json, urllib.request, urllib.error, urllib.parse, webbrowser, os, sys, csv, math, uuid, threading, base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
CELL_CSV = os.path.join(BASE, "cell_towers.csv")
GRID_SIZE = 0.1  # degrees, roughly 11km - buckets towers for fast nearest-neighbor lookup
TOWER_GRID = {}

# ---- Saved-routes storage --------------------------------------------------
# Deliberately isolated behind these three functions. Right now it's a JSON
# file next to the script, which is fine for testing but does NOT survive a
# Render free-tier restart/redeploy (ephemeral filesystem). If/when this
# needs to actually persist, swap these three functions for calls to
# Supabase (or another DB) - nothing else in this file needs to change.
ROUTES_FILE = os.path.join(BASE, "saved_routes.json")
_routes_lock = threading.Lock()

def load_routes_db():
    if not os.path.exists(ROUTES_FILE):
        return {}
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_routes_db(db):
    with _routes_lock:
        tmp = ROUTES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f)
        os.replace(tmp, ROUTES_FILE)

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
            stripped = body.lstrip()
            if stripped.startswith(b"{") or stripped.startswith(b"["):
                print(f"  Overpass query succeeded via {mirror}")
                return body, None
            last_err = f"{mirror} returned a non-JSON response"
            print(f"  {last_err} - trying next mirror")
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
            db = load_routes_db()
            rid = data.get("id") or uuid.uuid4().hex[:12]
            existing = db.get(rid, {})
            entry = {
                "id": rid,
                "name": data.get("name") or ("Route " + now_iso()),
                "savedAt": existing.get("savedAt") or now_iso(),
                "updatedAt": now_iso(),
                "meta": data.get("meta", {}),
                "data": data.get("data", {}),
            }
            db[rid] = entry
            save_routes_db(db)
            self._json({"id": rid, "name": entry["name"], "savedAt": entry["savedAt"]})
            return
        if self.path == "/routes/list":
            db = load_routes_db()
            items = [
                {"id": v["id"], "name": v["name"], "savedAt": v.get("savedAt"), "meta": v.get("meta", {})}
                for v in db.values()
            ]
            items.sort(key=lambda x: x.get("savedAt") or "", reverse=True)
            self._json({"routes": items})
            return
        if self.path == "/routes/load":
            db = load_routes_db()
            entry = db.get(data.get("id", ""))
            if not entry:
                self._json({"error": "Route not found - it may have been lost in a server restart"}, code=404)
                return
            self._json(entry)
            return
        if self.path == "/routes/rename":
            db = load_routes_db()
            rid = data.get("id", "")
            if rid in db:
                db[rid]["name"] = data.get("name", db[rid]["name"])
                save_routes_db(db)
                self._json({"ok": True})
            else:
                self._json({"error": "Route not found"}, code=404)
            return
        if self.path == "/routes/delete":
            db = load_routes_db()
            rid = data.get("id", "")
            if rid in db:
                del db[rid]
                save_routes_db(db)
            self._json({"ok": True})
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
                    "model": "claude-opus-4-6", "max_tokens": 800,
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
