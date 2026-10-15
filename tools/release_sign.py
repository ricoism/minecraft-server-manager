"""Sign SHA256SUMS for a release.

    UPDATE_SIGNING_KEY=<64 hex chars> python tools/release_sign.py dist/SHA256SUMS

Writes dist/SHA256SUMS.sig (hex Ed25519 signature over the exact file bytes). Refuses to sign when the
key doesn't belong to UPDATE_PUBKEY in modpack_server_builder.py, because the app would reject it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _msb import load_app  # noqa: E402


def read_seed(text: str) -> bytes:
    t = "".join((text or "").split()).lower()
    if t.startswith("0x"):
        t = t[2:]
    if len(t) not in (64, 128):
        raise SystemExit("UPDATE_SIGNING_KEY must be the 32-byte Ed25519 seed as 64 hex characters")
    try:
        return bytes.fromhex(t[:64])
    except ValueError:
        raise SystemExit("UPDATE_SIGNING_KEY is not valid hex") from None


def sign_file(app, sums_path: Path, seed: bytes) -> Path:
    data = sums_path.read_bytes()
    pub = app.ed25519_public_key(seed)
    if pub.hex() != app.UPDATE_PUBKEY.lower():
        raise SystemExit(f"The signing key's public key {pub.hex()} is not UPDATE_PUBKEY {app.UPDATE_PUBKEY}; "
                         "the app would reject this release.")
    sig = app.ed25519_sign(seed, data)
    if not app.ed25519_verify(bytes.fromhex(app.UPDATE_PUBKEY), data, sig):
        raise SystemExit("self-check failed: signature does not verify")
    signed = app.verify_signed_sums(data, sig.hex())
    if not signed["version"]:
        raise SystemExit(f"{sums_path} has no '# ModpackServerBuilder <version>' header line")
    out = sums_path.with_name(sums_path.name + ".sig")
    out.write_bytes((sig.hex() + "\n").encode("ascii"))
    return out


def main(argv):
    if len(argv) != 1:
        raise SystemExit(__doc__)
    key = os.environ.get("UPDATE_SIGNING_KEY", "")
    if not key.strip():
        raise SystemExit("UPDATE_SIGNING_KEY is not set")
    out = sign_file(load_app(), Path(argv[0]), read_seed(key))
    print(f"signed -> {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
