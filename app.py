import os, json, secrets, hashlib, base64
import re
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
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from google import genai
from groq import Groq
from cerebras.cloud.sdk import Cerebras
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
    "marketing": {"label": "Branding & Marketing", "weight": 0},
    "agentic_ai": {"label": "Agentic AI", "weight": 0},
    "product_innovation": {"label": "Product & Innovation", "weight": 0},
    "finance_business": {"label": "Finance & Business", "weight": 0},
    "industry_news": {"label": "Industry & Tech News", "weight": 0},
}

# User-selectable research topics. Each topic maps to a broad category for
# backwards compatibility with existing posts and dashboard statistics.
TOPICS = {
    "data_science": {"label": "Data Science", "category": "data_science", "query": '"data science" OR analytics OR statistics'},
    "machine_learning": {"label": "Machine Learning", "category": "data_science", "query": '"machine learning" OR "predictive modeling"'},
    "deep_learning": {"label": "Deep Learning", "category": "data_science", "query": '"deep learning" OR "representation learning"'},
    "neural_networks": {"label": "Neural Networks", "category": "data_science", "query": '"neural networks" OR transformers'},
    "nlp": {"label": "NLP", "category": "data_science", "query": '"natural language processing" OR NLP'},
    "artificial_intelligence": {"label": "Artificial Intelligence", "category": "ai_research", "query": '"artificial intelligence" OR AI research'},
    "agentic_ai": {"label": "Agentic AI", "category": "ai_research", "query": '"agentic AI" OR "AI agents" OR "multi-agent"'},
    "llms": {"label": "LLMs & Generative AI", "category": "ai_research", "query": 'LLM OR "large language model" OR "generative AI"'},
    "ai_research": {"label": "AI Research & Tools", "category": "ai_research", "query": 'AI research OR "AI tools" OR "foundation models"'},
    "startup_decoding": {"label": "Startup Decoding", "category": "business_decoding", "query": 'startup OR founder OR "business model"'},
    "business_strategy": {"label": "Business & Industry Decoding", "category": "business_decoding", "query": '"unit economics" OR strategy OR "industry analysis" OR operations'},
    "branding_marketing": {"label": "Branding & Marketing", "category": "marketing", "query": 'branding OR marketing OR advertising OR "consumer behaviour"'},
    "cloud_automation": {"label": "Cloud & Automation", "category": "cloud_automation", "query": 'cloud computing OR automation OR MLOps OR data engineering'},
    "tech_careers": {"label": "Technology Careers", "category": "careers", "query": '"data scientist jobs" OR "AI engineer jobs" OR tech hiring'},
}
DEFAULT_SELECTED_TOPICS = list(TOPICS.keys())


def get_selected_topics():
    raw = db_get("selected_topics", "")
    if not raw:
        return DEFAULT_SELECTED_TOPICS.copy()
    try:
        selected = json.loads(raw)
        valid = [topic for topic in selected if topic in TOPICS]
        return valid or DEFAULT_SELECTED_TOPICS.copy()
    except (TypeError, ValueError):
        return DEFAULT_SELECTED_TOPICS.copy()


WRITING_DEFAULTS = {
    "format": "narrative",
    "tone": "insightful",
    "voice": "first_person",
    "depth": "deep",
    "length": "medium",
}

WRITING_OPTIONS = {
    "format": {"narrative", "points", "breakdown", "hook", "contrarian", "case_study"},
    "tone": {"insightful", "conversational", "provocative", "technical", "storytelling"},
    "voice": {"first_person", "professional", "objective"},
    "depth": {"quick", "insight", "deep", "thought_leadership"},
    "length": {"short", "medium", "long"},
}


def get_writing_preferences():
    preferences = {}
    for key, default in WRITING_DEFAULTS.items():
        value = db_get("writing_" + key, default)
        preferences[key] = value if value in WRITING_OPTIONS[key] else default
    return preferences


def save_writing_preferences(values):
    clean = {}
    for key, allowed in WRITING_OPTIONS.items():
        value = values.get(key, WRITING_DEFAULTS[key])
        if value not in allowed:
            value = WRITING_DEFAULTS[key]
        clean[key] = value
        db_set("writing_" + key, value)
    return clean


def build_topic_feeds(selected_topics=None):
    selected_topics = selected_topics or get_selected_topics()
    feeds = []
    for topic_key in selected_topics:
        topic = TOPICS.get(topic_key)
        if not topic:
            continue
        query = topic["query"].replace(" ", "+")
        url = (
            "https://news.google.com/rss/search?q=" + query
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        feeds.append({
            "category": topic["category"],
            "topic": topic_key,
            "name": "Google News · " + topic["label"],
            "url": url,
        })
    # Add specialist research feeds when their topic families are selected.
    if any(t in selected_topics for t in ("machine_learning", "deep_learning", "neural_networks")):
        feeds.append({"category": "data_science", "topic": "machine_learning", "name": "arXiv Machine Learning", "url": "https://rss.arxiv.org/rss/cs.LG"})
    if "nlp" in selected_topics:
        feeds.append({"category": "data_science", "topic": "nlp", "name": "arXiv NLP", "url": "https://rss.arxiv.org/rss/cs.CL"})
    if any(t in selected_topics for t in ("artificial_intelligence", "agentic_ai", "ai_research", "llms")):
        feeds.append({"category": "ai_research", "topic": "ai_research", "name": "arXiv AI", "url": "https://rss.arxiv.org/rss/cs.AI"})
        feeds.append({"category": "ai_research", "topic": "ai_research", "name": "Google AI Blog", "url": "https://blog.research.google/feeds/posts/default"})
    return feeds

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    category = Column(String(40), nullable=False, index=True)
    topic = Column(String(60), nullable=True, index=True)
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
    account_id = Column(Integer, nullable=True, index=True)
    image_url = Column(String(1500), default="")
    content_domain = Column(String(80), default="industry_news")
    editorial_angle = Column(String(120), default="explain_why_it_matters")
    audience = Column(String(120), default="professionals")
    difficulty = Column(String(30), default="intermediate")
    content_brief = Column(Text, default="")

class LinkedInAccount(Base):
    __tablename__ = "linkedin_accounts"
    id = Column(Integer, primary_key=True)
    person_id = Column(String(200), unique=True, nullable=False, index=True)
    display_name = Column(String(300), default="LinkedIn member")
    encrypted_token = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class JobListing(Base):
    __tablename__ = "job_listings"
    id = Column(Integer, primary_key=True)
    external_id = Column(String(500), unique=True, nullable=False, index=True)
    company = Column(String(300), nullable=False)
    title = Column(String(500), nullable=False)
    location = Column(String(300), default="")
    workplace_type = Column(String(100), default="")
    employment_type = Column(String(100), default="")
    domain = Column(String(150), default="")
    description = Column(Text, default="")
    apply_url = Column(String(1500), nullable=False)
    source = Column(String(100), default="")
    posted_at = Column(String(100), default="")
    discovered_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)


# Public job-board integrations. These are live public endpoints; coverage depends
# on whether a company publishes jobs through one of these ATS platforms.
JOB_DOMAINS = {
    "Branding & Brand Strategy": ["brand strategist", "brand manager", "brand strategy", "brand marketing"],
    "Social Media": ["social media manager", "social media executive", "social media strategist", "community manager", "influencer marketing"],
    "Content & Creative": ["content strategist", "content marketing", "content writer", "creative strategist", "copywriter", "content creator"],
    "Digital Marketing & Growth": ["digital marketing", "performance marketing", "seo", "sem", "ppc", "growth marketing", "paid media", "email marketing"],
    "Advertising & Communications": ["advertising", "media planner", "campaign manager", "public relations", "communications manager"],
    "Design": ["ui designer", "ux designer", "graphic designer", "product designer", "visual designer"],
    "Technology & IT": ["software engineer", "developer", "it", "technology"],
    "Data Science & AI": ["data scientist", "machine learning", "artificial intelligence", "data analyst"],
    "Finance": ["finance", "accountant", "investment", "financial analyst"],
    "Sales & Business Development": ["sales", "business development", "account executive"],
    "Human Resources": ["human resources", "recruiter", "talent acquisition"],
    "Product Management": ["product manager", "product owner"],
    "Operations & Supply Chain": ["operations", "supply chain", "procurement", "logistics"],
    "Aviation & Travel": ["aviation", "airline", "airport", "travel"],
    "Healthcare & Pharma": ["healthcare", "pharma", "clinical"],
    "Consulting & Strategy": ["consultant", "strategy", "business analyst"],
    "Hospitality & Retail": ["hospitality", "hotel", "retail", "store manager"],
    "Education & Research": ["teacher", "education", "researcher", "academic"],
    "Legal & Compliance": ["legal", "compliance", "lawyer"],
    "Other / Custom": [],
}

