from .settings_local import *  # noqa: F401,F403

# Fast deterministic password hashing only for isolated automated tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
