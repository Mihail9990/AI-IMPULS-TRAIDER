"""One-time local setup helper for Pydroid 3."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "bot_config.example.json"
CONFIG = ROOT / "bot_config.json"


def main() -> None:
    if not CONFIG.exists():
        shutil.copyfile(EXAMPLE, CONFIG)
        print(f"Created {CONFIG.name}")
    else:
        print(f"Kept existing {CONFIG.name}")
    print("1. Open bot_config.json in Pydroid and enter DEMO + Telegram credentials.")
    print("2. Keep CAPITAL_DEMO=true. Keep BOT_DRY_RUN=true for the first read-only check.")
    print("3. Install requests in Pydroid Pip, then run main.py.")
    print("4. After read-only checks, set BOT_DRY_RUN=false and use /start in Telegram.")


if __name__ == "__main__":
    main()
