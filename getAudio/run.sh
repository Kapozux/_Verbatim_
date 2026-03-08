#!/bin/bash
cd /Users/kapozux/Documents/CODEelse/getAudio

# Load environment variables from .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

source ../venv/bin/activate
python app.py
