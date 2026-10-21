"""Lossless page operations: merge, split, rotate, export, protect.

None of these re-renders or re-encodes page content; pages are copied as
they are, so text, fonts and images stay bit-identical.
"""

import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pikepdf import Name

from .compress import DocumentError, fix_permissions, open_pdf, unique_path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".jp2"}
A4_PT = (595.276, 841.89)


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def parse_ranges(spec, count):
    """Parse "1-3, 5, 8-" into 0-based page indexes (in the given order).

    Empty means all pages. "-3" means 1-3, "8-" means 8 to the end, and a
    range like "5-2" runs backwards. Raises ValueError on bad input.
    """
    spec = (spec or "").strip()
    if not spec:
        return list(range(count))
    pages = []
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else count
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(part)
        if not (1 <= start <= count and 1 <= end <= count):
            raise ValueError(part)
        step = 1 if end >= start else -1
        pages.extend(i - 1 for i in range(start, end + step, step))
    if not pages:
        raise ValueError(spec)
    return pages


def page_count(path):
    """Number of pages, or 1 for an image file."""
    if is_image(path):
        return 1
    with open_pdf(path) as pdf:
        return len(pdf.pages)


def _image_pdf(path, fit_a4):
    """Wrap an image file into a one-page PDF without re-encoding JPEGs."""
    import img2pdf
    from PIL import Image

    layout = None
    if fit_a4:
        layout = img2pdf.get_layout_fun(A4_PT, fit=img2pdf.FitMode.into, auto_orient=True)
    try:
        data = img2pdf.convert(str(path), layout_fun=layout, rotation=img2pdf.Rotation.ifvalid)
    except (img2pdf.AlphaChannelError, img2pdf.ImageOpenError, img2pdf.UnsupportedColorspaceError,
            img2pdf.JpegColorspaceError, ValueError):
        # Transparency or exotic modes: flatten onto white and use PNG.
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA", "P", "PA"):
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                im = flat
            elif im.mode not in ("RGB", "L", "1", "CMYK"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG", dpi=im.info.get("dpi", (96, 96)))
        data = img2pdf.convert(buf.getvalue(), layout_fun=layout)
    return pikepdf.open(io.BytesIO(data))


# --- merge -------------------------------------------------------------------

@dataclass
class Source:
    path: Path
    pages: str = ""  # range spec, empty = all


def _outline_targets(pdf):
    """Map of page objgen -> page index for resolving bookmarks."""
    return {page.obj.objgen: i for i, page in enumerate(pdf.pages)}


def _named_destination(pdf, name):
    names = pdf.Root.get("/Names")
    if names is not None and "/Dests" in names:
        try:
            tree = pikepdf.NameTree(names.Dests)
            if str(name) in tree:
                return tree[str(name)]
        except (pikepdf.PdfError, TypeError, ValueError):
            pass
    dests = pdf.Root.get("/Dests")
    if isinstance(dests, pikepdf.Dictionary):
        key = "/" + str(name).lstrip("/")
        if key in dests:
            return dests[key]
    return None


def _resolve_page(pdf, item, targets):
    dest = item.destination
    if dest is None and item.action is not None and item.action.get("/S") == "/GoTo":
        dest = item.action.get("/D")
    if isinstance(dest, (pikepdf.Name, pikepdf.String)):
        dest = _named_destination(pdf, dest)
    if isinstance(dest, pikepdf.Dictionary):
        dest = dest.get("/D")
    if isinstance(dest, pikepdf.Array) and len(dest) > 0:
        target = dest[0]
        if isinstance(target, pikepdf.Dictionary) and target.is_indirect:
            return targets.get(target.objgen)
        if isinstance(target, int):
            return int(target)
    return None


def _copy_outline(pdf, items, targets, page_map, depth=0):
    copied = []
    if depth > 20:
        return copied
    for item in items:
        children = _copy_outline(pdf, item.children, targets, page_map, depth + 1)
        index = _resolve_page(pdf, item, targets)
        new_index = page_map.get(index) if index is not None else None
        if new_index is None:
            if not children:
                continue
            new_index = children[0].destination
        new = pikepdf.OutlineItem(str(item.title), new_index)
        new.children.extend(children)
        copied.append(new)
    return copied


def merge(sources, out_path, bookmarks=True, keep_outlines=True, duplex_blank=False, fit_a4=True,
          progress=lambda f: None, cancel=lambda: False):
    """Concatenate pages of PDFs and images into a new PDF."""
    out = pikepdf.new()
    opened = []
    outline_items = []
    try:
        for n, source in enumerate(sources):
            if cancel():
                return None
            progress(n / max(1, len(sources)))
            path = Path(source.path)
            src = _image_pdf(path, fit_a4) if is_image(path) else open_pdf(path)
            opened.append(src)
            indexes = parse_ranges(source.pages, len(src.pages))
            first = len(out.pages)
            page_map = {}
            for i in indexes:
                page_map.setdefault(i, len(out.pages))
                out.pages.append(src.pages[i])
            if duplex_blank and len(indexes) % 2 and n < len(sources) - 1:
                box = out.pages[-1].mediabox
                out.add_blank_page(page_size=(float(box[2]) - float(box[0]), float(box[3]) - float(box[1])))

            children = []
            if keep_outlines and not is_image(path):
                try:
                    with src.open_outline() as src_outline:
                        children = _copy_outline(src, src_outline.root, _outline_targets(src), page_map)
                except (pikepdf.PdfError, pikepdf.OutlineStructureError, AttributeError, TypeError, ValueError):
                    children = []
            if bookmarks:
                title = path.stem
                docinfo_title = None
                if not is_image(path):
                    try:
                        docinfo_title = str(src.docinfo.get("/Title", "")).strip()
                    except Exception:
                        docinfo_title = None
                item = pikepdf.OutlineItem(title if not docinfo_title else f"{title} – {docinfo_title}"
                                           if docinfo_title != title else title, first)
                item.children.extend(children)
                outline_items.append(item)
            else:
                outline_items.extend(children)
        if outline_items:
            with out.open_outline() as outline:
                outline.root.extend(outline_items)
            out.Root.PageMode = Name.UseOutlines
        progress(0.95)
        _save_atomic(out, out_path)
        progress(1.0)
        return Path(out_path)
    finally:
        out.close()
        for src in opened:
            src.close()


def _save_atomic(pdf, out_path, **kwargs):
    out_path = Path(out_path)
    fd, tmp = tempfile.mkstemp(prefix=".pdf-tools-", suffix=".pdf", dir=out_path.parent)
    os.close(fd)
    try:
        pdf.save(tmp, object_stream_mode=pikepdf.ObjectStreamMode.generate, **kwargs)
        fix_permissions(tmp)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- split -------------------------------------------------------------------

SPLIT_EACH = "each"
SPLIT_EVERY = "every"
SPLIT_RANGES = "ranges"
SPLIT_BOOKMARKS = "bookmarks"


def _safe_name(text, fallback):
    text = re.sub(r"[/\\\x00-\x1f]", "_", str(text)).strip(" .")
    return text[:80] or fallback


def split_groups(pdf, mode, value=""):
    """List of (label, [page indexes]) for the chosen split mode."""
    count = len(pdf.pages)
    if mode == SPLIT_EACH:
        return [(str(i + 1), [i]) for i in range(count)]
    if mode == SPLIT_EVERY:
        n = max(1, int(value))
        return [(f"{i + 1}-{min(i + n, count)}", list(range(i, min(i + n, count)))) for i in range(0, count, n)]
    if mode == SPLIT_RANGES:
        groups = []
        for part in re.split(r"[;\n]+", value or ""):
            if part.strip():
                pages = parse_ranges(part, count)
                groups.append((re.sub(r"\s+", "", part.strip()).replace(",", "_"), pages))
        if not groups:
            raise ValueError(value)
        return groups
    if mode == SPLIT_BOOKMARKS:
        targets = _outline_targets(pdf)
        starts = []
        with pdf.open_outline() as outline:
            for item in outline.root:
                index = _resolve_page(pdf, item, targets)
                if index is not None:
                    starts.append((index, str(item.title)))
        starts = sorted({i: t for i, t in reversed(starts)}.items())
        if not starts:
            raise ValueError("no bookmarks")
        if starts[0][0] != 0:
            starts.insert(0, (0, ""))
        groups = []
        for n, (start, title) in enumerate(starts):
            end = starts[n + 1][0] if n + 1 < len(starts) else count
            groups.append((f"{n + 1:02d} {_safe_name(title, '')}".strip(), list(range(start, end))))
        return groups
    raise ValueError(mode)


def split(path, mode, value, out_dir, progress=lambda f: None, cancel=lambda: False):
    """Write one PDF per group into out_dir. Returns the written paths."""
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with open_pdf(path) as src:
        groups = split_groups(src, mode, value)
        width = len(str(len(src.pages)))
        for n, (label, pages) in enumerate(groups):
            if cancel():
                break
            progress(n / len(groups))
            if mode == SPLIT_EACH:
                label = label.zfill(width)
            target = unique_path(out_dir / f"{path.stem}_{label}.pdf")
            with pikepdf.new() as part:
                for i in pages:
                    part.pages.append(src.pages[i])
                _save_atomic(part, target)
            written.append(target)
    progress(1.0)
    return written


# --- rotate ------------------------------------------------------------------

def rotate(path, angle, pages=""):
    """Rotate pages in place (only the /Rotate entry changes)."""
    path = Path(path)
    with open_pdf(path) as pdf:
        if _is_signed(pdf):
            raise DocumentError("signed")
        for i in parse_ranges(pages, len(pdf.pages)):
            pdf.pages[i].rotate(angle, relative=True)
        encrypted = pdf.is_encrypted
        fd, tmp = tempfile.mkstemp(prefix=".pdf-tools-", suffix=".pdf", dir=path.parent)
        os.close(fd)
        try:
            pdf.save(tmp, encryption=encrypted)
        except BaseException:
            os.unlink(tmp)
            raise
    st = path.stat()
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)


