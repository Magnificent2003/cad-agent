"""Shared, file-based CAD facts and geometric validation.

The package does not infer a site's meaning from drawing layers or extents.
"""

from .geometry import Site, check_dxf, digest, entity_polygon, inspect_dxf, load_site, occupied_polygon

__all__ = ["Site", "check_dxf", "digest", "entity_polygon", "inspect_dxf", "load_site", "occupied_polygon"]
