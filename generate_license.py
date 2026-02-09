#!/usr/bin/env python3
"""
O.D.I.N. License Key Generator
===============================
Generates Ed25519 keypair and signed license files.

Usage:
    python3 generate_license.py --keygen                    # Generate keypair
    python3 generate_license.py --tier pro --licensee "Name" --email "email" --expires 2026-05-07
    python3 generate_license.py --verify odin.license       # Verify a license file

Keys stored at: ~/.odin-keys/
Requires: pip install cryptography
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("❌ Missing dependency. Run:")
    print("   pip3 install cryptography")
    sys.exit(1)

KEYS_DIR = Path.home() / ".odin-keys"
PRIVATE_KEY_PATH = KEYS_DIR / "odin_private.pem"
PUBLIC_KEY_PATH = KEYS_DIR / "odin_public.pem"


def cmd_keygen():
    """Generate Ed25519 keypair."""
    KEYS_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_KEY_PATH.exists():
        resp = input(f"⚠️  Keys already exist at {KEYS_DIR}. Overwrite? (y/N): ")
        if resp.lower() != "y":
            print("Aborted.")
            return

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Save private key (PEM)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    PRIVATE_KEY_PATH.write_bytes(private_pem)
    os.chmod(PRIVATE_KEY_PATH, 0o600)

    # Save public key (PEM)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    PUBLIC_KEY_PATH.write_bytes(public_pem)

    # Also get raw base64 for embedding
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    public_b64 = base64.b64encode(public_raw).decode()

    print(f"\n✅ Keys generated at {KEYS_DIR}")
    print(f"   Private: {PRIVATE_KEY_PATH}")
    print(f"   Public:  {PUBLIC_KEY_PATH}")
    print(f"\n🔑 Public key (base64 — paste into license_manager.py):\n")
    print(f"   {public_b64}")
    print(f"\n   Replace REPLACE_WITH_YOUR_PUBLIC_KEY with the string above.")
    print(f"\n⚠️  NEVER share or commit the private key!")


def load_private_key():
    if not PRIVATE_KEY_PATH.exists():
        print(f"❌ Private key not found at {PRIVATE_KEY_PATH}")
        print("   Run: python3 generate_license.py --keygen")
        sys.exit(1)
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)


def load_public_key():
    if not PUBLIC_KEY_PATH.exists():
        print(f"❌ Public key not found at {PUBLIC_KEY_PATH}")
        print("   Run: python3 generate_license.py --keygen")
        sys.exit(1)
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    return load_pem_public_key(PUBLIC_KEY_PATH.read_bytes())


def cmd_generate(args):
    """Generate a signed license file."""
    private_key = load_private_key()

    payload = {
        "licensee": args.licensee,
        "email": args.email,
        "tier": args.tier,
        "max_printers": 0 if args.tier in ("pro", "education", "enterprise") else 5,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": f"{args.expires}T23:59:59Z",
        "features": get_tier_features(args.tier),
    }

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_bytes = payload_json.encode("utf-8")

    signature = private_key.sign(payload_bytes)
    signature_b64 = base64.b64encode(signature).decode()
    payload_b64 = base64.b64encode(payload_bytes).decode()

    license_data = {
        "format": "odin-license-v1",
        "payload": payload_b64,
        "signature": signature_b64,
    }

    out_file = args.output or "odin.license"
    with open(out_file, "w") as f:
        json.dump(license_data, f, indent=2)

    print(f"\n✅ License generated: {out_file}")
    print(f"   Licensee: {args.licensee}")
    print(f"   Email:    {args.email}")
    print(f"   Tier:     {args.tier}")
    print(f"   Expires:  {args.expires}")
    print(f"\n   Upload this file via O.D.I.N. Settings → License tab")


def cmd_verify(args):
    """Verify a license file."""
    public_key = load_public_key()

    try:
        with open(args.verify, "r") as f:
            license_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"❌ Cannot read license file: {e}")
        sys.exit(1)

    if license_data.get("format") != "odin-license-v1":
        print(f"❌ Unknown license format: {license_data.get('format')}")
        sys.exit(1)

    payload_b64 = license_data["payload"]
    signature_b64 = license_data["signature"]

    payload_bytes = base64.b64decode(payload_b64)
    signature = base64.b64decode(signature_b64)

    try:
        public_key.verify(signature, payload_bytes)
        print("✅ Signature VALID")
    except Exception:
        print("❌ Signature INVALID — license is tampered or wrong key")
        sys.exit(1)

    payload = json.loads(payload_bytes)
    print(f"\n   Licensee: {payload.get('licensee')}")
    print(f"   Email:    {payload.get('email')}")
    print(f"   Tier:     {payload.get('tier')}")
    print(f"   Expires:  {payload.get('expires_at')}")
    print(f"   Issued:   {payload.get('issued_at')}")
    print(f"   Features: {', '.join(payload.get('features', []))}")

    expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    if expires < datetime.now(timezone.utc):
        print(f"\n⚠️  License EXPIRED on {payload['expires_at']}")
    else:
        days_left = (expires - datetime.now(timezone.utc)).days
        print(f"\n   ✅ Valid for {days_left} more days")


def get_tier_features(tier):
    """Return feature flags for each tier."""
    community = ["dashboard", "cameras", "scheduling", "spool_tracking"]
    pro = community + [
        "unlimited_printers", "rbac", "sso", "orders", "products", "bom",
        "webhooks", "analytics", "csv_export", "white_label", "branding",
        "permissions", "mqtt_republish", "prometheus", "smart_plug",
        "quiet_hours", "energy_tracking", "utilization_report",
        "maintenance", "ntfy", "telegram", "email_notifications",
        "push_notifications",
    ]
    education = pro + [
        "job_approval", "class_sections", "print_quotas", "usage_reports",
    ]
    enterprise = education + [
        "opcua", "audit_export", "sqlcipher", "custom_integration",
    ]
    return {"community": community, "pro": pro, "education": education,
            "enterprise": enterprise}.get(tier, community)


def main():
    parser = argparse.ArgumentParser(description="O.D.I.N. License Key Generator")
    parser.add_argument("--keygen", action="store_true", help="Generate Ed25519 keypair")
    parser.add_argument("--tier", choices=["community", "pro", "education", "enterprise"])
    parser.add_argument("--licensee", help="Licensee name")
    parser.add_argument("--email", help="Licensee email")
    parser.add_argument("--expires", help="Expiry date (YYYY-MM-DD)")
    parser.add_argument("--output", "-o", help="Output filename (default: odin.license)")
    parser.add_argument("--verify", help="Verify a license file")

    args = parser.parse_args()

    if args.keygen:
        cmd_keygen()
    elif args.verify:
        cmd_verify(args)
    elif args.tier:
        if not all([args.licensee, args.email, args.expires]):
            parser.error("--tier requires --licensee, --email, and --expires")
        cmd_generate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
