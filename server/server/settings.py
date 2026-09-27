"""
Django settings for Smart Exam Cell Queue System backend.
"""

from email.utils import parseaddr
from pathlib import Path
import os
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def _require_env(name):
    """
    SECURITY: fail closed instead of silently falling back to a hardcoded default secret.
    A hardcoded fallback here is visible to anyone with repo access and would grant
    staff-level access in any deployment that forgets to set it.
    """
    value = os.getenv(name)
    if not value:
        raise ImproperlyConfigured(f"Required environment variable '{name}' is not set.")
    return value


SECRET_KEY = _require_env('DJANGO_SECRET_KEY')

# SECURITY: default to DEBUG=False. Verbose Django error pages leak secrets, environment
# details, and stack traces to whoever triggers a 500 — never let that be the silent
# default. Set DEBUG=True explicitly in .env for local development.
DEBUG = os.getenv('DEBUG', 'False') == 'True'

_allowed_hosts_env = os.getenv('ALLOWED_HOSTS', '')
if _allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(',') if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set (comma-separated) via env when DEBUG=False.")


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'exam_project.main',
    'rest_framework',
    'corsheaders',
]

REST_FRAMEWORK = {
    # Auth is a JWT bearer check in Handlers/auth_middleware.py — DRF's default
    # Session/Basic authenticators would never match anything here.
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    # JSON errors for everything, incl. "database unreachable" (503) — see Handlers/errors.py.
    'EXCEPTION_HANDLER': 'exam_project.main.Handlers.errors.api_exception_handler',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        # Generous on purpose: every open screen (student portal, staff terminal, lobby
        # display) polls GET /queue/ every couple of seconds, and a whole exam hall can
        # share one campus NAT IP.
        'anon': '600/minute',
        # Applied via ScopedRateThrottle to POST /tickets/ — caps how fast tokens can be
        # issued from one IP.
        'token': '10/minute',
        # Login / signup / OTP / password-reset / refresh — the credential-guessing and
        # email-sending surface. Per-account lockouts in auth_handler.py sit on top of this.
        'auth': '20/minute',
    }
}

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# CORS
# Auth is header-only (Authorization: Bearer <STAFF_TOKEN>) — no cookies are ever set, so
# credentialed CORS is not needed. Falls back to wildcard only while DEBUG=True.
_cors_origins_env = os.getenv('CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = []
CORS_ALLOW_ALL_ORIGINS = False
if _cors_origins_env:
    CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins_env.split(',') if o.strip()]
elif DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS must be set (comma-separated) via env when DEBUG=False.")
CORS_ALLOW_CREDENTIALS = False

CORS_ALLOW_METHODS = [
    "GET",
    "OPTIONS",
    "POST",
]

APPEND_SLASH = False

ROOT_URLCONF = 'server.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'server.wsgi.application'


# Database — SQLite for Django internals (sessions, admin)
# MongoDB is used directly via pymongo in Handlers/db_connection.py
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ── Auth ──────────────────────────────────────────────────────────────────────
# Signs access tokens (JWT HS256).
JWT_SECRET = _require_env('JWT_SECRET')
# AES-256-GCM key for personal fields at rest (emails, names) — base64 of 32 random bytes.
FIELD_ENCRYPTION_KEY = _require_env('FIELD_ENCRYPTION_KEY')
# HMAC key for blind indexes (email lookup) and OTP hashes — separate from the field key.
BLIND_INDEX_KEY = _require_env('BLIND_INDEX_KEY')

ACCESS_TOKEN_EXP_SECONDS = int(os.getenv('ACCESS_TOKEN_EXP_SECONDS', '1800'))  # 30 min
STUDENT_REFRESH_TOKEN_EXP_SECONDS = int(os.getenv('STUDENT_REFRESH_TOKEN_EXP_SECONDS', str(30 * 24 * 3600)))
STAFF_REFRESH_TOKEN_EXP_SECONDS = int(os.getenv('STAFF_REFRESH_TOKEN_EXP_SECONDS', str(24 * 3600)))

