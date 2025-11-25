#!/usr/bin/env python3
import os
import zipfile
import logging
import subprocess
from pathlib import Path
import shutil

# -------------------------------
# Logging Setup
# -------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

PHOTOS_DIR = Path("photos")
EXTRACT_DIR = Path("extracted_photos")
PROBLEM_DIR = EXTRACT_DIR / "problem_files"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}

HEIC_EXT = {".heic", ".HEIC"}

# -------------------------------
# Convert HEIC using heif-convert
# -------------------------------
def convert_heic(src: Path):
    dst = src.with_suffix(".jpg")

    try:
        os.makedirs(dst.parent, exist_ok=True)

        result = subprocess.run(
            ["heif-convert", str(src), str(dst)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logging.error(f"Failed to convert {src}: {result.stderr.strip()}")
            shutil.move(src, PROBLEM_DIR / src.name)
            return False

        logging.info(f"Converted {src} → {dst}")
        src.unlink(missing_ok=True)
        return True

    except Exception as e:
        logging.error(f"HEIC conversion error: {src} → {e}")
        shutil.move(src, PROBLEM_DIR / src.name)
        return False


# -------------------------------
# Extract a ZIP file
# -------------------------------
def extract_zip(zip_path: Path):
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(EXTRACT_DIR)
        logging.info(f"Unzipped: {zip_path}")
    except Exception as e:
        logging.error(f"Failed to unzip {zip_path}: {e}")
        shutil.move(zip_path, PROBLEM_DIR / zip_path.name)


# -------------------------------
# Process extracted files
# -------------------------------
def normalize_files():
    for root, _, files in os.walk(EXTRACT_DIR):
        for f in files:
            file_path = Path(root) / f
            ext = file_path.suffix.lower()

            # Ignore folders
            if file_path.is_dir():
                continue

            # Skip processed problem files
            if PROBLEM_DIR in file_path.parents:
                continue

            # HEIC files
            if ext in HEIC_EXT:
                convert_heic(file_path)
                continue

            # Genuine images/videos
            if ext in IMAGE_EXT or ext in VIDEO_EXT:
                continue  # valid file, keep it

            # Unknown / metadata / corrupted
            logging.warning(f"Unknown file type → moving: {file_path}")
            PROBLEM_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(file_path, PROBLEM_DIR / file_path.name)


# -------------------------------
# Main Function
# -------------------------------
if __name__ == "__main__":
    EXTRACT_DIR.mkdir(exist_ok=True)
    PROBLEM_DIR.mkdir(parents=True, exist_ok=True)

    # Process ZIP files in /photos
    for file in PHOTOS_DIR.iterdir():
        if file.suffix.lower() == ".zip":
            extract_zip(file)

        # Also move loose files if they are recognised
        elif file.is_file():
            ext = file.suffix.lower()

            if ext in IMAGE_EXT or ext in VIDEO_EXT or ext in HEIC_EXT:
                dest = EXTRACT_DIR / file.name
                shutil.copy(file, dest)
                logging.info(f"Copied loose file → {dest}")

            else:
                shutil.move(file, PROBLEM_DIR / file.name)
                logging.warning(f"Unknown file in photos/ → moved to problem: {file}")

    # Normalize extracted contents
    normalize_files()

    logging.info("Unzip and normalization complete.")
