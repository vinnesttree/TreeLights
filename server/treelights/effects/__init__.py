"""Effects (architecture §12): the effect interface, WLED-style palettes and parameters, the test
animations, and the effect library (3D volumetric, surface patterns, effects inspired by Matt Parker's
tree, basic, and the drawing painted from the phone)."""

from __future__ import annotations

from .base import (SLIDERS, Chase, Effect, Identify, Params, Renderer, StatusColors, SweepTest, TreeGeometry,
                   make_renderer)
from .basic import EFFECTS_BASIC
from .drawing import EFFECTS_DRAWING, Canvas, DrawingEffect
from .gift import EFFECTS_GIFT
from .palettes import DYNAMIC, GRADIENTS, PaletteBank, catalogue
from .surface import EFFECTS_SURFACE
from .volumetric import EFFECTS_3D

TEST_EFFECTS = {"sweep": SweepTest, "status": StatusColors, "chase": Chase}
LIBRARY: dict[str, type[Effect]] = {e.name: e for e in [*EFFECTS_3D, *EFFECTS_SURFACE, *EFFECTS_GIFT, *EFFECTS_BASIC,
                                                       *EFFECTS_DRAWING]}
EFFECTS: dict[str, type[Effect]] = {**TEST_EFFECTS, **LIBRARY}
PALETTE_IDS = set(GRADIENTS) | set(DYNAMIC)


def library() -> list[dict]:
    return [e.describe() for e in LIBRARY.values()]


__all__ = ["SLIDERS", "Canvas", "Chase", "DrawingEffect", "Effect", "Identify", "Params", "Renderer", "StatusColors",
           "SweepTest", "TreeGeometry", "make_renderer","EFFECTS", "LIBRARY", "TEST_EFFECTS", "PALETTE_IDS", "PaletteBank", "catalogue", "library"]
