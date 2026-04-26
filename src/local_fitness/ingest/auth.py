"""Garmin Connect credential storage via macOS Keychain (keyring lib).

We store the email under a fixed key so we know what account is wired up,
then password under that email. garminconnect / garth handles the actual
session-token caching to disk so MFA is only required on first login.
"""
from __future__ import annotations

import getpass

import keyring

SERVICE = "local-fitness-garmin"
EMAIL_KEY = "_email"  # underscored so it can't collide with a real email


def store_credentials(email: str, password: str) -> None:
    keyring.set_password(SERVICE, EMAIL_KEY, email)
    keyring.set_password(SERVICE, email, password)


def get_credentials() -> tuple[str, str] | None:
    email = keyring.get_password(SERVICE, EMAIL_KEY)
    if not email:
        return None
    password = keyring.get_password(SERVICE, email)
    if not password:
        return None
    return email, password


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