# Format: company name | ATS provider | board slug
# Add companies through the Job Search page. Slugs are the public board identifiers.
DEFAULT_JOB_COMPANIES = [
    # Greenhouse boards
    ("Canva", "greenhouse", "canva"),
    ("Figma", "greenhouse", "figma"),
    ("Stripe", "greenhouse", "stripe"),
    ("Databricks", "greenhouse", "databricks"),
    ("Duolingo", "greenhouse", "duolingo"),
    ("Coinbase", "greenhouse", "coinbase"),
    ("Rippling", "greenhouse", "rippling"),
    ("Brex", "greenhouse", "brex"),
    ("Plaid", "greenhouse", "plaid"),
    ("Webflow", "greenhouse", "webflow"),
    ("Grammarly", "greenhouse", "grammarly"),
    ("Asana", "greenhouse", "asana"),
    ("Airtable", "greenhouse", "airtable"),
    ("Miro", "greenhouse", "miro"),
    ("Intercom", "greenhouse", "intercom"),
    ("Amplitude", "greenhouse", "amplitude"),
    ("Cloudflare", "greenhouse", "cloudflare"),
    ("MongoDB", "greenhouse", "mongodb"),
    ("Elastic", "greenhouse", "elastic"),
    ("GitLab", "greenhouse", "gitlab"),
    ("Reddit", "greenhouse", "reddit"),
    ("DoorDash", "greenhouse", "doordash"),
    ("Instacart", "greenhouse", "instacart"),
    ("Toast", "greenhouse", "toast"),
    ("Gusto", "greenhouse", "gusto"),
    ("Oscar Health", "greenhouse", "oscar"),
    ("Vanta", "greenhouse", "vanta"),
    ("Samsara", "greenhouse", "samsara"),
    ("Scale AI", "greenhouse", "scaleai"),
    ("Anduril", "greenhouse", "andurilindustries"),
    # Ashby boards
    ("Airbnb", "ashby", "airbnb"),
    ("Notion", "ashby", "notion"),
    ("Ramp", "ashby", "ramp"),
    ("Linear", "ashby", "linear"),
    ("Mercury", "ashby", "mercury"),
    ("Vercel", "ashby", "vercel"),
    ("Deel", "ashby", "deel"),
    ("ElevenLabs", "ashby", "elevenlabs"),
    ("Perplexity", "ashby", "perplexity"),
    ("OpenAI", "ashby", "openai"),
    ("Cursor", "ashby", "cursor"),
    ("Lovable", "ashby", "lovable"),
    # Lever boards
    ("Netflix", "lever", "netflix"),
    ("Shopify", "lever", "shopify"),
    ("Twitch", "lever", "twitch"),
    ("Oura", "lever", "oura"),
    ("Affirm", "lever", "affirm"),
    ("Faire", "lever", "faire"),
    ("Checkr", "lever", "checkr"),
    ("Gong", "lever", "gong"),
    ("Lever Demo", "lever", "lever"),
]

def get_job_companies():
    raw = db_get("job_companies", "")
    companies = []
    if raw:
        try:
            for row in json.loads(raw):
                if isinstance(row, dict) and row.get("name") and row.get("provider") and row.get("slug"):
                    companies.append(row)
        except (TypeError, ValueError):
            pass
    if not companies:
        companies = [{"name": n, "provider": p, "slug": s} for n, p, s in DEFAULT_JOB_COMPANIES]
    return companies

def fetch_company_jobs(company):
    name, provider, slug = company["name"], company["provider"].lower(), company["slug"].strip()
    headers = {"User-Agent": "LinkedInContentStudio/1.0 (public job listing discovery)"}
    jobs = []
    if provider == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        for item in response.json().get("jobs", []):
            jobs.append({
                "external_id": f"greenhouse:{slug}:{item.get('id')}",
                "company": name, "title": item.get("title", ""),
                "location": (item.get("location") or {}).get("name", ""),
                "description": re.sub(r"<[^>]+>", " ", item.get("content", "") or "")[:6000],
                "apply_url": item.get("absolute_url", ""), "source": "Greenhouse",
                "posted_at": item.get("updated_at", ""),
            })
    elif provider == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        for item in response.json():
            categories = item.get("categories") or {}
            jobs.append({
                "external_id": f"lever:{slug}:{item.get('id')}",
                "company": name, "title": item.get("text", ""),
                "location": categories.get("location", ""),
                "workplace_type": categories.get("commitment", ""),
                "description": re.sub(r"<[^>]+>", " ", (item.get("descriptionPlain") or ""))[:6000],
                "apply_url": item.get("hostedUrl") or item.get("applyUrl") or "",
                "source": "Lever", "posted_at": item.get("createdAt", ""),
            })
    elif provider == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        for item in response.json().get("jobs", []):
            jobs.append({
                "external_id": f"ashby:{slug}:{item.get('id')}",
                "company": name, "title": item.get("title", ""),
                "location": item.get("location", ""),
                "workplace_type": item.get("workplaceType", ""),
                "employment_type": item.get("employmentType", ""),
                "description": re.sub(r"<[^>]+>", " ", item.get("descriptionPlain", "") or "")[:6000],
                "apply_url": item.get("jobUrl") or item.get("applyUrl") or "",
                "source": "Ashby", "posted_at": item.get("publishedAt", ""),
            })
    else:
        raise ValueError("Provider must be greenhouse, lever, or ashby.")
    return [j for j in jobs if j.get("title") and j.get("apply_url")]


class EditorialIdea(Base):
    __tablename__ = "editorial_ideas"
    id = Column(Integer, primary_key=True)
    category = Column(String(60), default="")
    topic = Column(String(80), default="")
    content_domain = Column(String(80), default="")
    editorial_angle = Column(String(120), default="")
    title = Column(String(500), nullable=False)
    summary = Column(Text, default="")
    source_url = Column(String(1500), default="")
    source_name = Column(String(300), default="")
    audience = Column(String(120), default="professionals")
    difficulty = Column(String(30), default="intermediate")
    status = Column(String(30), default="idea", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PostRevision(Base):
    __tablename__ = "post_revisions"
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, nullable=False, index=True)
    draft = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(String(300), default="Saved edit")



Base.metadata.create_all(engine)
app = FastAPI(title=APP_NAME)
from fastapi.staticfiles import StaticFiles
from pathlib import Path
Path("static/generated").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
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
    if "topic" not in columns:
        connection.execute(
            text("ALTER TABLE posts ADD COLUMN topic VARCHAR(60)")
        )
    if "account_id" not in columns:
        connection.execute(text("ALTER TABLE posts ADD COLUMN account_id INTEGER"))
    if "image_url" not in columns:
        connection.execute(text("ALTER TABLE posts ADD COLUMN image_url VARCHAR(1500) DEFAULT ''"))
    for column_name, sql_type, default_sql in [
        ("content_domain", "VARCHAR(80)", "'industry_news'"),
        ("editorial_angle", "VARCHAR(120)", "'explain_why_it_matters'"),
        ("audience", "VARCHAR(120)", "'professionals'"),
        ("difficulty", "VARCHAR(30)", "'intermediate'"),
        ("content_brief", "TEXT", "''"),
    ]:
        if column_name not in columns:
            connection.execute(text(f"ALTER TABLE posts ADD COLUMN {column_name} {sql_type} DEFAULT {default_sql}"))

