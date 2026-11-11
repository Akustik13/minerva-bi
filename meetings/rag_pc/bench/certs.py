"""Self-signed certificate for HTTPS in the office network.

Browsers allow microphone and screen-audio capture only on HTTPS (or localhost), so the bench
serves HTTPS itself. Later the DSM reverse proxy (meet.sevskiy.com) gives a real certificate.
"""
import datetime
import hashlib
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def ensure(cfg):
    """Create cert.pem / key.pem once; return (cert_path, key_path, sha256 fingerprint)."""
    cert_path = cfg.tls_dir / "cert.pem"
    key_path = cfg.tls_dir / "key.pem"
    if not (cert_path.exists() and key_path.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        names = []
        for name in cfg["cert_names"]:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                names.append(x509.DNSName(name))
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Minerva meeting bench"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Minerva"),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    return cert_path, key_path, fingerprint
