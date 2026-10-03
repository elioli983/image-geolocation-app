from __future__ import annotations

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from io import BytesIO
from urllib.parse import unquote, urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
import argparse
import json
import math
import os
import sqlite3
import threading
import uuid
import webbrowser
import time

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "mapillary_runs.sqlite3"
AI_CACHE_PATH = ROOT / "ai_cache"
TARGET_DIR = ROOT / "target_uploads"
TARGET_META_PATH = TARGET_DIR / "current.json"
MAX_TARGET_BYTES = 30 * 1024 * 1024
CONFIG_PATH = ROOT / "config.json"
MAX_JSON_BYTES = 50 * 1024 * 1024
GRAPH_URL = "https://graph.mapillary.com"
EARTH_RADIUS_M = 6371008.8

AI_JOBS = {}
AI_JOBS_LOCK = threading.Lock()

LARGE_AREA_WORKERS = {}
LARGE_AREA_LOCK = threading.Lock()
LARGE_AREA_CELL_RADIUS_M = 1500.0
LARGE_AREA_CENTER_SPACING_M = 2450.0
LARGE_AREA_ROW_SPACING_M = LARGE_AREA_CENTER_SPACING_M * math.sqrt(3.0) / 2.0
LARGE_AREA_MAX_RADIUS_M = 50000.0
LARGE_AREA_MAX_CELLS = 5000