# Preserve a previously connected single LinkedIn account when upgrading.
with SessionLocal() as db:
    legacy_token = db.get(Setting, "linkedin_token")
    legacy_person = db.get(Setting, "linkedin_person_id")
    if legacy_token and legacy_token.value and legacy_person and legacy_person.value:
        existing = db.query(LinkedInAccount).filter_by(person_id=legacy_person.value).first()
        if not existing:
            db.add(LinkedInAccount(person_id=legacy_person.value, display_name="Existing LinkedIn account", encrypted_token=legacy_token.value))
            db.commit()

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
    # Research only the topics currently selected in the dashboard.
    return build_topic_feeds(get_selected_topics())


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

def get_linkedin_accounts():
    with SessionLocal() as db:
        return db.query(LinkedInAccount).order_by(LinkedInAccount.id.asc()).all()

def save_linkedin_account(token, person_id, display_name):
    encrypted = secret_box().encrypt(token.encode()).decode()
    with SessionLocal() as db:
        account = db.query(LinkedInAccount).filter_by(person_id=person_id).first()
        if account is None:
            account = LinkedInAccount(person_id=person_id)
            db.add(account)
        account.display_name = (display_name or "LinkedIn member").strip()[:300]
        account.encrypted_token = encrypted
        db.commit()

def decrypt_account_token(account):
    return secret_box().decrypt(account.encrypted_token.encode()).decode()

def normalize_linkedin_bullets(value):
    """Convert Markdown list markers to real Unicode bullets for LinkedIn text posts."""
    lines = []
    for line in (value or "").splitlines():
        match = re.match(r"^(\s*)(?:[-*+]\s+)(.+)$", line)
        if match:
            line = f"{match.group(1)}• {match.group(2)}"
        # LinkedIn receives plain text: remove Markdown bold/italic markers.
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()

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
                    "topic": feed.get("topic", ""),
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
    category = CATEGORIES.get(item["category"], DISPLAY_CATEGORIES.get(item["category"], {"label": item["category"]}))["label"]
    topic_name = TOPICS.get(item.get("topic", ""), {"label": category})["label"]

    dynamic_authoring = item.get("source_name", "") == "AI-generated editorial idea" or "original educational brief" in item.get("source_name", "").lower()
    preferences = get_writing_preferences()
    post_format = preferences["format"]
    tone = preferences["tone"]
    voice = preferences["voice"]
    depth = preferences["depth"]
    length = preferences["length"]
    if dynamic_authoring:
        # Do not impose saved global writing preferences on AI-generated ideas.
        post_format, tone, voice, depth, length = "narrative", "insightful", "professional", "deep", "medium"

    format_guidance = {
        "narrative": "Write natural, flowing paragraphs. Vary paragraph length and build a clear line of thought.",
        "points": "Use a short opening followed by crisp, meaningful bullet points and a strong concluding insight. Do not turn every sentence into a bullet.",
        "breakdown": "Use a compelling title-like hook and 2-4 clearly labelled sections. Explain the mechanics, implications and conclusion.",
        "hook": "Open with a surprising but supportable observation or question. Build curiosity, explain the evidence, then deliver a clear payoff.",
        "contrarian": "Develop a fair, evidence-based counterintuitive argument. Explain the conventional view, what it misses, and where the evidence leads. Do not manufacture controversy.",
        "case_study": "Structure as context → decision/technology → mechanism → implications → lessons. Use only details supported by the source.",
    }[post_format]
    tone_guidance = {
        "insightful": "Analytical, clear and intellectually curious. Prioritise a useful inference over hype.",
        "conversational": "Natural and approachable, like explaining a sharp observation to a knowledgeable colleague.",
        "provocative": "Confident and challenging, but fair, specific and evidence-led.",
        "technical": "Technically precise. Explain mechanisms and trade-offs without drowning the reader in jargon.",
        "storytelling": "Use narrative tension and progression, while staying factual and avoiding invented scenes or experiences.",
    }[tone]
    voice_guidance = {
        "first_person": "Write in a recognisable first-person author voice where natural: use phrases such as 'What I find interesting…', 'My takeaway…' or 'I think the bigger question is…' only when they introduce a reasoned perspective. Never invent personal experience, experiments, employer details, or claims that the author did something.",
        "professional": "Use a confident professional commentator voice. Avoid forced 'I' statements and corporate press-release language.",
        "objective": "Use an evidence-led, neutral voice. Clearly separate reported facts from interpretation.",
    }[voice]
    depth_guidance = {
        "quick": "One sharp observation and its implication. Keep analysis compact.",
        "insight": "Go beyond summary: explain the mechanism, one meaningful implication and a clear conclusion.",
        "deep": "Develop a substantial argument: explain what changed, why it matters, second-order effects, trade-offs or limitations, and what follows. Include a distinct inference, not just a takeaway.",
        "thought_leadership": "Build an original, defensible point of view. Connect the development to a broader shift, consider a counterpoint, and end with a memorable conclusion. Do not overstate certainty.",
    }[depth]
    length_guidance = {"short": "70-120 words", "medium": "130-220 words", "long": "220-350 words"}[length]

    source_line_instruction = "Put the source URL on the final line."
    if "educational brief" in (item.get("source_name", "").lower()):
        source_line_instruction = "This is an original educational prompt, not a news source. Do not append a fabricated citation or source URL. Teach accurately, state assumptions where needed, and include a worked example or learner exercise when appropriate."

    business_instructions = ""
    if item["category"] == "business_decoding":
        business_instructions = """
For business and startup topics, examine the business model, customer value,
revenue logic, cost drivers, distribution, competitive advantage and strategic
trade-offs where the source supports them. Choose only the most relevant angles.
Never invent financial figures, market share, margins or internal practices.
"""
    elif item["category"] == "marketing":
        business_instructions = """
For branding and marketing topics, analyse the audience, positioning, creative
idea, channel strategy, consumer psychology and measurable campaign mechanics
only where supported by the source. Distinguish a campaign launch from evidence
of campaign success. Never invent reach, conversions, sales lift or ROI.
"""

    if dynamic_authoring:
        author_settings = (
            "Choose the format, tone, point of view, opening, depth and length that best serve "
            "THIS specific idea and its audience. Do not follow a universal template. For an "
            "educational topic, teach the underlying concept accurately and use a worked example "
            "or exercise where useful. For interview preparation, state the question, reason "
            "through the solution and explain common mistakes. For a case study, distinguish "
            "known facts from interpretation. For news, verify the announcement and explain "
            "its implications. Avoid forced first-person voice, generic CTAs and irrelevant hashtags."
        )
    else:
        author_settings = (
            f"Format: {post_format}. {format_guidance}\\n"
            f"Tone: {tone}. {tone_guidance}\\n"
            f"Voice: {voice}. {voice_guidance}\\n"
            f"Analysis depth: {depth}. {depth_guidance}\\n"
            f"Length: {length_guidance}."
        )

    prompt = f"""
You are writing as a thoughtful, technically literate professional who has a
point of view. You are NOT a news summariser, press-release writer or generic
social-media copywriter. Turn source material into original analysis that a
smart reader would genuinely find worth reading.

THE REQUIRED THINKING WORK (do this silently before drafting):
1. Establish exactly what the source confirms. Separate facts from claims and gaps.
2. Find the most interesting tension, consequence, overlooked detail or question.
3. Explain the mechanism: why does this development produce the effect you claim?
4. Infer what it could mean for practitioners, businesses or the wider industry.
5. Form one clear argument and a conclusion that follows from the reasoning.
If the source is too thin to support a meaningful inference, say less rather
than inventing insight. Do not treat a headline or promotional claim as proof.

AUTHOR SETTINGS:
{author_settings}

NON-NEGOTIABLE WRITING RULES:
- Do not merely paraphrase the article. A reader should get an idea, not just a recap.
- Avoid formulaic labels such as "Why it matters", "Practical takeaway", "Key takeaways" and "In today's rapidly evolving world".
- Avoid generic openings like "In a world where technology is changing".
- Do not force a list, headings, first-person phrases, questions or hashtags when they do not fit.
- Use concrete language, specific reasoning and varied sentence rhythm.
- Make the inference explicit. Explain why it follows from the facts.
- Include limitations or a counterpoint when it improves the analysis.
- Never invent personal anecdotes, tests, conversations, professional experience,
  statistics, quotations, product capabilities or causal relationships.
- Do not claim the author personally used, built, tested or witnessed anything
  unless the source explicitly says so (it usually will not).
- Do not overstate an announcement's significance or turn speculation into fact.
- For lists, use the actual Unicode bullet character • at the start of each list line. Never use Markdown asterisks (*) or hyphens (-) as bullet markers. Leave a blank line between list items where useful.
- Use 2-4 relevant hashtags, only if they add value.
- {source_line_instruction}
- Return only the finished LinkedIn post.

CATEGORY: {category}
SELECTED TOPIC: {topic_name}
CATEGORY-SPECIFIC CONTEXT: {business_instructions}
SOURCE TITLE: {item['title']}
PUBLISHER: {item['source_name']}
SOURCE URL: {item['url']}
PUBLICATION DATE: {item['published']}
SOURCE SNIPPET: {item['summary']}

Write something with a real point of view, sound reasoning and a satisfying
conclusion. It should feel authored, not assembled from a template.
"""

    # Try each configured provider in order. A provider is skipped when its
    # API key is absent. Quota, transient server, and connection errors fall
    # through to the next provider.
    failures = []

    def is_retryable_provider_error(exc):
        status = getattr(exc, "status_code", None)
        if status is None:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
        if status in (408, 409, 425, 429) or (status is not None and status >= 500):
            return True

        message = str(exc).lower()
        retry_terms = (
            "rate limit", "rate_limit", "quota", "resource_exhausted",
            "too many requests", "temporarily unavailable", "timeout",
            "timed out", "connection", "overloaded", "server error",
        )
        return any(term in message for term in retry_terms)

    # 1) Gemini (primary)
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
            with genai.Client(api_key=gemini_key) as client:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )
            draft = (response.text or "").strip()
            if draft:
                print(f"Draft generated with Gemini ({model})")
                return normalize_linkedin_bullets(draft)
            raise RuntimeError("Gemini returned an empty draft.")
        except Exception as exc:
            failures.append(f"Gemini: {exc}")
            print(f"Gemini generation failed; trying next provider: {exc}")
    else:
        failures.append("Gemini: GEMINI_API_KEY is not configured")

    # 2) Groq (first fallback)
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        try:
            model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            client = Groq(api_key=groq_key)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1200,
            )
            draft = (response.choices[0].message.content or "").strip()
            if draft:
                print(f"Draft generated with Groq ({model})")
                return normalize_linkedin_bullets(draft)
            raise RuntimeError("Groq returned an empty draft.")
        except Exception as exc:
            failures.append(f"Groq: {exc}")
            print(f"Groq generation failed; trying next provider: {exc}")
    else:
        failures.append("Groq: GROQ_API_KEY is not configured")

    # 3) Cerebras (second fallback)
    cerebras_key = os.getenv("CEREBRAS_API_KEY", "").strip()
    if cerebras_key:
        try:
            model = os.getenv("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct")
            client = Cerebras(api_key=cerebras_key)
            response = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_completion_tokens=1200,
                temperature=0.7,
                top_p=1,
                stream=False,
            )
            draft = (response.choices[0].message.content or "").strip()
            if draft:
                print(f"Draft generated with Cerebras ({model})")
                return normalize_linkedin_bullets(draft)
            raise RuntimeError("Cerebras returned an empty draft.")
        except Exception as exc:
            failures.append(f"Cerebras: {exc}")
            print(f"Cerebras generation failed: {exc}")
    else:
        failures.append("Cerebras: CEREBRAS_API_KEY is not configured")

    raise RuntimeError(
        "All configured AI providers failed. " + " | ".join(failures)
    )


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
                    topic=item.get("topic", ""),
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

