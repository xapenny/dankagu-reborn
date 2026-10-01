"""Certificate generation utilities for Danmaku Kagura backend.

Adheres strictly to Apple iOS 13+ / macOS 10.15+ TLS certificate requirements:
- Maximum 365 days validity period (Apple enforces strict <= 398 days limit on leaf certs).
- RSA 2048-bit key size with SHA-256.
- Explicit KeyUsage (Digital Signature, Key Encipherment).
- ExtendedKeyUsage (Server Auth, Client Auth).
- Subject Alternative Names (SAN) matching all game domains, local IPs, and hostnames.
"""

import datetime
import ipaddress
import logging
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from dankagu.config import settings
from dankagu.core.net import detect_local_ip

logger = logging.getLogger("dankagu.certs")


def generate_certificates(output_dir: Path, force_new_ca: bool = False) -> None:
    """Generate or update Root CA and Apple-compliant TLS server certificates."""
    output_dir.mkdir(parents=True, exist_ok=True)

    ca_key_path = output_dir / "ca.key"
    ca_crt_path = output_dir / "ca.crt"
    server_key_path = output_dir / "server.key"
    server_crt_path = output_dir / "server.crt"

    # 1. Root CA (Load existing if present to avoid requiring client re-installation)
    if ca_key_path.exists() and ca_crt_path.exists() and not force_new_ca:
        logger.info("Using existing Root CA key and certificate: %s", ca_crt_path)
        ca_key = serialization.load_pem_private_key(
            ca_key_path.read_bytes(), password=None, backend=default_backend()
        )
        ca_cert = x509.load_pem_x509_certificate(ca_crt_path.read_bytes(), backend=default_backend())
        ca_name = ca_cert.subject
    else:
        logger.info("Generating fresh Root CA...")
        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        ca_name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "DanKagu Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DanKagu Preservation"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "JP"),
        ])
        now = datetime.datetime.now(datetime.UTC)
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))  # Root CA is exempt from 398-day limit
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256(), default_backend())
        )

        ca_key_path.write_bytes(
            ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        ca_crt_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    # 2. Server Certificate (Apple ATS Compliant)
    logger.info("Generating Apple ATS-compliant TLS server certificate (365 days validity)...")
    server_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    # The common name follows DANKAGU_PUBLIC_HOST. With no host configured it
    # falls back to a neutral local name rather than claiming a hostname this
    # server does not own.
    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, settings.cert_common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DanKagu Local Preservation"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "JP"),
    ])

    san_names: list[x509.GeneralName] = [
        # Game domains from configuration (plus any operator-supplied names).
        *(x509.DNSName(name) for name in settings.cert_dns_names),
        # Loopback, for clients running on this host.
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]

    # Discover this host's IPv4 addresses so the certificate also covers them
    import socket
    detected_ips: set[str] = set()
    try:
        detected_ips.add(detect_local_ip())
        for a in socket.getaddrinfo(socket.gethostname(), None):
            ip_cand = a[4][0]
            if ":" not in ip_cand and not ip_cand.startswith("127."):
                detected_ips.add(ip_cand)
    except Exception:
        pass

    for ip_str in detected_ips:
        try:
            parsed = ipaddress.IPv4Address(ip_str)
            if parsed != ipaddress.IPv4Address("127.0.0.1") and x509.IPAddress(parsed) not in san_names:
                san_names.append(x509.IPAddress(parsed))
        except Exception:
            pass

    now = datetime.datetime.now(datetime.UTC)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        # Apple strictly mandates <= 398 days for TLS server certificates
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(san_names),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    server_key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Write full chain (leaf cert + CA cert) to server.crt
    full_chain_bytes = (
        server_cert.public_bytes(serialization.Encoding.PEM).strip()
        + b"\n\n"
        + ca_cert.public_bytes(serialization.Encoding.PEM).strip()
        + b"\n"
    )
    server_crt_path.write_bytes(full_chain_bytes)
    logger.info("✅ Apple-compliant server certificate (full chain) successfully generated: %s", server_crt_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from dankagu.config import settings
    generate_certificates(settings.certs_dir)
