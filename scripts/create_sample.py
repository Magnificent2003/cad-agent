"""Generate an explicitly synthetic, editable CAD sample for integration checks."""
import argparse
from pathlib import Path
import ezdxf


def create_sample(path: Path):
    if path.exists():
        raise FileExistsError("请选择不存在的输出文件。")
    doc = ezdxf.new("R2018")
    doc.units = 4
    for layer, color in (("SITE", 4), ("BUILDING", 7), ("ANNOTATION", 3)):
        doc.layers.new(layer, dxfattribs={"color": color})
    m = doc.modelspace()
    m.add_lwpolyline([(0, 0), (60000, 0), (60000, 40000), (0, 40000)], close=True, dxfattribs={"layer": "SITE"})
    m.add_lwpolyline([(10000, 10000), (28000, 10000), (28000, 25000), (10000, 25000)], close=True, dxfattribs={"layer": "BUILDING"})
    m.add_text("合成演示图 · 非真实工程", dxfattribs={"insert": (6000, 35000), "height": 1500, "layer": "ANNOTATION"})
    m.add_text("临时办公区", dxfattribs={"insert": (12000, 17000), "height": 1200, "layer": "ANNOTATION"})
    sheet = doc.layouts.new("说明页")
    sheet.add_text("演示图纸 / mm", dxfattribs={"insert": (20, 20), "height": 5})
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="samples/demo.dxf")
    print(create_sample(Path(parser.parse_args().output)))
