"""Launch the tool process without inheriting model credentials."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    # Do not load .env here. Only the host can supply the internal service token.
    allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TEMP", "TMP", "CAD_API_BASE_URL", "CAD_INTERNAL_TOKEN"}
    child_env = {name: value for name, value in os.environ.items() if name in allowed}
    child_env["PYTHONPATH"] = str(root)
    child_env["PYTHONUNBUFFERED"] = "1"
    os.chdir(root)
    os.execve(sys.executable, [sys.executable, "-m", "cad_mcp.server"], child_env)


if __name__ == "__main__":
    main()