OTP_EXPIRY_MINUTES = int(os.getenv('OTP_EXPIRY_MINUTES', '10'))
OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv('OTP_RESEND_COOLDOWN_SECONDS', '45'))
MAX_OTP_ATTEMPTS = int(os.getenv('MAX_OTP_ATTEMPTS', '5'))
MAX_LOGIN_ATTEMPTS = int(os.getenv('MAX_LOGIN_ATTEMPTS', '5'))
LOGIN_LOCKOUT_MINUTES = int(os.getenv('LOGIN_LOCKOUT_MINUTES', '15'))

# Only these emails may sign up / log in as staff (comma-separated). Removing an email
# ends that person's staff access at their next token check.
STAFF_EMAILS = {e.strip().lower() for e in os.getenv('STAFF_EMAILS', '').split(',') if e.strip()}

# URLs — Google redirects back to BACKEND_URL, then on to the frontend.
BACKEND_URL = os.getenv('BACKEND_URL', 'http://localhost:8000')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:8080')
# Origins the Google flow may return to: the frontend plus any CORS origins.
FRONTEND_ORIGINS = {FRONTEND_URL.rstrip('/')} | {o.rstrip('/') for o in CORS_ALLOWED_ORIGINS}
if DEBUG:
    # Vite picks the next free port if one is taken — accept any localhost port in dev.
    FRONTEND_ORIGINS |= {f"http://localhost:{p}" for p in range(3000, 3010)} | \
        {f"http://localhost:{p}" for p in range(5173, 5183)} | \
        {f"http://localhost:{p}" for p in range(8080, 8090)}

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')

# Email (Brevo HTTP API). Unset in local dev -> OTPs are printed to the server console.
BREVO_API_KEY = os.getenv('BREVO_API_KEY', '')
# Either EMAIL_SENDER="Display Name <address@example.com>" (khelomore-server's format) or
# the separate EMAIL_SENDER_NAME / EMAIL_SENDER_ADDRESS pair.
_sender_name, _sender_address = parseaddr(os.getenv('EMAIL_SENDER', ''))
EMAIL_SENDER_NAME = os.getenv('EMAIL_SENDER_NAME') or _sender_name or 'Smart Exam Cell'
EMAIL_SENDER_ADDRESS = os.getenv('EMAIL_SENDER_ADDRESS') or _sender_address
# "You've been called" emails go out on a background thread; tests switch this on to send
# inline so they can assert on the result.
NOTIFICATIONS_SYNC = os.getenv('NOTIFICATIONS_SYNC', 'False') == 'True'
# Email every STAFF_EMAILS address when a student joins the queue / books a slot. Each new
# ticket costs one email per staff member against the Brevo daily quota (300/day free).
STAFF_NEW_TICKET_EMAILS = os.getenv('STAFF_NEW_TICKET_EMAILS', 'True') == 'True'

# Queue
COUNTER_COUNT = int(os.getenv('COUNTER_COUNT', '3'))
# Used for wait estimates only until the day has real completed services to average.
DEFAULT_SERVICE_MINUTES = float(os.getenv('DEFAULT_SERVICE_MINUTES', '5'))
# Max appointments per 15-minute slot (across all services).
SLOT_CAPACITY = int(os.getenv('SLOT_CAPACITY', '3'))
# How far ahead a student may book an appointment.
BOOKING_WINDOW_DAYS = int(os.getenv('BOOKING_WINDOW_DAYS', '30'))
# An appointment jumps ahead of walk-ins from this many minutes before its slot starts.
APPOINTMENT_GRACE_MINUTES = int(os.getenv('APPOINTMENT_GRACE_MINUTES', '5'))


# ── Security headers (skipped in DEBUG so local http:// dev keeps working) ─────────────────
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'

if not DEBUG:
    # Render terminates TLS at its own edge proxy and forwards plain HTTP internally —
    # without trusting X-Forwarded-Proto, SECURE_SSL_REDIRECT below would loop forever.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
