"""Validate existing PNG previews before attaching them to a model request."""
from io import BytesIO
from pathlib import Path

from PIL import Image

MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_EDGE = 4096
MAX_PREVIEW_PIXELS = 16_000_000


class PreviewUnavailable(ValueError):
    """A preview is optional; its absence must not prevent file-based analysis."""


def read_preview(path: Path) -> tuple[bytes, dict[str, int]]:
    try:
        if path.is_symlink():
            raise PreviewUnavailable("当前版本预览是符号链接，未作为视觉输入使用。")
        with path.open('rb') as stream:
            data = stream.read(MAX_PREVIEW_BYTES + 1)
    except FileNotFoundError as exc:
        raise PreviewUnavailable("当前版本没有已生成的 PNG 预览。") from exc
    except OSError as exc:
        raise PreviewUnavailable("当前版本的 PNG 预览暂时无法读取。") from exc
    if len(data) > MAX_PREVIEW_BYTES:
        raise PreviewUnavailable("当前版本预览超过 4 MiB 附件上限。")
    try:
        with Image.open(BytesIO(data), formats=['PNG']) as image:
            width, height = image.size
            if max(width, height) > MAX_PREVIEW_EDGE or width * height > MAX_PREVIEW_PIXELS:
                raise PreviewUnavailable("当前版本预览超过 4096 边长或 1600 万像素附件上限。")
            if getattr(image, 'is_animated', False):
                raise PreviewUnavailable("当前版本预览不是单张静态 PNG。")
            image.verify()
        with Image.open(BytesIO(data), formats=['PNG']) as image:
            image.load()
    except PreviewUnavailable:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise PreviewUnavailable("当前版本的 PNG 预览无效或不完整。") from exc
    return data, {'width': width, 'height': height, 'bytes': len(data)}