os.chdir(ROOT)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_access_token() -> str:
    token = os.environ.get("MAPILLARY_ACCESS_TOKEN", "").strip()
    if token:
        return token
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(cfg.get("mapillary_access_token", "")).strip()
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                status TEXT,
                center_lat REAL NOT NULL,
                center_lng REAL NOT NULL,
                radius_m REAL NOT NULL,
                image_type TEXT NOT NULL,
                start_captured_at TEXT,
                end_captured_at TEXT,
                max_api_images INTEGER NOT NULL,
                api_images_seen INTEGER NOT NULL DEFAULT 0,
                ai_run_stats_json TEXT
            );

            CREATE TABLE IF NOT EXISTS images (
                run_id TEXT NOT NULL,
                image_number INTEGER NOT NULL,
                image_id TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                distance_m REAL NOT NULL,
                captured_at INTEGER,
                compass_angle REAL,
                is_pano INTEGER,
                sequence_id TEXT,
                camera_type TEXT,
                ai_score REAL,
                ai_best_crop TEXT,
                ai_model TEXT,
                ai_mode TEXT,
                ai_image_type TEXT,
                ai_thumb_size INTEGER,
                ai_scored_at TEXT,
                PRIMARY KEY (run_id, image_number),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shortlist (
                image_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                lat REAL,
                lng REAL,
                distance_m REAL,
                captured_at INTEGER,
                compass_angle REAL,
                is_pano INTEGER,
                sequence_id TEXT,
                camera_type TEXT,
                ai_score REAL,
                ai_best_crop TEXT,
                ai_model TEXT,
                ai_mode TEXT,
                ai_image_type TEXT,
                ai_thumb_size INTEGER,
                ai_scored_at TEXT
            );

            CREATE TABLE IF NOT EXISTS large_area_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                center_lat REAL NOT NULL,
                center_lng REAL NOT NULL,
                overall_radius_m REAL NOT NULL,
                cell_radius_m REAL NOT NULL,
                center_spacing_m REAL NOT NULL,
                row_spacing_m REAL NOT NULL,
                image_type TEXT NOT NULL,
                start_captured_at TEXT,
                end_captured_at TEXT,
                max_api_images_per_cell INTEGER NOT NULL,
                pause_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS large_area_cells (
                job_id TEXT NOT NULL,
                cell_number INTEGER NOT NULL,
                cell_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                row_position INTEGER NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                raw_image_count INTEGER NOT NULL DEFAULT 0,
                api_images_seen INTEGER NOT NULL DEFAULT 0,
                api_request_count INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                split_count INTEGER NOT NULL DEFAULT 0,
                cap_reached INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                PRIMARY KEY (job_id, cell_number),
                UNIQUE (job_id, cell_id),
                FOREIGN KEY (job_id) REFERENCES large_area_jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS large_area_images (
                job_id TEXT NOT NULL,
                image_id TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                distance_m REAL NOT NULL,
                captured_at INTEGER,
                compass_angle REAL,
                is_pano INTEGER,
                sequence_id TEXT,
                camera_type TEXT,
                first_cell_id TEXT,
                found_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (job_id, image_id),
                FOREIGN KEY (job_id) REFERENCES large_area_jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS large_area_image_cells (
                job_id TEXT NOT NULL,
                image_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                PRIMARY KEY (job_id, image_id, cell_id),
                FOREIGN KEY (job_id) REFERENCES large_area_jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_images_run_id ON images(run_id);
            CREATE INDEX IF NOT EXISTS idx_images_image_id ON images(image_id);
            CREATE INDEX IF NOT EXISTS idx_runs_saved_at ON runs(saved_at);
            CREATE INDEX IF NOT EXISTS idx_shortlist_added_at ON shortlist(added_at);
            CREATE INDEX IF NOT EXISTS idx_large_jobs_updated ON large_area_jobs(updated_at);
            CREATE INDEX IF NOT EXISTS idx_large_cells_job_status ON large_area_cells(job_id,status);
            CREATE INDEX IF NOT EXISTS idx_large_images_job_distance ON large_area_images(job_id,distance_m);
            """
        )
        # A server restart cannot preserve a running worker thread. Convert any
        # interrupted work into resumable state without discarding completed cells.
        conn.execute("UPDATE large_area_cells SET status='pending', error=COALESCE(error,'Interrupted by server restart.') WHERE status='running'")
        conn.execute("UPDATE large_area_jobs SET status='paused', pause_requested=0, cancel_requested=0, updated_at=? WHERE status IN ('running','pausing')", (utc_now_iso(),))
        # Upgrade databases created by v1.x without disturbing existing runs.
        for table in ("images", "shortlist"):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            upgrades = {
                "ai_score": "REAL",
                "ai_best_crop": "TEXT",
                "ai_model": "TEXT",
                "ai_mode": "TEXT",
                "ai_image_type": "TEXT",
                "ai_thumb_size": "INTEGER",
                "ai_scored_at": "TEXT",
            }
            for column, sql_type in upgrades.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        if "ai_run_stats_json" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN ai_run_stats_json TEXT")


def optional_float(v):
    if v is None or v == "":
        return None
    return float(v)


def optional_int(v):
    if v is None or v == "":
        return None
    return int(v)


def haversine_m(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def circle_bbox(lat, lng, radius_m):
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    cos_lat = max(0.000001, math.cos(math.radians(lat)))
    dlng = math.degrees(radius_m / (EARTH_RADIUS_M * cos_lat))
    return lng - dlng, lat - dlat, lng + dlng, lat + dlat



def load_target_meta():
    try:
        data = json.loads(TARGET_META_PATH.read_text(encoding="utf-8"))
        file_name = str(data.get("storedName") or "")
        path = TARGET_DIR / file_name
        if not file_name or not path.exists() or not path.is_file():
            return None
        data["path"] = path
        return data
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def current_target_path():
    meta = load_target_meta()
    return meta.get("path") if meta else None


def public_target_status():
    meta = load_target_meta()
    if not meta:
        return {"configured": False}
    path = meta["path"]
    return {
        "configured": True,
        "filename": meta.get("filename") or path.name,
        "contentType": meta.get("contentType") or "application/octet-stream",
        "sizeBytes": int(meta.get("sizeBytes") or path.stat().st_size),
        "updatedAt": meta.get("updatedAt") or "",
    }


def detect_image_type(raw: bytes, declared: str):
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if declared in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("The uploaded file does not match a supported image signature.")
    raise ValueError("Target must be a JPEG, PNG, or WebP image.")


def validate_decodable_target_image(raw: bytes, content_type: str):
    """Verify the image can actually be decoded when Pillow is available.

    Pillow is part of the optional AI requirements. The browser also performs
    a decode check before upload, so installations without Pillow retain the
    original signature validation without making basic app startup depend on
    the AI stack.
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return
    try:
        with Image.open(BytesIO(raw)) as im:
            im.verify()
    except Exception as exc:
        label = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WebP"}.get(content_type, "image")
        raise ValueError(f"The selected file appears to be a {label}, but the image data is corrupt or cannot be decoded. Please choose another image.") from exc


def save_target_upload(raw: bytes, declared_type: str, original_name: str):
    if not raw:
        raise ValueError("Target image upload is empty.")
    if len(raw) > MAX_TARGET_BYTES:
        raise ValueError("Target image is too large. Maximum size is 30 MB.")
    content_type, ext = detect_image_type(raw, declared_type)
    validate_decodable_target_image(raw, content_type)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    stored_name = f"target_{uuid.uuid4().hex}{ext}"
    path = TARGET_DIR / stored_name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)
    safe_original = Path(original_name or f"target{ext}").name[:240]
    meta = {
        "filename": safe_original,
        "storedName": stored_name,
        "contentType": content_type,
        "sizeBytes": len(raw),
        "updatedAt": utc_now_iso(),
    }
    meta_tmp = TARGET_META_PATH.with_suffix(".tmp")
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    meta_tmp.replace(TARGET_META_PATH)
    return public_target_status()


class MapillaryAPIError(RuntimeError):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Mapillary API HTTP {status_code}: {detail}")


def graph_get(url: str, token: str):
    req = Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
            "User-Agent": "ImageGeolocationEstimationApp/2.4",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=45) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body[:1000]
        raise MapillaryAPIError(exc.code, detail) from exc
    except URLError as exc:
        raise RuntimeError(f"Mapillary API connection failed: {exc.reason}") from exc


def is_reduce_data_error(exc):
    if not isinstance(exc, MapillaryAPIError):
        return False
    text = json.dumps(exc.detail, ensure_ascii=False).lower() if not isinstance(exc.detail, str) else exc.detail.lower()
    return "reduce the amount of data" in text or "too much data" in text


def split_bbox(bounds):
    west, south, east, north = bounds
    mid_lng = (west + east) / 2.0
    mid_lat = (south + north) / 2.0
    return [
        (west, south, mid_lng, mid_lat),
        (mid_lng, south, east, mid_lat),
        (west, mid_lat, mid_lng, north),
        (mid_lng, mid_lat, east, north),
    ]


def normalize_image(item, center_lat, center_lng):
    geom = item.get("geometry") or item.get("computed_geometry") or {}
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if not coords or len(coords) < 2:
        return None
    lng, lat = float(coords[0]), float(coords[1])
    sequence = item.get("sequence")
    if isinstance(sequence, dict):
        sequence = sequence.get("id")
    return {
        "id": str(item.get("id", "")),
        "lat": lat,
        "lng": lng,
        "distanceM": haversine_m(center_lat, center_lng, lat, lng),
        "capturedAt": optional_int(item.get("captured_at")),
        "compassAngle": optional_float(item.get("compass_angle")),
        "isPano": bool(item.get("is_pano")) if item.get("is_pano") is not None else None,
        "sequenceId": str(sequence) if sequence not in (None, "") else None,
        "cameraType": item.get("camera_type"),
    }


def discover_images(params, token):
    center_lat = float(params["centerLat"])
    center_lng = float(params["centerLng"])
    radius_m = float(params["radiusM"])
    image_type = str(params.get("imageType", "all")).lower()
    start_date = str(params.get("startCapturedAt") or "").strip()
    end_date = str(params.get("endCapturedAt") or "").strip()
    max_api_images = max(1, min(int(params.get("maxApiImages") or 20000), 200000))

    if not (-90 <= center_lat <= 90 and -180 <= center_lng <= 180):
        raise ValueError("Center latitude/longitude is invalid.")
    if radius_m <= 0 or radius_m > 50000:
        raise ValueError("Radius must be greater than 0 and no more than 50,000 meters.")
    if image_type not in {"all", "pano", "flat"}:
        raise ValueError("imageType must be all, pano, or flat.")

    initial_bbox = circle_bbox(center_lat, center_lng, radius_m)
    fields = "id,geometry,captured_at,compass_angle,is_pano,sequence,camera_type"

    # A smaller page size is much more reliable than requesting 2,000 records at
    # once from the Graph API. If Mapillary still returns its "reduce the amount
    # of data" error, the server transparently subdivides the geographic query
    # into four smaller bounding boxes and retries. Results are deduplicated by
    # image ID, so retries and quadrant boundaries do not duplicate candidates.
    page_limit = 200
    max_split_depth = 8
    queue = [(initial_bbox, 0)]

    seen_inside_circle = {}
    api_seen_ids = set()
    page_count = 0
    api_request_count = 0
    split_count = 0
    tile_count = 0
    cap_reached = False

    def make_url(bounds):
        west, south, east, north = bounds
        q = {
            "bbox": f"{west:.8f},{south:.8f},{east:.8f},{north:.8f}",
            "fields": fields,
            "limit": str(page_limit),
        }
        if image_type == "pano":
            q["is_pano"] = "true"
        elif image_type == "flat":
            q["is_pano"] = "false"
        if start_date:
            q["start_captured_at"] = start_date
        if end_date:
            q["end_captured_at"] = end_date
        return GRAPH_URL + "/images?" + urlencode(q, safe=",")

    while queue and not cap_reached:
        bounds, depth = queue.pop(0)
        tile_count += 1
        url = make_url(bounds)
        tile_failed_for_size = False

        while url and not cap_reached:
            api_request_count += 1
            try:
                payload = graph_get(url, token)
            except MapillaryAPIError as exc:
                if is_reduce_data_error(exc) and depth < max_split_depth:
                    # Restart this geographic area as four smaller queries. Any
                    # images already obtained from earlier pages remain safe: all
                    # API observations are deduplicated by image ID below.
                    queue[0:0] = [(b, depth + 1) for b in split_bbox(bounds)]
                    split_count += 1
                    tile_failed_for_size = True
                    break
                raise

            page_count += 1
            data = payload.get("data") or []
            if not isinstance(data, list):
                raise RuntimeError("Unexpected Mapillary API response: data is not a list.")

            for item in data:
                image_id = str(item.get("id") or "")
                if image_id and image_id not in api_seen_ids:
                    if len(api_seen_ids) >= max_api_images:
                        cap_reached = True
                        break
                    api_seen_ids.add(image_id)

                normalized = normalize_image(item, center_lat, center_lng)
                if not normalized or not normalized["id"]:
                    continue
                if normalized["distanceM"] <= radius_m + 0.001:
                    seen_inside_circle[normalized["id"]] = normalized

            if cap_reached:
                break

            paging = payload.get("paging") or {}
            next_url = paging.get("next")
            if not next_url:
                break
            url = next_url

        if tile_failed_for_size:
            continue

    images = list(seen_inside_circle.values())
    images.sort(key=lambda x: (x["distanceM"], x["capturedAt"] or 0, x["id"]))
    west, south, east, north = initial_bbox
    return {
        "status": "cap_reached" if cap_reached else "complete",
        "apiImagesSeen": len(api_seen_ids),
        "pageCount": page_count,
        "apiRequestCount": api_request_count,
        "splitCount": split_count,
        "tileCount": tile_count,
        "pageLimit": page_limit,
        "bbox": {"west": west, "south": south, "east": east, "north": north},
        "images": images,
    }



def destination_point(lat, lng, east_m, north_m):
    """Convert a local east/north offset into a WGS84 point using spherical geodesics."""
    distance = math.hypot(east_m, north_m)
    if distance == 0:
        return float(lat), float(lng)
    bearing = math.atan2(east_m, north_m)
    phi1 = math.radians(lat)
    lam1 = math.radians(lng)
    delta = distance / EARTH_RADIUS_M
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(bearing))
    lam2 = lam1 + math.atan2(
        math.sin(bearing) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0
    return math.degrees(phi2), lon2


def generate_large_area_cells(center_lat, center_lng, overall_radius_m):
    """Generate an overlapping triangular lattice of 1,500 m search circles."""
    center_lat = float(center_lat)
    center_lng = float(center_lng)
    overall_radius_m = float(overall_radius_m)
    if not (-90 <= center_lat <= 90 and -180 <= center_lng <= 180):
        raise ValueError("Center latitude/longitude is invalid.")
    if overall_radius_m <= LARGE_AREA_CELL_RADIUS_M or overall_radius_m > LARGE_AREA_MAX_RADIUS_M:
        raise ValueError(f"A radius of {int(LARGE_AREA_CELL_RADIUS_M):,} m or less can be searched directly with Standard Search. Large Area Search accepts {int(LARGE_AREA_CELL_RADIUS_M)+1:,}–{int(LARGE_AREA_MAX_RADIUS_M):,} m.")

    spacing = LARGE_AREA_CENTER_SPACING_M
    row_spacing = LARGE_AREA_ROW_SPACING_M
    extent = overall_radius_m + LARGE_AREA_CELL_RADIUS_M + spacing
    kmax = int(math.ceil(extent / row_spacing)) + 1
    nmax = int(math.ceil(extent / spacing)) + 2
    candidates = []
    for row_k in range(-kmax, kmax + 1):
        north_m = row_k * row_spacing
        offset = spacing / 2.0 if abs(row_k) % 2 else 0.0
        for col_n in range(-nmax, nmax + 1):
            east_m = col_n * spacing + offset
            center_distance = math.hypot(east_m, north_m)
            if center_distance > overall_radius_m + LARGE_AREA_CELL_RADIUS_M:
                continue
            lat, lng = destination_point(center_lat, center_lng, east_m, north_m)
            candidates.append({
                "rowK": row_k,
                "eastM": east_m,
                "northM": north_m,
                "lat": lat,
                "lng": lng,
                "centerDistanceM": center_distance,
            })

    if len(candidates) > LARGE_AREA_MAX_CELLS:
        raise ValueError(f"Large Area Search would create {len(candidates):,} cells; the current safety limit is {LARGE_AREA_MAX_CELLS:,}.")

    # Number rows from north to south and positions west to east for easy tracking.
    rows = sorted({c["rowK"] for c in candidates}, reverse=True)
    row_numbers = {row_k: idx + 1 for idx, row_k in enumerate(rows)}
    ordered = []
    for row_k in rows:
        items = sorted((c for c in candidates if c["rowK"] == row_k), key=lambda c: c["eastM"])
        for pos, c in enumerate(items, start=1):
            c = dict(c)
            c["rowNumber"] = row_numbers[row_k]
            c["rowPosition"] = pos
            ordered.append(c)
    for idx, c in enumerate(ordered, start=1):
        c["cellNumber"] = idx
        c["cellId"] = f"C{idx:04d}"
    return ordered


def validate_large_area_params(payload):
    if not isinstance(payload, dict):
        raise ValueError("Large Area Search payload must be an object.")
    center_lat = float(payload.get("centerLat"))
    center_lng = float(payload.get("centerLng"))
    overall_radius_m = float(payload.get("overallRadiusM"))
    image_type = str(payload.get("imageType") or "all").lower()
    if image_type not in {"all", "pano", "flat"}:
        raise ValueError("imageType must be all, pano, or flat.")
    max_api = max(1, min(int(payload.get("maxApiImages") or 20000), 200000))
    cells = generate_large_area_cells(center_lat, center_lng, overall_radius_m)
    return {
        "name": str(payload.get("name") or "").strip() or f"Large Area Search {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "centerLat": center_lat,
        "centerLng": center_lng,
        "overallRadiusM": overall_radius_m,
        "cellRadiusM": LARGE_AREA_CELL_RADIUS_M,
        "centerSpacingM": LARGE_AREA_CENTER_SPACING_M,
        "rowSpacingM": LARGE_AREA_ROW_SPACING_M,
        "imageType": image_type,
        "startCapturedAt": str(payload.get("startCapturedAt") or "").strip(),
        "endCapturedAt": str(payload.get("endCapturedAt") or "").strip(),
        "maxApiImages": max_api,
        "cells": cells,
    }


def create_large_area_job(payload):
    p = validate_large_area_params(payload)
    job_id = str(uuid.uuid4())
    now = utc_now_iso()
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO large_area_jobs(id,name,created_at,updated_at,status,center_lat,center_lng,overall_radius_m,
                                        cell_radius_m,center_spacing_m,row_spacing_m,image_type,start_captured_at,
                                        end_captured_at,max_api_images_per_cell,pause_requested,cancel_requested,error)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (job_id, p["name"], now, now, "pending", p["centerLat"], p["centerLng"], p["overallRadiusM"],
             p["cellRadiusM"], p["centerSpacingM"], p["rowSpacingM"], p["imageType"], p["startCapturedAt"],
             p["endCapturedAt"], p["maxApiImages"], 0, 0, None),
        )
        conn.executemany(
            """
            INSERT INTO large_area_cells(job_id,cell_number,cell_id,row_number,row_position,lat,lng,status)
            VALUES(?,?,?,?,?,?,?,'pending')
            """,
            [(job_id, c["cellNumber"], c["cellId"], c["rowNumber"], c["rowPosition"], c["lat"], c["lng"]) for c in p["cells"]],
        )
    return job_id


def large_area_job_summary(job_id, include_cells=False):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM large_area_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        counts = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                   SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(raw_image_count) AS raw_images,
                   SUM(api_images_seen) AS api_images_seen,
                   SUM(api_request_count) AS api_requests,
                   SUM(CASE WHEN cap_reached=1 THEN 1 ELSE 0 END) AS cap_cells
            FROM large_area_cells WHERE job_id=?
            """, (job_id,)
        ).fetchone()
        unique_images = conn.execute("SELECT COUNT(*) FROM large_area_images WHERE job_id=?", (job_id,)).fetchone()[0]
        data = {
            "id": row["id"], "name": row["name"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "status": row["status"], "error": row["error"],
            "params": {
                "centerLat": row["center_lat"], "centerLng": row["center_lng"], "overallRadiusM": row["overall_radius_m"],
                "cellRadiusM": row["cell_radius_m"], "centerSpacingM": row["center_spacing_m"], "rowSpacingM": row["row_spacing_m"],
                "imageType": row["image_type"], "startCapturedAt": row["start_captured_at"] or "",
                "endCapturedAt": row["end_captured_at"] or "", "maxApiImages": row["max_api_images_per_cell"],
            },
            "cellCounts": {k: int(counts[k] or 0) for k in ("total", "pending", "running", "complete", "failed")},
            "rawImages": int(counts["raw_images"] or 0), "uniqueImages": int(unique_images or 0),
            "duplicatesRemoved": max(0, int(counts["raw_images"] or 0) - int(unique_images or 0)),
            "apiImagesSeen": int(counts["api_images_seen"] or 0), "apiRequestCount": int(counts["api_requests"] or 0),
            "capCells": int(counts["cap_cells"] or 0),
            "pauseRequested": bool(row["pause_requested"]), "cancelRequested": bool(row["cancel_requested"]),
        }
        if include_cells:
            cell_rows = conn.execute(
                "SELECT cell_number,cell_id,row_number,row_position,lat,lng,status,attempts,started_at,finished_at,raw_image_count,api_images_seen,api_request_count,page_count,split_count,cap_reached,error FROM large_area_cells WHERE job_id=? ORDER BY cell_number",
                (job_id,),
            ).fetchall()
            data["cells"] = [dict(r) for r in cell_rows]
        return data


def list_large_area_jobs():
    with connect_db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM large_area_jobs ORDER BY updated_at DESC").fetchall()]
    return [large_area_job_summary(job_id, include_cells=False) for job_id in ids]


def large_area_results(job_id):
    summary = large_area_job_summary(job_id, include_cells=False)
    if summary is None:
        return None
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT image_id,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,first_cell_id,found_count
            FROM large_area_images WHERE job_id=? ORDER BY distance_m,captured_at,image_id
            """, (job_id,)
        ).fetchall()
    images = []
    for r in rows:
        images.append({
            "id": str(r["image_id"]), "lat": r["lat"], "lng": r["lng"], "distanceM": r["distance_m"],
            "capturedAt": r["captured_at"], "compassAngle": r["compass_angle"],
            "isPano": None if r["is_pano"] is None else bool(r["is_pano"]), "sequenceId": r["sequence_id"],
            "cameraType": r["camera_type"], "largeAreaFirstCell": r["first_cell_id"], "largeAreaFoundCount": r["found_count"],
        })
    summary["images"] = images
    return summary


