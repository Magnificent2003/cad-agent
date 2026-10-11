"""Shared physical tolerances for 2D layout calculations, in SI units.

These absorb floating-point roundoff at survey coordinates. They are not design
clearances and never change the geometry, required dimensions or native DXF.
"""
LINEAR_TOLERANCE_M = 1e-6
AREA_TOLERANCE_M2 = 1e-7
AXIS_TOLERANCE_DEG = 0.5
