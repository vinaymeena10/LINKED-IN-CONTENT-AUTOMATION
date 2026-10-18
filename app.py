import os, json, secrets, hashlib, base64
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet, InvalidToken
import feedparser, requests
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from email.utils import parsedate_to_datetime
load_dotenv()
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google import genai
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, desc
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import (
    create_engine, Column, Integer, String, Text,
    DateTime, Boolean, desc, inspect, text
)

MAX_PENDING_DRAFTS = int(os.getenv("MAX_PENDING_DRAFTS", "10"))
MAX_ACTIVE_DRAFTS = int(os.getenv("MAX_ACTIVE_DRAFTS", "15"))
MAX_DRAFTS_PER_CYCLE = int(os.getenv("MAX_DRAFTS_PER_CYCLE", "5"))
APP_NAME = "LinkedIn Content Studio"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./content_studio.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

CATEGORIES = {
    "data_science": {
        "label": "Data science, ML, neural networks & NLP",
        "weight": 25,
    },
    "ai_research": {
        "label": "AI research, LLMs & AI tools",
        "weight": 25,
    },
    "cloud_automation": {
        "label": "Cloud, automation & data engineering",
        "weight": 15,
    },
    "careers": {
        "label": "Tech careers & job openings",
        "weight": 15,
    },
    "business_decoding": {
        "label": "Startup & business deep dives",
        "weight": 20,
    },
}

DISPLAY_CATEGORIES = {
    **CATEGORIES,
    "marketing": {
        "label": "Legacy marketing content",
        "weight": 0,
    },
}

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    category = Column(String(40), nullable=False, index=True)
    planned_date = Column(String(10), nullable=True)
    title = Column(String(500), nullable=False)
    source_url = Column(String(1500), nullable=False)
    source_name = Column(String(300), default="")
    source_published = Column(String(100), default="")
    draft = Column(Text, nullable=False)
    status = Column(String(30), default="pending", index=True)  # pending, approved, published, rejected, failed
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    published_at = Column(DateTime, nullable=True)
    linkedin_post_id = Column(String(300), default="")
    error = Column(Text, default="")
    fingerprint = Column(String(64), unique=True, nullable=False)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)

Base.metadata.create_all(engine)
app = FastAPI(title=APP_NAME)
templates = Jinja2Templates(directory="templates")
scheduler = BackgroundScheduler(timezone=os.getenv("APP_TIMEZONE", "Asia/Kolkata"))


# Safe migration for existing databases.
with engine.begin() as connection:
    columns = {
        column["name"]
        for column in inspect(connection).get_columns("posts")
    }

    if "planned_date" not in columns:
        connection.execute(
            text("ALTER TABLE posts ADD COLUMN planned_date VARCHAR(10)")
        )

# Public feeds: configure/extend FEEDS_JSON for trusted sources. Search queries are discovery leads, not proof of a claim.
DEFAULT_FEEDS = [
    {
        "category": "data_science",
        "name": "arXiv Machine Learning",
        "url": "https://rss.arxiv.org/rss/cs.LG",
    },
    {
        "category": "data_science",
        "name": "arXiv NLP",
        "url": "https://rss.arxiv.org/rss/cs.CL",
    },
    {
        "category": "ai_research",
        "name": "arXiv AI",
        "url": "https://rss.arxiv.org/rss/cs.AI",
    },
    {
        "category": "ai_research",
        "name": "Google AI Blog",
        "url": "https://blog.research.google/feeds/posts/default",
    },
    {
        "category": "cloud_automation",
        "name": "Google Cloud Blog",
        "url": "https://cloudblog.withgoogle.com/rss/",
    },
    {
        "category": "cloud_automation",
        "name": "AWS News Blog",
        "url": "https://aws.amazon.com/blogs/aws/feed/",
    },
    {
        "category": "careers",
        "name": "Tech jobs (Google News RSS)",
        "url": (
            "https://news.google.com/rss/search?"
            "q=%28%22data+scientist%22+OR+%22machine+learning+engineer%22"
            "+OR+%22AI+engineer%22+OR+%22data+engineer%22%29"
            "+%28hiring+OR+jobs+OR+vacancies+OR+careers%29"
            "&hl=en-IN&gl=IN&ceid=IN:en"
        ),
    },
    {
        "category": "business_decoding",
        "name": "Startup & Business Deep Dives (Google News RSS)",
        "url": (
            "https://news.google.com/rss/search?"
            "q=%28startup+OR+company+OR+industry%29"
            "+%28%22business+model%22+OR+%22unit+economics%22"
            "+OR+%22supply+chain%22+OR+%22revenue+model%22"
            "+OR+operations+OR+strategy+OR+%22case+study%22%29"
            "&hl=en-IN&gl=IN&ceid=IN:en"
        ),
    },
]

