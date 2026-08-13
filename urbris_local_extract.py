#!/usr/bin/env python3
"""
UrbRis local extraction tool.

Runs entirely on your own machine - the original video never leaves your
computer. Pulls GPS telemetry, samples it to roughly one point every 100m,
extracts a real frame from the video at each of those points, and uploads
just that small result set (images + coordinates) to Urbris.

This exists specifically for large files that choke a browser tab: the
extraction and frame-grabbing happen with your laptop's own CPU/disk, not
inside a Chrome tab's memory limits - the same reason ffmpeg can seek an
8GB file in seconds where a <video> element in a browser struggles.

Requirements (install once):
    pip install telemetry-parser requests --break-system-packages
    ffmpeg must be installed and on your PATH (ffmpeg.org/download.html)

Usage:
    python3 urbris_local_extract.py my_ride.mp4 --url https://urbris.com --name "Ride 2026-08-12"

What it does NOT do: any route-shape editing, road-snapping, or risk
scoring - that all still happens on the Urbris side, same as any other
import. This tool's only job is turning "one huge video" into "a small,
uploadable set of GPS-tagged photos."
"""
import argparse
import ast
import json
import math
import os
import struct
import subprocess
import sys
import tempfile

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def extract_gps_points(video_path):
    """GPMF's telemetry_parser library returns the GPS9 stream still in its raw,
    undecoded binary form (confirmed directly against a real GoPro file) - not
    parsed lat/lon values. This decodes that raw KLV payload directly: an 8-byte
    header (FourCC + type + structsize + repeat count) followed by `repeat` fixed-
    size samples, each 7 signed int32 values + 2 unsigned int16 values (per GPS9's
    own documented field order: lat, lon, alt, 2D speed, 3D speed, days, secs, DOP,
    fix), scaled down using the Scale array the stream itself provides."""
    try:
        import telemetry_parser
    except ImportError:
        sys.exit("Missing dependency - run: pip install telemetry-parser --break-system-packages")

    parser = telemetry_parser.Parser(video_path)
    raw = parser.telemetry(human_readable=True)
    blocks = raw if isinstance(raw, list) else [raw]

    points = []
    gps_quality_samples_seen = []
    gps_key_found = False
    raw_sample_for_debug = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if "GPS" not in str(key).upper() or not isinstance(val, dict):
                continue
            gps_key_found = True
            if raw_sample_for_debug is None:
                raw_sample_for_debug = val

            # The raw binary payload sits under a key like '0x47505339' (the FourCC's
            # hex bytes) - find it rather than assume the exact hex string, and pull
            # the actual bytes out of its "NNN bytes: xx xx xx ..." text format.
            payload_bytes = None
            for k, v in val.items():
                if isinstance(k, str) and k.startswith("0x") and isinstance(v, str) and ":" in v:
                    hex_part = v.split(":", 1)[1].strip()
                    try:
                        payload_bytes = bytes.fromhex(hex_part.replace(" ", ""))
                    except ValueError:
                        continue
                    break
            if not payload_bytes or len(payload_bytes) < 8:
                continue

            try:
                structsize = payload_bytes[5]
                repeat = struct.unpack(">H", payload_bytes[6:8])[0]
                scale = ast.literal_eval(val.get("Scale", "[]")) if isinstance(val.get("Scale"), str) else (val.get("Scale") or [])
                if not scale:
                    continue
                n_int32 = 7  # per GPS9's documented layout: lat,lon,alt,2D,3D,days,secs as int32
                n_uint16 = 2  # DOP, fix as uint16
                fmt = ">" + "i" * n_int32 + "H" * n_uint16
                sample_size = struct.calcsize(fmt)
                offset = 8
                rejected_quality = 0
                for i in range(repeat):
                    chunk = payload_bytes[offset:offset + sample_size]
                    if len(chunk) < sample_size:
                        break
                    vals = struct.unpack(fmt, chunk)
                    decoded = [v / s for v, s in zip(vals, scale)]
                    lat, lon, dop, fix = decoded[0], decoded[1], decoded[7], decoded[8]
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        offset += sample_size
                        continue
                    if fix < 2 or dop > 10:
                        rejected_quality += 1
                        if len(gps_quality_samples_seen) < 30:
                            gps_quality_samples_seen.append((round(dop, 2), fix))
                        offset += sample_size
                        continue
                    # decoded[5] = days, decoded[6] = secs-of-day - combining both
                    # matters if a ride spans a midnight rollover in GPS time, where
                    # secs-of-day alone would reset partway through and silently
                    # corrupt every timestamp (and therefore every point-to-frame
                    # pairing) after that moment.
                    total_secs = decoded[5] * 86400 + decoded[6]
                    points.append({"lat": lat, "lon": lon, "t": total_secs})
                    offset += sample_size
            except (struct.error, IndexError, SyntaxError):
                continue

    if not points:
        detail = "No GPS-matching key found at all in this file's telemetry."
        if gps_key_found:
            detail = (
                "A GPS stream was found but couldn't be decoded with this format. "
                "Raw structure, for debugging:\n" + repr(raw_sample_for_debug)[:2000]
            )
        sys.exit(detail)

    if gps_quality_samples_seen:
        print(f"NOTE: some GPS samples were rejected by the quality filter (fix<2 or DOP>10).")
        print(f"Real (DOP, fix) values actually seen on REJECTED samples, for calibration: {gps_quality_samples_seen}")
    return points

def thin_to_spacing(points, spacing_m=100):
    if not points:
        return []
    thinned = [points[0]]
    for p in points[1:]:
        last = thinned[-1]
        if haversine_m(last["lat"], last["lon"], p["lat"], p["lon"]) >= spacing_m:
            thinned.append(p)
    return thinned

