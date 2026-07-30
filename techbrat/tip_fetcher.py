"""
tip_fetcher.py — Motivation & Tips Discovery with AI Tech Validation
====================================================================
Validates tips are tech-related, fetches real tips, and generates
actionable motivation via AI fallback.
"""

import json
from datetime import datetime, timedelta
from django.conf import settings
from django.utils import timezone
from techbrat.models import Tip
from utils.openrouter_client import post_openrouter


# ─── Non-Tech Keywords (Fast Filter) ───────────────────────────

NON_TECH_KEYWORDS = {
    'gym', 'fitness', 'diet', 'exercise', 'weight', 'calorie',
    'muscle', 'protein', 'sports', 'football', 'cricket',
    'fashion', 'style', 'clothing', 'makeup', 'beauty',
    'music', 'singing', 'dancing', 'art', 'painting',
    'cooking', 'recipe', 'food', 'restaurant', 'chef',
    'astrology', 'horoscope', 'religion', 'politics'
}


def is_tip_tech_related(title, explanation):
    """
    Two-stage tech validation:
    1. Quick check against known non-tech keywords
    2. AI validation if unclear
    
    Returns: (is_tech, message)
    """
    combined_text = f"{title} {explanation}".lower().strip()
    
    # Stage 1: Fast filter
    for keyword in NON_TECH_KEYWORDS:
        if keyword in combined_text:
            return False, "This section provides only tech-related motivation and learning tips."
    
    # Stage 2: AI validation for unclear content
    api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
    if not api_key:
        return True, ""
    
    try:
        response = post_openrouter(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': getattr(settings, 'OPENROUTER_MODEL', 'google/gemma-4-26b-a4b-it:free'),
                'messages': [
                    {
                        'role': 'user',
                        'content': f"""Is this motivation/tip related to learning programming or technology career growth?

Title: {title}
Explanation: {explanation}

Answer YES or NO only:"""
                    }
                ],
                'temperature': 0.3,
                'max_tokens': 10,
            },
            timeout=5
        )
        
        if response.status_code == 200:
            answer = response.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip().upper()
            is_tech = 'YES' in answer
            return is_tech, "" if is_tech else "This section provides only tech-related motivation and learning tips."
    except Exception as e:
        print(f"Tip validation error: {str(e)}")
        # If API fails, assume tech if not in blocked list
        return True, ""
    
    return True, ""


def fetch_tips_from_db(category=None, limit=5):
    """Fetch tips from database with optional category filter."""
    tips = Tip.objects.filter(is_ai_generated=False)
    
    if category and category != 'all':
        tips = tips.filter(category=category)
    
    return tips[:limit]


def get_daily_tip():
    """Get a tip marked as daily boost, or random high-quality tip."""
    # Try to get one marked as daily boost
    daily = Tip.objects.filter(daily_boost=True).first()
    if daily:
        return {
            'title': daily.title,
            'explanation': daily.explanation,
            'category': daily.category,
            'icon': daily.icon,
        }
    
    # Fallback: get most recent tip
    tip = Tip.objects.order_by('-created_at').first()
    if tip:
        return {
            'title': tip.title,
            'explanation': tip.explanation,
            'category': tip.category,
            'icon': tip.icon,
        }
    
    # Ultimate fallback
    return {
        'title': 'Keep Building',
        'explanation': "Every line of code you write makes you stronger. Consistency beats intensity.",
        'category': 'consistency',
        'icon': 'fa-rocket',
    }


