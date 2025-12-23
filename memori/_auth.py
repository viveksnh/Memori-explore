r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

from __future__ import annotations

import os

SERVICE_NAME = "memori"
API_KEY_USERNAME = "api_key"
EMAIL_USERNAME = "account_email"
ENV_API_KEY = "MEMORI_API_KEY"
ENV_DISABLE_KEYRING = "MEMORI_DISABLE_KEYRING"


def resolve_api_key() -> tuple[str | None, str | None]:
    env_key = os.environ.get(ENV_API_KEY)
    if env_key:
        return env_key, "env"

    if os.environ.get(ENV_DISABLE_KEYRING) == "1":
        return None, None

    keyring = _keyring_module()
    if keyring is None:
        return None, None

    try:
        api_key = keyring.get_password(SERVICE_NAME, API_KEY_USERNAME)
    except Exception:
        return None, None

    if api_key:
        return api_key, "keyring"

    return None, None


def get_api_key() -> str | None:
    return resolve_api_key()[0]


def get_account_email() -> str | None:
    if os.environ.get(ENV_DISABLE_KEYRING) == "1":
        return None

    keyring = _keyring_module()
    if keyring is None:
        return None

    try:
        return keyring.get_password(SERVICE_NAME, EMAIL_USERNAME)
    except Exception:
        return None


def save_api_key(api_key: str, email: str | None = None) -> None:
    if not api_key:
        raise ValueError("API key cannot be empty.")

    keyring = _keyring_module()
    if keyring is None:
        raise RuntimeError("Keyring is not available.")

    try:
        keyring.set_password(SERVICE_NAME, API_KEY_USERNAME, api_key)
        if email:
            keyring.set_password(SERVICE_NAME, EMAIL_USERNAME, email)
    except Exception as exc:
        raise RuntimeError("Failed to save API key in keyring.") from exc


def delete_api_key() -> None:
    keyring = _keyring_module()
    if keyring is None:
        raise RuntimeError("Keyring is not available.")

    try:
        keyring.delete_password(SERVICE_NAME, API_KEY_USERNAME)
    except Exception:
        pass

    try:
        keyring.delete_password(SERVICE_NAME, EMAIL_USERNAME)
    except Exception:
        pass


def _keyring_module():
    try:
        import keyring
    except Exception:
        return None

    return keyring
