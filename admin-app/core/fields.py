"""Encrypted text field for ID numbers (Aadhaar, passport, …)."""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = "enc:"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured("Set FIELD_ENCRYPTION_KEY.")
        # Development only: derive a stable key from SECRET_KEY.
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    return Fernet(key)


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        return "[cannot decrypt — wrong FIELD_ENCRYPTION_KEY]"


class EncryptedTextField(models.TextField):
    """Stored encrypted; read back as plain text. Not searchable.

    `value_to_string` keeps the encrypted form, so JSON backups never contain plain ID numbers.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value) if value else value

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        return decrypt(value)

    def value_to_string(self, obj):
        return encrypt(self.value_from_object(obj) or "")


def mask(value: str, visible: int = 4) -> str:
    """'123412341234' → '•••• •••• 1234'."""
    if not value:
        return ""
    clean = value.replace(" ", "")
    return "•••• " * 2 + clean[-visible:] if len(clean) > visible else clean
