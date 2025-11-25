#!/usr/bin/env python3
# app/main.py

import os
import time
import threading
import shutil
import hashlib
import asyncio
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# PIL optional for resolution/EXIF
try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent.parent  # project root
EXTRACTED_DIR = BASE_DIR / "extracted_photos"
PROBLEM_DIR = EXTRACTED_DIR / "problem_files"
FILTERED_DIR = BASE_DIR / "filtered_photos"
FILTERED_DIR.mkdir(exist_ok=True)

# Ensure EXTRACTED_DIR exists for StaticFiles
EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)

# Mount static for served images/videos
app.mount("/extracted_photos", StaticFiles(directory=str(EXTRACTED_DIR)), name="extracted_photos")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# State & locks
progress_lock = threading.Lock()
groups_lock = threading.Lock()
progress_data = {"processed": 0, "total": 0, "status": "idle"}  # idle/running/done
groups_global: List[Dict[str, Any]] = []  # each group: {"best": rel, "files": [rel,...]}


# Utilities
def is_media_file(p: Path) -> bool:
    ext = p.suffix.lower()
    return ext in {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
        ".mp4", ".mov", ".avi", ".mkv",
        ".heic", ".heif", ".jpg.org"
    }


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_image_resolution(path: Path):
    if Image is None:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return im.size  # (width, height)
    except Exception:
        return (0, 0)


def get_exif_datetime(path: Path):
    if Image is None or ExifTags is None:
        return None
    try:
        with Image.open(path) as im:
            exif = im._getexif()
            if not exif:
                return None
            # find DateTimeOriginal then DateTime
            for key, name in ExifTags.TAGS.items():
                if name == "DateTimeOriginal" and key in exif:
                    return exif.get(key)
            for key, name in ExifTags.TAGS.items():
                if name == "DateTime" and key in exif:
                    return exif.get(key)
    except Exception:
        return None
    return None


def safe_rel(p: Path) -> str:
    try:
        return str(p.relative_to(EXTRACTED_DIR).as_posix())
    except Exception:
        return str(p.as_posix())


def pick_best_in_group(paths: List[Path]) -> Path:
    """Pick best by resolution -> size -> exif datetime (newer)."""
    best = None
    best_score = None
    for p in paths:
        w, h = get_image_resolution(p)
        res = w * h
        size = p.stat().st_size if p.exists() else 0
        exif_dt = get_exif_datetime(p) or ""
        score = (res, size, exif_dt)
        if best is None or score > best_score:
            best = p
            best_score = score
    return best


# Scanning & grouping (background)
def scan_and_group():
    global groups_global
    print("[scan] start")
    with progress_lock:
        progress_data["processed"] = 0
        progress_data["total"] = 0
        progress_data["status"] = "running"

    # collect files
    files: List[Path] = []
    for root, _, fnames in os.walk(str(EXTRACTED_DIR)):
        rootp = Path(root)
        # skip problem_files folder
        if PROBLEM_DIR.exists() and (PROBLEM_DIR == rootp or PROBLEM_DIR in rootp.parents):
            continue
        for fn in fnames:
            p = rootp / fn
            if is_media_file(p):
                files.append(p)

    total = len(files)
    with progress_lock:
        progress_data["total"] = total
        progress_data["processed"] = 0

    # compute hashes
    hash_map: Dict[str, List[Path]] = {}
    for p in files:
        try:
            h = compute_sha256(p)
        except Exception:
            h = f"ERR_{p.name}_{int(time.time())}"
        hash_map.setdefault(h, []).append(p)
        with progress_lock:
            progress_data["processed"] += 1

    # Build groups - include singletons (size 1)
    groups: List[Dict[str, Any]] = []
    for h, group_paths in hash_map.items():
        if not group_paths:
            continue
        try:
            best_path = pick_best_in_group(group_paths)
        except Exception:
            best_path = group_paths[0]
        ordered = [safe_rel(best_path)] + [safe_rel(p) for p in group_paths if p != best_path]
        groups.append({"best": safe_rel(best_path), "files": ordered})

    groups.sort(key=lambda g: len(g["files"]), reverse=True)

    with groups_lock:
        groups_global = groups

    with progress_lock:
        progress_data["status"] = "done"
        progress_data["processed"] = progress_data.get("total", 0)
    print(f"[scan] done groups={len(groups_global)}")


# Routes
@app.get("/")
async def index():
    idx = Path(__file__).resolve().parent / "templates" / "index.html"
    if not idx.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(idx)


@app.get("/start_scan")
async def start_scan():
    with progress_lock:
        if progress_data.get("status") == "running":
            return JSONResponse({"status": "running"})
        progress_data["processed"] = 0
        progress_data["total"] = 0
        progress_data["status"] = "starting"
    thread = threading.Thread(target=scan_and_group, daemon=True)
    thread.start()
    return JSONResponse({"status": "started"})