def linkedin_publish(post_text, account_id):
    with SessionLocal() as db:
        account = db.get(LinkedInAccount, account_id)
        if not account:
            raise RuntimeError("Select a connected LinkedIn account before publishing.")
        token = decrypt_account_token(account)
        person_id = account.person_id
    version = os.getenv("LINKEDIN_API_VERSION", "202607")
    url = "https://api.linkedin.com/rest/posts"
    clean_text = normalize_linkedin_bullets(post_text)
    payload = {"author": f"urn:li:person:{person_id}", "commentary": clean_text, "visibility": "PUBLIC", "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    response = requests.post(url, headers={"Authorization":f"Bearer {token}", "Linkedin-Version":version, "X-Restli-Protocol-Version":"2.0.0", "Content-Type":"application/json"}, json=payload, timeout=30)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"LinkedIn API returned {response.status_code}: {response.text[:800]}")
    return response.headers.get("x-restli-id", response.headers.get("X-RestLi-Id", "published"))

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, notice: str = ""):
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()

    with SessionLocal() as db:
        posts = db.query(Post).filter(Post.status.in_(["pending", "failed"])).order_by(desc(Post.created_at)).limit(100).all()
        approved_posts = db.query(Post).filter(Post.status == "approved").order_by(desc(Post.created_at)).limit(100).all()
        published_posts = db.query(Post).filter(Post.status == "published").order_by(desc(Post.published_at)).limit(100).all()

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
            "approved_posts": approved_posts,
            "published_posts": published_posts,
            "accounts": get_linkedin_accounts(),
            "stats": stats,
            "categories": DISPLAY_CATEGORIES,
            "topics": TOPICS,
            "selected_topics": get_selected_topics(),
            "writing_preferences": get_writing_preferences(),
            "notice": notice,
            "planned_posts": planned_posts,
            "connected": bool(get_linkedin_accounts()),
        },
    )


@app.post("/generate")
def generate_now(
    selected_topics: list[str] = Form([]),
    post_format: str = Form("narrative"),
    tone: str = Form("insightful"),
    voice: str = Form("first_person"),
    depth: str = Form("deep"),
    length: str = Form("medium"),
):
    valid_topics = [topic for topic in selected_topics if topic in TOPICS]
    if not valid_topics:
        return RedirectResponse(url="/?notice=Select+at+least+one+topic+first", status_code=303)
    db_set("selected_topics", json.dumps(valid_topics))
    save_writing_preferences({
        "format": post_format,
        "tone": tone,
        "voice": voice,
        "depth": depth,
        "length": length,
    })
    try:
        n = run_research_cycle(max_drafts=5)
        msg = f"Created {n} draft(s) using your selected writing style and {len(valid_topics)} topic(s)."
    except Exception as exc:
        msg = f"Generation failed: {exc}"
    return RedirectResponse(url="/?notice=" + requests.utils.quote(msg), status_code=303)


@app.post("/topics/save")
def save_topics(selected_topics: list[str] = Form([])):
    valid_topics = [topic for topic in selected_topics if topic in TOPICS]
    if not valid_topics:
        return RedirectResponse(url="/?notice=Select+at+least+one+topic+to+save", status_code=303)
    db_set("selected_topics", json.dumps(valid_topics))
    return RedirectResponse(url="/?notice=Content+preferences+saved", status_code=303)

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
        if post.draft and post.draft != draft:
            db.add(PostRevision(post_id=post.id, draft=post.draft, note="Before editor save"))
        post.draft = normalize_linkedin_bullets(draft); post.status = "pending"; post.error = ""; db.commit()
    return RedirectResponse(url="/?notice=Draft+saved", status_code=303)

@app.post("/posts/{post_id}/approve")
def approve(post_id: int):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status == "published": raise HTTPException(409, "Already published")
        post.status = "approved"; post.error = ""; db.commit()
    return RedirectResponse(url="/editorial?notice=Post+approved.+It+is+ready+to+publish", status_code=303)

