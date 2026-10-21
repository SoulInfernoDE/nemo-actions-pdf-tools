"""Decoding, re-encoding and measuring of PDF image XObjects.

Only describe_image(), extract_image() and apply_result() touch pikepdf
objects; they run on the engine thread. Everything else works on plain bytes
and PIL images and is safe to run in a thread pool (Pillow releases the GIL
while decoding, resizing and encoding).
"""

import io
import struct
from dataclasses import dataclass, field

import numpy as np
import pikepdf
from pikepdf import Name
from PIL import Image, TiffImagePlugin

# Scans in PDFs are legitimately huge.
Image.MAX_IMAGE_PIXELS = None
# PDF only supports single-strip CCITT payloads, so keep libtiff from
# splitting the Group 4 data into several strips.
TiffImagePlugin.STRIP_SIZE = 1 << 30

KIND_PHOTO = "photo"
KIND_BILEVEL = "bilevel"
KIND_SMASK = "smask"
KIND_SKIP = "skip"

METHOD_JPEG = "jpeg"
METHOD_FLATE = "flate"
METHOD_BILEVEL = "bilevel"

# An image is only downsampled when it exceeds the target by this factor,
# like the "for images above" setting in Acrobat.
DOWNSAMPLE_THRESHOLD = 1.25

# Standard JPEG luminance quantisation table (quality 50).
_STD_LUMA = (
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
)

_CS_MODES = {
    "/DeviceGray": "L",
    "/CalGray": "L",
    "/DeviceRGB": "RGB",
    "/CalRGB": "RGB",
    "/DeviceCMYK": "CMYK",
}
_ICC_MODES = {1: "L", 3: "RGB", 4: "CMYK"}
_MODE_COMPONENTS = {"1": 1, "L": 1, "RGB": 3, "CMYK": 4}


class SkipImage(Exception):
    """The image cannot be processed safely and is left untouched."""


@dataclass
class ImageEntry:
    """What the analysis knows about one image XObject (no pixel data)."""

    objgen: tuple
    width: int
    height: int
    bpc: int
    mode: str  # PIL mode the samples decode to; "" = taken from the JPEG 2000 file
    filter: str  # single filter name without slash, "" for none
    raw_size: int
    kind: str
    lossless_only: bool = False
    jpeg_quality: int | None = None  # estimated quality of JPEG originals
    decode_inverted: bool = False  # CMYK with /Decode [1 0 1 0 1 0 1 0]
    dpi: float | None = None  # lowest effective resolution where it is placed
    pages: set = field(default_factory=set)

    @property
    def pixels(self):
        return self.width * self.height


@dataclass
class Plan:
    method: str
    scale: float  # 1.0 = keep resolution
    quality: int | None = None
    gray: bool = False  # try converting near-gray colour images to gray
    best: bool = False  # slowest, strongest Flate (final run, not live estimates)


@dataclass
class Result:
    accepted: bool
    data: bytes = b""
    filter: str = ""
    parms: dict | None = None
    width: int = 0
    height: int = 0
    mode: str = ""
    to_gray: bool = False
    ssim: float | None = None
    downsampled: bool = False
    decode_inverted: bool = False

    @property
    def size(self):
        return len(self.data)


# --- analysis (engine thread) ------------------------------------------------

def _filters(obj):
    f = obj.get("/Filter")
    if f is None:
        return []
    if isinstance(f, pikepdf.Array):
        return [str(x)[1:] for x in f]
    return [str(f)[1:]]


def _colorspace_mode(cs):
    if cs is None:
        return None
    if isinstance(cs, pikepdf.Name):
        return _CS_MODES.get(str(cs))
    if isinstance(cs, pikepdf.Array) and len(cs) >= 1:
        family = str(cs[0])
        if family == "/ICCBased" and len(cs) >= 2:
            try:
                return _ICC_MODES.get(int(cs[1].get("/N", 0)))
            except (TypeError, ValueError, AttributeError):
                return None
        if family in ("/CalRGB", "/CalGray"):
            return _CS_MODES.get(family)
    return None


