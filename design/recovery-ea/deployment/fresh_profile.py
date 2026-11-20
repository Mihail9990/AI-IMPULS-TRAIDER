"""Create the first local profile for a clean VPS. No network or order actions."""
import json
import os
from pathlib import Path
import secrets
import uuid

PROFILE_NAME = "recovery-profile.json"


def initialize_fresh_profile(root: Path) -> Path:
    """Only an empty directory (or a path that doesn't exist) is a fresh install.

    The caller/installer selects the private local installation directory.
    Deployment ACLs and connecting MT5/Caddy are separate runtime responsibilities.
    This function never searches another installation, reads environment secrets,
    copies account data or starts services.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Choose a local installation directory, not a link.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise FileExistsError("Choose a new, empty installation directory.")

    target = root / PROFILE_NAME
    profile = {
        "schema": 1,
        "install_id": str(uuid.uuid4()),
        "panel_key": secrets.token_urlsafe(32),
        "mt5": {"login": None, "server": None, "symbol": "XAUUSD.s"},
    }
    # Never overwrite a newer install, even if two installer windows are open.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    return target
