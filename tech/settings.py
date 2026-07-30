"""
Django settings for tech project.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

try:
    import dj_database_url
except ImportError:
    dj_database_url = None

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

LOCAL_DATA_DIR = BASE_DIR / '.data'


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_any(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


# --------------------------------------------------
# Security
# --------------------------------------------------
DEBUG = env_bool('DEBUG', default=True)

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-c7rb&3u0dzxgsg94bqjq_)=qu_-a@v2htepbbu(xf^vgvmjgk('
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("The DJANGO_SECRET_KEY environment variable must be set in production.")

ALLOWED_HOSTS_RAW = os.getenv('ALLOWED_HOSTS')
if ALLOWED_HOSTS_RAW:
    ALLOWED_HOSTS = [host.strip() for host in ALLOWED_HOSTS_RAW.split(',') if host.strip()]
else:
    if DEBUG:
        if DEBUG:
            ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", ".vercel.app"]
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("The ALLOWED_HOSTS environment variable must be set in production.")

CSRF_TRUSTED_ORIGINS_RAW = os.getenv('CSRF_TRUSTED_ORIGINS')
if CSRF_TRUSTED_ORIGINS_RAW:
    CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in CSRF_TRUSTED_ORIGINS_RAW.split(',') if origin.strip()]
else:
    if DEBUG:
        CSRF_TRUSTED_ORIGINS = []
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("The CSRF_TRUSTED_ORIGINS environment variable must be set in production.")

SITE_DOMAIN = os.getenv('SITE_DOMAIN', 'localhost')
SITE_NAME = os.getenv('SITE_NAME', 'TechBrat')

STATIC_ROOT = BASE_DIR / 'staticfiles'


# --------------------------------------------------
# Applications
# --------------------------------------------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',

    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.github',

    'techbrat',
]


# --------------------------------------------------
# Middleware
# --------------------------------------------------
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',

    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',

    'techbrat.middleware.CanonicalLocalhostMiddleware',

    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',

    'allauth.account.middleware.AccountMiddleware',

    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]


# --------------------------------------------------
# URLs & Templates
# --------------------------------------------------
ROOT_URLCONF = 'tech.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'techbrat.context_processors.saved_items_summary',
            ],
        },
    },
]

WSGI_APPLICATION = 'tech.wsgi.application'


# --------------------------------------------------
# Database
# --------------------------------------------------
DATABASE_URL = env_any(
    'DATABASE_URL',
    'RAILWAY_DATABASE_URL'
)

if DATABASE_URL and dj_database_url:
    DATABASES = {
        'default': dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600
        ),
    }
else:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("The DATABASE_URL environment variable must be set to a PostgreSQL database connection string.")


# --------------------------------------------------
# Password validation
# --------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# --------------------------------------------------
# Internationalization
# --------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


# --------------------------------------------------
# Static files
# --------------------------------------------------
STATIC_URL = '/static/'

STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

STATICFILES_STORAGE = (
    'whitenoise.storage.CompressedManifestStaticFilesStorage'
)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --------------------------------------------------
# Authentication / Allauth
# --------------------------------------------------
SITE_ID = 1

LOGIN_REDIRECT_URL = '/welcome/'
ACCOUNT_LOGIN_REDIRECT_URL = '/welcome/'
ACCOUNT_SIGNUP_REDIRECT_URL = '/welcome/'
SOCIALACCOUNT_LOGIN_REDIRECT_URL = '/welcome/'
LOGOUT_REDIRECT_URL = '/'

AUTHENTICATION_BACKENDS = (
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
)

SOCIALACCOUNT_ADAPTER = (
    'techbrat.adapters.TechBratSocialAccountAdapter'
)

SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_STORE_TOKENS = True

ACCOUNT_SIGNUP_FIELDS = [
    'email*',
    'password1*',
    'password2*'
]

ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_EMAIL_VERIFICATION = 'optional'
ACCOUNT_SESSION_REMEMBER = True
SOCIALACCOUNT_AUTO_SIGNUP = True

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')

GITHUB_CLIENT_ID = os.getenv('GITHUB_CLIENT_ID')
GITHUB_CLIENT_SECRET = os.getenv('GITHUB_CLIENT_SECRET')


# --------------------------------------------------
# Messages
# --------------------------------------------------
from django.contrib.messages import constants as messages

MESSAGE_TAGS = {
    messages.SUCCESS: 'success',
    messages.ERROR: 'danger',
    messages.WARNING: 'warning',
    messages.INFO: 'info',
}


# --------------------------------------------------
# Cache
# --------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'techbrat-courses',
        'TIMEOUT': 600,
    }
}


OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL = os.getenv(
    'OPENROUTER_MODEL',
    'google/gemma-4-26b-a4b-it:free'
)

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    USE_X_FORWARDED_HOST = True
    
    # SSL and HSTS configurations
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', default=True)
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', 31536000))  # 1 year default
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    
    # Additional security headers
    SECURE_CONTENT_TYPE_NOSNIFF = True