@app.post("/posts/{post_id}/publish")
def publish(post_id: int, account_id: int = Form(...)):
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post: raise HTTPException(404, "Post not found")
        if post.status != "approved": raise HTTPException(409, "Approve the post before publishing")
        text = post.draft
    try:
        post_id_remote = linkedin_publish(text, account_id)
        with SessionLocal() as db:
            post = db.get(Post, post_id); post.status = "published"; post.published_at = datetime.utcnow(); post.linkedin_post_id = post_id_remote; post.account_id = account_id; post.error = ""; db.commit()
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

def linkedin_redirect_uri(request: Request | None = None) -> str:
    configured = os.getenv("LINKEDIN_REDIRECT_URI", "").strip()
    if configured:
        return configured
    if request is not None:
        return str(request.base_url).rstrip("/") + "/auth/linkedin/callback"
    return "http://localhost:8080/auth/linkedin/callback"


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = ""):
    return templates.TemplateResponse(
    request=request,
    name="settings.html",
    context={
        "request": request,
        "connected": bool(get_linkedin_accounts()),
        "accounts": get_linkedin_accounts(),
        "notice": notice,
        "redirect_uri": linkedin_redirect_uri(request),
    },
)

@app.get("/auth/linkedin/start")
def linkedin_start(request: Request):
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "")
    redirect_uri = linkedin_redirect_uri(request)
    if not client_id or not redirect_uri: return RedirectResponse("/settings?notice=Set+LINKEDIN_CLIENT_ID+and+LINKEDIN_REDIRECT_URI+first", status_code=303)
    state = secrets.token_urlsafe(24); db_set("oauth_state_" + state, "pending")
    params = {"response_type":"code", "client_id":client_id, "redirect_uri":redirect_uri, "state":state, "scope":"openid profile w_member_social"}
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params), status_code=302)

@app.get("/auth/linkedin/callback")
def linkedin_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/settings?notice=" + requests.utils.quote("LinkedIn authorization error: " + error), status_code=303)
    if not code or not state:
        return RedirectResponse("/settings?notice=LinkedIn+did+not+return+an+authorization+code+and+state.+Restart+Connect+LinkedIn.", status_code=303)
    expected = db_get("oauth_state_" + state)
    if not state or expected != "pending": raise HTTPException(400, "Invalid or expired OAuth state")
    data = {"grant_type":"authorization_code", "code":code, "redirect_uri":linkedin_redirect_uri(request), "client_id":os.getenv("LINKEDIN_CLIENT_ID", ""), "client_secret":os.getenv("LINKEDIN_CLIENT_SECRET", "")}
    token_response = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data=data, timeout=30)
    if token_response.status_code != 200: return RedirectResponse("/settings?notice=" + requests.utils.quote("Token exchange failed: " + token_response.text[:200]), status_code=303)
    token = token_response.json().get("access_token", "")
    profile_response = requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization":f"Bearer {token}"}, timeout=30)
    if profile_response.status_code != 200: return RedirectResponse("/settings?notice=" + requests.utils.quote("Could not retrieve LinkedIn member identity: " + profile_response.text[:200]), status_code=303)
    profile = profile_response.json(); sub = str(profile.get("sub", ""))
    if not sub: return RedirectResponse("/settings?notice=LinkedIn+did+not+return+a+member+ID", status_code=303)
    display_name = " ".join(part for part in [profile.get("given_name", ""), profile.get("family_name", "")] if part).strip() or profile.get("name", "LinkedIn member")
    try: save_linkedin_account(token, sub, display_name)
    except Exception as exc: return RedirectResponse("/settings?notice=" + requests.utils.quote(str(exc)), status_code=303)
    db_set("oauth_state_" + state, "")
    return RedirectResponse("/settings?notice=" + requests.utils.quote(f"Connected {display_name} successfully. Total accounts: {len(get_linkedin_accounts())}"), status_code=303)

@app.post("/settings/accounts/{account_id}/disconnect")
def disconnect_account(account_id: int):
    with SessionLocal() as db:
        account = db.get(LinkedInAccount, account_id)
        if account:
            db.delete(account)
            db.commit()
    return RedirectResponse("/settings?notice=Selected+LinkedIn+account+disconnected", status_code=303)

@app.post("/settings/disconnect")
def disconnect_all():
    with SessionLocal() as db:
        db.query(LinkedInAccount).delete()
        db.commit()
    return RedirectResponse("/settings?notice=All+LinkedIn+accounts+disconnected", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, notice: str = ""):
    with SessionLocal() as db:
        listings = db.query(JobListing).order_by(desc(JobListing.discovered_at)).limit(300).all()
    return templates.TemplateResponse(
        request=request, name="jobs.html",
        context={"request": request, "notice": notice, "jobs": listings,
                 "domains": list(JOB_DOMAINS.keys()), "companies": get_job_companies()}
    )

