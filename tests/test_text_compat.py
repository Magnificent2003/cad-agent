import hashlib
from pathlib import Path

import ezdxf
from fontTools import subset
from fontTools.ttLib import TTFont

from backend.app.cad import render_drawing
from backend.app.cad.reader import query_drawing
from backend.app.cad.text_compat import FontResolver, decode_text
from backend.app.preview_cache import FONT_ROOT


def test_custom_font_registration_and_actual_glyph_coverage(tmp_path):
    # A real installed font can still lack Chinese glyphs. This catches the
    # former filename-only fallback and missing support_dirs registration.
    font = TTFont(FONT_ROOT / "noto/NotoSansCJKsc-Regular.otf")
    cutter = subset.Subsetter()
    cutter.populate(text=" AB123")
    cutter.subset(font)
    for name_id in (1, 3, 4, 6, 16):
        font["name"].setName("CADTestLatin", name_id, 3, 1, 0x409)
    custom = tmp_path / "CADTestLatin.otf"
    font.save(custom)
    font.close()
    resolver = FontResolver({"font_dirs": [str(tmp_path)]})
    requested = resolver.face_for_name(custom.name)
    assert requested.filename == custom.name
    assert resolver.choose("AB123", requested) == requested
    chosen = resolver.choose("总平面图 AB123 Φ ± ㎡", requested)
    assert chosen.filename == "NotoSansCJKsc-Regular.otf"
    assert not resolver.report()["missing_codepoints"]
    assert all({ord(c) for c in "中文 AB123 Φ ± ㎡"} <= resolver.coverage(face) for face in resolver.fallbacks[:2])


def test_legacy_text_in_query_and_preview_preserves_original(tmp_path):
    raw = r"\M+5CBCE\M+5CCE5 AB123 %%p \U+03A6"
    assert decode_text(raw) == "宋体 AB123 %%p Φ"
    assert decode_text(r"bad \M+5FFFF") == r"bad \M+5FFFF"
    doc = ezdxf.new("R2018")
    doc.units = 4
    doc.styles.new("missing", dxfattribs={"font": "not-installed.shx", "bigfont": "not-installed-big.shx"})
    model = doc.modelspace()
    text = model.add_text(raw, dxfattribs={"height": 3, "insert": (0, 20), "style": "missing"})
    model.add_mtext(r"{\fNotInstalled|b0|i0;" + raw + r"\P道路}", dxfattribs={"char_height": 3, "insert": (0, 10), "width": 80})
    block = doc.blocks.new("labels")
    block.add_text(raw, dxfattribs={"height": 3})
    reference = model.add_blockref("labels", (0, -15))
    reference.add_attrib("LABEL", raw, (0, -20), dxfattribs={"height": 3})
    source = tmp_path / "legacy.dxf"
    doc.saveas(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    queried = query_drawing(str(source), {"text": "宋体"})
    assert queried["total"] == 3
    record = next(item for item in queried["entities"] if item["handle"] == text.dxf.handle)
    assert record["text"] == "宋体 AB123 ± Φ"
    assert record["raw_text"] == raw
    preview = render_drawing(str(source), str(tmp_path / "preview"), settings={"preview_dpi": 72})
    assert preview["preferred_format"] == "svg"
    assert preview["fonts"]["legacy_text_entities_decoded"] >= 4
    assert not preview["fonts"]["missing_codepoints"]
    assert Path(tmp_path / "preview/preview.svg").stat().st_size > 1000
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert ezdxf.readfile(source).entitydb[text.dxf.handle].dxf.text == raw