def get_feeds():
    raw = os.getenv("FEEDS_JSON", "").strip()
    if not raw: return DEFAULT_FEEDS
    try:
        parsed = json.loads(raw)
        return [x for x in parsed if x.get("category") in CATEGORIES and x.get("url") and x.get("name")]
    except Exception:
        return DEFAULT_FEEDS

def secret_box():
    key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is not configured. Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
    return Fernet(key.encode())

def db_get(key, default=""):
    with SessionLocal() as db:
        row = db.get(Setting, key)
        return row.value if row else default

def db_set(key, value):
    with SessionLocal() as db:
        row = db.get(Setting, key)
        if row is None: row = Setting(key=key, value=value); db.add(row)
        else: row.value = value
        db.commit()

def get_token():
    encrypted = db_get("linkedin_token")
    if not encrypted:
        return ""
    try:
        return secret_box().decrypt(encrypted.encode()).decode()
    except Exception:
        return ""
    
def set_token(token):
    db_set("linkedin_token", secret_box().encrypt(token.encode()).decode())

def fingerprint(url, title):
    return hashlib.sha256((url.strip().lower() + "|" + title.strip().lower()).encode()).hexdigest()

def pick_category(db):
    # Weighted round-robin over the last 20 generated/published items, minimizing deviation from target shares.
    recent = db.query(Post).filter(Post.status.in_(["pending", "approved", "published"])).order_by(desc(Post.created_at)).limit(20).all()
    counts = {k: 0 for k in CATEGORIES}
    for post in recent: counts[post.category] = counts.get(post.category, 0) + 1
    n = len(recent)
    # Choose category with largest target deficit; small random tie-break avoids fixed ordering.
    deficits = {k: (CATEGORIES[k]["weight"] / 100 * (n + 1)) - counts[k] for k in CATEGORIES}
    return max(deficits, key=deficits.get)

def gemini_client():
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is missing")
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def parse_publication_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")

    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    raw = entry.get("published") or entry.get("updated") or ""

    if not raw:
        return None

    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def discover_items():
    found = []

    for feed in get_feeds():
        try:
            parsed = feedparser.parse(feed["url"])

            if getattr(parsed, "bozo", False):
                print(
                    f"Feed warning for {feed['name']}: "
                    f"{getattr(parsed, 'bozo_exception', 'unknown error')}"
                )

            for entry in parsed.entries[:25]:
                url = entry.get("link", "").strip()
                title = entry.get("title", "").strip()

                if not url.startswith("http") or not title:
                    continue

                summary = entry.get(
                    "summary",
                    entry.get("description", ""),
                )

                from html import unescape
                import re

                summary = unescape(
                    re.sub(r"<[^>]+>", " ", summary or "")
                )
                summary = " ".join(summary.split())[:2500]

                published_dt = parse_publication_date(entry)

                found.append({
                    "category": feed["category"],
                    "source_name": feed["name"],
                    "url": url,
                    "title": title,
                    "summary": summary,
                    "published": (
                        published_dt.isoformat()
                        if published_dt
                        else entry.get("published", "")
                    ),
                    "published_dt": published_dt,
                })

        except Exception as exc:
            print(f"Feed fetch failed for {feed['name']}: {exc}")

    return found


