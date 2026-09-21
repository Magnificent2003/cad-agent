"""Synthetic-only vision acceptance MCP; never enabled by the application."""

from __future__ import annotations

import base64
from io import BytesIO
import os

from mcp.server import MCPServer
from mcp.types import ImageContent
from PIL import Image, ImageDraw, ImageFont

mcp = MCPServer("cad-vision-acceptance")
_CODE = os.environ["CAD_SMOKE_CODE"]
_COUNT = int(os.environ["CAD_SMOKE_CIRCLES"])


@mcp.tool()
def render_region() -> ImageContent:
    """生成验收专用图片；请看图读取四位数字和红色圆圈数量，随后调用 verify_visual。"""
    canvas = Image.new("RGB", (600, 240), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("DejaVuSans.ttf", 72)
    draw.text((170, 15), _CODE, fill="black", font=font)
    for index in range(_COUNT):
        left = 45 + index * 100
        draw.ellipse((left, 140, left + 65, 205), fill="red")
    stream = BytesIO()
    canvas.save(stream, format="PNG")
    return ImageContent(type="image", data=base64.b64encode(stream.getvalue()).decode("ascii"), mime_type="image/png")


@mcp.tool()
def verify_visual(code: str, red_circles: int) -> dict[str, bool]:
    """提交刚刚在图片中读到的四位数字及红色圆圈数量，校验视觉链路。"""
    return {"verified": code == _CODE and red_circles == _COUNT}


if __name__ == "__main__":
    mcp.run(transport="stdio")
