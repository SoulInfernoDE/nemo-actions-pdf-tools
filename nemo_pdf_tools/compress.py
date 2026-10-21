"""Shrink PDF files without touching text, fonts or vector graphics.

Strategy (similar to Acrobat's "Reduce file size"):

* structure: object streams, maximum Flate compression for every stream,
  removal of unused objects, merging of identical images;
* lossless image work: PNG predictors, CCITT G4 for black-and-white scans,
  colour images that are really gray become gray;
* optional lossy image work: downsampling above a target resolution and
  JPEG re-encoding at a chosen quality;
* finally the result is rendered and compared with the original page by
  page, and images that would break a page are left as they were.
"""

import hashlib
import io
import os
import tempfile
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import pikepdf
from pikepdf import Name

from . import images, verify
from .images import KIND_PHOTO, KIND_SKIP, SkipImage
from .placement import PlacementScanner

pikepdf.settings.set_flate_compression_level(9)

WORKERS = max(1, min(8, os.cpu_count() or 2))
SAMPLE_SIZE = 12
DECODED_CACHE_BYTES = 600 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    max_dpi: int | None = None
    jpeg_quality: int | None = None
    grayscale: bool = True
    remove_extras: bool = True
    remove_metadata: bool = False
    linearize: bool = False
    verify: bool = True

    @property
    def lossless(self):
        return self.jpeg_quality is None

    @property
    def image_key(self):
        return (self.max_dpi, self.jpeg_quality, self.grayscale)


# Strength 0 is lossless; the other anchors match the presets.
_ANCHORS = ((1, 450, 95), (25, 300, 88), (50, 200, 78), (75, 144, 62), (100, 96, 45))
PRESETS = (("lossless", 0), ("high", 25), ("balanced", 50), ("strong", 75), ("maximum", 100))


def strength_values(strength):
    """(max_dpi, jpeg_quality) for a strength of 0..100; (None, None) = lossless."""
    strength = max(0, min(100, int(round(strength))))
    if strength == 0:
        return None, None
    for (s0, d0, q0), (s1, d1, q1) in zip(_ANCHORS, _ANCHORS[1:]):
        if s0 <= strength <= s1:
            t = (strength - s0) / (s1 - s0)
            return int(round(d0 + (d1 - d0) * t)), int(round(q0 + (q1 - q0) * t))
    return _ANCHORS[-1][1], _ANCHORS[-1][2]


def settings_for_strength(strength, **options):
    dpi, quality = strength_values(strength)
    return Settings(max_dpi=dpi, jpeg_quality=quality, **options)


class DocumentError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason  # "password", "damaged", "io"
        self.detail = detail


def open_pdf(path):
    try:
        return pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise DocumentError("password") from exc
    except pikepdf.PdfError as exc:
        raise DocumentError("damaged", str(exc)) from exc
    except OSError as exc:
        raise DocumentError("io", exc.strerror or str(exc)) from exc


class _CountingSink(io.RawIOBase):
    """A write-only stream that only counts bytes."""

    def __init__(self):
        super().__init__()
        self.count = 0

    def writable(self):
        return True

    def write(self, b):
        n = len(b)
        self.count += n
        return n


def _strip_keys(pdf, keys):
    for obj in pdf.objects:
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
            for key in keys:
                if key in obj:
                    del obj[key]


def prepare(pdf, settings, pdfa=False):
    """Remove data a reader never shows, according to the settings."""
    if settings.remove_extras:
        # Page thumbnails and private editor data (e.g. Illustrator's
        # PieceInfo, which can be larger than the visible content).
        _strip_keys(pdf, ("/Thumb", "/PieceInfo"))
    if settings.remove_metadata and not pdfa:
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
        if "/Info" in pdf.trailer:
            del pdf.trailer["/Info"]


def _walk_direct(obj, visit, depth=0):
    """Call visit(dict) for obj and every direct dictionary inside it."""
    if depth > 32:
        return
    if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
        visit(obj)
        for _, value in obj.items():
            if isinstance(value, (pikepdf.Dictionary, pikepdf.Array)) and not value.is_indirect:
                _walk_direct(value, visit, depth + 1)
    elif isinstance(obj, pikepdf.Array):
        for value in obj:
            if isinstance(value, (pikepdf.Dictionary, pikepdf.Array)) and not value.is_indirect:
                _walk_direct(value, visit, depth + 1)


