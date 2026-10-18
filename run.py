"""Start the game server over HTTPS on the LAN.

Phones only expose motion sensors (and later the camera) to pages served over
HTTPS, so this generates a self-signed certificate on first run.
"""

import argparse
import datetime
import importlib.util
import ipaddress
import os
import socket
import subprocess
import sys
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_DIR = Path(__file__).resolve().parent / "data" / "certs"
COMBAT_DIR = Path(__file__).resolve().parent / "combat"


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


def start_combat(port: int, game_server: str) -> subprocess.Popen | None:
    """The shooting server (combat/server.py: finger guns, and the match: bases, flag) runs
    alongside: phones stream camera frames to it on port+1 (HTTPS), the viewer reaches it on port
    (HTTP), and it follows everyone's position from this game server (game_server)."""
    missing = [m for m in ("aiohttp", "cv2", "mediapipe", "pupil_apriltags") if importlib.util.find_spec(m) is None]
    if missing:
        print(f"  Shooting:  not started, missing {', '.join(missing)} (pip install -r requirements.txt)\n")
        return None
    env = {**os.environ, "PORT": str(port), "HTTPS_PORT": str(port + 1), "GAME_SERVER": game_server}
    print(f"  Shooting:  http://localhost:{port}/ (phones stream to port {port + 1})\n")
    return subprocess.Popen([sys.executable, "server.py"], cwd=COMBAT_DIR, env=env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--http", action="store_true", help="plain HTTP (e.g. behind an HTTPS tunnel)")
    parser.add_argument("--combat-port", type=int, default=8765, help="shooting server port (HTTPS on the next one)")
    parser.add_argument("--no-combat", action="store_true", help="don't start the shooting server")
    args = parser.parse_args()

    ip = lan_ip()
    ssl = {}
    scheme = "http"
    if not args.http:
        cert, key = ensure_cert(ip)
        ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        scheme = "https"
    print(f"\n  Host map:  {scheme}://localhost:{args.port}/web/host.html")
    print(f"  Phones:    {scheme}://{ip}:{args.port}/web/phone.html")
    print(f"  Player app server address: {ip}:{args.port}\n")
    combat = None if args.no_combat else start_combat(args.combat_port, f"{scheme}://127.0.0.1:{args.port}")
    try:
        uvicorn.run("server.main:app", host="0.0.0.0", port=args.port, log_level="warning", **ssl)
    finally:
        if combat:
            combat.terminate()
            combat.wait(timeout=5)


if __name__ == "__main__":
    main()