def _is_default_decode(decode, components):
    if decode is None:
        return True
    try:
        values = [float(v) for v in decode]
    except (TypeError, ValueError):
        return False
    return values == [0.0, 1.0] * components


def estimate_jpeg_quality(data):
    """Estimate the IJG quality a JPEG was saved with, from its tables."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            tables = getattr(im, "quantization", None)
            if not tables or 0 not in tables:
                return None
            luma = list(tables[0])
    except Exception:
        return None
    if len(luma) != 64:
        return None
    scale = 100.0 * sum(luma) / sum(_STD_LUMA)
    quality = (200.0 - scale) / 2.0 if scale <= 100.0 else 5000.0 / scale
    return int(max(1, min(100, round(quality))))


def describe_image(obj, objgen, smask_ids):
    """Classify an image XObject. Never raises for odd images, it skips them."""
    width = int(obj.get("/Width", 0))
    height = int(obj.get("/Height", 0))
    filters = _filters(obj)
    image_mask = bool(obj.get("/ImageMask", False))
    try:
        raw_size = int(obj.get("/Length", 0))
    except (TypeError, ValueError):
        raw_size = 0
    entry = ImageEntry(objgen, width, height, 0, "", filters[0] if filters else "", raw_size, KIND_SKIP)
    if width <= 0 or height <= 0 or len(filters) > 1:
        return entry
    if entry.filter in ("JBIG2Decode", "CCITTFaxDecode"):
        return entry  # bilevel data that is already compressed well
    bpc = int(obj.get("/BitsPerComponent", 8 if entry.filter == "JPXDecode" else 0))
    entry.bpc = bpc
    lossy_codec = entry.filter in ("DCTDecode", "JPXDecode")

    if image_mask or bpc == 1:
        mode = "1" if image_mask else _colorspace_mode(obj.get("/ColorSpace"))
        if mode in ("1", "L") and not lossy_codec:
            # The bits are re-encoded unchanged, so any /Decode stays valid.
            entry.mode = "1"
            entry.kind = KIND_BILEVEL
        return entry

    if obj.get("/SMaskInData") or bpc != 8:
        return entry
    mode = _colorspace_mode(obj.get("/ColorSpace"))
    if mode is None and not (entry.filter == "JPXDecode" and obj.get("/ColorSpace") is None):
        return entry  # Indexed, Separation, DeviceN, Lab, ... stay as they are
    if entry.filter == "DCTDecode" and obj.get("/DecodeParms") is not None:
        return entry  # explicit /ColorTransform handling, leave it alone
    decode = obj.get("/Decode")
    if mode == "CMYK" and decode is not None and _is_default_decode(list(decode)[::-1], 4):
        # Photoshop writes inverted CMYK JPEGs and flips them back this way.
        entry.decode_inverted = True
    elif not _is_default_decode(decode, _MODE_COMPONENTS.get(mode or "RGB", 3)):
        return entry
    entry.mode = mode or ""
    if objgen in smask_ids:
        entry.kind = KIND_SMASK if mode == "L" else KIND_SKIP
        entry.lossless_only = True
        return entry
    entry.kind = KIND_PHOTO
    # Colour-key masking needs exact colours, which JPEG cannot guarantee.
    entry.lossless_only = isinstance(obj.get("/Mask"), pikepdf.Array)
    if entry.filter == "DCTDecode":
        entry.jpeg_quality = estimate_jpeg_quality(obj.read_raw_bytes())
    return entry


def mono_target_dpi(max_dpi):
    # Black-and-white scans need more pixels than grayscale for legible text.
    return max(300, min(600, 2 * max_dpi))


def make_plan(entry, settings, best=False):
    """Decide what to do with an image for the given settings, or None."""
    if entry.kind == KIND_SKIP:
        return None
    lossy = settings.jpeg_quality is not None
    if entry.kind in (KIND_SMASK,):
        if entry.filter in ("DCTDecode", "JPXDecode"):
            return None
        return Plan(METHOD_FLATE, 1.0, best=best)

    scale = 1.0
    if lossy and settings.max_dpi and entry.dpi:
        target = settings.max_dpi if entry.kind == KIND_PHOTO else mono_target_dpi(settings.max_dpi)
        if entry.dpi > target * DOWNSAMPLE_THRESHOLD:
            scale = target / entry.dpi
            # Never shrink to a ridiculous size, whatever the placement says.
            if min(entry.width, entry.height) * scale < 16:
                scale = 1.0

    if entry.kind == KIND_BILEVEL:
        return Plan(METHOD_BILEVEL, scale)

    if lossy and not entry.lossless_only:
        q = settings.jpeg_quality
        if entry.filter == "DCTDecode" and scale == 1.0:
            # Re-encoding a JPEG at a similar quality only adds artefacts.
            if entry.jpeg_quality is None or entry.jpeg_quality <= q + 5:
                return None
        return Plan(METHOD_JPEG, scale, q, settings.grayscale)
    if entry.filter in ("DCTDecode", "JPXDecode"):
        return None
    return Plan(METHOD_FLATE, 1.0, None, settings.grayscale, best)


@dataclass
class RawImage:
    data: bytes
    encoded: bool  # True: a JPEG/JPEG 2000 file, False: raw samples


def extract_image(obj, entry):
    if entry.filter in ("DCTDecode", "JPXDecode"):
        return RawImage(bytes(obj.read_raw_bytes()), True)
    try:
        return RawImage(bytes(obj.read_bytes()), False)
    except pikepdf.PdfError as exc:
        raise SkipImage(str(exc)) from exc


def apply_result(obj, entry, result):
    """Replace the stream data and dictionary entries of an image."""
    parms = pikepdf.Dictionary(result.parms) if result.parms else None
    obj.write(result.data, filter=Name("/" + result.filter), decode_parms=parms)
    obj.Width = result.width
    obj.Height = result.height
    obj.BitsPerComponent = 1 if result.mode == "1" else 8
    if result.mode == "CMYK":
        if result.decode_inverted:
            obj.Decode = pikepdf.Array([1, 0] * 4)
        elif "/Decode" in obj:
            del obj["/Decode"]
    if result.to_gray:
        obj.ColorSpace = Name.DeviceGray
    elif obj.get("/ColorSpace") is None and entry.kind == KIND_PHOTO:
        # JPEG 2000 originals may carry the colour space inside the file.
        obj.ColorSpace = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK}[result.mode]
    for key in ("/SMaskInData",):
        if key in obj:
            del obj[key]


# --- pixel work (thread pool) -----------------------------------------------

def decode(entry, raw):
    if raw.encoded:
        im = Image.open(io.BytesIO(raw.data))
        im.load()
        if im.mode not in ("L", "RGB", "CMYK"):
            raise SkipImage("unsupported JPEG mode " + im.mode)
        if entry.mode and im.mode != entry.mode:
            raise SkipImage("colour space does not match the JPEG data")
        if entry.filter == "JPXDecode" and im.mode == "CMYK":
            raise SkipImage("CMYK JPEG 2000")
        return im
    mode = entry.mode
    components = _MODE_COMPONENTS[mode]
    bits = 1 if mode == "1" else 8
    stride = (entry.width * components * bits + 7) // 8
    needed = stride * entry.height
    if len(raw.data) < needed:
        raise SkipImage("image data is truncated")
    return Image.frombytes(mode, (entry.width, entry.height), raw.data[:needed])


def is_near_gray(im, exact):
    """True if an RGB image carries no real colour information."""
    small = im if exact or min(im.size) < 64 else im.reduce(2)
    arr = np.asarray(small, dtype=np.int16)
    chroma = arr.max(axis=2) - arr.min(axis=2)
    if exact:
        return not chroma.any()
    # Scanner noise stays well below 24; a coloured stamp or signature
    # covers far more than 0.05 % of the page.
    return np.count_nonzero(chroma > 24) <= chroma.size * 0.0005 and float(chroma.mean()) < 4.0


def encode_jpeg(im, quality):
    buf = io.BytesIO()
    kwargs = {"quality": quality, "optimize": True}
    if im.mode == "RGB":
        kwargs["subsampling"] = 0 if quality >= 90 else 2
    im.save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def encode_flate(im, best=False):
    """Encode as PNG and lift the zlib data out: Flate with PNG predictors.

    best=True is about 5 % smaller but six times slower, so live estimates
    use the fast setting and only the final run the best one.
    """
    buf = io.BytesIO()
    im.save(buf, "PNG", **({"optimize": True} if best else {"compress_level": 6}))
    png = buf.getvalue()
    pos = 8
    idat = []
    bit_depth = color_type = interlace = None
    while pos < len(png):
        length, ctype = struct.unpack(">I4s", png[pos:pos + 8])
        chunk = png[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            bit_depth, color_type, interlace = chunk[8], chunk[9], chunk[12]
        elif ctype == b"IDAT":
            idat.append(chunk)
        pos += 12 + length
    colors = {0: 1, 2: 3}.get(color_type)
    if colors is None or interlace != 0:
        raise SkipImage("unexpected PNG layout")
    parms = {"/Predictor": 15, "/Colors": colors, "/BitsPerComponent": bit_depth, "/Columns": im.width}
    return b"".join(idat), parms


def encode_ccitt(im):
    """Lossless CCITT Group 4 for 1-bit images."""
    buf = io.BytesIO()
    # A fresh image avoids libtiff aborting on some PIL internals.
    Image.frombytes("1", im.size, im.tobytes()).save(buf, "TIFF", compression="group4")
    buf.seek(0)
    with Image.open(buf) as tif:
        offsets = tif.tag_v2[TiffImagePlugin.STRIPOFFSETS]
        counts = tif.tag_v2[TiffImagePlugin.STRIPBYTECOUNTS]
        photometric = tif.tag_v2.get(TiffImagePlugin.PHOTOMETRIC_INTERPRETATION, 0)
    if len(offsets) != 1:
        raise SkipImage("CCITT data spans several strips")
    data = buf.getvalue()[offsets[0]:offsets[0] + counts[0]]
    parms = {"/K": -1, "/Columns": im.width, "/Rows": im.height}
    # Group 4 codes "white" and "black" runs. For MinIsBlack data libtiff
    # codes the 1-bits as black runs; BlackIs1 makes the PDF decoder return
    # them as 1-bits again, so the bits round-trip unchanged (checked by
    # rendering) and any /Decode of the image stays valid.
    parms["/BlackIs1"] = photometric == 1
    return data, parms


def resize(im, size):
    if im.mode == "1":
        return im.convert("L").resize(size, Image.LANCZOS, reducing_gap=3.0).convert("1", dither=Image.NONE)
    return im.resize(size, Image.LANCZOS, reducing_gap=3.0)


def _luma(im):
    return im.convert("L") if im.mode != "L" else im


def _blur(x, k=7):
    """Box filter with 'valid' borders via an integral image."""
    c = np.cumsum(np.cumsum(np.pad(x, ((1, 0), (1, 0))), axis=0), axis=1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def ssim(a, b):
    """Structural similarity of two equally sized grayscale PIL images."""
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if x.shape[0] < 8 or x.shape[1] < 8:
        return 1.0
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx, my = _blur(x), _blur(y)
    sxx = _blur(x * x) - mx * mx
    syy = _blur(y * y) - my * my
    sxy = _blur(x * y) - mx * my
    m = ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))
    return float(m.mean())


def detail_box(im, side):
    """Pick the square of the given side with the most fine detail."""
    w, h = im.size
    side = min(side, w, h)
    if side <= 0:
        return (0, 0, w, h)
    thumb_scale = min(1.0, 256.0 / max(w, h))
    tw, th = max(1, int(w * thumb_scale)), max(1, int(h * thumb_scale))
    arr = np.asarray(_luma(im).resize((tw, th), Image.BILINEAR), dtype=np.float32)
    grad = np.abs(np.diff(arr, axis=0))[:, :-1] + np.abs(np.diff(arr, axis=1))[:-1, :]
    k = max(1, int(side * thumb_scale))
    if grad.shape[0] <= k or grad.shape[1] <= k:
        x0, y0 = (w - side) // 2, (h - side) // 2
        return (x0, y0, x0 + side, y0 + side)
    energy = _blur(grad.astype(np.float64), k)
    iy, ix = np.unravel_index(int(np.argmax(energy)), energy.shape)
    x0 = min(w - side, int(ix / thumb_scale))
    y0 = min(h - side, int(iy / thumb_scale))
    return (x0, y0, x0 + side, y0 + side)


def _measure(reference, encoded_im):
    """SSIM on the most detailed region at the output resolution."""
    ref = _luma(reference)
    out = _luma(encoded_im)
    box = detail_box(ref, 512)
    return ssim(ref.crop(box), out.crop(box))


def process(entry, raw, plan, measure=False, decoded=None):
    """Re-encode one image. Returns (Result, decoded original image)."""
    im = decoded if decoded is not None else decode(entry, raw)
    original = im
    out = im
    if plan.scale < 1.0:
        size = (max(1, round(im.width * plan.scale)), max(1, round(im.height * plan.scale)))
        out = resize(im, size)
    to_gray = False
    if plan.gray and out.mode == "RGB" and is_near_gray(out, exact=plan.method != METHOD_JPEG):
        out = out.convert("L")
        to_gray = True

    if plan.method == METHOD_JPEG:
        data, filt, parms = encode_jpeg(out, plan.quality), "DCTDecode", None
    elif plan.method == METHOD_BILEVEL:
        options = [(*encode_ccitt(out), "CCITTFaxDecode"), (*encode_flate(out), "FlateDecode")]
        data, parms, filt = min(options, key=lambda o: len(o[0]))
    else:
        data, parms = encode_flate(out, plan.best)
        filt = "FlateDecode"

    downsampled = out.size != im.size
    # Keep the original unless the new data is clearly worth it.
    limit = entry.raw_size
    if entry.filter in ("DCTDecode", "JPXDecode") and not downsampled:
        limit = entry.raw_size * 0.9
    accepted = len(data) < limit
    result = Result(accepted, data, filt, parms, out.width, out.height, out.mode, to_gray, None, downsampled)
    if out.mode == "CMYK":
        result.decode_inverted = entry.decode_inverted
        if filt == "DCTDecode":
            # Pillow writes CMYK JPEGs inverted (Adobe style), and PDF viewers
            # take the samples as they are. Unless the source was stored the
            # same way, /Decode has to flip them back.
            stored_inverted = entry.filter == "DCTDecode" and "adobe" in original.info
            if not stored_inverted:
                result.decode_inverted = not result.decode_inverted
    if accepted and measure and plan.method == METHOD_JPEG:
        reference = original if not downsampled else resize(original, out.size)
        if to_gray:
            reference = reference.convert("L")
        result.ssim = _measure(reference, Image.open(io.BytesIO(data)))
    if not accepted:
        result.data = b""
    return result, original


def decode_result(result):
    if result.filter == "DCTDecode":
        im = Image.open(io.BytesIO(result.data))
        im.load()
        return im
    return None


def preview_pair(original, after, dpi, zoom_dpi, side=360):
    """Crops of the same spot before and after, shown at zoom_dpi.

    The crop covers the most detailed area, scaled to the given display
    resolution so the user sees what a viewer would show at that zoom level.
    """
    dpi = dpi or 300.0
    factor = zoom_dpi / dpi
    src_side = max(8, int(side / factor))
    box = detail_box(original, src_side)
    before = original.crop(box)
    ratio_x = after.width / original.width
    ratio_y = after.height / original.height
    abox = (int(box[0] * ratio_x), int(box[1] * ratio_y),
            max(int(box[0] * ratio_x) + 1, int(box[2] * ratio_x)),
            max(int(box[1] * ratio_y) + 1, int(box[3] * ratio_y)))
    after_crop = after.crop(abox)
    size = (side, side) if box[2] - box[0] == box[3] - box[1] else (
        int((box[2] - box[0]) * factor), int((box[3] - box[1]) * factor))
    size = (max(1, size[0]), max(1, size[1]))

    def show(im):
        if im.mode == "1":
            im = im.convert("L")
        if im.mode == "CMYK":
            im = im.convert("RGB")
        return im.convert("RGB").resize(size, Image.BICUBIC)

    return show(before), show(after_crop)