def create_draft(item):
    category = CATEGORIES[item["category"]]["label"]

    business_instructions = ""

    if item["category"] == "business_decoding":
        business_instructions = """
For startup and business deep dives, explain as much as the source supports:
- What the company does and whom it serves.
- Its business model and how it creates value.
- How it earns revenue, if the source explains this.
- Its cost structure, supply chain, distribution, or operations, if relevant.
- Its competitive advantage, strategy, and major business trade-offs.
- What other businesses or professionals can learn from the example.

Choose the most interesting angle; do not force every point into every post.
Distinguish facts reported by the source from your own clearly framed analysis.
Never invent financial figures, margins, revenue, market share, or internal
company practices. If evidence is missing, acknowledge the limitation.
"""

    prompt = f"""
You are a thoughtful LinkedIn editor writing for professionals interested in
data science, AI, cloud, technology careers, startups, and business strategy.

Create an original LinkedIn post based on the source below.

CATEGORY-SPECIFIC INSTRUCTIONS:
{business_instructions}

GENERAL RULES:
- Aim for 120-220 words.
- Start with a strong, specific hook, not clickbait.
- Explain what happened or what the research found, then why it matters.
- Include a useful practical takeaway.
- Do not copy sentences or distinctive phrasing from the source.
- Do not invent facts, metrics, quotations, results, or claims.
- Treat the supplied snippet as potentially incomplete.
- If the source is older, present it as valuable research, background,
  or a retrospective—not as breaking news or a new discovery.
- For job-related content, call it a confirmed opening only if the source
  actually establishes that a role is open. Otherwise call it a job lead
  and direct readers to the employer's official careers page.
- Avoid generic filler and repetitive introductions.
- Do not invent personal anecdotes or claim you personally tested something.
- Use 3-5 relevant hashtags.
- Put the source URL on the final line.
- Return only the post text.

Category: {category}
Source title: {item['title']}
Publisher/feed: {item['source_name']}
Source URL: {item['url']}
Publication date: {item['published']}
Source snippet: {item['summary']}

Write the post now.
"""

    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Check your .env configuration."
        )

    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    with genai.Client(api_key=api_key) as client:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        draft = (response.text or "").strip()

    if not draft:
        raise RuntimeError("Gemini returned an empty draft.")

    return draft


def freshness_score(item):
    published_dt = item.get("published_dt")

    if not published_dt:
        return 0.0

    age_days = max(
        0,
        (datetime.now(timezone.utc) - published_dt).days,
    )

    category = item["category"]

    # Older research and business case studies remain eligible.
    if category in {
        "data_science",
        "ai_research",
        "business_decoding",
    }:
        return max(0.0, 6.0 - age_days / 120.0)

    # Current developments and job leads benefit more from freshness.
    return max(-10.0, 10.0 - age_days / 20.0)


def title_tokens(title):
    import re

    ignored = {
        "the", "and", "for", "with", "from", "how", "what",
        "why", "your", "this", "that", "into", "after",
        "new", "guide", "about",
    }

    words = re.findall(r"[a-z0-9]+", title.lower())
    return {word for word in words if word not in ignored}


def title_is_near_duplicate(title, existing_titles):
    current = title_tokens(title)

    if not current:
        return False

    for existing in existing_titles:
        previous = title_tokens(existing)

        if not previous:
            continue

        union = current | previous
        similarity = len(current & previous) / len(union)

        if similarity >= 0.75:
            return True

    return False


