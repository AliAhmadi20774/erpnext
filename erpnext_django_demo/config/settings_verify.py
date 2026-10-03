from .settings_local import *  # noqa: F401,F403

# Browser QA uses a separate database; never reset the user's local demo data.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                          "NAME": BASE_DIR / "backups" / "manager-verify.sqlite3"}}
