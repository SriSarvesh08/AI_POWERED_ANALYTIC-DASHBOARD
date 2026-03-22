import os
import logging

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "thisISaSECRET_1234")

SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://datapilot:datapilot@postgres:5432/superset",
)

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": os.environ.get("REDIS_URL", "redis://redis:6379/0"),
}

FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_NATIVE_FILTERS": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "GENERIC_CHART_AXES": True,          # allow line/area charts with non-datetime x-axis
}

GUEST_ROLE_NAME = "Public"
GUEST_TOKEN_JWT_ALGO = "HS256"

WTF_CSRF_ENABLED = False
TALISMAN_ENABLED = False
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False
SESSION_COOKIE_HTTPONLY = False

# Allow Superset to be loaded in an iframe
HTTP_HEADERS = {"X-Frame-Options": "ALLOWALL"}
OVERRIDE_HTTP_HEADERS = {"X-Frame-Options": "ALLOWALL"}

CONTENT_SECURITY_POLICY_WARNING = False
PUBLIC_ROLE_LIKE_GAMMA = True
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["*"],
    "origins": ["*"],
}

# HTML sanitization off for embedded use
HTML_SANITIZATION = False
HTML_SANITIZATION_SCHEMA_EXTENSIONS = {}

logging.basicConfig(level=logging.INFO)