#!/bin/bash

#kill any process using port 8000
kill -9 $(lsof -t -i:8000) 

# Folder where all photos are extracted
EXTRACTED_DIR="extracted_photos"

# Check if folder exists
if [ ! -d "$EXTRACTED_DIR" ]; then
    echo "Extracted photos folder not found. Running unzip_all.py..."
    # Activate virtual environment
    source venv/bin/activate
    python unzip_all.py
    echo "Unzipping and HEIC conversion done!"
fi

echo "Starting FastAPI app..."
# Activate virtual environment
source venv/bin/activate
uvicorn app.main:app --reload
