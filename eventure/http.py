"""One HTTPS helper so every component trusts the same CA bundle.

Some Python builds — including the python.org macOS installer and slim Debian
images — ship with an empty certificate store, which turns every request into
`CERTIFICATE_VERIFY_FAILED`. Rather than requiring SSL_CERT_FILE to be set for
each command, resolve it once here.
"""

from __future__ import annotations

import os
import ssl
import urllib.request

_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",                    # macOS / LibreSSL
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu, Alpine-ish
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL, Fedora
    "/etc/ssl/ca-bundle.pem",               # openSUSE
)


def ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509", 0) > 0:
        return context
    try:
        import certifi  # optional; present if the environment installed it
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall through to system bundles
        pass
    for candidate in _CA_CANDIDATES:
        if not os.path.exists(candidate):
            continue
        try:
            return ssl.create_default_context(cafile=candidate)
        except Exception:  # noqa: BLE001 - keep trying the next path
            continue
    return context


def urlopen(request, timeout: float = 30.0):
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())