@app.post("/jobs/companies/detect")
def detect_job_company(company_name: str = Form(...), careers_url: str = Form(...)):
    from urllib.parse import urlparse
    try:
        parsed = urlparse(careers_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a valid careers-page URL.")
        response = requests.get(careers_url, headers={"User-Agent":"Mozilla/5.0 LinkedInContentStudio"}, timeout=20)
        response.raise_for_status()
        page = response.text[:2_000_000]
        patterns = [
            ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)"),
            ("lever", r"jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
            ("ashby", r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
        ]
        detected = None
        for provider, pattern in patterns:
            match = re.search(pattern, page, re.I)
            if match:
                detected = {"name": company_name.strip()[:300], "provider": provider, "slug": match.group(1)}
                break
        if not detected:
            return RedirectResponse("/jobs?notice=No+supported+ATS+detected.+This+company+may+use+Workday+or+a+custom+career+site.", status_code=303)
        companies = get_job_companies()
        companies = [c for c in companies if c["name"].lower() != detected["name"].lower()]
        companies.append(detected)
        db_set("job_companies", json.dumps(companies))
        return RedirectResponse("/jobs?notice=" + requests.utils.quote(f'Detected {detected["provider"]} board for {detected["name"]}.'), status_code=303)
    except Exception as exc:
        return RedirectResponse("/jobs?notice=" + requests.utils.quote(f"ATS detection failed: {str(exc)[:180]}"), status_code=303)

@app.post("/jobs/companies/save")
def save_job_companies(companies_text: str = Form("")):
    companies = []
    for raw_line in companies_text.splitlines():
        parts = [p.strip() for p in raw_line.split("|")]
        if len(parts) == 3 and all(parts):
            name, provider, slug = parts
            provider = provider.lower()
            if provider in {"greenhouse", "lever", "ashby"}:
                companies.append({"name": name[:300], "provider": provider, "slug": slug[:200]})
    if not companies:
        return RedirectResponse("/jobs?notice=Add+at+least+one+valid+company+entry", status_code=303)
    db_set("job_companies", json.dumps(companies))
    return RedirectResponse(f"/jobs?notice=Saved+{len(companies)}+company+sources", status_code=303)

@app.post("/jobs/search")
def search_jobs(
    domains: list[str] = Form([]),
    custom_domain: str = Form(""),
    keywords: str = Form(""),
    location: str = Form("India"),
    experience: str = Form("All experience levels"),
):
    valid_domains = [d for d in domains if d in JOB_DOMAINS]
    custom_domain = custom_domain.strip()[:100]
    terms = []
    for domain in valid_domains:
        terms.extend(JOB_DOMAINS[domain])
    if custom_domain:
        terms.append(custom_domain.lower())
    if keywords.strip():
        terms.extend([x.strip().lower() for x in keywords.split(",") if x.strip()])
    terms = list(dict.fromkeys(terms))
    companies = get_job_companies()
    found_count = 0
    errors = []
    with SessionLocal() as db:
        for company in companies:
            try:
                for item in fetch_company_jobs(company):
                    haystack = (item["title"] + " " + item["description"]).lower()
                    if terms and not any(term in haystack for term in terms):
                        continue
                    if location.strip() and location.strip().lower() not in {"anywhere", "all"}:
                        loc = (item.get("location", "") + " " + item.get("workplace_type", "")).lower()
                        wanted = location.strip().lower()
                        if wanted == "india":
                            india_places = ("india", "bengaluru", "bangalore", "mumbai", "delhi", "new delhi", "gurugram", "gurgaon", "noida", "hyderabad", "pune", "chennai", "kolkata", "ahmedabad", "jaipur", "kochi", "thiruvananthapuram", "indore", "lucknow", "chandigarh")
                            is_remote = "remote" in loc or "remote" in item.get("workplace_type", "").lower()
                            is_india_location = any(place in loc for place in india_places)
                            if is_remote or not is_india_location:
                                continue
                        elif wanted == "remote":
                            if "remote" not in loc:
                                continue
                        elif wanted == "worldwide":
                            if "worldwide" not in loc and "anywhere" not in loc:
                                continue
                        elif wanted not in loc:
                            continue
                    existing = db.query(JobListing).filter_by(external_id=item["external_id"]).first()
                    if existing:
                        existing.title = item["title"]
                        existing.location = item["location"]
                        existing.description = item["description"]
                        existing.apply_url = item["apply_url"]
                        existing.posted_at = item["posted_at"]
                        continue
                    db.add(JobListing(
                        external_id=item["external_id"], company=item["company"],
                        title=item["title"], location=item["location"],
                        workplace_type=item.get("workplace_type", ""),
                        employment_type=item.get("employment_type", ""),
                        domain=", ".join(valid_domains) or custom_domain or "All domains",
                        description=item["description"], apply_url=item["apply_url"],
                        source=item["source"], posted_at=str(item["posted_at"] or "")
                    ))
                    found_count += 1
            except Exception as exc:
                errors.append(f'{company["name"]}: {str(exc)[:120]}')
        db.commit()
    message = f"Search complete: {found_count} new matching jobs found."
    if errors:
        message += f" {len(errors)} company source(s) could not be checked."
    return RedirectResponse("/jobs?notice=" + requests.utils.quote(message), status_code=303)

@app.post("/jobs/{job_id}/generate")
def generate_job_post(job_id: int):
    with SessionLocal() as db:
        job = db.get(JobListing, job_id)
        if not job:
            raise HTTPException(404, "Job listing not found")
        item = {
            "category": "careers", "topic": "tech_careers",
            "title": f'{job.title} — {job.company}',
            "source_name": f'{job.company} careers ({job.source})',
            "url": job.apply_url,
            "published": job.posted_at or "",
            "summary": (
                f'Job title: {job.title}. Company: {job.company}. '
                f'Location: {job.location}. Workplace: {job.workplace_type}. '
                f'Employment: {job.employment_type}. Job description: {job.description[:3500]}. '
                f'Application link: {job.apply_url}. '
                'Create a factual job opportunity announcement. Preserve the exact application URL.'
            )
        }
        draft = create_draft(item)
        fp = fingerprint(job.apply_url, job.title)
        existing_post = db.query(Post).filter_by(fingerprint=fp).first()
        if existing_post:
            return RedirectResponse("/?notice=This+job+already+has+a+post", status_code=303)
        db.add(Post(
            category="careers", topic="tech_careers", title=item["title"][:500],
            source_url=job.apply_url, source_name=item["source_name"],
            source_published=job.posted_at or "", draft=draft,
            status="pending", fingerprint=fp
        ))
        db.commit()
    return RedirectResponse("/?notice=Job+post+generated+in+Drafts", status_code=303)


@app.post("/posts/{post_id}/image")
def generate_post_image(post_id: int):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return RedirectResponse("/?notice=Set+OPENAI_API_KEY+in+Render+to+enable+post+image+generation", status_code=303)
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        prompt = (
            "Create a polished, professional LinkedIn editorial visual. "
            "No text, no logos, no watermarks. The visual should communicate this post's subject "
            "through a clear central concept, tasteful composition, and premium art direction. "
            "Use a visual style appropriate to the topic and industry. Post title: "
            + post.title[:400] + ". Post content: " + post.draft[:1800]
        )
        try:
            response = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"), "prompt": prompt,
                      "size": "1024x1024", "quality": "medium"},
                timeout=180,
            )
            response.raise_for_status()
            image_data = response.json()["data"][0].get("b64_json")
            if not image_data:
                raise RuntimeError("Image API returned no image data.")
            from pathlib import Path
            image_dir = Path("static/generated")
            image_dir.mkdir(parents=True, exist_ok=True)
            filename = f"post_{post.id}_{secrets.token_hex(5)}.png"
            (image_dir / filename).write_bytes(base64.b64decode(image_data))
            post.image_url = "/static/generated/" + filename
            db.commit()
        except Exception as exc:
            return RedirectResponse("/?notice=" + requests.utils.quote(f"Image generation failed: {str(exc)[:180]}"), status_code=303)
    return RedirectResponse("/?notice=Post+image+generated", status_code=303)


# Editorial taxonomy: the same topic can be explored through different domains.
EDITORIAL_TAXONOMY = {
 "data_science": {"label":"Data Science & Analytics","topics":["Data analysis","SQL & databases","Statistics & experimentation","Business analytics","Data engineering","Forecasting & optimisation","Dashboards & storytelling"],"angles":["SQL interview challenge","Explain a concept with an example","Practical project walkthrough","Real-world business case","Industry development decoded","Career skills roadmap"]},
 "ai_research": {"label":"AI, ML & Research","topics":["Machine learning","Deep learning","Neural networks","NLP","LLMs & generative AI","Computer vision","AI research & benchmarks","AI tools & models","AI ethics & governance"],"angles":["Research paper decoded","Model comparison","Build it step by step","Concept explained simply","New breakthrough decoded","Business application","Myth versus reality"]},
 "agentic_ai": {"label":"Agentic AI & Automation","topics":["AI agents","Multi-agent systems","Agent frameworks","Tool calling & MCP","Agent memory","Coding agents","Enterprise agents","Agent evaluation","Agent security"],"angles":["Build an agent tutorial","Architecture breakdown","Framework comparison","Enterprise use case","New product decoded","Security failure analysis"]},
 "business_decoding": {"label":"Startup & Business Decoding","topics":["Emerging startups","Newly funded startups","Founder stories","Business models","Unit economics","Venture capital","Product innovation","Market disruption","Startup failures","Competitor analysis"],"angles":["Startup spotlight","How it makes money","Why it is growing","Founder decision decoded","Competitor versus incumbent","Funding round explained","What could go wrong"]},
 "marketing": {"label":"Branding & Marketing","topics":["Emerging brands","Brand positioning & identity","Campaigns & advertisements","Viral marketing","Consumer psychology","Growth marketing","Influencer marketing","Product launches","Promotions & pricing","Brand crises & rebranding","Martech & AI marketing"],"angles":["Campaign decoded","Brand strategy breakdown","Consumer psychology explained","Creative strategy analysis","Promotion mechanics","Competitor positioning","What marketers can learn"]},
 "cloud_automation": {"label":"Cloud, Technology & Automation","topics":["AWS, Azure & GCP","Cloud architecture","DevOps & CI/CD","Docker & Kubernetes","Data platforms","APIs & integrations","Cybersecurity","Open source releases","Automation workflows"],"angles":["Hands-on tutorial","Architecture explained","Tool comparison","Pricing decoded","Product release analysis","Implementation case study"]},
 "careers": {"label":"Technology Careers","topics":["Career roadmaps","SQL & Python interviews","Machine learning interviews","System design","Portfolio projects","Hiring trends","Skills intelligence","Resume & LinkedIn","Career transitions"],"angles":["Interview question with solution","30-day learning roadmap","Portfolio project brief","Skill gap analysis","Hiring trend decoded","Career myth versus reality"]}
}
CONTENT_DOMAINS = {
 "education":"Education & learning", "industry_news":"Industry news & announcements", "research":"Research & publications", "business":"Business intelligence & case studies", "emerging_players":"Emerging startups & brands", "tools":"Tools, tutorials & implementation", "careers":"Careers & hiring", "opinion":"Opinion, debate & thought leadership", "media":"Campaigns, media & creative work", "evergreen":"Evergreen explainers"
}
EDITORIAL_ANGLES = {
 "explain_why_it_matters":"Explain a concept and why it matters", "step_by_step":"Step-by-step tutorial", "challenge":"Question, challenge or quiz", "case_study":"Real-world case study", "decoded":"Decode a company, product or campaign", "comparison":"Compare alternatives", "trend_analysis":"Trend analysis", "research_summary":"Research paper / report decoded", "contrarian":"Contrarian viewpoint", "career_roadmap":"Career roadmap or interview prep", "founder_story":"Founder or origin story", "how_it_makes_money":"How the business makes money"
}

