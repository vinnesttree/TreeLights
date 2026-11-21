"""Local certificate authority and server certificate (architecture §6.3).

The CA is created once. The server certificate is re-issued automatically when
the host's addresses change or it nears expiry, so a phone that installed the
CA never has to redo anything. Tapping through Chrome's warning (the default
setup) also works; the warning appears when the page is opened, not per frame.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import storage

log = logging.getLogger(__name__)

PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16"]
LEAF_DAYS = 397
RENEW_BEFORE_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    storage.atomic_write_bytes(path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.chmod(0o600)


def ensure_ca(tls_dir: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    cert_path, key_path = tls_dir / "ca.crt", tls_dir / "ca.key"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return cert, key  # type: ignore[return-value]
    tls_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"TreeLights local CA ({socket.gethostname()})")])
    permitted: list[x509.GeneralName] = [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETS]
    permitted += [x509.DNSName("local"), x509.DNSName("localhost"), x509.DNSName("lan"), x509.DNSName("home.arpa")]
    host = socket.gethostname()
    if host and "." not in host:
        permitted.append(x509.DNSName(host))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        .not_valid_after(_now() + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(key_path, key)
    storage.atomic_write_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    log.info("created local CA in %s", tls_dir)
    return cert, key


def _sans_of(cert: x509.Certificate) -> set[str]:
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(v) for v in ext.get_values_for_type(x509.IPAddress)} | set(ext.get_values_for_type(x509.DNSName))


def ensure_server_cert(tls_dir: Path, ips: list[str], hostnames: list[str]) -> tuple[Path, Path]:
    """Return (cert_path, key_path), re-issuing when SANs changed or expiry is near."""
    ca_cert, ca_key = ensure_ca(tls_dir)
    cert_path, key_path = tls_dir / "server.crt", tls_dir / "server.key"
    want = set(ips) | set(hostnames) | {"localhost", "127.0.0.1"}
    if cert_path.exists() and key_path.exists():
        cur = x509.load_pem_x509_certificate(cert_path.read_bytes())
        fresh = cur.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_BEFORE_DAYS)
        if fresh and want <= _sans_of(cur):
            return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = []
    for v in sorted(want):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(v)))
        except ValueError:
            sans.append(x509.DNSName(v))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TreeLights")]))
        .issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        .not_valid_after(_now() + dt.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(key_path, key)
    storage.atomic_write_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    log.info("issued server certificate for %s", ", ".join(sorted(want)))
    return cert_path, key_path


def ca_der(tls_dir: Path) -> bytes:
    cert, _ = ensure_ca(tls_dir)
    return cert.public_bytes(serialization.Encoding.DER)