def run_research_cycle(max_drafts=5):
    candidates = discover_items()
    created = 0

    with SessionLocal() as db:
        pending_total = (
            db.query(Post)
            .filter(Post.status == "pending")
            .count()
        )
        active_total = (
            db.query(Post)
            .filter(Post.status.in_(["pending", "approved"]))
            .count()
        )

        if pending_total >= MAX_PENDING_DRAFTS:
            print(
                f"Research paused: {pending_total} drafts need review "
                f"(limit {MAX_PENDING_DRAFTS})."
            )
            return 0

        if active_total >= MAX_ACTIVE_DRAFTS:
            print(
                f"Research paused: {active_total} active drafts "
                f"(limit {MAX_ACTIVE_DRAFTS})."
            )
            return 0

        slots = min(
            max(0, max_drafts),
            MAX_DRAFTS_PER_CYCLE,
            MAX_PENDING_DRAFTS - pending_total,
            MAX_ACTIVE_DRAFTS - active_total,
        )
        if slots <= 0:
            return 0

        active_statuses = ["pending", "approved", "published"]
        recent_posts = (
            db.query(Post)
            .filter(Post.status.in_(active_statuses))
            .order_by(desc(Post.created_at))
            .limit(20)
            .all()
        )

        counts = {category: 0 for category in CATEGORIES}
        for post in recent_posts:
            counts[post.category] = counts.get(post.category, 0) + 1

        total = len(recent_posts)
        deficits = {
            category: (
                CATEGORIES[category]["weight"] / 100 * (total + 1)
                - counts[category]
            )
            for category in CATEGORIES
        }

        existing_titles = [
            row[0]
            for row in (
                db.query(Post.title)
                .filter(Post.status.in_(active_statuses))
                .order_by(desc(Post.created_at))
                .limit(100)
                .all()
            )
        ]

        candidates.sort(
            key=lambda item: (
                -deficits.get(item["category"], 0),
                -freshness_score(item),
            )
        )

        source_counts = {}

        for item in candidates:
            if created >= slots:
                break

            category = item["category"]
            fp = fingerprint(item["url"], item["title"])

            if db.query(Post.id).filter(Post.fingerprint == fp).first():
                continue

            if title_is_near_duplicate(item["title"], existing_titles):
                continue

            pending_count = (
                db.query(Post)
                .filter(
                    Post.category == category,
                    Post.status == "pending",
                )
                .count()
            )
            if pending_count >= 3:
                continue

            feed_name = item["source_name"]
            if source_counts.get(feed_name, 0) >= 2:
                continue

            try:
                draft = create_draft(item)
                if not draft:
                    continue

                db.add(Post(
                    category=category,
                    title=item["title"][:500],
                    source_url=item["url"],
                    source_name=feed_name,
                    source_published=item["published"],
                    draft=draft,
                    status="pending",
                    fingerprint=fp,
                ))
                db.commit()

                created += 1
                source_counts[feed_name] = (
                    source_counts.get(feed_name, 0) + 1
                )
                existing_titles.append(item["title"])

            except Exception as exc:
                db.rollback()
                print(f"Draft generation failed: {exc}")

    return created

def linkedin_publish(post_text):
    token = get_token()
    if not token: raise RuntimeError("LinkedIn is not connected. Connect it in Settings first.")
    person_id = db_get("linkedin_person_id")
    if not person_id: raise RuntimeError("LinkedIn member ID missing. Reconnect LinkedIn.")
    version = os.getenv("LINKEDIN_API_VERSION", "202607")
    url = "https://api.linkedin.com/rest/posts"
    payload = {"author": f"urn:li:person:{person_id}", "commentary": post_text, "visibility": "PUBLIC", "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    response = requests.post(url, headers={"Authorization":f"Bearer {token}", "Linkedin-Version":version, "X-Restli-Protocol-Version":"2.0.0", "Content-Type":"application/json"}, json=payload, timeout=30)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"LinkedIn API returned {response.status_code}: {response.text[:800]}")
    return response.headers.get("x-restli-id", response.headers.get("X-RestLi-Id", "published"))

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, notice: str = ""):
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()

    with SessionLocal() as db:
        posts = (
            db.query(Post)
            .filter(Post.status != "rejected")
            .order_by(desc(Post.created_at))
            .limit(100)
            .all()
        )

        stats = {
            status: db.query(Post)
            .filter(Post.status == status)
            .count()
            for status in [
                "pending",
                "approved",
                "published",
                "rejected",
                "failed",
            ]
        }

        planned_posts = (
            db.query(Post)
            .filter(
                Post.planned_date >= today,
                Post.status == "approved",
            )
            .order_by(Post.planned_date.asc())
            .limit(14)
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "posts": posts,
            "stats": stats,
            "categories": DISPLAY_CATEGORIES,
            "notice": notice,
            "planned_posts": planned_posts,
            "connected": bool(get_token()),
        },
    )