def _idea_query(category, topic, domain, angle, custom_topic=""):
    subject = (custom_topic or topic or "").strip()
    if domain == "education":
        return f'{subject} tutorial OR explained OR examples OR exercises OR interview questions'
    if domain == "research":
        return f'{subject} research paper OR study OR benchmark OR arxiv'
    if domain == "emerging_players":
        return f'{subject} startup launch OR funding OR founder OR company'
    if domain == "business":
        return f'{subject} business model OR market analysis OR revenue OR case study'
    if domain == "tools":
        return f'{subject} tool release OR tutorial OR documentation OR GitHub'
    if domain == "careers":
        return f'{subject} skills OR hiring trends OR interview OR career roadmap'
    if domain == "media":
        return f'{subject} brand campaign OR advertising OR promotion OR marketing campaign'
    if domain == "opinion":
        return f'{subject} debate OR analysis OR industry perspective'
    if domain == "evergreen":
        return f'{subject} fundamentals OR guide OR explained OR practical examples'
    return f'{subject} latest news OR announcement OR launch OR industry development'

DYNAMIC_CATEGORIES = {
 "data_science": "Data Science & Analytics",
 "ai_research": "AI & Machine Learning",
 "agentic_ai": "Agentic AI",
 "business_decoding": "Startups & Business",
 "marketing": "Branding & Marketing",
 "cloud_automation": "Cloud & Technology",
 "product_innovation": "Product & Innovation",
 "finance_business": "Finance & Business",
 "careers": "Technology Careers",
 "industry_news": "Industry & Tech News",
}

def _ai_json(prompt):
    """Use configured text providers in the same fallback order as drafting."""
    errors=[]
    key=os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        try:
            with genai.Client(api_key=key) as client:
                r=client.models.generate_content(model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), contents=prompt)
            raw=(r.text or "").strip()
            raw=re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
            return json.loads(raw)
        except Exception as e: errors.append(f"Gemini: {e}")
    key=os.getenv("GROQ_API_KEY", "").strip()
    if key:
        try:
            r=Groq(api_key=key).chat.completions.create(model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),messages=[{"role":"user","content":prompt}],temperature=0.65,max_tokens=1600)
            raw=(r.choices[0].message.content or "").strip(); raw=re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
            return json.loads(raw)
        except Exception as e: errors.append(f"Groq: {e}")
    key=os.getenv("CEREBRAS_API_KEY", "").strip()
    if key:
        try:
            r=Cerebras(api_key=key).chat.completions.create(model=os.getenv("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct"),messages=[{"role":"user","content":prompt}],temperature=0.65,max_completion_tokens=1600)
            raw=(r.choices[0].message.content or "").strip(); raw=re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
            return json.loads(raw)
        except Exception as e: errors.append(f"Cerebras: {e}")
    raise RuntimeError("AI providers unavailable or returned invalid JSON. " + " | ".join(errors))

@app.post("/discover/generate-stage")
async def discover_generate_stage(request: Request):
    payload=await request.json()
    stage=(payload.get("stage") or "").strip()
    category=(payload.get("category") or "").strip()
    if category not in DYNAMIC_CATEGORIES: raise HTTPException(400,"Choose a valid main category")
    label=DYNAMIC_CATEGORIES[category]
    if stage=="subtopics":
        instruction=f"For the broad category '{label}', propose 8 diverse, useful subtopics spanning learning, practical work, interviews, industry/business applications and current developments where relevant. Avoid generic labels."
        field="subtopics"
    elif stage=="tracks":
        subtopic=(payload.get("subtopic") or "").strip()[:180]
        if not subtopic: raise HTTPException(400,"Choose a subtopic")
        instruction=f"For category '{label}' and subtopic '{subtopic}', propose 6 distinct learning/content tracks or angles. Mix fundamentals, advanced concepts, practical implementation, interview preparation, real-world cases and critical analysis as relevant."
        field="tracks"
    elif stage=="ideas":
        subtopic=(payload.get("subtopic") or "").strip()[:180]
        track=(payload.get("track") or "").strip()[:180]
        if not subtopic or not track: raise HTTPException(400,"Choose a subtopic and track")
        instruction=f"For category '{label}', subtopic '{subtopic}', and track '{track}', create 6 highly specific LinkedIn content ideas. Each should teach something or offer a defensible insight. Avoid generic news headlines, invented statistics and repetitive ideas. Return objects with title and summary (2-3 sentences describing what the post will actually explain)."
        field="ideas"
    else: raise HTTPException(400,"Unknown generation stage")
    prompt=("You are a rigorous editorial taxonomy designer for a professional LinkedIn content studio. "+instruction+f" Return ONLY valid JSON in this exact shape: {{\"{field}\": ["+("{\"title\":\"...\",\"summary\":\"...\"}" if field=="ideas" else "\"...\"")+ "]}}. No markdown fences.")
    try:
        result=_ai_json(prompt)
        values=result.get(field,[])
        if not isinstance(values,list) or not values: raise ValueError("AI returned no options")
        if field!="ideas": values=[str(v).strip()[:180] for v in values if str(v).strip()][:10]
        else:
            clean=[]
            with SessionLocal() as db:
                for value in values[:8]:
                    if not isinstance(value,dict) or not value.get("title"): continue
                    title=str(value["title"]).strip()[:500]
                    summary=str(value.get("summary","")).strip()[:2500]
                    exists=db.query(EditorialIdea).filter_by(category=category,topic=subtopic,title=title).first()
                    if exists: clean.append({"id":exists.id,"title":exists.title,"summary":exists.summary}); continue
                    idea=EditorialIdea(category=category,topic=subtopic[:80],content_domain="education",editorial_angle="explain_why_it_matters",title=title,summary=summary+f"\n\nSelected track: {track}",source_name="AI-generated editorial idea",audience="professionals",difficulty="intermediate",status="idea")
                    db.add(idea); db.flush(); clean.append({"id":idea.id,"title":title,"summary":summary})
                db.commit()
            values=clean
        return JSONResponse({field:values})
    except Exception as exc:
        raise HTTPException(502, f"Could not generate the next step: {str(exc)[:350]}")

@app.get("/topics", response_class=HTMLResponse)
def topic_library(request: Request, notice: str = "", q: str = ""):
    with SessionLocal() as db:
        query=db.query(EditorialIdea).order_by(desc(EditorialIdea.created_at))
        if q.strip(): query=query.filter((EditorialIdea.title.ilike(f"%{q.strip()}%")) | (EditorialIdea.topic.ilike(f"%{q.strip()}%")) | (EditorialIdea.summary.ilike(f"%{q.strip()}%")))
        ideas=query.limit(200).all()
    return templates.TemplateResponse(request=request,name="topics.html",context={"request":request,"ideas":ideas,"notice":notice,"q":q,"categories":DYNAMIC_CATEGORIES})

@app.get("/discover", response_class=HTMLResponse)
def discover_page(request: Request, notice: str = ""):
    with SessionLocal() as db:
        ideas = db.query(EditorialIdea).order_by(desc(EditorialIdea.created_at)).limit(40).all()
    return templates.TemplateResponse(request=request, name="discover.html", context={"request":request,"notice":notice,"taxonomy":DYNAMIC_CATEGORIES,"domains":CONTENT_DOMAINS,"angles":EDITORIAL_ANGLES,"ideas":ideas})

