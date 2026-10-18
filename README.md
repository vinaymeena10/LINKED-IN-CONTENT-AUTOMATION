# LinkedIn Content Studio

A separate Python web app for researching public RSS content, generating original LinkedIn drafts with Google Gemini, reviewing/editing drafts, and publishing only after a human explicitly approves them.

## Content allocation

- 30% data science, machine learning, neural networks and NLP
- 30% AI research, LLMs and AI tools
- 20% cloud, automation and data engineering
- 15% technology careers and job openings
- 5% marketing and brand trends

The quota is a rolling target over time. A small number of items may not match exact percentages because source availability varies.

## Run locally

Requires Python 3.11+ (3.12 recommended).

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env  # Windows; on macOS/Linux use cp .env.example .env
```

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put that value in `APP_ENCRYPTION_KEY` in `.env`, then add your `GEMINI_API_KEY`. Load environment variables before running. On PowerShell, for example:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Za-z_][A-Za-z0-9_]*=' -and $_ -notmatch '^\s*#' } | ForEach-Object { $name,$value=$_.Split('=',2); Set-Item -Path "Env:$name" -Value $value }
uvicorn app:app --reload --port 8080
```

Open http://localhost:8080. The scheduled task creates drafts at 08:00 Asia/Kolkata by default. The **Generate drafts** button can be used to run research on demand.

## LinkedIn setup

1. Create a LinkedIn developer app and request the product/permission needed to share posts on behalf of a member. `w_member_social` is the typical member posting permission; access depends on LinkedIn's app approval and current program requirements.
2. Register the exact callback URL, locally `http://localhost:8080/auth/linkedin/callback`, and in production the HTTPS URL for your domain.
3. Set `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, and `LINKEDIN_REDIRECT_URI` as environment variables.
4. In Settings, click Connect LinkedIn and approve the requested permissions.
5. Test by creating a draft, reviewing its source, editing it, clicking Approve, and only then clicking Publish to LinkedIn.

The app calls the official LinkedIn Posts API (`POST /rest/posts`) with the configured `LINKEDIN_API_VERSION` (starter default `202607`). Check the current supported version in LinkedIn's developer documentation before deployment. OAuth tokens are encrypted at rest using `APP_ENCRYPTION_KEY`; never share or commit API keys, client secrets, tokens, or `.env` files.

## Cloud deployment

Deploy the included Dockerfile to a managed container service (for example, Google Cloud Run) and attach a **persistent PostgreSQL database**. Configure all secrets in the hosting provider's secret manager, not in source code. Set `DATABASE_URL` to the PostgreSQL connection string, `LINKEDIN_REDIRECT_URI` to your production HTTPS callback, and `APP_ENCRYPTION_KEY` to a stable Fernet key. Keep a single scheduler instance running so duplicate daily jobs are not triggered by multiple replicas. Configure a persistent volume or managed database; the default SQLite file is intended only for local development.

For a reliable production setup, deploy the web app and the scheduled worker as separate services (the web service uses `ENABLE_SCHEDULER=false`; one worker runs the scheduler) or use your cloud provider's scheduler to call an authenticated internal job endpoint after adding authentication. Do not expose an unauthenticated job-trigger endpoint publicly. Add authentication to the dashboard before exposing it to the internet; this starter is intended for a private deployment and does not yet include user login/CSRF protection.

## Research sources

Default feeds include arXiv RSS, Google AI Blog, Google Cloud Blog, AWS News Blog, and Google News RSS searches for career and brand trends. Feed availability can change. Customize via `FEEDS_JSON`, a JSON array with `category`, `name`, and `url` fields. Categories must be one of `data_science`, `ai_research`, `cloud_automation`, `careers`, `marketing`.

Job posts from news/search feeds are not verified vacancies. Verify role, location, recency, and application link on the employer's official careers page before publishing.

## Current limitations / next production steps

- Feed-based research; not a general-purpose LinkedIn scraper.
- Human approval is mandatory; there is no automatic publishing job.
- A cloud deployment needs HTTPS, a persistent database, secrets management, and dashboard authentication.
- OAuth token refresh/expiry handling should be added based on the actual LinkedIn app/token policy before long-running production use.
- The initial quota algorithm uses category targets over a rolling queue; exact long-term quotas depend on source availability.
