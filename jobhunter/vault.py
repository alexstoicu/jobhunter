"""Site account passwords in Windows Credential Manager (protected by the Windows login).

One random password per site host; created on first use and never written anywhere else.
"""
import secrets
import string

import keyring

SERVICE = "jobhunter-site"


def _generate(length=20):
    # Meets common ATS rules: upper, lower, digit and special character.
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw


def get(host):
    """Return (password, created_now)."""
    pw = keyring.get_password(SERVICE, host)
    if pw:
        return pw, False
    pw = _generate()
    keyring.set_password(SERVICE, host, pw)
    return pw, True


def exists(host):
    return keyring.get_password(SERVICE, host) is not None
