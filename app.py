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
    "marketing": {
        "label": "Legacy marketing content",
        "weight": 0,
    },
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
    if "topic" not in columns:
        connection.execute(
            text("ALTER TABLE posts ADD COLUMN topic VARCHAR(60)")
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

    preferences = get_writing_preferences()
    post_format = preferences["format"]
    tone = preferences["tone"]
    voice = preferences["voice"]
    depth = preferences["depth"]
    length = preferences["length"]

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

    business_instructions = ""
    if item["category"] == "business_decoding":
        business_instructions = """
For business and startup topics, examine the business model, customer value,
revenue logic, cost drivers, distribution, competitive advantage and strategic
trade-offs where the source supports them. Choose only the most relevant angles.
Never invent financial figures, market share, margins or internal practices.
"""

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
Format: {post_format}. {format_guidance}
Tone: {tone}. {tone_guidance}
Voice: {voice}. {voice_guidance}
Analysis depth: {depth}. {depth_guidance}
Length: {length_guidance}.

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
- Use 2-4 relevant hashtags, only if they add value.
- Put the source URL on the final line.
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
                return draft
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
                return draft
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
                return draft
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
            "topics": TOPICS,
            "selected_topics": get_selected_topics(),
            "writing_preferences": get_writing_preferences(),
            "notice": notice,
            "planned_posts": planned_posts,
            "connected": bool(get_token()),
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
        "connected": bool(get_token()),
        
        "notice": notice,
        "redirect_uri": linkedin_redirect_uri(request),
    },
)

@app.get("/auth/linkedin/start")
def linkedin_start(request: Request):
    client_id = os.getenv("LINKEDIN_CLIENT_ID", "")
    redirect_uri = linkedin_redirect_uri(request)
    if not client_id or not redirect_uri: return RedirectResponse("/settings?notice=Set+LINKEDIN_CLIENT_ID+and+LINKEDIN_REDIRECT_URI+first", status_code=303)
    state = secrets.token_urlsafe(24); db_set("oauth_state", state)
    params = {"response_type":"code", "client_id":client_id, "redirect_uri":redirect_uri, "state":state, "scope":"openid profile w_member_social"}
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params), status_code=302)

@app.get("/auth/linkedin/callback")
def linkedin_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/settings?notice=" + requests.utils.quote("LinkedIn authorization error: " + error), status_code=303)
    if not code or not state:
        return RedirectResponse("/settings?notice=LinkedIn+did+not+return+an+authorization+code+and+state.+Restart+Connect+LinkedIn.", status_code=303)
    expected = db_get("oauth_state")
    if not state or not expected or not secrets.compare_digest(state, expected): raise HTTPException(400, "Invalid OAuth state")
    data = {"grant_type":"authorization_code", "code":code, "redirect_uri":linkedin_redirect_uri(request), "client_id":os.getenv("LINKEDIN_CLIENT_ID", ""), "client_secret":os.getenv("LINKEDIN_CLIENT_SECRET", "")}
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
