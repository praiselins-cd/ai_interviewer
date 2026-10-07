#!/bin/sh
set -e

# One-time interactive setup: a virtual display plus a VNC server (and a
# browser-based noVNC front end on top of it, so no VNC client needs to be
# installed on the host) so a human can see and control the Chromium window
# from outside the container to sign in by hand -- see
# app/bot/login_setup.py's own docstring for the sign-in steps themselves.
# Not used for normal operation (that's docker-entrypoint.sh).

Xvfb :99 -screen 0 1440x900x24 &
XVFB_PID=$!
sleep 1

# No password (-nopw): fine for a one-time local setup step behind a port
# you control; never expose this port beyond your own machine.
x11vnc -display :99 -nopw -forever -shared &
VNC_PID=$!

websockify --web=/usr/share/novnc 6080 localhost:5900 &
NOVNC_PID=$!

cleanup() {
    kill "$NOVNC_PID" "$VNC_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Open http://localhost:6080/vnc.html in your own browser to sign in."
exec python -m app.bot.login_setup
