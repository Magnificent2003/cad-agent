"""Decode legacy CAD text and choose fonts for derived previews only."""
from collections import Counter
from functools import lru_cache
from pathlib import Path
import re

import ezdxf
from ezdxf.fonts import fonts
from ezdxf.lldxf.encoding import decode_dxf_unicode, decode_mif_to_unicode
from ezdxf.tools.text import plain_mtext

from ..preview_cache import DEFAULT_FONT, font_directories


def decode_text(value: str) -> str:
    return decode_dxf_unicode(decode_mif_to_unicode(value))


def plain_text(entity) -> str:
    if entity.dxftype() == "MTEXT":
        return plain_mtext(decode_text(entity.text))
    # Let ezdxf handle %%d/%%p/%%c after decoding the legacy string.
    copy = entity.copy()
    copy.dxf.text = decode_text(entity.dxf.get("text", ""))
    return copy.plain_text() if hasattr(copy, "plain_text") else copy.dxf.text


def configure_fonts(settings: dict):
    directories = font_directories(settings)
    ezdxf.options.support_dirs = list(dict.fromkeys([*directories, *ezdxf.options.support_dirs]))
    # support_dirs alone does not register fonts with ezdxf's font manager.
    fonts.font_manager.scan_all([directory for directory in directories if Path(directory).is_dir()])


class FontResolver:
    def __init__(self, settings: dict):
        configure_fonts(settings)
        names = [settings.get("fallback_font", DEFAULT_FONT), DEFAULT_FONT, "NotoSerifCJKsc-Regular.otf", "DejaVuSans.ttf"]
        self.fallbacks = list(dict.fromkeys(fonts.find_font_face(name) for name in names))
        self.substitutions = Counter()
        self.missing = set()
        self.decoded_entities = 0

    @lru_cache(maxsize=256)
    def coverage(self, face) -> frozenset[int]:
        try:
            if Path(face.filename).suffix.lower() in {".shx", ".shp"}:
                cache = fonts.font_manager.get_shapefile_glyph_cache(face.filename)
                return frozenset(cache.font.shapes)
            font = fonts.font_manager.get_ttf_font(face.filename)
            return frozenset(font.getBestCmap() or {})
        except (OSError, ValueError, KeyError, AttributeError, fonts.FontNotFoundError, fonts.UnsupportedFont):
            return frozenset()

    def face_for_name(self, name: str):
        name = decode_text(name).replace("\\", "/").rsplit("/", 1)[-1]
        if fonts.font_manager.has_font(name):
            return fonts.find_font_face(name)
        return fonts.find_best_match(family=name)

    def choose(self, text: str, requested=None):
        text = decode_text(text)
        codepoints = {ord(c) for c in text if not c.isspace() and c.isprintable()}
        if requested and not requested.filename:
            requested = self.face_for_name(requested.family)
        candidates = list(dict.fromkeys([*([requested] if requested else []), *self.fallbacks]))
        selected = next((face for face in candidates if codepoints <= self.coverage(face)), None)
        if selected is None:
            selected = max(candidates, key=lambda face: len(codepoints & self.coverage(face)))
            self.missing.update(codepoints - self.coverage(selected))
        if selected != requested:
            self.substitutions[(requested.filename if requested else "missing", selected.filename)] += 1
        return selected

    def normalize(self, entity):
        """Operate on a disposable document or virtual copy, never save it."""
        if entity.dxftype() == "MTEXT":
            raw = entity.text
            decoded = decode_text(raw)
            self.decoded_entities += decoded != raw
            visible = plain_mtext(decoded)

            def inline_font(match):
                face = self.face_for_name(match[2])
                chosen = self.choose(visible, face)
                return match[0] if chosen == face else match[1] + chosen.family + match[3] + ";"

            entity.text = re.sub(r"(\\[fF])([^;|]+)([^;]*);", inline_font, decoded)
        elif entity.dxf.hasattr("text"):
            raw = entity.dxf.text
            entity.dxf.text = decode_text(raw)
            self.decoded_entities += raw != entity.dxf.text

    def report(self) -> dict:
        return {
            "fallback_fonts": [face.filename for face in self.fallbacks],
            "legacy_text_entities_decoded": self.decoded_entities,
            "substitutions": [{"requested": old, "used": new, "runs": count} for (old, new), count in sorted(self.substitutions.items())],
            "missing_codepoints": [f"U+{code:04X}" for code in sorted(self.missing)],
        }


def compatible_frontend(context, backend, config, resolver):
    from ezdxf.addons.drawing import Frontend

    class CompatibleFrontend(Frontend):
        # Proxy graphics and exploded virtual block entities arrive here too.
        def draw_text_entity(self, entity, properties):
            copy = entity.copy()
            resolver.normalize(copy)
            properties.font = resolver.choose(plain_text(copy), properties.font)
            super().draw_text_entity(copy, properties)

        def draw_mtext_entity(self, entity, properties):
            copy = entity.copy()
            resolver.normalize(copy)
            properties.font = resolver.choose(plain_text(copy), properties.font)
            super().draw_mtext_entity(copy, properties)

    return CompatibleFrontend(context, backend, config=config)
