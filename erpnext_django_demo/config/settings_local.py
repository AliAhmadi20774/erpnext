from .settings_base import *  # noqa: F401,F403


# Deliberately public and valid only for the isolated local demo.
SECRET_KEY = "django-insecure-public-local-demo-key"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