def _is_signed(pdf):
    form = pdf.Root.get("/AcroForm")
    try:
        return bool(form is not None and int(form.get("/SigFlags", 0)) & 1)
    except (TypeError, ValueError):
        return False


# --- protect -----------------------------------------------------------------

def protect(path, out_path, user_password, owner_password="", allow_print=True, allow_copy=True,
            allow_edit=True):
    """Save an AES-256 encrypted copy."""
    with open_pdf(path) as pdf:
        allow = pikepdf.Permissions(
            accessibility=True,
            extract=allow_copy,
            modify_annotation=allow_edit,
            modify_assembly=allow_edit,
            modify_form=allow_edit,
            modify_other=allow_edit,
            print_lowres=allow_print,
            print_highres=allow_print,
        )
        owner = owner_password or user_password
        _save_atomic(pdf, out_path, encryption=pikepdf.Encryption(user=user_password, owner=owner, R=6, allow=allow))
    return Path(out_path)


# --- export ------------------------------------------------------------------

def extract_images(path, out_dir, min_size=32, progress=lambda f: None, cancel=lambda: False):
    """Save the embedded images in their original format where possible."""
    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    seen = set()
    with open_pdf(path) as pdf:
        total = len(pdf.pages)
        for p, page in enumerate(pdf.pages):
            if cancel():
                break
            progress(p / max(1, total))
            for name, raw in page.images.items():
                if raw.objgen in seen:
                    continue
                seen.add(raw.objgen)
                try:
                    image = pikepdf.PdfImage(raw)
                    if image.width < min_size or image.height < min_size:
                        continue
                    stem = out_dir / f"{path.stem}_p{p + 1}_{len(written) + 1}"
                    written.append(Path(image.extract_to(fileprefix=str(stem))))
                except Exception:
                    continue
    progress(1.0)
    return written


