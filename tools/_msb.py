"""Load modpack_server_builder.py as a module so the release tools reuse its code (one Ed25519)."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_path() -> Path:
    return Path(os.environ.get("MSB_SOURCE") or ROOT / "modpack_server_builder.py")


def load_app():
    path = source_path()
    spec = importlib.util.spec_from_file_location("modpack_server_builder", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    part = os.environ.get("MSB_UPDATER_PART")          # dev only: updater code not pasted in yet
    if part and not hasattr(mod, "ed25519_sign"):
        exec(compile(Path(part).read_text(encoding="utf-8"), part, "exec"), mod.__dict__)
    if not hasattr(mod, "ed25519_sign"):
        raise SystemExit(f"{path} has no updater code (ed25519_sign missing)")
    return mod
