"""Invariants of the generated TLS material.

These are not cosmetic assertions. The certificate has to satisfy Apple's App
Transport Security rules or iOS rejects the connection outright, and the failure
mode is an opaque TLS handshake error rather than a useful message. The chain
layout matters too: `server.crt` ships a full chain, and a future change that
writes only the leaf would still "look" fine on disk.
"""

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from dankagu.config import settings
from dankagu.core.certs import generate_certificates

# Apple enforces <= 398 days on leaf certificates. certs.py targets 365, so
# assert against the real limit with a little slack for clock skew rather than
# against 365 itself; this catches a regression without breaking on a one-day
# rounding difference.
_APPLE_LEAF_LIMIT_DAYS = 398


@pytest.fixture(scope="module")
def cert_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a fresh CA and server certificate into an isolated directory."""
    out = tmp_path_factory.mktemp("certs")
    generate_certificates(out)
    return out


@pytest.fixture(scope="module")
def chain(cert_dir: Path) -> tuple[x509.Certificate, x509.Certificate]:
    """Return (leaf, ca) parsed from the generated PEM files."""
    blobs = x509.load_pem_x509_certificates((cert_dir / "server.crt").read_bytes())
    assert len(blobs) == 2, "server.crt must contain the leaf plus the CA"
    leaf, ca = blobs[0], blobs[1]
    return leaf, ca


def test_server_crt_ships_a_full_chain(cert_dir: Path) -> None:
    data = (cert_dir / "server.crt").read_bytes()
    assert data.count(b"BEGIN CERTIFICATE") == 2, "expected leaf + CA in server.crt"


def test_leaf_is_signed_by_the_ca(chain: tuple[x509.Certificate, x509.Certificate]) -> None:
    leaf, ca = chain
    # Raises InvalidSignature if the two certificates in server.crt are not
    # actually linked.
    ca.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )


def test_issuer_and_subject_link_the_chain(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    leaf, ca = chain
    assert leaf.issuer == ca.subject


def test_ca_is_a_ca_and_leaf_is_not(chain: tuple[x509.Certificate, x509.Certificate]) -> None:
    leaf, ca = chain
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False


def test_leaf_validity_is_within_apples_limit(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    leaf, _ = chain
    span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
    assert span <= datetime.timedelta(days=_APPLE_LEAF_LIMIT_DAYS), (
        f"leaf validity {span.days}d exceeds Apple's {_APPLE_LEAF_LIMIT_DAYS}d limit"
    )
    # And it must actually be valid right now.
    now = datetime.datetime.now(datetime.UTC)
    assert leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc


def test_leaf_has_server_and_client_auth_eku(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    leaf, _ = chain
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_leaf_key_usage_allows_digital_signature_and_key_encipherment(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    leaf, _ = chain
    ku = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature is True
    assert ku.key_encipherment is True
    # A server certificate must not be able to sign other certificates.
    assert ku.key_cert_sign is False


def test_leaf_san_covers_loopback(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    leaf, _ = chain
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    loopback = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert "127.0.0.1" in loopback


def test_common_name_matches_configured_public_host(
    chain: tuple[x509.Certificate, x509.Certificate],
) -> None:
    """The CN must track DANKAGU_PUBLIC_HOST rather than a hard-coded name."""
    leaf, _ = chain
    cn = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == settings.cert_common_name


def test_leaf_key_is_rsa_2048(chain: tuple[x509.Certificate, x509.Certificate]) -> None:
    leaf, _ = chain
    key = leaf.public_key()
    assert isinstance(key, rsa.RSAPublicKey)
    assert key.key_size == 2048


def test_leaf_is_signed_with_sha256(chain: tuple[x509.Certificate, x509.Certificate]) -> None:
    leaf, _ = chain
    assert isinstance(leaf.signature_hash_algorithm, hashes.SHA256)


def test_existing_ca_is_reused_so_devices_need_no_reinstall(cert_dir: Path) -> None:
    """A second run must not silently mint a new CA.

    Reinstalling the trust profile on every device is the expensive failure
    mode here, so the reuse branch in certs.py is worth pinning.
    """
    before = (cert_dir / "ca.crt").read_bytes()
    generate_certificates(cert_dir)
    assert (cert_dir / "ca.crt").read_bytes() == before
