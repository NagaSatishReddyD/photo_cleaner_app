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

try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent.parent
EXTRACTED_DIR = BASE_DIR / "extracted_photos"
FILTERED_DIR = BASE_DIR / "filtered_photos"

EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
FILTERED_DIR.mkdir(exist_ok=True)

app.mount("/extracted_photos", StaticFiles(directory=str(EXTRACTED_DIR)), name="extracted_photos")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

progress_lock = threading.Lock()
groups_lock = threading.Lock()
progress_data = {"processed": 0, "total": 0, "status": "idle"}
groups_global: List[Dict[str, Any]] = []


# -------------------- UTILITIES --------------------

def is_media_file(p: Path) -> bool:
    return p.suffix.lower() in {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
        ".mp4", ".mov", ".avi", ".mkv",
        ".heic", ".heif"
    }


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(p: Path) -> str:
    return str(p.relative_to(EXTRACTED_DIR).as_posix())


def get_image_resolution(path: Path):
    if Image is None:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def pick_best_in_group(paths: List[Path]) -> Path:
    best = None
    best_score = None
    for p in paths:
        w, h = get_image_resolution(p)
        res = w * h
        size = p.stat().st_size if p.exists() else 0
        score = (res, size)
        if best is None or score > best_score:
            best = p
            best_score = score
    return best


# -------------------- SCAN & GROUP --------------------

def scan_and_group():
    global groups_global

    print("[scan] started")

    with progress_lock:
        progress_data.update({"processed": 0, "total": 0, "status": "running"})

    all_files: List[Path] = []
    for root, _, files in os.walk(EXTRACTED_DIR):
        for name in files:
            p = Path(root) / name
            if is_media_file(p):
                all_files.append(p)

    with progress_lock:
        progress_data["total"] = len(all_files)

    # -------------------- HASHING --------------------
    hash_map: Dict[str, List[Path]] = {}
    for p in all_files:
        try:
            h = compute_sha256(p)
        except Exception:
            continue
        hash_map.setdefault(h, []).append(p)

        with progress_lock:
            progress_data["processed"] += 1

    # -------------------- AUTO DELETE: SAME NAME + SAME HASH --------------------
    cleaned_hash_map: Dict[str, List[Path]] = {}

    for h, paths in hash_map.items():
        name_map: Dict[str, List[Path]] = {}

        for p in paths:
            name_map.setdefault(p.name.lower(), []).append(p)

        final_paths = []

        for name, same_name_files in name_map.items():
            if len(same_name_files) > 1:
                # PERMANENT DELETE ALL BUT ONE
                keeper = same_name_files[0]
                final_paths.append(keeper)

                for dup in same_name_files[1:]:
                    try:
                        dup.unlink()
                        print(f"[auto-deleted exact duplicate] {dup}")
                    except Exception as e:
                        print(f"[delete failed] {dup} | {e}")
            else:
                final_paths.append(same_name_files[0])

        if final_paths:
            cleaned_hash_map[h] = final_paths

    # -------------------- BUILD GROUPS --------------------
    groups: List[Dict[str, Any]] = []

    for h, group_paths in cleaned_hash_map.items():
        if not group_paths:
            continue

        best = pick_best_in_group(group_paths)
        ordered = [safe_rel(best)] + [safe_rel(p) for p in group_paths if p != best]

        groups.append({
            "best": safe_rel(best),
            "files": ordered
        })

    groups.sort(key=lambda g: len(g["files"]), reverse=True)

    with groups_lock:
        groups_global = groups

    with progress_lock:
        progress_data["status"] = "done"

    print(f"[scan] done | groups={len(groups_global)}")


# -------------------- ROUTES --------------------

@app.get("/")
async def index():
    return FileResponse(Path(__file__).resolve().parent / "templates" / "index.html")


@app.get("/start_scan")
async def start_scan():
    with progress_lock:
        if progress_data["status"] == "running":
            return {"status": "already running"}
        progress_data.update({"processed": 0, "total": 0, "status": "starting"})

    threading.Thread(target=scan_and_group, daemon=True).start()
    return {"status": "started"}


@app.get("/progress")
async def progress_sse():
    async def gen():
        last = (-1, -1)
        while True:
            with progress_lock:
                p = progress_data["processed"]
                t = progress_data["total"]
                status = progress_data["status"]

            if (p, t) != last:
                last = (p, t)
                yield f"data:{p}/{t}\n\n"

            if status == "done":
                break

            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/groups")
async def api_groups():
    with groups_lock:
        return JSONResponse(groups_global)


# -------------------- DELETE / MOVE --------------------

@app.post("/api/delete_file")
async def delete_file(payload: Dict[str, Any]):
    rel = payload.get("path")
    if not rel:
        raise HTTPException(status_code=400)

    tgt = EXTRACTED_DIR / rel
    if tgt.exists():
        tgt.unlink()

    return {"status": "ok"}


@app.post("/api/delete_group")
async def delete_group(payload: Dict[str, Any]):
    paths = payload.get("paths", [])
    for rel in paths:
        p = EXTRACTED_DIR / rel
        if p.exists():
            p.unlink()
    return {"status": "ok"}


@app.post("/api/move_group")
async def move_group(payload: Dict[str, Any]):
    paths = payload.get("paths", [])

    for rel in paths:
        src = EXTRACTED_DIR / rel
        dst = FILTERED_DIR / src.name
        if src.exists():
            shutil.move(str(src), str(dst))

    return {"status": "ok"}