def referenced_images(pdf):
    """objgens of image XObjects that are reachable through resources."""
    found = set()

    def visit(d):
        xobjects = d.get("/XObject")
        if isinstance(xobjects, pikepdf.Dictionary):
            for _, x in xobjects.items():
                if isinstance(x, pikepdf.Stream) and x.is_indirect and x.get("/Subtype") == "/Image":
                    found.add(x.objgen)
        if d.get("/Subtype") == "/Image":
            for key in ("/SMask", "/Mask"):
                m = d.get(key)
                if isinstance(m, pikepdf.Stream) and m.is_indirect:
                    found.add(m.objgen)

    for obj in pdf.objects:
        _walk_direct(obj, visit)
    return found


def dedupe_images(pdf):
    """Point identical image streams to a single copy. Returns removed objgens."""
    removed = set()
    # Identical images with identical (but separate) soft masks only match
    # once their masks have been merged, hence a second pass.
    for _ in range(2):
        found = _dedupe_pass(pdf)
        if not found:
            break
        removed |= found
    return removed


def _dedupe_pass(pdf):
    groups = {}
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image" and obj.is_indirect:
            try:
                raw = obj.read_raw_bytes()
            except pikepdf.PdfError:
                continue
            h = hashlib.sha256(raw)
            for key in sorted(obj.keys()):
                if key != "/Length":
                    h.update(key.encode())
                    value = obj[key]
                    h.update(repr(value.objgen if getattr(value, "is_indirect", False) else value).encode())
            groups.setdefault(h.hexdigest(), []).append(obj)
    mapping = {}
    for objs in groups.values():
        for dup in objs[1:]:
            mapping[dup.objgen] = objs[0]
    if not mapping:
        return set()

    def visit(d):
        xobjects = d.get("/XObject")
        if isinstance(xobjects, pikepdf.Dictionary):
            for name, x in list(xobjects.items()):
                if getattr(x, "is_indirect", False) and x.objgen in mapping:
                    xobjects[name] = mapping[x.objgen]
        if d.get("/Subtype") == "/Image":
            for key in ("/SMask", "/Mask"):
                m = d.get(key)
                if isinstance(m, pikepdf.Stream) and m.is_indirect and m.objgen in mapping:
                    d[key] = mapping[m.objgen]

    for obj in pdf.objects:
        _walk_direct(obj, visit)
    return set(mapping)


def _deflate(data):
    return zlib.compress(data, 9)


def recompress_streams(pdf, skip, cancel=lambda: False):
    """Recompress plain Flate streams at the highest level.

    qpdf's own recompress_flate would also strip the PNG predictors from the
    images this module has just optimised, so it is done here instead, and
    the images handled elsewhere are skipped.
    """
    batch = []

    def flush():
        datas = _POOL.map(_deflate, [data for _, data, _ in batch])
        for (obj, _, old_size), new in zip(batch, datas):
            if len(new) < old_size:
                obj.write(new, filter=Name.FlateDecode)
        batch.clear()

    pending_bytes = 0
    for obj in pdf.objects:
        if not isinstance(obj, pikepdf.Stream) or not obj.is_indirect or obj.objgen in skip:
            continue
        if obj.get("/Filter") != Name.FlateDecode or "/DecodeParms" in obj:
            continue
        if cancel():
            raise Cancelled()
        try:
            old_size = len(obj.read_raw_bytes())
            data = obj.read_bytes()
        except pikepdf.PdfError:
            continue
        batch.append((obj, bytes(data), old_size))
        pending_bytes += len(data)
        if pending_bytes > 64 * 1024 * 1024 or len(batch) >= 256:
            flush()
            pending_bytes = 0
    if batch:
        flush()


def save(pdf, target, settings, progress=None, encrypted=False):
    pdf.save(
        target,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        recompress_flate=False,
        linearize=settings.linearize and not isinstance(target, io.IOBase),
        encryption=bool(encrypted),
        progress=progress,
    )


@dataclass
class Preview:
    before: object  # PIL image
    after: object
    page: int | None
    dpi_before: float | None
    dpi_after: float | None


@dataclass
class Estimate:
    original_size: int
    size: int
    exact: bool
    image_count: int = 0
    changed: int = 0
    downsampled: int = 0
    to_gray: int = 0
    min_dpi: float | None = None
    ssim: float | None = None
    preview: Preview | None = None

    @property
    def saving(self):
        if not self.original_size:
            return 0.0
        return 1.0 - self.size / self.original_size