def _merge_large_area_cell_images(conn, job_row, cell_id, images):
    for im in images:
        image_id = str(im.get("id") or "")
        if not image_id:
            continue
        overall_distance = haversine_m(job_row["center_lat"], job_row["center_lng"], float(im["lat"]), float(im["lng"]))
        is_pano = None if im.get("isPano") is None else (1 if im.get("isPano") else 0)
        conn.execute(
            """
            INSERT INTO large_area_images(job_id,image_id,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,first_cell_id,found_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(job_id,image_id) DO UPDATE SET
                lat=excluded.lat,lng=excluded.lng,distance_m=excluded.distance_m,captured_at=excluded.captured_at,
                compass_angle=excluded.compass_angle,is_pano=excluded.is_pano,sequence_id=excluded.sequence_id,camera_type=excluded.camera_type
            """,
            (job_row["id"], image_id, float(im["lat"]), float(im["lng"]), overall_distance,
             optional_int(im.get("capturedAt")), optional_float(im.get("compassAngle")), is_pano,
             im.get("sequenceId"), im.get("cameraType"), cell_id),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO large_area_image_cells(job_id,image_id,cell_id) VALUES(?,?,?)",
            (job_row["id"], image_id, cell_id),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE large_area_images SET found_count=(SELECT COUNT(*) FROM large_area_image_cells WHERE job_id=? AND image_id=?) WHERE job_id=? AND image_id=?",
                (job_row["id"], image_id, job_row["id"], image_id),
            )


