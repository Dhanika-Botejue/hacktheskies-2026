"""Start the game server over HTTPS on the LAN.

Phones only expose motion sensors (and later the camera) to pages served over
HTTPS, so this generates a self-signed certificate on first run.
"""

import argparse
import datetime
import ipaddress
import socket
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_DIR = Path(__file__).resolve().parent / "data" / "certs"


def lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def ensure_cert(ip: str) -> tuple[Path, Path]:
    cert_path, key_path = CERT_DIR / f"cert-{ip}.pem", CERT_DIR / f"key-{ip}.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hacktheskies")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address(ip)),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return cert_path, key_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--http", action="store_true", help="plain HTTP (e.g. behind an HTTPS tunnel)")
    args = parser.parse_args()

    ip = lan_ip()
    ssl = {}
    scheme = "http"
    if not args.http:
        cert, key = ensure_cert(ip)
        ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        scheme = "https"
    print(f"\n  Host map:  {scheme}://localhost:{args.port}/web/host.html")
    print(f"  Phones:    {scheme}://{ip}:{args.port}/web/phone.html\n")
    uvicorn.run("server.main:app", host="0.0.0.0", port=args.port, log_level="warning", **ssl)


if __name__ == "__main__":
    main()