@dataclass
class CompressResult:
    status: str  # "ok", "not_smaller", "cancelled"
    original_size: int
    size: int = 0
    path: Path | None = None
    skipped_images: int = 0
    verified: bool | None = None  # None: rendering not available
    fallback: bool = False


class Cancelled(Exception):
    pass


class _DecodedCache:
    """Shared LRU cache of decoded images, bounded by memory use."""

    def __init__(self, limit=DECODED_CACHE_BYTES):
        self.limit = limit
        self.used = 0
        self.items = OrderedDict()
        self.lock = threading.Lock()

    @staticmethod
    def _cost(im):
        return im.width * im.height * max(1, len(im.getbands()))

    def get(self, key):
        with self.lock:
            im = self.items.get(key)
            if im is not None:
                self.items.move_to_end(key)
            return im

    def put(self, key, im):
        cost = self._cost(im)
        if cost > self.limit // 3:
            return
        with self.lock:
            if key in self.items:
                return
            self.items[key] = im
            self.used += cost
            while self.used > self.limit and self.items:
                _, old = self.items.popitem(last=False)
                self.used -= self._cost(old)


DECODED = _DecodedCache()
_POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="pdf-image")


class Document:
    """One PDF: analysis, live estimates and the final compression.

    All methods must be called from the same (engine) thread; pixel work is
    spread over a shared thread pool internally.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self.pdf = open_pdf(self.path)
        self.page_count = len(self.pdf.pages)
        self.encrypted = self.pdf.is_encrypted
        self.signed = self._is_signed()
        try:
            self.pdfa = bool(self.pdf.open_metadata().pdfa_status)
        except Exception:
            self.pdfa = False
        self.images = []
        self.duplicates = set()
        self.unscanned_pages = set()
        self._baselines = {}
        self._results = OrderedDict()
        self.analyzed = False

    def close(self):
        self.pdf.close()

    def _is_signed(self):
        form = self.pdf.Root.get("/AcroForm")
        try:
            return bool(form is not None and int(form.get("/SigFlags", 0)) & 1)
        except (TypeError, ValueError):
            return False

    # -- analysis -----------------------------------------------------------

    def analyze(self, cancel=lambda: False):
        # Merging duplicates first keeps the estimate in line with the result.
        self.duplicates = dedupe_images(self.pdf)
        scanner = PlacementScanner(self.pdf)
        placements = scanner.scan(cancel)
        self.unscanned_pages = scanner.unscanned_pages
        reachable = referenced_images(self.pdf)
        smask_ids = set()
        streams = []
        for objgen in reachable:
            try:
                obj = self.pdf.get_object(objgen)
            except (pikepdf.PdfError, ValueError):
                continue
            streams.append(obj)
            smask = obj.get("/SMask")
            if isinstance(smask, pikepdf.Stream) and smask.is_indirect:
                smask_ids.add(smask.objgen)
        for obj in streams:
            if cancel():
                raise Cancelled()
            try:
                entry = images.describe_image(obj, obj.objgen, smask_ids)
            except (pikepdf.PdfError, ValueError, TypeError):
                continue
            place = placements.get(obj.objgen)
            if place is not None:
                entry.dpi = place.min_dpi if place.min_dpi != float("inf") else None
                entry.pages = set(place.pages)
            self.images.append(entry)
        self.images.sort(key=lambda e: -e.raw_size)
        self._handled = {e.objgen for e in self.images if e.kind != KIND_SKIP}
        self.analyzed = True

    def baseline(self, settings):
        """Size after structural optimisation alone."""
        key = (settings.remove_extras, settings.remove_metadata)
        if key not in self._baselines:
            pdf = open_pdf(self.path)
            try:
                prepare(pdf, settings, self.pdfa)
                dedupe_images(pdf)
                recompress_streams(pdf, self._handled)
                sink = _CountingSink()
                save(pdf, sink, settings, encrypted=self.encrypted)
                self._baselines[key] = sink.count
            finally:
                pdf.close()
        return self._baselines[key]

    # -- image work ---------------------------------------------------------

    @staticmethod
    def _result_key(entry, settings, plan):
        return (entry.objgen, settings.image_key, plan.best and plan.method == images.METHOD_FLATE)

    def _cached(self, entry, settings, plan):
        key = self._result_key(entry, settings, plan)
        result = self._results.get(key)
        if result is not None:
            self._results.move_to_end(key)
        return result

    def _store(self, entry, settings, plan, result):
        self._results[self._result_key(entry, settings, plan)] = result
        while len(self._results) > 200:
            self._results.popitem(last=False)

    def _run(self, pdf, jobs, settings, measure, cancel, done=None):
        """Process (entry, plan) jobs; returns {objgen: Result}."""
        out = {}
        pending = []
        in_flight = WORKERS * 2

        def collect(item):
            entry, plan, future = item
            try:
                result, original = future.result()
            except SkipImage:
                result = images.Result(False)
            except Exception:
                result = images.Result(False)
            else:
                if pdf is self.pdf:
                    DECODED.put((str(self.path), entry.objgen), original)
            self._store(entry, settings, plan, result)
            out[entry.objgen] = result
            if done:
                done(len(out))

        for entry, plan in jobs:
            if cancel():
                raise Cancelled()
            cached = self._cached(entry, settings, plan)
            if cached is not None and (not measure or cached.ssim is not None or not cached.accepted
                                       or plan.method != images.METHOD_JPEG):
                out[entry.objgen] = cached
                if done:
                    done(len(out))
                continue
            decoded = DECODED.get((str(self.path), entry.objgen))
            raw = None
            if decoded is None:
                try:
                    raw = images.extract_image(pdf.get_object(entry.objgen), entry)
                except (SkipImage, pikepdf.PdfError):
                    out[entry.objgen] = images.Result(False)
                    continue
            pending.append((entry, plan, _POOL.submit(images.process, entry, raw, plan, measure, decoded)))
            if len(pending) >= in_flight:
                collect(pending.pop(0))
        while pending:
            if cancel():
                for *_, f in pending:
                    f.cancel()
                raise Cancelled()
            collect(pending.pop(0))
        return out

    @staticmethod
    def _sample(planned):
        if len(planned) <= SAMPLE_SIZE:
            return planned
        # The biggest images dominate the result; add a spread of the rest.
        top = planned[: SAMPLE_SIZE // 2]
        rest = planned[SAMPLE_SIZE // 2:]
        step = len(rest) / (SAMPLE_SIZE - len(top))
        return top + [rest[int(i * step)] for i in range(SAMPLE_SIZE - len(top))]

    def estimate(self, settings, cancel=lambda: False, zoom_dpi=192, preview=True):
        base = self.baseline(settings)
        planned = [(e, p) for e in self.images if (p := images.make_plan(e, settings))]
        sample = self._sample(planned)
        results = self._run(self.pdf, sample, settings, True, cancel)

        # Extrapolate the images that were not sampled from bytes per output
        # pixel of the sampled images with the same method.
        stats = {}
        for entry, plan in sample:
            r = results.get(entry.objgen)
            s = stats.setdefault(plan.method, [0, 0, 0, 0])  # new bytes, out pixels, raw bytes, raw kept
            if r is not None and r.accepted:
                s[0] += r.size
                s[1] += r.width * r.height
            else:
                s[2] += entry.raw_size
                s[3] += entry.pixels * plan.scale * plan.scale

        est = Estimate(self.size, base, exact=len(sample) == len(planned), image_count=len(self.images))
        delta = 0
        ssim_sum = ssim_weight = 0.0
        for entry, plan in planned:
            r = results.get(entry.objgen)
            if r is None:
                new_bytes, new_pixels, _, kept_pixels = stats.get(plan.method, (0, 0, 0, 0))
                out_pixels = entry.pixels * plan.scale * plan.scale
                if new_pixels and new_pixels >= kept_pixels:
                    predicted = out_pixels * new_bytes / new_pixels
                    if predicted < entry.raw_size:
                        delta += predicted - entry.raw_size
                        est.changed += 1
                        est.downsampled += plan.scale < 1.0
                continue
            if not r.accepted:
                continue
            est.changed += 1
            delta += r.size - entry.raw_size
            if r.downsampled:
                est.downsampled += 1
            if r.to_gray:
                est.to_gray += 1
            if entry.dpi and entry.kind == KIND_PHOTO:
                out_dpi = entry.dpi * r.width / entry.width
                est.min_dpi = out_dpi if est.min_dpi is None else min(est.min_dpi, out_dpi)
            if r.ssim is not None:
                weight = float(r.width * r.height)
                ssim_sum += r.ssim * weight
                ssim_weight += weight
        est.size = max(1, int(base + delta))
        if ssim_weight:
            est.ssim = ssim_sum / ssim_weight
        if preview:
            est.preview = self._preview(sample, results, zoom_dpi)
        return est

    def _preview(self, sample, results, zoom_dpi):
        candidates = [
            (e, results[e.objgen]) for e, p in sample
            if results.get(e.objgen) is not None and results[e.objgen].accepted
            and (results[e.objgen].filter == "DCTDecode" or results[e.objgen].downsampled)
        ]
        if not candidates:
            return None
        entry, result = max(candidates, key=lambda c: c[0].pixels)
        original = DECODED.get((str(self.path), entry.objgen))
        if original is None:
            return None
        after = images.decode_result(result)
        if after is None:
            # Lossless codecs: the only change is the resolution.
            after = images.resize(original, (result.width, result.height))
        before_im, after_im = images.preview_pair(original, after, entry.dpi, zoom_dpi)
        dpi_after = entry.dpi * result.width / entry.width if entry.dpi else None
        page = min(entry.pages) if entry.pages else None
        return Preview(before_im, after_im, page, entry.dpi, dpi_after)

    # -- final compression ----------------------------------------------------

    def compress(self, settings, out_path, progress=lambda f, phase: None, cancel=lambda: False):
        """Write the compressed PDF to out_path. Never overwrites a file in place."""
        out_path = Path(out_path)
        exclude = set()
        attempt_settings = settings
        fallback = False
        for attempt in range(3):
            tmp = self._compress_once(attempt_settings, out_path.parent, exclude, progress, cancel)
            verified = None
            if settings.verify and verify.available():
                progress(0.9, "verify")
                changed_pages = self._changed_pages(attempt_settings, exclude)
                bad = verify.failing_pages(self.path, tmp, changed_pages, cancel)
                if cancel():
                    os.unlink(tmp)
                    raise Cancelled()
                verified = not bad
                if bad:
                    os.unlink(tmp)
                    if attempt == 0:
                        # Leave the images on the broken pages untouched.
                        exclude |= {e.objgen for e in self.images if e.pages & set(bad) or not e.pages}
                    else:
                        attempt_settings = replace(settings, max_dpi=None, jpeg_quality=None)
                        fallback = True
                    continue
            break
        else:
            raise DocumentError("verify")

        size = os.path.getsize(tmp)
        if size >= self.size:
            os.unlink(tmp)
            return CompressResult("not_smaller", self.size, size, verified=verified, fallback=fallback)
        fix_permissions(tmp)
        os.replace(tmp, out_path)
        return CompressResult("ok", self.size, size, out_path, len(exclude), verified, fallback)

    def _changed_pages(self, settings, exclude):
        pages = set()
        unknown = False
        for entry in self.images:
            plan = images.make_plan(entry, settings, best=True)
            if entry.objgen in exclude or not plan:
                continue
            r = self._cached(entry, settings, plan)
            if r is not None and not r.accepted:
                continue
            if entry.pages:
                pages |= entry.pages
            else:
                unknown = True
        if unknown or self.unscanned_pages:
            pages |= set(range(min(self.page_count, verify.MAX_PAGES)))
        return verify.pick_pages(self.page_count, pages)

    def _compress_once(self, settings, out_dir, exclude, progress, cancel):
        progress(0.0, "prepare")
        pdf = open_pdf(self.path)
        try:
            prepare(pdf, settings, self.pdfa)
            removed = dedupe_images(pdf)
            jobs = []
            for entry in self.images:
                if entry.objgen in removed or entry.objgen in exclude:
                    continue
                plan = images.make_plan(entry, settings, best=True)
                if plan:
                    jobs.append((entry, plan))
            total = max(1, len(jobs))
            results = self._run(pdf, jobs, settings, False, cancel,
                                done=lambda n: progress(0.05 + 0.65 * n / total, "images"))
            by_id = {e.objgen: e for e, _ in jobs}
            for objgen, result in results.items():
                if result.accepted:
                    images.apply_result(pdf.get_object(objgen), by_id[objgen], result)
            progress(0.7, "save")
            recompress_streams(pdf, self._handled, cancel)
            if cancel():
                raise Cancelled()
            fd, tmp = tempfile.mkstemp(prefix=".pdf-tools-", suffix=".pdf", dir=out_dir)
            os.close(fd)
            try:
                save(pdf, tmp, settings, encrypted=self.encrypted,
                     progress=lambda p: progress(0.75 + 0.15 * p / 100.0, "save"))
            except BaseException:
                os.unlink(tmp)
                raise
            return tmp
        finally:
            pdf.close()


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def fix_permissions(path):
    """mkstemp creates 0600 files; give results the usual permissions."""
    os.chmod(path, 0o666 & ~_umask())


def unique_path(path):
    """path, or path with -2, -3, ... appended to the stem if it exists."""
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1