@app.post("/generate")
def generate_now():
    try: n = run_research_cycle(max_drafts=5); msg = f"Created {n} new draft(s)."
    except Exception as exc: msg = f"Generation failed: {exc}"
    return RedirectResponse(url="/?notice=" + requests.utils.quote(msg), status_code=303)

@app.post("/planner/build")
def build_daily_plan():
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
    today = datetime.now(ZoneInfo(timezone_name)).date()

    # Plan the next 7 days, starting tomorrow.
    plan_dates = [
        (today + timedelta(days=offset)).isoformat()
        for offset in range(1, 8)
    ]

    with SessionLocal() as db:
        already_planned = (
            db.query(Post.planned_date)
            .filter(
                Post.status == "approved",
                Post.planned_date.in_(plan_dates),
            )
            .all()
        )
        used_dates = {
            planned_date
            for (planned_date,) in already_planned
            if planned_date
        }
        free_dates = [
            planned_date
            for planned_date in plan_dates
            if planned_date not in used_dates
        ]

        approved_posts = (
            db.query(Post)
            .filter(
                Post.status == "approved",
                Post.planned_date.is_(None),
            )
            .order_by(Post.created_at.asc())
            .limit(len(free_dates))
            .all()
        )

        for post, planned_date in zip(approved_posts, free_dates):
            post.planned_date = planned_date

        db.commit()
        assigned = len(approved_posts)

    if assigned:
        message = f"Planned {assigned} approved post(s), one per day."
    else:
        message = (
            "No posts were added to the plan. Approve some drafts first, "
            "or check whether the next 7 days are already planned."
        )

    return RedirectResponse(
        url="/?notice=" + requests.utils.quote(message),
        status_code=303,
    )

@app.post("/posts/{post_id}/save")
def save_post(post_id: int, draft: str = Form(...)):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status == "published": raise HTTPException(409, "Published posts cannot be edited here")
        post.draft = draft.strip(); post.status = "pending"; post.error = ""; db.commit()
    return RedirectResponse(url="/?notice=Draft+saved", status_code=303)

@app.post("/posts/{post_id}/approve")
def approve(post_id: int):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status == "published": raise HTTPException(409, "Already published")
        post.status = "approved"; post.error = ""; db.commit()
    return RedirectResponse(url="/?notice=Post+approved.+It+has+not+been+published", status_code=303)

@app.post("/posts/{post_id}/publish")
def publish(post_id: int):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status != "approved": raise HTTPException(409, "Approve the post before publishing")
        text = post.draft
    try:
        post_id_remote = linkedin_publish(text)
        with SessionLocal() as db:
            post = db.get(Post, post_id); post.status = "published"; post.published_at = datetime.utcnow(); post.linkedin_post_id = post_id_remote; post.error = ""; db.commit()
        msg = "Published successfully"
    except Exception as exc:
        with SessionLocal() as db:
            post = db.get(Post, post_id); post.error = str(exc); db.commit()
        msg = "Publishing failed; see the post card for details"
    return RedirectResponse(url="/?notice=" + requests.utils.quote(msg), status_code=303)

