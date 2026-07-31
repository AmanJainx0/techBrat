import requests
from django.conf import settings
from django.core.cache import cache
import random
from urllib.parse import quote
from utils.ai_client import chat_completion, is_ai_configured

TECH_NORMALIZATION_MAP = {
    "redi": "Redis",
    "ai": "AI/ML",
    "ml": "AI/ML",
    "machine learning": "AI/ML",
    "artificial intelligence": "AI/ML",
    "js": "JavaScript",
    "reactjs": "React",
    "node": "Node.js",
    "py": "Python",
    "k8s": "Kubernetes",
    "aws": "AWS",
}

TECH_ALTERNATIVES = [
    "Web Development, AI/ML, or Databases",
    "Cloud Computing, Mobile Development, or Data Science",
    "Backend Architecture, Frontend Development, or DevOps"
]

TECH_KEYWORDS = {
    "api", "apis", "backend", "frontend", "fullstack", "web", "website",
    "django", "flask", "fastapi", "react", "nextjs", "node", "node.js",
    "python", "javascript", "typescript", "java", "c++", "c#", "go",
    "rust", "sql", "mysql", "postgres", "mongodb", "redis", "database",
    "databases", "docker", "kubernetes", "devops", "cloud", "aws", "azure",
    "gcp", "linux", "git", "github", "programming", "software", "coding",
    "code", "developer", "development", "system design", "ai", "ml",
    "machine learning", "artificial intelligence", "data science",
    "cybersecurity", "security"
}

# Only obvious non-tech words (NOT exhaustive)
NON_TECH_KEYWORDS = [
    "dance", "music", "cooking", "recipe", "sports",
    "gym", "yoga", "fashion", "makeup", "astrology",
    "politics", "religion", "acting", "singing"
]

def is_obviously_non_tech(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in NON_TECH_KEYWORDS)

def normalize_tech_query(query: str) -> str:
    """Auto-corrects and normalizes common tech queries."""
    if not query:
        return query
    
    lowered = query.strip().lower()
    return TECH_NORMALIZATION_MAP.get(lowered, query.strip())

def get_alternative_suggestions() -> str:
    """Returns a random suggestion string for blocked queries."""
    return random.choice(TECH_ALTERNATIVES)


def contains_obvious_tech(text: str) -> bool:
    text = text.lower()
    return any(keyword in text for keyword in TECH_KEYWORDS)


def ai_is_tech_related(query: str) -> bool:
    if not is_ai_configured():
        return False
    try:
        response = chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict but intelligent classifier.\n"
                        "Answer ONLY YES or NO.\n\n"
                        "Return YES if query is related to:\n"
                        "- programming\n"
                        "- software development\n"
                        "- computer science\n"
                        "- IT\n"
                        "- databases (Redis, MySQL, MongoDB)\n"
                        "- cloud (AWS, Azure)\n"
                        "- DevOps tools (Docker, Kubernetes)\n"
                        "- AI/ML, data science\n"
                        "- backend, frontend, system design\n\n"
                        "Return NO for non-tech topics like:\n"
                        "- cooking, dance, fitness, sports, fashion, music"
                    )
                },
                {"role": "user", "content": query}
            ],
            temperature=0,
            timeout=6
        )

        content = (
            response.json()
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .upper()
        )

        return content == "YES"

    except Exception:
        return False


def is_tech_query(query: str) -> bool:
    if not query:
        return True

    query = query.strip().lower()

    cache_key = f"tech_check_{quote(query, safe='')}"
    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    # Fast rejection
    if is_obviously_non_tech(query):
        cache.set(cache_key, False, 3600)
        return False

    # Fast acceptance for common tech terms so valid prompts don't depend on AI/network
    if contains_obvious_tech(query):
        cache.set(cache_key, True, 3600)
        return True

    # AI validation
    result = ai_is_tech_related(query)
    cache.set(cache_key, result, 3600)

    return result
