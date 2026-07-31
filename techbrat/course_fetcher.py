"""
course_fetcher.py — External Course Fetching & Deduplication
============================================================
Fetches real-world courses via Gemini (OpenRouter) with
platform-specific URLs, and provides deduplication utilities.
"""

import json
from django.conf import settings
from utils.ai_client import chat_completion, is_ai_configured


# ─── External Course Fetcher ───────────────────────────────────

def fetch_external_courses(level, domain, learning_type, is_free):
    """
    Uses Gemini via OpenRouter to generate REAL courses from
    actual platforms (Coursera, Udemy, Khan Academy, etc.)
    with verifiable links.

    Returns list[dict] matching the Course API schema.
    On ANY failure, retries once with a simpler prompt.
    If both fail, returns [] silently.
    """
    if not is_ai_configured():
        return []

    free_label = "Free" if is_free else "Paid"
    domain_text = domain if domain else "general technology"

    # ─── Primary prompt (detailed) ─────────────────────────
    prompt = f"""You are a real-world tech course recommendation engine.

Your job is to find HIGH-QUALITY, REAL courses available on the internet.

User Requirements:
- Skill Level: {level}
- Domain: {domain_text}
- Learning Type: {learning_type}
- Pricing: {free_label}

IMPORTANT RULES:
1. Courses MUST be REAL and available online.
2. Prefer platforms:
   - Coursera
   - Udemy
   - FreeCodeCamp
   - edX
   - YouTube (for free courses)
   - Official documentation/tutorials
   - Khan Academy
   - MIT OCW
   - Codecademy
3. Links MUST be valid and usable. Use REAL platform URLs in these formats:
   - Coursera: https://www.coursera.org/learn/[course-slug]
   - Udemy: https://www.udemy.com/course/[course-slug]/
   - Khan Academy: https://www.khanacademy.org/computing/[path]
   - edX: https://www.edx.org/learn/[subject]/[course-slug]
   - freeCodeCamp: https://www.freecodecamp.org/learn/[path]
   - YouTube: https://www.youtube.com/watch?v=[video-id] or playlist links
   - MIT OCW: https://ocw.mit.edu/courses/[course-slug]
   - Codecademy: https://www.codecademy.com/learn/[course-slug]
4. DO NOT generate fake courses.
5. If exact matches are limited, return CLOSE relevant courses from the same technology family.
6. Include a mix of platforms (at least 3 different platforms).
7. Descriptions should be 2-3 lines, factual.
8. Duration should be realistic (e.g., "4 weeks", "12 hours", "Self-paced").

Return ONLY a valid JSON array (no markdown, no explanation):

[
  {{
    "title": "",
    "platform": "",
    "level": "{level}",
    "domain": "{domain_text}",
    "learning_type": "{learning_type}",
    "is_free": {str(is_free).lower()},
    "duration": "",
    "description": "2-3 line explanation",
    "link": ""
  }}
]"""

    # ─── Try primary prompt ────────────────────────────────
    result = _call_ai(prompt, level, domain_text, learning_type, is_free)
    if result:
        return result

    # ─── Retry with simplified prompt ──────────────────────
    simple_prompt = f"""Generate 6 real {free_label.lower()} online courses for learning {domain_text} at {level} level.
Return ONLY a valid JSON array. Each object must have: title, platform, level, domain, learning_type, is_free, duration, description, link.
Use real platforms like Coursera, Udemy, YouTube, edX, freeCodeCamp.
No markdown, no extra text."""

    result = _call_ai(simple_prompt, level, domain_text, learning_type, is_free)
    return result if result else []


def _call_ai(prompt, level, domain_text, learning_type, is_free):
    """
    Internal helper: makes the actual AI API call and parses/validates the result.
    Returns list[dict] on success, or None on failure.
    """
    try:
        response = chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=15
        )

        if response.status_code != 200:
            return None

        content = response.json()['choices'][0]['message']['content']
        content = content.replace("```json", "").replace("```", "").strip()

        # Find JSON array start
        arr_start = content.find("[")
        if arr_start != -1:
            content = content[arr_start:]

        data = json.loads(content)

        if isinstance(data, list):
            valid = []
            for c in data:
                if isinstance(c, dict) and c.get('title') and c.get('platform') and c.get('link'):
                    valid.append({
                        'title': str(c.get('title', '')),
                        'platform': str(c.get('platform', '')),
                        'level': str(c.get('level', level)),
                        'domain': str(c.get('domain', domain_text)),
                        'learning_type': str(c.get('learning_type', learning_type)),
                        'is_free': bool(c.get('is_free', is_free)),
                        'duration': str(c.get('duration', '')),
                        'description': str(c.get('description', '')),
                        'link': str(c.get('link', '')),
                    })
            return valid if valid else None

        return None

    except Exception as e:
        print(f"OpenRouter call failed: {e}")
        return None


# ─── Deduplication ─────────────────────────────────────────────

def deduplicate_courses(courses_list):
    """
    Remove duplicate courses based on lowercase title + platform.
    Preserves order (first occurrence wins — so DB > External > AI).

    Args:
        courses_list: list of dicts with 'title' and 'platform' keys

    Returns:
        Deduplicated list preserving input order.
    """
    seen = set()
    result = []

    for course in courses_list:
        key = (
            course.get('title', '').strip().lower(),
            course.get('platform', '').strip().lower()
        )
        if key not in seen:
            seen.add(key)
            result.append(course)

    return result