@app.post("/posts/{post_id}/reject")
def reject(post_id: int):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status == "published": raise HTTPException(409, "Already published")
        post.status = "rejected"; db.commit()
    return RedirectResponse(url="/?notice=Post+rejected", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = ""):
    return templates.TemplateResponse(
    request=request,
    name="settings.html",
    context={
        "request": request,
        "connected": bool(get_token()),
        
        "notice": notice,
        "redirect_uri": os.getenv(
            "LINKEDIN_REDIRECT_URI",
            "http://localhost:8080/auth/linkedin/callback"
        ),
    },
)

@app.get("/auth/linkedin/start")
def linkedin_start():
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI", "")
    if not client_id or not redirect_uri: return RedirectResponse("/settings?notice=Set+LINKEDIN_CLIENT_ID+and+LINKEDIN_REDIRECT_URI+first", status_code=303)
    state = secrets.token_urlsafe(24); db_set("oauth_state", state)
    params = {"response_type":"code", "client_id":client_id, "redirect_uri":redirect_uri, "state":state, "scope":"openid profile w_member_social"}
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params), status_code=302)

@app.get("/auth/linkedin/callback")
def linkedin_callback(code: str = "", state: str = "", error: str = ""):
    if error: return RedirectResponse("/settings?notice=" + requests.utils.quote("LinkedIn authorization error: " + error), status_code=303)
    expected = db_get("oauth_state")
    if not state or not expected or not secrets.compare_digest(state, expected): raise HTTPException(400, "Invalid OAuth state")
    data = {"grant_type":"authorization_code", "code":code, "redirect_uri":os.getenv("LINKEDIN_REDIRECT_URI", ""), "client_id":os.getenv("LINKEDIN_CLIENT_ID", ""), "client_secret":os.getenv("LINKEDIN_CLIENT_SECRET", "")}
    token_response = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data=data, timeout=30)
    if token_response.status_code != 200: return RedirectResponse("/settings?notice=" + requests.utils.quote("Token exchange failed: " + token_response.text[:200]), status_code=303)
    token = token_response.json().get("access_token", "")
    profile_response = requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization":f"Bearer {token}"}, timeout=30)
    if profile_response.status_code != 200: return RedirectResponse("/settings?notice=" + requests.utils.quote("Could not retrieve LinkedIn member identity: " + profile_response.text[:200]), status_code=303)
    profile = profile_response.json(); sub = str(profile.get("sub", ""))
    if not sub: return RedirectResponse("/settings?notice=LinkedIn+did+not+return+a+member+ID", status_code=303)
    try: set_token(token)
    except Exception as exc: return RedirectResponse("/settings?notice=" + requests.utils.quote(str(exc)), status_code=303)
    db_set("linkedin_person_id", sub); db_set("oauth_state", "")
    return RedirectResponse("/settings?notice=LinkedIn+connected+successfully", status_code=303)

@app.post("/settings/disconnect")
def disconnect():
    db_set("linkedin_token", ""); db_set("linkedin_person_id", "")
    return RedirectResponse("/settings?notice=LinkedIn+disconnected", status_code=303)

@app.get("/health")
def health(): return {"ok":True,"app":APP_NAME,"time":datetime.now(timezone.utc).isoformat()}

def scheduled_job():
    try: print(f"Scheduled research created {run_research_cycle(max_drafts=5)} draft(s)")
    except Exception as exc: print(f"Scheduled research failed: {exc}")

@app.on_event("startup")
def startup():
    if os.getenv("ENABLE_SCHEDULER", "true").lower() == "true":
        hour = int(os.getenv("SCHEDULE_HOUR", "8")); minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
        scheduler.add_job(scheduled_job, "cron", hour=hour, minute=minute, id="daily_research", replace_existing=True, max_instances=1)
        scheduler.start()

@app.on_event("shutdown")
def shutdown():
    if scheduler.running: scheduler.shutdown(wait=False)
