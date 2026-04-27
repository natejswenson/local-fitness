"""Garmin Connect credential storage via macOS Keychain (keyring lib).

We store the email under a fixed key so we know what account is wired up,
then password under that email. garminconnect / garth handles the actual
session-token caching to disk so MFA is only required on first login.

Container deployments can't read the host's macOS Keychain, so
get_credentials() falls back to GARMIN_EMAIL + GARMIN_PASSWORD env vars
when both are set. Host CLI keeps Keychain primacy.
"""
from __future__ import annotations

import getpass
import os

import keyring

SERVICE = "local-fitness-garmin"
EMAIL_KEY = "_email"  # underscored so it can't collide with a real email


def store_credentials(email: str, password: str) -> None:
    keyring.set_password(SERVICE, EMAIL_KEY, email)
    keyring.set_password(SERVICE, email, password)


def get_credentials() -> tuple[str, str] | None:
    # Env vars win when both are set — required for container deployments
    # where macOS Keychain isn't reachable. Host CLI typically has these
    # unset and falls through to Keychain.
    env_email = os.environ.get("GARMIN_EMAIL")
    env_password = os.environ.get("GARMIN_PASSWORD")
    if env_email and env_password:
        return env_email, env_password

    try:
        email = keyring.get_password(SERVICE, EMAIL_KEY)
        if not email:
            return None
        password = keyring.get_password(SERVICE, email)
        if not password:
            return None
        return email, password
    except keyring.errors.KeyringError:
        # No usable Keychain backend (e.g. Linux container without the
        # appropriate keyring service installed). Treat as no creds rather
        # than crashing.
        return None


def clear_credentials() -> None:
    email = keyring.get_password(SERVICE, EMAIL_KEY)
    if email:
        try:
            keyring.delete_password(SERVICE, email)
        except keyring.errors.PasswordDeleteError:
            pass
        try:
            keyring.delete_password(SERVICE, EMAIL_KEY)
        except keyring.errors.PasswordDeleteError:
            pass


def prompt_and_store() -> tuple[str, str]:
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password: ")
    store_credentials(email, password)
    return email, password