def _large_area_should_stop(job_id):
    with connect_db() as conn:
        row = conn.execute("SELECT pause_requested,cancel_requested FROM large_area_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return "deleted"
    if row["cancel_requested"]:
        return "cancelled"
    if row["pause_requested"]:
        return "paused"
    return None


def large_area_worker(job_id):
    token = load_access_token()
    try:
        if not token:
            raise RuntimeError("Mapillary access token is not configured.")
        with connect_db() as conn:
            job_row = conn.execute("SELECT * FROM large_area_jobs WHERE id=?", (job_id,)).fetchone()
            if job_row is None:
                return
            conn.execute("UPDATE large_area_jobs SET status='running',pause_requested=0,cancel_requested=0,error=NULL,updated_at=? WHERE id=?", (utc_now_iso(), job_id))

        while True:
            stop = _large_area_should_stop(job_id)
            if stop:
                with connect_db() as conn:
                    if stop != "deleted":
                        conn.execute("UPDATE large_area_jobs SET status=?,updated_at=? WHERE id=?", (stop, utc_now_iso(), job_id))
                return

            with connect_db() as conn:
                job_row = conn.execute("SELECT * FROM large_area_jobs WHERE id=?", (job_id,)).fetchone()
                cell = conn.execute(
                    "SELECT * FROM large_area_cells WHERE job_id=? AND status='pending' ORDER BY cell_number LIMIT 1",
                    (job_id,),
                ).fetchone()
                if cell is None:
                    failed = conn.execute("SELECT COUNT(*) FROM large_area_cells WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
                    status = "complete_with_errors" if failed else "complete"
                    conn.execute("UPDATE large_area_jobs SET status=?,updated_at=? WHERE id=?", (status, utc_now_iso(), job_id))
                    return
                conn.execute(
                    "UPDATE large_area_cells SET status='running',attempts=attempts+1,started_at=?,finished_at=NULL,error=NULL WHERE job_id=? AND cell_number=?",
                    (utc_now_iso(), job_id, cell["cell_number"]),
                )
                conn.execute("UPDATE large_area_jobs SET updated_at=? WHERE id=?", (utc_now_iso(), job_id))

            cell_params = {
                "centerLat": cell["lat"], "centerLng": cell["lng"], "radiusM": job_row["cell_radius_m"],
                "imageType": job_row["image_type"], "startCapturedAt": job_row["start_captured_at"] or "",
                "endCapturedAt": job_row["end_captured_at"] or "", "maxApiImages": job_row["max_api_images_per_cell"],
            }
            try:
                result = discover_images(cell_params, token)
                images = result.get("images") or []
                with connect_db() as conn:
                    fresh_job = conn.execute("SELECT * FROM large_area_jobs WHERE id=?", (job_id,)).fetchone()
                    if fresh_job is None:
                        return
                    _merge_large_area_cell_images(conn, fresh_job, cell["cell_id"], images)
                    conn.execute(
                        """
                        UPDATE large_area_cells SET status='complete',finished_at=?,raw_image_count=?,api_images_seen=?,api_request_count=?,page_count=?,split_count=?,cap_reached=?,error=NULL
                        WHERE job_id=? AND cell_number=?
                        """,
                        (utc_now_iso(), len(images), int(result.get("apiImagesSeen") or 0), int(result.get("apiRequestCount") or 0),
                         int(result.get("pageCount") or 0), int(result.get("splitCount") or 0), 1 if result.get("status") == "cap_reached" else 0,
                         job_id, cell["cell_number"]),
                    )
                    conn.execute("UPDATE large_area_jobs SET updated_at=? WHERE id=?", (utc_now_iso(), job_id))
            except Exception as exc:
                # One cell failure must not terminate the overall job. It remains available for Retry Failed Cells.
                with connect_db() as conn:
                    conn.execute(
                        "UPDATE large_area_cells SET status='failed',finished_at=?,error=? WHERE job_id=? AND cell_number=?",
                        (utc_now_iso(), str(exc)[:4000], job_id, cell["cell_number"]),
                    )
                    conn.execute("UPDATE large_area_jobs SET updated_at=? WHERE id=?", (utc_now_iso(), job_id))
    except Exception as exc:
        with connect_db() as conn:
            conn.execute("UPDATE large_area_jobs SET status='error',error=?,updated_at=? WHERE id=?", (str(exc)[:4000], utc_now_iso(), job_id))
    finally:
        with LARGE_AREA_LOCK:
            LARGE_AREA_WORKERS.pop(job_id, None)


def start_large_area_worker(job_id):
    with LARGE_AREA_LOCK:
        existing = LARGE_AREA_WORKERS.get(job_id)
        if existing is not None and existing.is_alive():
            return False
        # Avoid hammering Mapillary with multiple whole-city jobs at once in this POC.
        for other_id, thread in LARGE_AREA_WORKERS.items():
            if other_id != job_id and thread.is_alive():
                raise ValueError("Another Large Area Search is currently running. Pause or finish it before starting this one.")
        thread = threading.Thread(target=large_area_worker, args=(job_id,), daemon=True, name=f"large-area-{job_id[:8]}")
        LARGE_AREA_WORKERS[job_id] = thread
        thread.start()
        return True


def validate_run(run):
    if not isinstance(run, dict):
        raise ValueError("Run payload must be an object.")
    for key in ("id", "name", "savedAt", "params", "images"):
        if key not in run:
            raise ValueError(f"Missing required field: {key}")
    if not isinstance(run["images"], list):
        raise ValueError("images must be an array.")
    return run


def validate_shortlist_item(item):
    if not isinstance(item, dict):
        raise ValueError("Shortlist item must be an object.")
    image_id = str(item.get("id") or item.get("imageId") or "").strip()
    if not image_id:
        raise ValueError("Image ID cannot be empty.")
    return {
        "id": image_id,
        "lat": optional_float(item.get("lat")),
        "lng": optional_float(item.get("lng")),
        "distanceM": optional_float(item.get("distanceM")),
        "capturedAt": optional_int(item.get("capturedAt")),
        "compassAngle": optional_float(item.get("compassAngle")),
        "isPano": None if item.get("isPano") is None else bool(item.get("isPano")),
        "sequenceId": item.get("sequenceId"),
        "cameraType": item.get("cameraType"),
        "aiScore": optional_float(item.get("aiScore")),
        "aiBestCrop": item.get("aiBestCrop"),
        "aiModel": item.get("aiModel"),
        "aiMode": item.get("aiMode"),
        "aiImageType": item.get("aiImageType"),
        "aiThumbSize": optional_int(item.get("aiThumbSize")),
        "aiScoredAt": item.get("aiScoredAt"),
    }


def run_summary(row):
    return {
        "id": row["id"],
        "schemaVersion": row["schema_version"],
        "name": row["name"],
        "savedAt": row["saved_at"],
        "status": row["status"],
        "params": {
            "centerLat": row["center_lat"],
            "centerLng": row["center_lng"],
            "radiusM": row["radius_m"],
            "imageType": row["image_type"],
            "startCapturedAt": row["start_captured_at"] or "",
            "endCapturedAt": row["end_captured_at"] or "",
            "maxApiImages": row["max_api_images"],
        },
        "apiImagesSeen": row["api_images_seen"],
        "imageCount": row["image_count"],
        "aiRunStats": (json.loads(row["ai_run_stats_json"]) if "ai_run_stats_json" in row.keys() and row["ai_run_stats_json"] else None),
    }



def ai_environment_status():
    try:
        from ai_matcher import check_environment, cache_info
        info = check_environment()
        info["cache"] = cache_info(AI_CACHE_PATH)
        return info
    except Exception as exc:
        return {"ready": False, "message": str(exc), "missing": [], "cache": {"files": 0, "bytes": 0}}


def public_ai_job(job):
    if job is None:
        return None
    out = {k: v for k, v in job.items() if k not in {"cancelEvent"}}
    return out


def update_ai_job(job_id, **changes):
    with AI_JOBS_LOCK:
        job = AI_JOBS.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updatedAt"] = utc_now_iso()


def ai_worker(job_id, images, token, settings, target_path):
    worker_started = time.perf_counter()
    started_at = utc_now_iso()
    update_ai_job(job_id, status="running", startedAt=started_at)
    try:
        from ai_matcher import RankSettings, rank_images

        def progress(data):
            update_ai_job(job_id, progress=data, status="running")

        with AI_JOBS_LOCK:
            cancel_event = AI_JOBS[job_id]["cancelEvent"]
        rank_settings = RankSettings(
            mode=str(settings.get("mode") or "fast"),
            thumb_size=int(settings.get("thumbSize") or 1024),
            device=str(settings.get("device") or "auto"),
            max_images=int(settings.get("maxImages") or 0),
            keep_cache=bool(settings.get("keepCache", True)),
        )
        result = rank_images(
            images=images,
            token=token,
            target_path=target_path,
            cache_root=AI_CACHE_PATH,
            settings=rank_settings,
            progress=progress,
            cancel_event=cancel_event,
        )
        image_type = str(settings.get("imageType") or "all")
        finished_at = utc_now_iso()
        result["imageType"] = image_type
        result["startedAt"] = started_at
        result["finishedAt"] = finished_at
        for score in result.get("scores") or []:
            score["imageType"] = image_type
        elapsed_sec = float(result.get("elapsedSec") or (time.perf_counter() - worker_started))
        update_ai_job(
            job_id,
            status=result.get("status", "complete"),
            result=result,
            progress={
                "phase": "complete",
                "message": f"Scored {result.get('scored', 0):,} image(s); {result.get('failed', 0):,} failed.",
                "completed": result.get("scored", 0) + result.get("failed", 0),
                "total": result.get("requested", len(images)),
                "failed": result.get("failed", 0),
                "cacheHits": result.get("cacheHits", 0),
                "elapsedSec": elapsed_sec,
                "imagesPerSec": (float(result.get("scored", 0)) / elapsed_sec) if elapsed_sec > 0 else None,
                "device": result.get("device"),
            },
            elapsedSec=elapsed_sec,
            finishedAt=finished_at,
        )
    except Exception as exc:
        finished_at = utc_now_iso()
        update_ai_job(
            job_id,
            status="error",
            error=str(exc),
            elapsedSec=max(0.0, time.perf_counter() - worker_started),
            finishedAt=finished_at,
        )


def start_ai_job(images, settings, token):
    target_path = current_target_path()
    if target_path is None:
        raise ValueError("Choose and upload a target image before starting AI ranking.")
    if not isinstance(images, list) or not images:
        raise ValueError("A current run with at least one image is required.")
    settings = dict(settings or {})
    image_type = str(settings.get("imageType") or "all").lower()
    if image_type not in {"all", "pano", "flat"}:
        raise ValueError("AI image type must be all, pano, or flat.")
    settings["imageType"] = image_type
    if image_type == "pano":
        images = [im for im in images if im.get("isPano") is True]
        if not images:
            raise ValueError("No panorama images are available in the current candidate set. Select Flat images or Both.")
    elif image_type == "flat":
        images = [im for im in images if im.get("isPano") is False]
        if not images:
            raise ValueError("No flat images are available in the current candidate set. Select Panoramas or Both.")
    with AI_JOBS_LOCK:
        active = [v for v in AI_JOBS.values() if v.get("status") in {"queued", "running"}]
        if active:
            raise ValueError(f"An AI ranking job is already active ({active[0]['id']}). Cancel or wait for it to finish.")
    job_id = str(uuid.uuid4())
    cancel_event = threading.Event()
    job = {
        "id": job_id,
        "status": "queued",
        "createdAt": utc_now_iso(),
        "startedAt": None,
        "finishedAt": None,
        "elapsedSec": None,
        "updatedAt": utc_now_iso(),
        "settings": settings,
        "progress": {"phase": "queued", "message": "AI job queued.", "completed": 0, "total": min(len(images), int(settings.get("maxImages") or 0)) if int(settings.get("maxImages") or 0) > 0 else len(images)},
        "result": None,
        "error": None,
        "cancelEvent": cancel_event,
    }
    with AI_JOBS_LOCK:
        finished = [k for k, v in AI_JOBS.items() if v.get("status") in {"complete", "cancelled", "error"}]
        for old in finished[:-8]:
            AI_JOBS.pop(old, None)
        AI_JOBS[job_id] = job
    thread = threading.Thread(target=ai_worker, args=(job_id, images, token, settings, target_path), daemon=True, name=f"ai-rank-{job_id[:8]}")
    thread.start()
    return public_ai_job(job)


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "ImageGeolocationEstimationApp/2.4"

    def end_headers(self):
        path = urlparse(self.path).path.lower()
        if path == "/" or path.endswith((".html", ".js", ".css", ".json")):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)

    def _api_path(self):
        return unquote(urlparse(self.path).path)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._send_json({"error": str(message)}, status)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            raise ValueError("Request body is required.")
        if n > MAX_JSON_BYTES:
            raise OverflowError("Request body is too large.")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        path = self._api_path()
        if path == "/api/target":
            return self._send_json(public_target_status())
        if path == "/api/target/image":
            meta = load_target_meta()
            if not meta:
                return self._error("No target image has been uploaded.", 404)
            target_path = meta["path"]
            raw = target_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", meta.get("contentType") or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/settings":
            token = load_access_token()
            return self._send_json({
                "accessToken": token,
                "tokenConfigured": bool(token),
                "database": DB_PATH.name,
            })
        if path == "/api/runs":
            return self._list_runs()
        if path.startswith("/api/runs/"):
            rid = path[len("/api/runs/"):]
            if not rid or "/" in rid:
                return self._error("Invalid run ID.")
            return self._get_run(rid)
        if path == "/api/shortlist":
            return self._get_shortlist()
        if path.startswith("/api/image/"):
            image_id = path[len("/api/image/"):]
            if not image_id or "/" in image_id:
                return self._error("Invalid image ID.")
            return self._image_metadata(image_id)
        if path == "/api/large-area/jobs":
            return self._send_json({"jobs": list_large_area_jobs()})
        if path.startswith("/api/large-area/jobs/"):
            rest = path[len("/api/large-area/jobs/"):].strip("/")
            parts = rest.split("/") if rest else []
            if not parts or not parts[0]:
                return self._error("Invalid Large Area Search job ID.")
            job_id = parts[0]
            if len(parts) == 1:
                data = large_area_job_summary(job_id, include_cells=True)
                return self._send_json(data) if data is not None else self._error("Large Area Search job not found.", 404)
            if len(parts) == 2 and parts[1] == "results":
                data = large_area_results(job_id)
                return self._send_json(data) if data is not None else self._error("Large Area Search job not found.", 404)
            return self._error("Unknown Large Area Search endpoint.", 404)
        if path == "/api/storage":
            return self._send_json({"database": DB_PATH.name, "path": str(DB_PATH)})
        if path == "/api/ai/status":
            return self._send_json(ai_environment_status())
        if path.startswith("/api/ai/jobs/"):
            job_id = path[len("/api/ai/jobs/"):]
            with AI_JOBS_LOCK:
                job = AI_JOBS.get(job_id)
                if job is None:
                    return self._error("AI job not found.", 404)
                data = public_ai_job(job)
            return self._send_json(data)
        return super().do_GET()

    def do_POST(self):
        path = self._api_path()
        try:
            if path == "/api/target":
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n <= 0:
                    return self._error("Target image upload is empty.", 400)
                if n > MAX_TARGET_BYTES:
                    return self._error("Target image is too large. Maximum size is 30 MB.", 413)
                raw = self.rfile.read(n)
                filename = unquote(self.headers.get("X-Filename", "target"))
                result = save_target_upload(raw, self.headers.get("Content-Type", ""), filename)
                return self._send_json(result, 201)
            payload = self._read_json()
            if path == "/api/discover":
                token = load_access_token()
                if not token:
                    return self._error("Mapillary access token is not configured. Set MAPILLARY_ACCESS_TOKEN or edit config.json.", 400)
                radius_m = float(payload.get("radiusM") or 0) if isinstance(payload, dict) else 0
                if radius_m > LARGE_AREA_CELL_RADIUS_M:
                    return self._error(f"Standard Search is limited to {int(LARGE_AREA_CELL_RADIUS_M):,} meters. Use Large Area Search for a larger radius.", 400)
                return self._send_json(discover_images(payload, token))
            if path == "/api/large-area/jobs/start":
                token = load_access_token()
                if not token:
                    return self._error("Mapillary access token is not configured. Set MAPILLARY_ACCESS_TOKEN or edit config.json.", 400)
                job_id = create_large_area_job(payload)
                start_large_area_worker(job_id)
                return self._send_json(large_area_job_summary(job_id, include_cells=True), 202)
            if path.startswith("/api/large-area/jobs/"):
                rest = path[len("/api/large-area/jobs/"):].strip("/")
                parts = rest.split("/") if rest else []
                if len(parts) != 2:
                    return self._error("Invalid Large Area Search action.", 404)
                job_id, action = parts
                summary = large_area_job_summary(job_id, include_cells=False)
                if summary is None:
                    return self._error("Large Area Search job not found.", 404)
                if action == "pause":
                    with connect_db() as conn:
                        conn.execute("UPDATE large_area_jobs SET pause_requested=1,status=CASE WHEN status='running' THEN 'pausing' ELSE status END,updated_at=? WHERE id=?", (utc_now_iso(), job_id))
                    return self._send_json({"ok": True, "jobId": job_id, "message": "Pause requested; the current cell will finish first."})
                if action == "cancel":
                    with connect_db() as conn:
                        conn.execute("UPDATE large_area_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (utc_now_iso(), job_id))
                    return self._send_json({"ok": True, "jobId": job_id, "message": "Cancel requested; the current cell will finish first."})
                if action == "resume":
                    with connect_db() as conn:
                        conn.execute("UPDATE large_area_jobs SET pause_requested=0,cancel_requested=0,error=NULL,status='pending',updated_at=? WHERE id=?", (utc_now_iso(), job_id))
                    start_large_area_worker(job_id)
                    return self._send_json(large_area_job_summary(job_id, include_cells=True), 202)
                if action == "retry-failed":
                    with connect_db() as conn:
                        conn.execute("UPDATE large_area_cells SET status='pending',error=NULL,finished_at=NULL WHERE job_id=? AND status='failed'", (job_id,))
                        conn.execute("UPDATE large_area_jobs SET pause_requested=0,cancel_requested=0,error=NULL,status='pending',updated_at=? WHERE id=?", (utc_now_iso(), job_id))
                    start_large_area_worker(job_id)
                    return self._send_json(large_area_job_summary(job_id, include_cells=True), 202)
                return self._error("Unknown Large Area Search action.", 404)
            if path == "/api/runs":
                return self._save_run(validate_run(payload))
            if path == "/api/shortlist/add":
                return self._shortlist_add(validate_shortlist_item(payload))
            if path == "/api/shortlist/remove":
                return self._shortlist_remove(validate_shortlist_item(payload)["id"])
            if path == "/api/shortlist/clear":
                return self._shortlist_clear()
            if path == "/api/ai/start":
                token = load_access_token()
                if not token:
                    return self._error("Mapillary access token is not configured.", 400)
                images = payload.get("images") if isinstance(payload, dict) else None
                settings = payload.get("settings") if isinstance(payload, dict) else None
                if not isinstance(settings, dict):
                    settings = {}
                return self._send_json(start_ai_job(images, settings, token), 202)
            if path == "/api/ai/cancel":
                job_id = str(payload.get("jobId") or "") if isinstance(payload, dict) else ""
                with AI_JOBS_LOCK:
                    job = AI_JOBS.get(job_id)
                    if job is None:
                        return self._error("AI job not found.", 404)
                    job["cancelEvent"].set()
                    job["updatedAt"] = utc_now_iso()
                return self._send_json({"ok": True, "jobId": job_id})
            if path == "/api/ai/cache/clear":
                with AI_JOBS_LOCK:
                    if any(v.get("status") in {"queued", "running"} for v in AI_JOBS.values()):
                        return self._error("Cannot clear the AI cache while a ranking job is active.", 409)
                from ai_matcher import clear_cache
                before = clear_cache(AI_CACHE_PATH)
                return self._send_json({"ok": True, "removed": before})
            return self._error("Unknown API endpoint.", 404)
        except OverflowError as exc:
            return self._error(exc, 413)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._error(exc, 400)
        except RuntimeError as exc:
            return self._error(exc, 502)
        except sqlite3.IntegrityError as exc:
            return self._error(exc, 409)
        except Exception as exc:
            return self._error(exc, 500)

    def do_DELETE(self):
        path = self._api_path()
        if path.startswith("/api/large-area/jobs/"):
            job_id = path[len("/api/large-area/jobs/"):].strip("/")
            if not job_id or "/" in job_id:
                return self._error("Invalid Large Area Search job ID.")
            with LARGE_AREA_LOCK:
                thread = LARGE_AREA_WORKERS.get(job_id)
                if thread is not None and thread.is_alive():
                    return self._error("Pause or cancel the running Large Area Search before deleting it.", 409)
            with connect_db() as conn:
                cur = conn.execute("DELETE FROM large_area_jobs WHERE id=?", (job_id,))
                if cur.rowcount == 0:
                    return self._error("Large Area Search job not found.", 404)
            return self._send_json({"ok": True})
        if path.startswith("/api/runs/"):
            rid = path[len("/api/runs/"):]
            if not rid or "/" in rid:
                return self._error("Invalid run ID.")
            with connect_db() as conn:
                cur = conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
                if cur.rowcount == 0:
                    return self._error("Saved run not found.", 404)
            return self._send_json({"ok": True})
        return self._error("Unknown API endpoint.", 404)

    def _list_runs(self):
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT r.*, COUNT(i.image_number) AS image_count
                FROM runs r LEFT JOIN images i ON i.run_id = r.id
                GROUP BY r.id ORDER BY r.saved_at DESC
                """
            ).fetchall()
        return self._send_json({"runs": [run_summary(r) for r in rows]})

    def _get_run(self, rid):
        with connect_db() as conn:
            row = conn.execute(
                """
                SELECT r.*, COUNT(i.image_number) AS image_count
                FROM runs r LEFT JOIN images i ON i.run_id = r.id
                WHERE r.id = ? GROUP BY r.id
                """, (rid,)
            ).fetchone()
            if row is None:
                return self._error("Saved run not found.", 404)
            imgs = conn.execute(
                """
                SELECT image_id,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,
                       ai_score,ai_best_crop,ai_model,ai_mode,ai_image_type,ai_thumb_size,ai_scored_at
                FROM images WHERE run_id = ? ORDER BY image_number
                """, (rid,)
            ).fetchall()
        result = run_summary(row)
        result.pop("imageCount", None)
        result["images"] = [self._db_image_dict(i) for i in imgs]
        return self._send_json(result)

    def _save_run(self, run):
        p = run["params"]
        rid = str(run.get("id") or uuid.uuid4())
        name = str(run.get("name") or "Unnamed run").strip() or "Unnamed run"
        saved_at = str(run.get("savedAt") or utc_now_iso())
        images = run["images"]
        with connect_db() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (rid,))
            conn.execute(
                """
                INSERT INTO runs(id,schema_version,name,saved_at,status,center_lat,center_lng,radius_m,image_type,
                                 start_captured_at,end_captured_at,max_api_images,api_images_seen,ai_run_stats_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rid, 2, name, saved_at, run.get("status"), float(p["centerLat"]), float(p["centerLng"]),
                    float(p["radiusM"]), str(p.get("imageType") or "all"), str(p.get("startCapturedAt") or ""),
                    str(p.get("endCapturedAt") or ""), int(p.get("maxApiImages") or 20000), int(run.get("apiImagesSeen") or 0),
                    json.dumps(run.get("aiRunStats"), ensure_ascii=False, separators=(",", ":")) if run.get("aiRunStats") else None,
                )
            )
            conn.executemany(
                """
                INSERT INTO images(run_id,image_number,image_id,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,
                                   ai_score,ai_best_crop,ai_model,ai_mode,ai_image_type,ai_thumb_size,ai_scored_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        rid, idx + 1, str(im["id"]), float(im["lat"]), float(im["lng"]), float(im["distanceM"]),
                        optional_int(im.get("capturedAt")), optional_float(im.get("compassAngle")),
                        None if im.get("isPano") is None else (1 if im.get("isPano") else 0),
                        im.get("sequenceId"), im.get("cameraType"), optional_float(im.get("aiScore")),
                        im.get("aiBestCrop"), im.get("aiModel"), im.get("aiMode"), im.get("aiImageType"), optional_int(im.get("aiThumbSize")),
                        im.get("aiScoredAt"),
                    )
                    for idx, im in enumerate(images)
                ]
            )
        return self._send_json({"ok": True, "id": rid, "name": name, "imageCount": len(images)})

    def _get_shortlist(self):
        with connect_db() as conn:
            rows = conn.execute(
                """
                SELECT image_id,added_at,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,
                       ai_score,ai_best_crop,ai_model,ai_mode,ai_image_type,ai_thumb_size,ai_scored_at
                FROM shortlist ORDER BY added_at DESC
                """
            ).fetchall()
        items = []
        for r in rows:
            d = self._db_image_dict(r)
            d["addedAt"] = r["added_at"]
            items.append(d)
        return self._send_json({"items": items})

    def _shortlist_add(self, item):
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO shortlist(image_id,added_at,lat,lng,distance_m,captured_at,compass_angle,is_pano,sequence_id,camera_type,
                                      ai_score,ai_best_crop,ai_model,ai_mode,ai_image_type,ai_thumb_size,ai_scored_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(image_id) DO UPDATE SET
                    lat=excluded.lat,lng=excluded.lng,distance_m=excluded.distance_m,captured_at=excluded.captured_at,
                    compass_angle=excluded.compass_angle,is_pano=excluded.is_pano,sequence_id=excluded.sequence_id,camera_type=excluded.camera_type,
                    ai_score=excluded.ai_score,ai_best_crop=excluded.ai_best_crop,ai_model=excluded.ai_model,ai_mode=excluded.ai_mode,
                    ai_image_type=excluded.ai_image_type,ai_thumb_size=excluded.ai_thumb_size,ai_scored_at=excluded.ai_scored_at
                """,
                (
                    item["id"], utc_now_iso(), item["lat"], item["lng"], item["distanceM"], item["capturedAt"],
                    item["compassAngle"], None if item["isPano"] is None else (1 if item["isPano"] else 0),
                    item["sequenceId"], item["cameraType"], item["aiScore"], item["aiBestCrop"], item["aiModel"],
                    item["aiMode"], item["aiImageType"], item["aiThumbSize"], item["aiScoredAt"],
                )
            )
        return self._send_json({"ok": True})

    def _shortlist_remove(self, image_id):
        with connect_db() as conn:
            conn.execute("DELETE FROM shortlist WHERE image_id = ?", (image_id,))
        return self._send_json({"ok": True})

    def _shortlist_clear(self):
        with connect_db() as conn:
            conn.execute("DELETE FROM shortlist")
        return self._send_json({"ok": True})

    def _image_metadata(self, image_id):
        token = load_access_token()
        if not token:
            return self._error("Mapillary access token is not configured.", 400)
        fields = "id,geometry,captured_at,compass_angle,is_pano,sequence,camera_type,creator,thumb_1024_url,thumb_2048_url"
        url = f"{GRAPH_URL}/{image_id}?" + urlencode({"fields": fields}, safe=",")
        try:
            data = graph_get(url, token)
            return self._send_json(data)
        except RuntimeError as exc:
            return self._error(exc, 502)

    @staticmethod
    def _db_image_dict(r):
        return {
            "id": str(r["image_id"]),
            "lat": r["lat"],
            "lng": r["lng"],
            "distanceM": r["distance_m"],
            "capturedAt": r["captured_at"],
            "compassAngle": r["compass_angle"],
            "isPano": None if r["is_pano"] is None else bool(r["is_pano"]),
            "sequenceId": r["sequence_id"],
            "cameraType": r["camera_type"],
            "aiScore": r["ai_score"] if "ai_score" in r.keys() else None,
            "aiBestCrop": r["ai_best_crop"] if "ai_best_crop" in r.keys() else None,
            "aiModel": r["ai_model"] if "ai_model" in r.keys() else None,
            "aiMode": r["ai_mode"] if "ai_mode" in r.keys() else None,
            "aiImageType": r["ai_image_type"] if "ai_image_type" in r.keys() else None,
            "aiThumbSize": r["ai_thumb_size"] if "ai_thumb_size" in r.keys() else None,
            "aiScoredAt": r["ai_scored_at"] if "ai_scored_at" in r.keys() else None,
        }


def main():
    parser = argparse.ArgumentParser(description="Image Geolocation Estimation App")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    init_db()
    token = load_access_token()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://localhost:{args.port}/"
    print(f"Serving {ROOT}")
    print(f"Saved-run/shortlist database: {DB_PATH}")
    print(f"Mapillary token configured: {'yes' if token else 'NO'}")
    print(f"Open: {url}")
    if not token:
        print("Configure MAPILLARY_ACCESS_TOKEN or edit config.json before discovering/viewing images.")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
