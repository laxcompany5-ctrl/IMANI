"""Self-signed TLS certificates used for phone camera pairing."""
from __future__ import annotations

from imani.certs import ensure_self_signed


def test_creates_certificate_with_lan_san(tmp_path):
    cert_path = tmp_path / "certs" / "imani.crt"
    key_path = tmp_path / "certs" / "imani.key"

    cert, key = ensure_self_signed(
        str(cert_path), str(key_path), ["192.0.2.55", "imani.local"]
    )

    assert cert_path.exists()
    assert key_path.exists()
    assert cert == str(cert_path)
    assert key == str(key_path)
    assert b"BEGIN CERTIFICATE" in cert_path.read_bytes()
    assert b"BEGIN PRIVATE KEY" in key_path.read_bytes() or b"BEGIN RSA PRIVATE KEY" in key_path.read_bytes()

    from cryptography import x509

    parsed = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    names = {str(name.value) for name in san}
    assert "192.0.2.55" in names
    assert "imani.local" in names
    assert "localhost" in names


def test_reuses_an_existing_certificate(tmp_path):
    cert_path = tmp_path / "imani.crt"
    key_path = tmp_path / "imani.key"
    first = ensure_self_signed(str(cert_path), str(key_path), ["192.0.2.55"])
    original = cert_path.read_bytes()

    second = ensure_self_signed(str(cert_path), str(key_path), ["192.0.2.99"])

    assert first == second
    assert cert_path.read_bytes() == original