def render_pages(path, out_dir, fmt="png", dpi=150, pages="", jpeg_quality=90,
                 progress=lambda f: None, cancel=lambda: False):
    """Render pages to PNG or JPEG files with Poppler."""
    import cairo
    import gi

    gi.require_version("Poppler", "0.18")
    from gi.repository import Gio, Poppler
    from PIL import Image

    path = Path(path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc = Poppler.Document.new_from_file(Gio.File.new_for_path(str(path)).get_uri(), None)
    except Exception as exc:
        raise DocumentError("damaged", str(exc)) from exc
    count = doc.get_n_pages()
    indexes = parse_ranges(pages, count)
    width = len(str(count))
    written = []
    scale = dpi / 72.0
    for n, i in enumerate(indexes):
        if cancel():
            break
        progress(n / max(1, len(indexes)))
        page = doc.get_page(i)
        w_pt, h_pt = page.get_size()
        w, h = max(1, round(w_pt * scale)), max(1, round(h_pt * scale))
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
        ctx = cairo.Context(surface)
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.scale(scale, scale)
        page.render_for_printing(ctx)
        surface.flush()
        im = Image.frombuffer("RGB", (w, h), bytes(surface.get_data()), "raw", "BGRX", surface.get_stride(), 1)
        ext = "jpg" if fmt == "jpeg" else "png"
        target = unique_path(out_dir / f"{path.stem}_{str(i + 1).zfill(width)}.{ext}")
        if fmt == "jpeg":
            im.save(target, "JPEG", quality=jpeg_quality, optimize=True, dpi=(dpi, dpi))
        else:
            im.save(target, "PNG", optimize=False, dpi=(dpi, dpi))
        written.append(target)
    progress(1.0)
    return written
