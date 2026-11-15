"""Self-signed TLS certificates for LAN phone capture.

Mobile browsers only expose ``getUserMedia`` on secure origins (``https://``
or ``http://localhost``). A phone opening ``http://192.168.x.x:5055`` therefore
cannot use its camera. For local deployments without a domain, IMANI can serve
HTTPS with a self-signed certificate generated here.

The certificate is written once under ``instance/certs/``. It includes the
machine's LAN IP, ``localhost``, and ``127.0.0.1`` in the Subject Alternative
Name so the warning is at least about trust (which the operator can accept),
not about a mismatched name. If the machine's IP changes, delete the files and
restart to regenerate them.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import os
from pathlib import Path


def ensure_self_signed(cert_path: str, key_path: str, hosts=None) -> tuple[str, str]:
    """Create ``cert_path``/``key_path`` if they do not already exist.

    Returns ``(cert_path, key_path)``. Raises if the ``cryptography`` package
    is unavailable, so callers can fall back to HTTP with a clear message.
    """
    cert = Path(cert_path).expanduser()
    key = Path(key_path).expanduser()
    if cert.exists() and key.exists():
        return str(cert), str(key)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "IMANI local server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "IMANI"),
    ])

    alt_names = [x509.DNSName("localhost")]
    seen = {"localhost", "127.0.0.1", "::1"}
    for host in [*(hosts or []), "127.0.0.1", "::1"]:
        if not host:
            continue
        text = str(host).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(text)))
        except ValueError:
            alt_names.append(x509.DNSName(text))

    now = _dt.datetime.now(_dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    key.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        os.chmod(key, 0o600)
    except OSError:  # pragma: no cover - filesystem dependent
        pass
    return str(cert), str(key)
