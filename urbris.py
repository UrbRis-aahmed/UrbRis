#!/usr/bin/env python3
import json, urllib.request, urllib.error, urllib.parse, webbrowser, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(" ", args[0], self.path)
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
        try:
            if self.path == "/analyse":
                akey = data.get("akey", "")
                if not akey:
                    raise ValueError("No Anthropic API key")
                # Fetch the Street View image and convert to base64
                # (Anthropic cannot access Google Maps URLs directly)
                img_url = data["imageUrl"]
                img_req = urllib.request.Request(img_url)
                with urllib.request.urlopen(img_req) as img_resp:
                    img_data = img_resp.read()
                    content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                import base64
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
                url = ("https://roads.googleapis.com/v1/snapToRoads"
                       "?interpolate=false&path="
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
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if os.environ.get("PORT") else "localhost"
    print(f"\n  Urbris running at http://{host}:{port}")
    if host == "localhost":
        print("  Opening browser...")
        webbrowser.open(f"http://{host}:{port}")
    print("  Ctrl+C to stop\n")
    HTTPServer((host, port), H).serve_forever()