def extract_frame(video_path, time_offset_sec, out_path):
    """A direct ffmpeg seek - this is the whole reason this runs locally
    instead of in a browser: ffmpeg seeks a multi-gigabyte file by jumping to
    the nearest keyframe on disk, not by loading the file into memory.
    Scaled down to roughly the same size Urbris already uses for Street View
    images - native GoPro resolution (often 4K) produces frames far larger
    than a card thumbnail needs, and was the actual cause of a 36MB upload
    timing out server-side on a single write."""
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(time_offset_sec), "-i", video_path,
         "-frames:v", "1", "-vf", "scale=800:-1", "-q:v", "6", out_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
    )

def upload_folder(folder, url):
    """Uploads a previously-saved manifest.json + images folder - the offline half
    of the pipeline (capture and process, no signal needed) and this, the online
    half (upload whenever connectivity actually returns), are now fully separate
    steps instead of one all-or-nothing run."""
    manifest_path = os.path.join(folder, "manifest.json")
    if not os.path.exists(manifest_path):
        sys.exit(f"No manifest.json found in {folder} - is this a folder saved with --out?")
    with open(manifest_path) as f:
        manifest = json.load(f)

    print(f"Uploading manifest + images from {folder} to {url} ...")
    try:
        import requests
    except ImportError:
        sys.exit("Missing dependency for upload - run: pip install requests --break-system-packages")

    files = {}
    for p in manifest.get("points", []):
        if p.get("image_file"):
            img_path = os.path.join(folder, p["image_file"])
            if os.path.exists(img_path):
                files[p["image_file"]] = open(img_path, "rb")
    try:
        resp = requests.post(
            url.rstrip("/") + "/routes/import-local",
            data={"manifest": json.dumps(manifest)},
            files=files,
            timeout=900
        )
    finally:
        for f in files.values():
            f.close()

    if resp.status_code == 200:
        print("Uploaded successfully:", resp.json())
    else:
        print(f"Upload failed (HTTP {resp.status_code}): {resp.text[:500]}")

def main():
    ap = argparse.ArgumentParser(description="Extract a route + verification photos from a GoPro video, locally.")
    ap.add_argument("video", nargs="?", default=None, help="Path to the GoPro/DJI video file")
    ap.add_argument("--spacing", type=float, default=100, help="Meters between sampled points (default 100)")
    ap.add_argument("--url", default="http://localhost:8000", help="Urbris server URL, e.g. https://urbris.com")
    ap.add_argument("--name", default=None, help="Route name (defaults to the video filename)")
    ap.add_argument("--out", default=None, help="Save the manifest+images locally instead of/as well as uploading")
    ap.add_argument("--upload-folder", default=None,
                     help="Skip extraction entirely and upload a folder previously saved with --out - "
                          "for uploading later once you actually have signal again")
    args = ap.parse_args()

    if args.upload_folder:
        upload_folder(args.upload_folder, args.url)
        return

    if not args.video:
        sys.exit("A video file is required unless using --upload-folder.")
    if not os.path.exists(args.video):
        sys.exit(f"File not found: {args.video}")

    print(f"Reading telemetry from {args.video} (this reads the file directly, no full load into memory)...")
    points = extract_gps_points(args.video)
    print(f"Found {len(points)} raw GPS samples.")

    thinned = thin_to_spacing(points, args.spacing)
    print(f"Thinned to {len(thinned)} points at ~{args.spacing:.0f}m spacing.")

    with tempfile.TemporaryDirectory() as tmpdir:
        route_points = []
        t0 = thinned[0].get("t") or 0
        for i, p in enumerate(thinned):
            heading = 0
            if i < len(thinned) - 1:
                heading = bearing_deg(p["lat"], p["lon"], thinned[i + 1]["lat"], thinned[i + 1]["lon"])
            elif i > 0:
                heading = bearing_deg(thinned[i - 1]["lat"], thinned[i - 1]["lon"], p["lat"], p["lon"])

            t = p.get("t") or 0
            offset_sec = max(0, (t - t0) if isinstance(t, (int, float)) and t > 1000 else i)
            img_path = os.path.join(tmpdir, f"pt_{i:04d}.jpg")
            try:
                extract_frame(args.video, offset_sec, img_path)
                has_image = os.path.exists(img_path)
            except subprocess.CalledProcessError:
                has_image = False

            route_points.append({
                "lat": p["lat"], "lon": p["lon"], "heading": round(heading, 1),
                "image_file": f"pt_{i:04d}.jpg" if has_image else None,
                "eta_sec": round(offset_sec, 1)
            })
            print(f"  point {i+1}/{len(thinned)} - frame {'ok' if has_image else 'MISSING'}", end="\r")
        print()

        manifest = {
            "name": args.name or os.path.splitext(os.path.basename(args.video))[0],
            "source": "local-desktop-extract",
            "points": route_points
        }

        if args.out:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=2)
            for p in route_points:
                if p["image_file"]:
                    src = os.path.join(tmpdir, p["image_file"])
                    dst = os.path.join(args.out, p["image_file"])
                    with open(src, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
            print(f"Saved manifest + {len(route_points)} images to {args.out}")
            print(f"Upload not attempted (--out was given) - once you have signal, run:")
            print(f"  python urbris_local_extract.py --upload-folder {args.out} --url {args.url}")
            return

        # upload_folder() reads manifest.json from disk, same as a --out folder would
        # have - writing it here too keeps both code paths using the exact same logic
        # instead of two versions that could quietly drift apart.
        with open(os.path.join(tmpdir, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        upload_folder(tmpdir, args.url)

if __name__ == "__main__":
    main()
