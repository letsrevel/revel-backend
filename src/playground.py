import os
import django


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

django.setup()

# imports start here
from wallet.apple.generator import ApplePassGenerator
from events.models import Ticket

ticket = Ticket.objects.get(id="d7ab2149-dc77-4b78-be24-e52bba2ef51c")
generator = ApplePassGenerator()
apple_pass = generator.generate_pass(ticket)

import pathlib
path = pathlib.Path("/Users/biagio/Desktop/event11.pkpass")
path.write_bytes(apple_pass)
print(f"Pass saved: {len(apple_pass)} bytes")

# Verification steps
import subprocess
import tempfile
import zipfile
import json

print("\n" + "=" * 60)
print("VERIFICATION")
print("=" * 60)

# Extract pkpass to temp dir
with tempfile.TemporaryDirectory() as tmpdir:
    with zipfile.ZipFile(path, 'r') as zf:
        zf.extractall(tmpdir)

    tmppath = pathlib.Path(tmpdir)

    # 1. Show pass.json content
    print("\n--- pass.json ---")
    pass_json = json.loads((tmppath / "pass.json").read_text())
    print(json.dumps(pass_json, indent=2))

    # 2. Show manifest.json
    print("\n--- manifest.json ---")
    manifest = json.loads((tmppath / "manifest.json").read_text())
    print(json.dumps(manifest, indent=2))

    # 3. Verify manifest hashes match actual files
    print("\n--- Hash verification ---")
    import hashlib
    for filename, expected_hash in manifest.items():
        filepath = tmppath / filename
        if filepath.exists():
            actual_hash = hashlib.sha1(filepath.read_bytes()).hexdigest()
            status = "OK" if actual_hash == expected_hash else "MISMATCH"
            print(f"  {filename}: {status}")
            if status == "MISMATCH":
                print(f"    Expected: {expected_hash}")
                print(f"    Actual:   {actual_hash}")
        else:
            print(f"  {filename}: FILE MISSING")

    # 4. Verify PKCS#7 signature
    print("\n--- Signature verification ---")
    result = subprocess.run(
        [
            "openssl", "smime", "-verify",
            "-in", str(tmppath / "signature"),
            "-inform", "DER",
            "-content", str(tmppath / "manifest.json"),
            "-noverify"  # Don't verify cert chain, just signature
        ],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print("  Signature: VALID")
    else:
        print("  Signature: INVALID")
        print(f"  Error: {result.stderr}")

    # 5. Check certificate info in signature
    print("\n--- Certificate in signature ---")
    result = subprocess.run(
        [
            "openssl", "pkcs7",
            "-in", str(tmppath / "signature"),
            "-inform", "DER",
            "-print_certs"
        ],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        # Just show subject lines
        for line in result.stdout.split('\n'):
            if line.startswith('subject=') or line.startswith('issuer='):
                print(f"  {line}")
    else:
        print(f"  Error: {result.stderr}")
