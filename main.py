import os
from pathlib import Path
import sys


# Pydroid executes an opened file through ``exec()``; __file__ may still point to its runner.
# Locate the project before importing the package.
def _project_root() -> Path:
    candidates = [
        Path.cwd(),
        Path(sys.argv[0]).resolve().parent,
        Path("/storage/emulated/0/Download/AI-IMPULS-TRAIDER"),
        Path(globals().get("__file__", ".")).resolve().parent,
    ]
    for candidate in candidates:
        if (candidate / "trader" / "__init__.py").is_file():
            return candidate
    raise RuntimeError(
        "Не найдена папка trader. Запустите pydroid_installer.py и затем откройте "
        "/storage/emulated/0/Download/AI-IMPULS-TRAIDER/main.py"
    )


ROOT = _project_root()
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.app import main  # noqa: E402


if __name__ == "__main__":
    main()
