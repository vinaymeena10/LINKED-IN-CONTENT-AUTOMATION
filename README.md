# LinkedIn Growth Studio — Editorial Intelligence Edition

A FastAPI app that keeps the existing LinkedIn OAuth, AI text providers, approval-gated publishing, job discovery and image-generation workflow, and adds a more granular editorial discovery layer.

## What's included

- Warm ivory / charcoal / forest-green / terracotta editorial UI.
- Discover workspace with seven main editorial categories and detailed subtopics.
- Reusable content domains: Education, Industry News, Research, Business Intelligence, Emerging Players, Tools & Tutorials, Careers, Opinion, Media/Campaigns and Evergreen.
- Editorial angles, audience and difficulty controls.
- Education and evergreen idea generation without requiring a recent news article.
- Google News RSS discovery for current news, research, startup, business and campaign queries.
- Idea bank with one-click conversion into an AI draft.
- Editorial kanban board for drafts, approved, published and failed posts.
- Saved edit history for posts.
- Existing LinkedIn account management, publishing approval gate, job search and image route retained.
- Strict India job filtering: only India-located roles, excludes remote listings when India is selected.

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r L_requirements.txt
uvicorn app:app --reload --port 8000
```

Open `http://127.0.0.1:8000`.

## Render deployment

- Build command: `pip install -r L_requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Keep your existing `APP_ENCRYPTION_KEY`, LinkedIn credentials and database settings unchanged.
- Set AI provider keys in Render Environment, never in source files or Git.
- The current image-generation route still uses the OpenAI Images API and is optional/paid. This package does not silently switch image providers.
- Add a persistent disk for `content_studio.db` if using SQLite and for `static/generated` if you want generated images to survive deploys/restarts. PostgreSQL is preferable for production.

## Upgrade safety

Back up your existing database before deployment. The app adds tables for editorial ideas and post revisions, and safely adds metadata columns to the existing `posts` table. It does not intentionally delete or recreate existing posts or LinkedIn accounts.

## Important limitations

- Public RSS discovery is a discovery aid, not a verification engine. Review the original source and verify facts before publishing.
- Educational ideas are generated as prompts. Check technical details, examples and version-sensitive syntax before publishing.
- Startup growth, funding, revenue and brand-performance claims must be verified from original sources; do not infer private metrics from headlines.
- ATS coverage remains limited to supported Greenhouse, Lever and Ashby public boards. Workday and custom ATS crawling are not implemented.
- Publishing remains human-approved; no automatic LinkedIn publishing was added.