@app.get("/progress")
async def progress_sse():
    async def gen():
        last = (-1, -1)
        while True:
            with progress_lock:
                processed = progress_data.get("processed", 0)
                total = progress_data.get("total", 0)
                status = progress_data.get("status", "idle")
            if (processed, total) != last:
                last = (processed, total)
                yield f"data:{processed}/{total}\n\n"
            if status == "done":
                yield f"data:{processed}/{total}\n\n"
                break
            await asyncio.sleep(0.12)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/groups")
async def api_groups():
    with groups_lock:
        return JSONResponse(list(groups_global))


@app.get("/api/debug")
async def api_debug():
    sample = []
    total_spotted = 0
    for root, _, fnames in os.walk(EXTRACTED_DIR):
        total_spotted += len(fnames)
        for fn in fnames[:50]:
            sample.append(os.path.relpath(os.path.join(root, fn), EXTRACTED_DIR))
    return JSONResponse({"exists": EXTRACTED_DIR.exists(), "total_spotted": total_spotted, "sample": sample})


# POST endpoints that perform actions in ONE request (avoid per-file confirmations)
@app.post("/api/move_group")
async def api_move_group(payload: Dict[str, Any]):
    """
    payload: {"paths": ["rel1","rel2"...], "target": "optional/subdir"}
    Moves all paths to FILTERED_DIR/target in one operation. Returns moved list.
    """
    paths = payload.get("paths")
    target = payload.get("target", "") or ""
    if not isinstance(paths, list) or len(paths) == 0:
        raise HTTPException(status_code=400, detail="paths must be non-empty list")
    dest = FILTERED_DIR / target
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for rel in paths:
        src = EXTRACTED_DIR / Path(rel)
        if src.exists():
            dst = dest / src.name
            if dst.exists():
                base = dst.stem
                ext = dst.suffix
                i = 1
                while True:
                    cand = dest / f"{base}_{i}{ext}"
                    if not cand.exists():
                        dst = cand
                        break
                    i += 1
            shutil.move(str(src), str(dst))
            moved.append(rel)
    # update groups_global (remove moved)
    with groups_lock:
        new_groups = []
        for g in groups_global:
            new_files = [p for p in g["files"] if p not in moved]
            if new_files:
                best = g["best"] if g["best"] not in moved else (new_files[0] if new_files else None)
                if best and best in new_files:
                    new_files = [best] + [x for x in new_files if x != best]
                new_groups.append({"best": best, "files": new_files})
        groups_global[:] = new_groups
    return JSONResponse({"status": "ok", "moved": moved})


@app.post("/api/delete_group")
async def api_delete_group(payload: Dict[str, Any]):
    """
    payload: {"paths": ["rel1","rel2",...]}
    Delete all files in one request. Returns deleted list.
    """
    paths = payload.get("paths")
    if not isinstance(paths, list) or len(paths) == 0:
        raise HTTPException(status_code=400, detail="paths must be non-empty list")
    deleted = []
    for rel in paths:
        tgt = EXTRACTED_DIR / Path(rel)
        if tgt.exists():
            try:
                tgt.unlink()
                deleted.append(rel)
            except Exception:
                pass
    # update groups_global
    with groups_lock:
        new_groups = []
        for g in groups_global:
            new_files = [p for p in g["files"] if p not in deleted]
            if new_files:
                best = g["best"]
                if best in deleted:
                    best = new_files[0]
                if best in new_files:
                    new_files = [best] + [x for x in new_files if x != best]
                new_groups.append({"best": best, "files": new_files})
        groups_global[:] = new_groups
    return JSONResponse({"status": "ok", "deleted": deleted})


@app.post("/api/delete_file")
async def api_delete_file(payload: Dict[str, Any]):
    """
    payload: {"path": "rel/path.jpg"}
    Deletes single file.
    """
    rel = payload.get("path")
    if not rel:
        raise HTTPException(status_code=400, detail="missing path")
    tgt = EXTRACTED_DIR / Path(rel)
    deleted = []
    if tgt.exists():
        try:
            tgt.unlink()
            deleted.append(rel)
        except Exception:
            pass
    # update groups_global
    with groups_lock:
        new_groups = []
        for g in groups_global:
            new_files = [p for p in g["files"] if p != rel]
            if new_files:
                best = g["best"]
                if best == rel:
                    best = new_files[0]
                if best in new_files:
                    new_files = [best] + [x for x in new_files if x != best]
                new_groups.append({"best": best, "files": new_files})
        groups_global[:] = new_groups
    return JSONResponse({"status": "ok", "deleted": deleted})
