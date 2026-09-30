import os

from django.core.exceptions import ImproperlyConfigured

from .settings_base import *  # noqa: F401,F403


SECRET_KEY = os.environ.get("ERP_DEMO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("ERP_DEMO_SECRET_KEY must be set for deployment.")

DEBUG = False
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("ERP_DEMO_ALLOWED_HOSTS", "").split(",") if host.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ERP_DEMO_ALLOWED_HOSTS must contain at least one host.")

CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = os.environ.get("ERP_DEMO_SECURE_SSL_REDIRECT", "1") == "1"
SECURE_HSTS_SECONDS = int(os.environ.get("ERP_DEMO_HSTS_SECONDS", "3600"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = os.environ.get("ERP_DEMO_HSTS_PRELOAD", "1") == "1"
