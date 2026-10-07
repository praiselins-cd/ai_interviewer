FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        xvfb x11vnc novnc websockify \
        chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

# NOTE: we deliberately do NOT use Playwright's own bundled "Chrome for
# Testing" binary here -- confirmed via chrome://policy (--dump-dom) that it
# silently ignores the managed-policy file below (empty "Applied policies"
# table), since it's a Google testing-automation build, not a distro package
# built with enterprise-policy support compiled in. The real Debian
# `chromium` package above does read /etc/chromium/policies/managed/, the
# same mechanism as the Windows registry URLBlocklist fix for real Chrome.
ENV BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

# Chromium (unbranded) reads managed enterprise policy from this path on
# Linux -- the exact analogue of the Windows registry URLBlocklist fix
# already confirmed working for real Chrome, suppresses the native
# "Open Microsoft Teams?" external-app-launch prompt.
COPY chromium-policy.json /etc/chromium/policies/managed/policy.json

COPY app ./app
COPY docker-entrypoint.sh docker-entrypoint-login.sh ./
RUN chmod +x docker-entrypoint.sh docker-entrypoint-login.sh

EXPOSE 8001 5900 6080

ENTRYPOINT ["./docker-entrypoint.sh"]
