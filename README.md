# AI Interviewer

An AI-driven interview bot that joins Microsoft Teams meetings, conducts interviews using Azure OpenAI (including a realtime voice API), and manages interview scheduling and evaluation.

## Project Structure

```
app/
  api/            API layer
  bot/            Teams meeting bot (browser automation via Playwright)
  db/             Database access
  evaluation/     Interview evaluation logic
  infra/          Infrastructure helpers
  integrations/   Third-party integrations
  realtime/       Realtime API (voice) handling
  repositories/   Data repositories
  scheduling/     Interview scheduling
  services/       Core business logic (e.g. ai_interview_service.py)
  state/          State management
  worker/         Background worker/state-machine
```

## Prerequisites

- Python 3.10+
- PostgreSQL
- Redis
- Microsoft Teams app credentials (client ID/secret, tenant ID)
- Azure OpenAI resource (chat + realtime deployments)

## Setup

1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

3. Run the database DDL against your Postgres instance (see Module 2 setup notes).

4. Start the app:
   ```bash
   uvicorn app.api.main:app --reload
   ```

## Docker

```bash
docker build -t ai-interviewer .
docker run --env-file .env ai-interviewer
```

## Notes

- `.env` holds secrets and is gitignored — never commit it.
- Browser profile and debug snapshot directories under `app/bot/` are local runtime artifacts and are gitignored.