def generate_tips_via_ai(category=None, count=5):
    """
    Use AI to generate practical tech motivation tips.
    Returns: list[dict] with tip data
    """
    api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
    if not api_key:
        return []
    
    category_text = category if category and category != 'all' else 'general tech learning and career growth'
    
    prompt = f"""You are an expert tech mentor and life coach for programmers. Generate {count} PRACTICAL, ACTIONABLE motivation tips.

Category: {category_text}

CRITICAL RULES:
1. Each tip must be SPECIFIC to programming/tech learning
2. No generic life advice
3. No fitness/diet/fashion content
4. Focus on: consistency, learning mindset, practical coding habits, career growth
5. Each tip must include: title, explanation, action_step
6. Response MUST be valid JSON only, NO markdown

Return ONLY a JSON array with this exact structure:
[
  {{
    "title": "Build, Don't Just Watch",
    "explanation": "Instead of only watching tutorials, build something small after each lesson to reinforce learning.",
    "action_step": "Create a mini project today using what you learned.",
    "category": "productivity|learning_strategy|coding_practice|career_growth|consistency|mindset",
    "icon": "fa-lightbulb"
  }}
]

Examples of GOOD tips:
- Title: "Code Every Day, Even 15 Minutes"
  Explanation: Consistency beats marathon sessions. Daily practice builds muscle memory.
  Action: Open your project and write 15 lines today.

- Title: "Build Projects, Not Just Follow Tutorials"
  Explanation: Tutorials teach concepts but projects teach you how to think like a developer.
  Action: Start a small project this week.

- Title: "Read Others' Code"
  Explanation: Learning from well-written code improves your problem-solving skills.
  Action: Find an open-source project and study 3 functions.

Generate NOW:"""

    try:
        response = post_openrouter(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': getattr(settings, 'OPENROUTER_MODEL', 'google/gemma-4-26b-a4b-it:free'),
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.7,
            },
            timeout=10
        )
        
        if response.status_code == 200:
            text = response.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            
            # Extract JSON from response (may have markdown code blocks)
            if '```json' in text:
                text = text.split('```json')[1].split('```')[0]
            elif '```' in text:
                text = text.split('```')[1].split('```')[0]
            
            tips_data = json.loads(text)
            
            # Validate and clean data
            valid_tips = []
            for tip in tips_data:
                if all(k in tip for k in ['title', 'explanation', 'action_step', 'category']):
                    # Validate tech-related
                    is_tech, _ = is_tip_tech_related(tip['title'], tip['explanation'])
                    if is_tech:
                        # Create tip in DB if it doesn't exist
                        Tip.objects.get_or_create(
                            title=tip['title'],
                            defaults={
                                'explanation': tip.get('explanation', ''),
                                'action_step': tip.get('action_step', ''),
                                'category': tip.get('category', 'productivity'),
                                'icon': tip.get('icon', 'fa-lightbulb'),
                                'is_ai_generated': True,
                            }
                        )
                        valid_tips.append(tip)
            
            return valid_tips[:count]
    except Exception as e:
        print(f"AI tip generation error: {str(e)}")
        return []
    
    return []


def get_tips_hybrid(category=None, limit=5):
    """
    Hybrid tip fetching:
    1. Try database first
    2. If results < threshold, generate via AI
    
    Returns: list[dict] with tips
    """
    db_tips = fetch_tips_from_db(category, limit)
    
    # Convert to dict format
    tips_list = [
        {
            'title': t.title,
            'explanation': t.explanation,
            'action_step': t.action_step,
            'category': t.category,
            'icon': t.icon,
            'is_ai_generated': False,
        }
        for t in db_tips
    ]
    
    # If not enough results, generate via AI
    if len(tips_list) < 3:
        ai_tips = generate_tips_via_ai(category, count=limit - len(tips_list))
        tips_list.extend(ai_tips)
    
    return tips_list[:limit]


def get_consistency_streak(user):
    """
    Calculate user's learning consistency streak.
    Returns: streak_days, last_visit_date
    """
    if not user or not user.is_authenticated:
        return 0, None
    
    # A simple approach: check if user has been active recently
    # In production, track actual learning activity
    last_login = user.last_login
    if not last_login:
        return 0, None
    
    today = timezone.now()
    diff = (today - last_login).days
    
    # If logged in today or yesterday, streak is 1+
    if diff <= 1:
        streak = 1
    else:
        streak = 0
    
    return streak, last_login
