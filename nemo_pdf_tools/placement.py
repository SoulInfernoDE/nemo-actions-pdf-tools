"""Find out how large each image is drawn, to know its effective resolution.

The content streams of all pages (and the forms they use) are walked while
tracking the transformation matrix. An image drawn into a unit square scaled
by the current matrix covers |(a, b)| x |(c, d)| points.
"""

import math
from dataclasses import dataclass, field

import pikepdf

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_DEPTH = 12


def multiply(m, n):
    """m x n in PDF's row-vector convention (apply m first, then n)."""
    return (
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    )


def _matrix(values):
    try:
        m = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    return m if len(m) == 6 else None


@dataclass
class Placement:
    min_dpi: float = math.inf
    pages: set = field(default_factory=set)


class PlacementScanner:
    def __init__(self, pdf):
        self.pdf = pdf
        self.placements = {}
        self.unscanned_pages = set()
        self._parsed = {}

    def scan(self, cancel=None):
        for index, page in enumerate(self.pdf.pages):
            if cancel is not None and cancel():
                return self.placements
            try:
                resources = page.obj.get("/Resources")
                self._walk(page, page.obj.objgen, resources, IDENTITY, index, 0)
            except (pikepdf.PdfError, ValueError, TypeError, AttributeError, RecursionError):
                self.unscanned_pages.add(index)
        return self.placements

    def _instructions(self, target, key):
        cached = self._parsed.get(key)
        if cached is None:
            cached = [
                (list(ins.operands), str(ins.operator))
                for ins in pikepdf.parse_content_stream(target, "q Q cm Do")
            ]
            if key != (0, 0):
                self._parsed[key] = cached
        return cached

    def _record(self, xobj, ctm, page_index):
        w_pt = math.hypot(ctm[0], ctm[1])
        h_pt = math.hypot(ctm[2], ctm[3])
        if w_pt <= 0 or h_pt <= 0:
            return

        def note(obj):
            if not obj.is_indirect:
                return
            try:
                width = int(obj.get("/Width", 0))
                height = int(obj.get("/Height", 0))
            except (TypeError, ValueError):
                return
            if width <= 0 or height <= 0:
                return
            dpi = min(width / (w_pt / 72.0), height / (h_pt / 72.0))
            place = self.placements.setdefault(obj.objgen, Placement())
            place.min_dpi = min(place.min_dpi, dpi)
            place.pages.add(page_index)

        note(xobj)
        for key in ("/SMask", "/Mask"):
            mask = xobj.get(key)
            if isinstance(mask, pikepdf.Stream):
                note(mask)

    def _walk(self, target, key, resources, ctm, page_index, depth):
        if depth > MAX_DEPTH:
            return
        xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
        stack = []
        for operands, op in self._instructions(target, key):
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                if stack:
                    ctm = stack.pop()
            elif op == "cm":
                m = _matrix(operands)
                if m:
                    ctm = multiply(m, ctm)
            elif op == "Do" and xobjects is not None and operands:
                xobj = xobjects.get(operands[0])
                if not isinstance(xobj, pikepdf.Stream):
                    continue
                subtype = xobj.get("/Subtype")
                if subtype == "/Image":
                    self._record(xobj, ctm, page_index)
                elif subtype == "/Form":
                    m = _matrix(xobj.get("/Matrix", IDENTITY)) or IDENTITY
                    inner = xobj.get("/Resources", resources)
                    fkey = xobj.objgen if xobj.is_indirect else (0, 0)
                    self._walk(xobj, fkey, inner, multiply(m, ctm), page_index, depth + 1)
