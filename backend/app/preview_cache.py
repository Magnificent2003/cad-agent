"""Version derived images independently of immutable drawing revisions."""
import hashlib
import json
from pathlib import Path

FONT_ROOT = Path(__file__).resolve().parents[2] / "assets" / "fonts"
RENDERER_VERSION = 2
DEFAULT_FONT = "NotoSansCJKsc-Regular.otf"


def font_directories(settings: dict) -> list[str]:
    custom = settings.get("font_dirs", [])
    if isinstance(custom, str):
        custom = [value for value in custom.split(":") if value]
    return list(dict.fromkeys([str(FONT_ROOT / "noto"), str(FONT_ROOT / "local"), *custom]))


def preview_signature(settings: dict | None = None) -> str:
    settings = settings or {}
    files = []
    for directory in font_directories(settings):
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file() and path.suffix.lower() in {".ttf", ".ttc", ".otf", ".shx", ".shp", ".lff"}:
                stat = path.stat()
                files.append([str(path.resolve()), stat.st_size, stat.st_mtime_ns])
    payload = [RENDERER_VERSION, settings.get("fallback_font", DEFAULT_FONT), settings.get("preview_dpi", 140), files]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:24]
