#!/usr/bin/env python3
"""
cam_pairs.py — one shot: snapshot every configured camera at 4K, detect
AprilTag (tag36h11) markers, and print, for each pair of cameras, the tag IDs
seen by BOTH cameras of the pair together with the tag centre on each camera.

Config-driven and self-contained. Camera names / IPs / credentials come from a
config file (config.yaml); tag detection is inline (no other project files
needed). It always captures the main 4K stream.

TWO MODES
    python cam_pairs.py            # QUIET: prints ONLY the JSON, nothing else
    python cam_pairs.py --log      # + progress and full error detail on stderr
If no JSON comes out, something failed — rerun with --log to see why.

OTHER FLAGS
    --keep-images   keep the captured JPEGs in ./shots (default: deleted)
    --pretty        indent the JSON
    --out FILE      write the JSON to FILE instead of stdout
    --config FILE   config path (default: config.yaml next to this script)
    --cameras a,b   restrict to a subset of the configured camera names

OUTPUT (stdout, always pure JSON)
    {
      "pairs": [
        {"cameras": ["0015", "0016"],
         "tags": [
           {"id": 2, "placement": "floor",
            "0015": {"x": 863.9, "y": 1881.1},
            "0016": {"x": 1710.2, "y": 640.5}}
         ]},
        ... one entry per camera pair ...
      ]
    }
A tag appears in a pair only if BOTH cameras detected that id. "placement" is
"floor" for even ids and "elevated" for odd ids. On failure the output is a
single JSON object {"error": "..."} instead.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

import numpy as np
import cv2

try:
    from pupil_apriltags import Detector as _AT3Detector
    _HAS_AT3 = True
except Exception:  # pragma: no cover
    _HAS_AT3 = False


# =========================================================================== #
#  Logging — QUIET by default. Progress goes to stderr only with --log; stdout
#  is always reserved for pure JSON.
# =========================================================================== #
_LOG = False


def log(msg):
    if _LOG:
        print(msg, file=sys.stderr)
        sys.stderr.flush()


class ConfigError(Exception):
    """Raised for anything wrong with the config / reachability."""


# =========================================================================== #
#  Config — everything deployment-specific lives here, nothing is hardcoded.
# =========================================================================== #
SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
MAIN_CHANNEL = "101"        # always the 4K main stream (sub/preview not exposed)


def load_config(path):
    """Read config.yaml, expand ${ENV_VAR} in every string value."""
    try:
        import yaml
    except ImportError:
        raise ConfigError("PyYAML is not installed — run ./setup.sh "
                          "(pip install -r requirements.txt)")
    if not os.path.exists(path):
        raise ConfigError(f"config not found: {path} "
                          f"(copy config.example.yaml -> config.yaml and edit it)")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(v)
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v

    cfg = expand(raw)

    cams = cfg.get("cameras") or {}
    if not isinstance(cams, dict) or not cams:
        raise ConfigError("config.cameras is empty — add at least one 'name: ip'")
    cam_ip = {str(name): str(ip) for name, ip in cams.items()}

    creds = cfg.get("credentials") or {}
    user = str(creds.get("user", "")).strip()
    password = str(creds.get("password", ""))
    if not user or not password or password.startswith("${"):
        raise ConfigError("config.credentials.user/password missing "
                          "(is the CAM_PASSWORD env var set?)")

    vpn = cfg.get("vpn") or {}
    return {
        "cam_ip": cam_ip,
        "cred": f"{user}:{password}",
        "vpn_auto": bool(vpn.get("auto_bring_up", False)),
        "vpn_interface": str(vpn.get("interface", "") or ""),
        "vpn_config_path": str(vpn.get("config_path", "") or ""),
    }


# =========================================================================== #
#  Reachability / optional VPN bring-up
#  Default assumption: the tunnel is managed OUTSIDE this script (already up).
#  We only probe; we touch the VPN only if vpn.auto_bring_up is true.
# =========================================================================== #
def _reachable(ip, cred, timeout=4):
    """True if the camera answers ISAPI — i.e. the route to it is up."""
    try:
        p = subprocess.run(
            ["curl", "-s", "--digest", "-u", cred, "--max-time", str(timeout),
             "-o", os.devnull, "-w", "%{http_code}",
             f"http://{ip}/ISAPI/System/deviceInfo"],
            capture_output=True, text=True, timeout=timeout + 3)
        return p.stdout.strip().endswith("200")
    except Exception:
        return False


def _find_wg_quick():
    for p in ("/usr/bin/wg-quick", "/usr/local/bin/wg-quick",
              "/opt/homebrew/bin/wg-quick"):
        if os.path.exists(p):
            return p
    return shutil.which("wg-quick") or "wg-quick"


def _any_reachable(cfg, tries=3, gap=2):
    """True if ANY configured camera answers. Retries a few times so a fresh /
    warming-up tunnel (first packet is slow) or a single dead camera doesn't
    abort the whole run."""
    ips = list(cfg["cam_ip"].values())
    for attempt in range(tries):
        for ip in ips:
            if _reachable(ip, cfg["cred"]):
                log(f"net: reachable via {ip}")
                return True
        if attempt < tries - 1:
            log("net: no camera answered yet, retrying…")
            time.sleep(gap)
    return False


def ensure_reachable(cfg):
    """Make sure at least one camera answers. Returns None on success, else raises."""
    if _any_reachable(cfg):
        return
    if not cfg["vpn_auto"]:
        raise ConfigError(
            "cameras unreachable — is the tunnel up? The VPN is expected to be "
            "managed externally; set vpn.auto_bring_up: true to let this script "
            "bring it up.")
    conf = cfg["vpn_config_path"]
    if not conf:
        raise ConfigError("vpn.auto_bring_up is true but vpn.config_path is empty")
    log("net: unreachable — trying to bring up WireGuard…")
    try:
        subprocess.run(["sudo", "-n", _find_wg_quick(), "up", conf],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        raise ConfigError(f"could not run wg-quick up: {e}")
    time.sleep(3)
    if not _any_reachable(cfg):
        raise ConfigError("still unreachable after wg-quick up — check the tunnel")
    log("net: tunnel brought up")


# =========================================================================== #
#  Capture — Hikvision ISAPI snapshot via curl (digest auth, hard --max-time)
# =========================================================================== #
def _is_jpeg(path):
    try:
        with open(path, "rb") as f:
            head = f.read(3)
        return head == b"\xff\xd8\xff" and os.path.getsize(path) > 1024
    except OSError:
        return False


def snapshot(name, ip, cred, outdir, run_ts):
    """Grab one 4K JPEG from a camera. Returns (path or None, error or None)."""
    out = os.path.join(outdir, f"cam{name}_{run_ts}_4K.jpg")
    url = f"http://{ip}/ISAPI/Streaming/channels/{MAIN_CHANNEL}/picture"
    try:
        p = subprocess.run(
            ["curl", "-s", "--digest", "-u", cred, "--max-time", "30",
             "-w", "%{http_code}", "-o", out, url],
            capture_output=True, text=True, timeout=40)
    except Exception as e:
        return None, f"curl error: {e}"
    code = p.stdout.strip()[-3:]
    if code == "200" and _is_jpeg(out):
        return out, None
    try:
        os.remove(out)
    except OSError:
        pass
    return None, f"HTTP {code or '000'} / not a valid JPEG"


# =========================================================================== #
#  AprilTag detection (tag36h11) — multi-pass pipeline for small / far tags:
#  full-res + CLAHE + overlapping upscaled tiles, dedupe across passes,
#  subpixel corners, centre = intersection of the quad diagonals.
# =========================================================================== #
def _quad_side(c):
    s = 0.0
    for i in range(4):
        s += math.hypot(c[i][0] - c[(i + 1) % 4][0], c[i][1] - c[(i + 1) % 4][1])
    return s / 4.0


def _diagonal_intersection(c):
    x1, y1 = c[0]; x2, y2 = c[2]; x3, y3 = c[1]; x4, y4 = c[3]
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-9:
        return (sum(p[0] for p in c) / 4.0, sum(p[1] for p in c) / 4.0)
    a = x1 * y2 - y1 * x2
    b = x3 * y4 - y3 * x4
    return ((a * (x3 - x4) - (x1 - x2) * b) / d,
            (a * (y3 - y4) - (y1 - y2) * b) / d)


def _refine(gray, corners, size):
    win = int(max(2, min(9, round(size / 8.0))))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.01)
    pts = np.array(corners, dtype=np.float32).reshape(-1, 1, 2)
    try:
        cv2.cornerSubPix(gray, pts, (win, win), (-1, -1), crit)
        return [[float(x), float(y)] for x, y in pts.reshape(-1, 2)]
    except cv2.error:
        return [[float(a), float(b)] for a, b in corners]


def _tiles(w, h, grid, ov):
    cols, rows = grid
    tw, th = w / cols, h / rows
    ox, oy = tw * ov, th * ov
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append((int(max(0, math.floor(c * tw - ox))),
                        int(max(0, math.floor(r * th - oy))),
                        int(min(w, math.ceil((c + 1) * tw + ox))),
                        int(min(h, math.ceil((r + 1) * th + oy)))))
    return out


# Detector objects are cached and reused: constructing/destroying them
# repeatedly corrupts the heap; calling .detect() on one instance is safe.
_AT3_CACHE = {}


def _get_at3(nthreads, quad_sigma):
    key = (int(nthreads), round(float(quad_sigma), 3))
    det = _AT3_CACHE.get(key)
    if det is None:
        det = _AT3Detector(families="tag36h11", nthreads=int(nthreads),
                           quad_decimate=1.0, quad_sigma=float(quad_sigma),
                           refine_edges=1, decode_sharpening=0.25)
        _AT3_CACHE[key] = det
    return det


def _pack(d, off, sc):
    ox, oy = off
    return [[float(p[0]) / sc + ox, float(p[1]) / sc + oy] for p in d.corners]


def _detect_raw(gray, nthreads, use_clahe, use_tiling, grid, ov, upscales, quad_sigma):
    det = _get_at3(nthreads, quad_sigma)
    h, w = gray.shape[:2]
    raw = []

    def _add(dets, off, sc):
        for d in dets:
            raw.append((int(d.tag_id), _pack(d, off, sc),
                        int(d.hamming), float(d.decision_margin)))

    _add(det.detect(gray, estimate_tag_pose=False), (0, 0), 1.0)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)) if use_clahe else None
    if clahe is not None:
        _add(det.detect(clahe.apply(gray), estimate_tag_pose=False), (0, 0), 1.0)
    if use_tiling:
        for (x0, y0, x1, y1) in _tiles(w, h, grid, ov):
            base = np.ascontiguousarray(gray[y0:y1, x0:x1])
            variants = [base] + ([clahe.apply(base)] if clahe is not None else [])
            for g0 in variants:
                for up in upscales:
                    if up and up != 1.0:
                        gu = cv2.resize(g0, None, fx=up, fy=up,
                                        interpolation=cv2.INTER_CUBIC)
                        sc = up
                    else:
                        gu, sc = g0, 1.0
                    _add(det.detect(gu, estimate_tag_pose=False), (x0, y0), sc)
    return raw


def _merge(raw):
    items = []
    for tid, corners, ham, margin in raw:
        cx = sum(p[0] for p in corners) / 4.0
        cy = sum(p[1] for p in corners) / 4.0
        items.append({"id": tid, "c": corners, "ham": ham, "m": margin,
                      "cx": cx, "cy": cy, "sz": _quad_side(corners)})
    used = [False] * len(items)
    out = []
    for i in range(len(items)):
        if used[i]:
            continue
        grp = [items[i]]
        used[i] = True
        for j in range(i + 1, len(items)):
            if used[j] or items[j]["id"] != items[i]["id"]:
                continue
            thr = 0.5 * max(items[i]["sz"], items[j]["sz"], 10.0)
            if math.hypot(items[i]["cx"] - items[j]["cx"],
                          items[i]["cy"] - items[j]["cy"]) < thr:
                grp.append(items[j])
                used[j] = True
        out.append(sorted(grp, key=lambda g: (g["ham"], -g["m"]))[0])
    return out


def detect_tags(image_bgr, nthreads=None, max_hamming=2, use_clahe=True,
                use_tiling=True, grid=(5, 4), overlap=0.25, upscales=(2.0, 3.0),
                quad_sigma=0.0):
    """Return [{"id", "x", "y"}] for tag36h11 markers, one entry per id."""
    if not _HAS_AT3:
        raise ConfigError("pupil-apriltags is not installed — run ./setup.sh")
    if nthreads is None:
        nthreads = max(1, os.cpu_count() or 4)
    gray = np.ascontiguousarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY))
    raw = _detect_raw(gray, nthreads, use_clahe, use_tiling, grid, overlap,
                      upscales, quad_sigma)

    best_by_id = {}
    for m in _merge(raw):
        if m["ham"] > max_hamming:
            continue
        prev = best_by_id.get(m["id"])
        if prev is None or m["m"] > prev["m"]:
            best_by_id[m["id"]] = m

    tags = []
    for m in sorted(best_by_id.values(), key=lambda g: g["id"]):
        corners = _refine(gray, m["c"], m["sz"])
        cx, cy = _diagonal_intersection(corners)
        tags.append({"id": int(m["id"]), "x": round(cx, 2), "y": round(cy, 2)})
    return tags


# =========================================================================== #
#  Pairing
# =========================================================================== #
def _placement(tag_id):
    """Even ids lie on the floor; odd ids are elevated / in the air."""
    return "floor" if int(tag_id) % 2 == 0 else "elevated"


def build_pairs(cams, per_cam):
    """
    per_cam: {cam: {id: (x, y)}}. One dict per unordered camera pair; a tag is
    listed only if both cameras of the pair detected that id.
    """
    pairs = []
    for a, b in combinations(cams, 2):
        ta, tb = per_cam.get(a, {}), per_cam.get(b, {})
        tags = []
        for tid in sorted(set(ta) & set(tb)):
            tags.append({
                "id": tid,
                "placement": _placement(tid),
                a: {"x": ta[tid][0], "y": ta[tid][1]},
                b: {"x": tb[tid][0], "y": tb[tid][1]},
            })
        pairs.append({"cameras": [a, b], "tags": tags})
    return pairs


# =========================================================================== #
#  Main
# =========================================================================== #
def build_parser():
    ap = argparse.ArgumentParser(
        description="Snapshot the configured cameras (4K), detect tag36h11, and "
                    "report the pairwise common tags with per-camera centres.")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.yaml"),
                    help="config file (default: config.yaml next to this script)")
    ap.add_argument("--cameras", default="",
                    help="restrict to a comma-separated subset of camera names")
    ap.add_argument("--out", "-o", help="write JSON here instead of stdout")
    ap.add_argument("--pretty", action="store_true", help="indented JSON")
    ap.add_argument("--max-hamming", type=int, default=2,
                    help="max corrected error bits to accept (default 2)")
    ap.add_argument("--keep-images", action="store_true",
                    help="keep the captured JPEGs in ./shots (default: deleted)")
    ap.add_argument("--log", action="store_true",
                    help="print progress and full error detail to stderr")
    return ap


def run(args):
    """Do the whole job and return the result dict (may be {'error': ...})."""
    cfg = load_config(args.config)

    cam_ip = cfg["cam_ip"]
    if args.cameras.strip():
        wanted = [c for c in args.cameras.replace(" ", "").split(",") if c]
        unknown = [c for c in wanted if c not in cam_ip]
        if unknown:
            raise ConfigError(f"unknown camera(s) {unknown}; "
                              f"configured: {list(cam_ip)}")
        cam_ip = {c: cam_ip[c] for c in wanted}
    cams = list(cam_ip)

    ensure_reachable({**cfg, "cam_ip": cam_ip})

    run_ts = time.strftime("%Y%m%d_%H%M%S")
    keep = args.keep_images
    outdir = os.path.join(SCRIPT_DIR, "shots") if keep else tempfile.mkdtemp(
        prefix="cam_pairs_")
    os.makedirs(outdir, exist_ok=True)

    # Capture in parallel (network-bound; the tunnel is already up).
    paths, errors = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, len(cams))) as ex:
        futs = {c: ex.submit(snapshot, c, cam_ip[c], cfg["cred"], outdir, run_ts)
                for c in cams}
    for c, fut in futs.items():
        path, err = fut.result()
        if path:
            paths[c] = path
            log(f"captured {c}: {path}")
        else:
            errors[c] = err
            log(f"capture FAILED {c}: {err}")

    # Detect sequentially — each detect saturates all cores and the
    # pupil-apriltags Detector is not safe to share across threads.
    per_cam = {}
    for c in cams:
        if c not in paths:
            continue
        img = cv2.imread(paths[c], cv2.IMREAD_COLOR)
        if img is None:
            errors[c] = "captured file could not be read"
            log(f"read FAILED {c}")
            continue
        t0 = time.time()
        tags = detect_tags(img, max_hamming=args.max_hamming)
        per_cam[c] = {t["id"]: (t["x"], t["y"]) for t in tags}
        log(f"detect {c}: {len(tags)} tag(s) {sorted(per_cam[c])} "
            f"({time.time() - t0:.1f}s)")

    if not keep:
        shutil.rmtree(outdir, ignore_errors=True)

    result = {"pairs": build_pairs(cams, per_cam)}
    if errors:
        result["errors"] = errors
    return result


def emit(result, args):
    text = json.dumps(result, ensure_ascii=False,
                      indent=2 if args.pretty else None)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        log(f"wrote {args.out}")
    else:
        print(text)


def main(argv=None):
    global _LOG
    args = build_parser().parse_args(argv)
    _LOG = args.log
    try:
        result = run(args)
        rc = 0
    except ConfigError as e:
        log("ERROR: " + str(e))
        result = {"error": str(e)}
        rc = 1
    except Exception as e:  # unexpected — surface a short message, full trace in --log
        if _LOG:
            traceback.print_exc()
        result = {"error": f"{type(e).__name__}: {e}"}
        rc = 1
    emit(result, args)
    return rc


if __name__ == "__main__":
    # Flush then hard-exit to bypass pupil-apriltags' teardown crash, which can
    # otherwise drop buffered stdout when piped.
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if _rc is None else int(_rc))
