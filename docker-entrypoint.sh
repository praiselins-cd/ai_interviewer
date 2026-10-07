#!/bin/sh
set -e

# Xvfb :99 -- harmless when engine.py launches headless=True (the normal
# app path), kept only so this entrypoint stays parallel to
# docker-entrypoint-login.sh, which genuinely needs it.
Xvfb :99 -screen 0 1440x900x24 &
XVFB_PID=$!
sleep 1

cleanup() {
    kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