@app.post("/discover/search")
def discover_search(category: str = Form(...), topic: str = Form(""), content_domain: str = Form("industry_news"), editorial_angle: str = Form("explain_why_it_matters"), audience: str = Form("professionals"), difficulty: str = Form("intermediate"), custom_topic: str = Form(""), period: str = Form("latest"), post_format: str = Form("narrative"), post_tone: str = Form("professional"), point_of_view: str = Form("balanced"), hook_style: str = Form("strong_insight"), post_length: str = Form("medium"), cta_style: str = Form("insight"), creation_notes: str = Form("")):
    if category not in EDITORIAL_TAXONOMY: category = "data_science"
    if content_domain not in CONTENT_DOMAINS: content_domain = "industry_news"
    if editorial_angle not in EDITORIAL_ANGLES: editorial_angle = "explain_why_it_matters"
    selected_topic = custom_topic.strip()[:180] or topic.strip()[:180]
    # Keep the creation brief with the idea so it reaches the drafting prompt later.
    format_labels = {"narrative":"Insight-led narrative", "breakdown":"Structured breakdown", "points":"Actionable points", "case_study":"Case study", "contrarian":"Contrarian perspective", "hook":"Hook-led post", "framework":"Framework / model", "story":"Story with lesson"}
    tone_labels = {"professional":"Professional & authoritative", "conversational":"Conversational", "analytical":"Analytical & data-led", "bold":"Bold & challenging", "educational":"Clear & educational"}
    pov_labels = {"balanced":"Balanced analysis", "first_person":"First-person perspective", "expert":"Expert commentary", "beginner":"Beginner-friendly explainer", "operator":"Practitioner / operator lens"}
    hook_labels = {"strong_insight":"Strong insight", "question":"Thought-provoking question", "statistic":"Evidence-backed statistic (source required)", "problem":"Problem-first opening", "story":"Short scenario; do not invent personal experience", "direct":"Direct statement"}
    length_labels = {"short":"80–130 words", "medium":"130–220 words", "long":"220–350 words"}
    cta_labels = {"insight":"Memorable closing insight", "question":"Invite thoughtful discussion", "action":"Practical next step", "none":"No CTA; clean ending"}
    creation_brief = (f"CONTENT CREATION SETTINGS: Format: {format_labels.get(post_format, 'Insight-led narrative')}. Tone: {tone_labels.get(post_tone, 'Professional & authoritative')}. Point of view: {pov_labels.get(point_of_view, 'Balanced analysis')}. Opening: {hook_labels.get(hook_style, 'Strong insight')}. Length: {length_labels.get(post_length, '130–220 words')}. Ending: {cta_labels.get(cta_style, 'Memorable closing insight')}. Specific instructions: {creation_notes.strip()[:1200] or 'None'}. Follow these settings when drafting.")
    if not selected_topic:
        return RedirectResponse("/discover?notice=Choose+a+topic+or+enter+a+custom+topic", status_code=303)
    query = _idea_query(category, selected_topic, content_domain, editorial_angle)
    if period == "month":
        query += " when:30d"
    elif period == "evergreen":
        query += " fundamentals OR guide OR tutorial"
    new_count = 0
    # Education and evergreen are idea-first: no artificial requirement for a fresh news article.
    if content_domain in {"education", "evergreen"}:
        titles = [
            f"{selected_topic}: the fundamentals explained with a practical example",
            f"A hands-on {selected_topic} exercise to test your understanding",
            f"5 common mistakes people make when learning {selected_topic}",
            f"A beginner-to-intermediate learning path for {selected_topic}",
            f"{selected_topic} interview challenge: solve it step by step",
        ]
        with SessionLocal() as db:
            for title in titles:
                db.add(EditorialIdea(category=category,topic=selected_topic,content_domain=content_domain,editorial_angle=editorial_angle,title=title,summary=f"Original educational content brief for {selected_topic}. Audience: {audience}. Level: {difficulty}. Build a correct explanation, practical example and learner exercise. Verify technical details before publishing.\n{creation_brief}",audience=audience,difficulty=difficulty,status="idea"))
                new_count += 1
            db.commit()
    else:
        url = "https://news.google.com/rss/search?q=" + requests.utils.quote(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            feed = feedparser.parse(url)
            entries = feed.entries[:18]
        except Exception:
            entries = []
        with SessionLocal() as db:
            for entry in entries:
                title = (entry.get("title") or "").strip()
                link = entry.get("link") or ""
                if not title or not link: continue
                exists = db.query(EditorialIdea).filter_by(title=title, source_url=link).first()
                if exists: continue
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "")[:3000]
                db.add(EditorialIdea(category=category,topic=selected_topic,content_domain=content_domain,editorial_angle=editorial_angle,title=title,summary=(summary or "") + "\n" + creation_brief,source_url=link,source_name="Google News discovery",audience=audience,difficulty=difficulty,status="idea"))
                new_count += 1
            db.commit()
    return RedirectResponse("/discover?notice=" + requests.utils.quote(f"Added {new_count} idea(s). Review the sources and choose what to develop."), status_code=303)

@app.post("/ideas/{idea_id}/research")
def mark_idea_researching(idea_id: int):
    with SessionLocal() as db:
        idea = db.get(EditorialIdea, idea_id)
        if not idea: raise HTTPException(404, "Idea not found")
        idea.status = "researching"
        db.commit()
    return RedirectResponse("/editorial?notice=Idea+moved+to+Researching", status_code=303)

@app.post("/ideas/{idea_id}/generate")
def generate_from_idea(idea_id: int):
    with SessionLocal() as db:
        idea = db.get(EditorialIdea, idea_id)
        if not idea: raise HTTPException(404, "Idea not found")
        url = idea.source_url or ("https://www.google.com/search?q=" + requests.utils.quote(idea.topic or idea.title))
        source_name = idea.source_name or "Original educational brief"
        if idea.content_domain in {"education", "evergreen"}:
            url = "https://www.google.com/search?q=" + requests.utils.quote(idea.topic or idea.title)
            source_name = "Original educational brief · search reference"
        idea_category = idea.category
        item = {"category":idea_category if idea_category in DISPLAY_CATEGORIES else "data_science","topic":idea.topic,"title":idea.title,"source_name":source_name,"url":url,"published":"","summary":(idea.summary or "") + f"\nEditorial domain: {CONTENT_DOMAINS.get(idea.content_domain,idea.content_domain)}. Editorial angle: {EDITORIAL_ANGLES.get(idea.editorial_angle,idea.editorial_angle)}. Audience: {idea.audience}. Difficulty: {idea.difficulty}. Do not invent facts or statistics."}
        draft = create_draft(item)
        fp = fingerprint(url, idea.title + str(idea.id))
        post = Post(category=item["category"],topic=idea.topic[:60],title=idea.title[:500],source_url=url,source_name=item["source_name"],source_published="",draft=draft,status="pending",fingerprint=fp,content_domain=idea.content_domain,editorial_angle=idea.editorial_angle,audience=idea.audience,difficulty=idea.difficulty,content_brief=idea.summary or "")
        db.add(post); idea.status="drafted"; db.commit()
    return RedirectResponse("/?notice=Idea+turned+into+a+draft", status_code=303)

@app.get("/editorial", response_class=HTMLResponse)
def editorial_board(request: Request, notice: str = ""):
    with SessionLocal() as db:
        columns = {status: db.query(Post).filter(Post.status == status).order_by(desc(Post.created_at)).limit(60).all() for status in ["pending","approved","published","rejected","failed"]}
        ideas = db.query(EditorialIdea).filter(EditorialIdea.status.in_(["idea", "researching"])).order_by(desc(EditorialIdea.created_at)).limit(24).all()
    return templates.TemplateResponse(request=request,name="editorial.html",context={"request":request,"notice":notice,"columns":columns,"ideas":ideas,"domains":CONTENT_DOMAINS,"angles":EDITORIAL_ANGLES,"categories":DISPLAY_CATEGORIES,"accounts":get_linkedin_accounts()})

@app.get("/posts/{post_id}/history", response_class=HTMLResponse)
def post_history(request: Request, post_id: int):
    with SessionLocal() as db:
        post=db.get(Post,post_id)
        if not post: raise HTTPException(404,"Post not found")
        revisions=db.query(PostRevision).filter_by(post_id=post_id).order_by(desc(PostRevision.created_at)).all()
    return templates.TemplateResponse(request=request,name="history.html",context={"request":request,"post":post,"revisions":revisions})

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
