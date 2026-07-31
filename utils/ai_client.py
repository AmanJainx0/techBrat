import logging
import requests
from requests.adapters import HTTPAdapter
from django.conf import settings

logger = logging.getLogger(__name__)

# Keep a shared HTTP session for efficient requests.
_ai_session = requests.Session()
_ai_session.trust_env = False
_ai_session.mount('https://', HTTPAdapter(pool_connections=100, pool_maxsize=100))
_ai_session.mount('http://', HTTPAdapter(pool_connections=100, pool_maxsize=100))


def is_ai_configured() -> bool:
    """
    Check if the currently active AI provider is properly configured with an API key.
    """
    provider = settings.AI_PROVIDER.lower()
    if provider == 'openrouter':
        return bool(
            getattr(settings, "OPENROUTER_API_KEY", None)
            and getattr(settings, "OPENROUTER_MODEL", None)
)
    elif provider == 'groq':
        return bool(
            getattr(settings, "GROQ_API_KEY", None)
            and getattr(settings, "GROQ_MODEL", None)
)
    return False


def chat_completion(messages, temperature=None, max_tokens=None, timeout=None):
    """
    Exposes a single function to generate chat completions using the configured AI provider.
    Supported providers: openrouter, groq.
    """
    provider = getattr(settings, 'AI_PROVIDER', 'openrouter').lower()

    if provider == 'openrouter':
        api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
        model = settings.OPENROUTER_MODEL
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "TechBrat Roadmap Generator",
        }
    elif provider == 'groq':
        api_key = getattr(settings, 'GROQ_API_KEY', None)
        model = settings.GROQ_MODEL
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    else:
        raise ValueError(f"Unsupported AI provider: {provider}")

    if not api_key or not model:
        raise ValueError(
            f"AI provider '{provider}' is not fully configured."
        )
    payload = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    # Send POST request using the shared HTTP session
    try:
        return _ai_session.post(
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException:
        logger.exception("AI request failed.")
        raise
