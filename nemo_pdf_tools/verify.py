"""Render pages of two PDFs at low resolution and compare them.

This is the safety net behind the compressor: text and vector graphics are
never touched, so any page that looks clearly different afterwards points to
an image that was decoded wrongly (inverted colours, broken masks, ...).
"""

import numpy as np

try:
    import gi

    gi.require_version("Poppler", "0.18")
    import cairo
    from gi.repository import Gio, Poppler
except (ImportError, ValueError):
    Poppler = None

RENDER_DPI = 50
MAX_PAGES = 80
# Mean absolute difference (0..255) and share of strongly changed pixels
# above which a page counts as broken.
MEAN_LIMIT = 8.0
BIG_DIFF = 80
BIG_SHARE = 0.01


def available():
    return Poppler is not None


def _open(path, password=None):
    uri = Gio.File.new_for_path(str(path)).get_uri()
    return Poppler.Document.new_from_file(uri, password)


def render_gray(doc, index, dpi=RENDER_DPI):
    page = doc.get_page(index)
    w_pt, h_pt = page.get_size()
    scale = dpi / 72.0
    width, height = max(1, int(w_pt * scale)), max(1, int(h_pt * scale))
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    ctx.scale(scale, scale)
    page.render(ctx)
    surface.flush()
    stride = surface.get_stride()
    data = np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(height, stride // 4, 4)
    rgb = data[:, :width, :3].astype(np.float32)
    return rgb.mean(axis=2)


def pick_pages(page_count, candidates):
    pages = sorted(p for p in candidates if 0 <= p < page_count)
    if len(pages) > MAX_PAGES:
        step = len(pages) / MAX_PAGES
        pages = [pages[int(i * step)] for i in range(MAX_PAGES)]
    return pages


def failing_pages(original, compressed, pages, cancel=None, password=None):
    """Pages that differ visibly. Returns None if rendering is unavailable."""
    if Poppler is None or not pages:
        return None if Poppler is None else []
    try:
        doc_a = _open(original, password)
        doc_b = _open(compressed, password)
    except Exception:
        return None
    if doc_a.get_n_pages() != doc_b.get_n_pages():
        return list(pages)
    bad = []
    for index in pages:
        if cancel is not None and cancel():
            break
        a = render_gray(doc_a, index)
        b = render_gray(doc_b, index)
        if a.shape != b.shape:
            bad.append(index)
            continue
        diff = np.abs(a - b)
        if float(diff.mean()) > MEAN_LIMIT or np.count_nonzero(diff > BIG_DIFF) > diff.size * BIG_SHARE:
            bad.append(index)
    return bad
