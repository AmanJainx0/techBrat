"""
tool_fetcher.py — Tool Discovery with AI Tech Validation
=========================================================
Validates queries are technology-related, fetches real tools,
and generates tool recommendations via AI fallback.
"""

import json
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from techbrat.models import Tool
from utils.openrouter_client import post_openrouter


# ─── Non-Tech Keywords (Fast Filter) ───────────────────────────

NON_TECH_KEYWORDS = {
    'dance', 'music', 'singing', 'cooking', 'recipe', 'sports',
    'gym', 'fitness', 'fashion', 'clothing', 'astrology', 'horoscope',
    'politics', 'religion', 'philosophy', 'literature', 'novel',
    'movie', 'film', 'art', 'painting', 'sculpture', 'poetry',
    'gardening', 'plants', 'flowers', 'animals', 'pets', 'travel',
    'photography', 'yoga', 'meditation', 'psychology', 'meditation'
}


def is_query_tech_related(query):
    """
    Two-stage tech validation:
    1. Quick check against known non-tech keywords
    2. AI validation if unclear
    
    Returns: (is_tech, message)
    """
    query_lower = query.lower().strip()
    
    # Stage 1: Fast filter
    for keyword in NON_TECH_KEYWORDS:
        if keyword in query_lower:
            return False, "This platform supports only technology-related tools and platforms."
    
    # Stage 2: AI validation for unclear queries
    api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
    if not api_key:
        # If no AI available, assume tech if not in blocked list
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
                        'content': f"""Is this related to software development, programming, technology, or tech tools?
Query: "{query}"

Respond with ONLY: YES or NO"""
                    }
                ],
                'temperature': 0.3,
            },
            timeout=5
        )
        
        if response.status_code == 200:
            text = response.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip().upper()
            is_tech = 'YES' in text
            return is_tech, "" if is_tech else "This platform supports only technology-related tools and platforms."
    except:
        # If AI fails, assume tech to avoid blocking legitimate queries
        return True, ""
    
    return True, ""


def fetch_tools_from_db(category=None, difficulty=None, use_case=None, search_query=None):
    """Fetch tools from database with filters."""
    tools = Tool.objects.all()
    
    if category and category != 'all':
        tools = tools.filter(category=category)
    if difficulty and difficulty != 'all':
        tools = tools.filter(difficulty=difficulty)
    if use_case and use_case != 'all':
        tools = tools.filter(use_case=use_case)
    if search_query:
        tools = tools.filter(name__icontains=search_query) | tools.filter(description__icontains=search_query)
    
    return tools


def generate_tools_via_ai(category=None, difficulty=None, use_case=None, count=6):
    """
    Use AI to generate real technology tools/platforms.
    Returns: list[dict] with tool data
    """
    api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
    if not api_key:
        return []
    
    category_text = category if category and category != 'all' else 'various technology domains'
    difficulty_text = difficulty if difficulty and difficulty != 'all' else 'all difficulty levels'
    use_case_text = use_case if use_case and use_case != 'all' else 'learning, practice, and production'
    
    prompt = f"""You are a tech tools curator. Generate {count} REAL, existing technology tools or platforms.

Specifications:
- Category: {category_text}
- Difficulty: {difficulty_text}
- Use Cases: {use_case_text}

CRITICAL RULES:
1. Tools MUST be REAL and currently available online
2. No fictional or fake tools
3. Include: name, category, description, use_case, difficulty, link
4. Response MUST be valid JSON only, NO markdown or explanations

Return ONLY a JSON array with this exact structure:
[
  {{
    "name": "Tool Name",
    "category": "development|version_control|practice_platform|design|devops|ai_tools|databases|api_tools|cloud|ci_cd|collaboration|other",
    "description": "Brief description of what this tool does",
    "use_case": "learning|practice|production|all",
    "difficulty": "beginner|intermediate|advanced|all",
    "link": "https://example.com"
  }}
]

Examples of GOOD tools to generate:
- GitHub (version_control)
- VS Code (development)
- LeetCode (practice_platform)
- Docker (devops)
- Figma (design)
- ChatGPT (ai_tools)

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
            
            tools_data = json.loads(text)
            
            # Validate and clean data
            valid_tools = []
            for tool in tools_data:
                if all(k in tool for k in ['name', 'category', 'description', 'use_case', 'difficulty', 'link']):
                    # Create tool in DB if it doesn't exist
                    Tool.objects.get_or_create(
                        name=tool['name'],
                        defaults={
                            'category': tool.get('category', 'other'),
                            'description': tool.get('description', ''),
                            'use_case': tool.get('use_case', 'all'),
                            'difficulty': tool.get('difficulty', 'all'),
                            'link': tool.get('link', ''),
                            'is_ai_generated': True,
                        }
                    )
                    valid_tools.append(tool)
            
            return valid_tools[:count]
    except Exception as e:
        print(f"AI tool generation error: {str(e)}")
        return []
    
    return []


def get_tools_hybrid(category=None, difficulty=None, use_case=None, search_query=None):
    """
    Hybrid tool fetching:
    1. Try database first
    2. If results < threshold, generate via AI
    
    Returns: list[dict] with tools
    """
    db_tools = fetch_tools_from_db(category, difficulty, use_case, search_query)
    
    # Convert to dict format
    tools_list = [
        {
            'name': t.name,
            'category': t.category,
            'description': t.description,
            'use_case': t.use_case,
            'difficulty': t.difficulty,
            'link': t.link,
        }
        for t in db_tools
    ]
    
    # If not enough results, generate via AI
    if len(tools_list) < 3:
        ai_tools = generate_tools_via_ai(category, difficulty, use_case, count=6 - len(tools_list))
        tools_list.extend(ai_tools)
    
    return tools_list[:6]  # Max 6 tools
