#!/usr/bin/python3
"""Build test PDFs that cover the image types the compressor handles.

Usage: make_samples.py OUTPUT_DIR [PHOTO.jpg]
"""

import glob
import io
import sys
import zlib
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name
from PIL import Image, ImageDraw, ImageFont

A4 = (595.0, 842.0)


def photo_source(arg=None):
    if arg:
        return Image.open(arg).convert("RGB")
    for pattern in ("/usr/share/backgrounds/linuxmint-vera/*.jpg", "/usr/share/backgrounds/**/*.jpg"):
        files = sorted(glob.glob(pattern, recursive=True))
        if files:
            return Image.open(files[0]).convert("RGB")
    # Fallback: synthetic texture.
    im = Image.effect_mandelbrot((2400, 1800), (-2, -1.2, 1, 1.2), 200).convert("RGB")
    return im


def text_image(size, mode="L", text_size=34):
    im = Image.new("L", size, 255)
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", text_size)
    except OSError:
        font = ImageFont.load_default()
    y = 60
    line = "Scanned letter: The quick brown fox jumps over the lazy dog 0123456789."
    while y < size[1] - 60:
        draw.text((80, y), line, fill=0, font=font)
        y += int(text_size * 1.6)
    return im.convert(mode) if mode != "L" else im


def new_pdf():
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    return pdf, font


def add_page(pdf, font, xobjects, draws, title):
    """draws: list of (name, x, y, w, h) in points."""
    ops = [f"BT /F1 18 Tf 50 800 Td ({title}) Tj ET".encode()]
    for name, x, y, w, h in draws:
        ops.append(f"q {w} 0 0 {h} {x} {y} cm /{name} Do Q".encode())
    content = pdf.make_stream(b"\n".join(ops))
    page = Dictionary(
        Type=Name.Page,
        MediaBox=[0, 0, *A4],
        Resources=Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(**xobjects)),
        Contents=content,
    )
    pdf.pages.append(pikepdf.Page(page))


def image_stream(pdf, im, flate=True, jpeg_quality=None, **extra):
    cs = {"L": Name.DeviceGray, "RGB": Name.DeviceRGB, "CMYK": Name.DeviceCMYK, "1": Name.DeviceGray}[im.mode]
    d = dict(Type=Name.XObject, Subtype=Name.Image, Width=im.width, Height=im.height,
             ColorSpace=cs, BitsPerComponent=1 if im.mode == "1" else 8)
    if jpeg_quality:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=jpeg_quality)
        data, d["Filter"] = buf.getvalue(), Name.DCTDecode
    else:
        data = im.tobytes()
        if flate:
            data, d["Filter"] = zlib.compress(data, 6), Name.FlateDecode
    d.update(extra)
    stream = pikepdf.Stream(pdf, data)
    for k, v in d.items():
        stream[Name("/" + k)] = v
    return pdf.make_indirect(stream)


def build(out_dir, photo_path=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    photo = photo_source(photo_path)
    photo = photo.resize((2400, int(2400 * photo.height / photo.width)))

    # 1. Colour photo as Flate at ~600 dpi plus a high-quality JPEG.
    pdf, font = new_pdf()
    xo = {"Im1": image_stream(pdf, photo)}
    add_page(pdf, font, xo, [("Im1", 50, 400, 288, 288 * photo.height / photo.width)], "Flate photo 600 dpi")
    xo = {"Im2": image_stream(pdf, photo, jpeg_quality=97)}
    add_page(pdf, font, xo, [("Im2", 50, 300, 500, 500 * photo.height / photo.width)], "JPEG q97")
    pdf.save(out / "photos.pdf")

    # 2. Black-and-white scan, 300 dpi, Flate; and a 600 dpi one.
    pdf, font = new_pdf()
    scan = text_image((2480, 3508)).convert("1", dither=Image.NONE)
    add_page(pdf, font, {"S": image_stream(pdf, scan)}, [("S", 0, 0, *A4)], "")
    scan600 = text_image((4960, 7016), text_size=68).convert("1", dither=Image.NONE)
    add_page(pdf, font, {"S": image_stream(pdf, scan600)}, [("S", 0, 0, *A4)], "")
    pdf.save(out / "bw-scan.pdf")

    # 3. Gray letter scanned as RGB, with a small red stamp on page 2.
    pdf, font = new_pdf()
    gray = text_image((2480, 3508)).convert("RGB")
    add_page(pdf, font, {"G": image_stream(pdf, gray)}, [("G", 0, 0, *A4)], "")
    stamped = gray.copy()
    ImageDraw.Draw(stamped).ellipse((1700, 2900, 2200, 3300), outline=(200, 20, 20), width=14)
    add_page(pdf, font, {"G": image_stream(pdf, stamped)}, [("G", 0, 0, *A4)], "")
    pdf.save(out / "gray-as-rgb.pdf")

    # 4. CMYK: Flate, JPEG, and a Photoshop-style inverted JPEG.
    pdf, font = new_pdf()
    cmyk = photo.convert("CMYK")
    add_page(pdf, font, {"C": image_stream(pdf, cmyk)}, [("C", 50, 400, 288, 288 * photo.height / photo.width)], "CMYK Flate")
    add_page(pdf, font, {"C": image_stream(pdf, cmyk, jpeg_quality=97)},
             [("C", 50, 400, 288, 288 * photo.height / photo.width)], "CMYK JPEG")
    pdf.save(out / "cmyk.pdf")

    # 5. Soft mask, stencil mask, indexed image and a form used twice.
    pdf, font = new_pdf()
    alpha = Image.new("L", photo.size, 0)
    ImageDraw.Draw(alpha).ellipse((100, 100, photo.width - 100, photo.height - 100), fill=255)
    smask = image_stream(pdf, alpha)
    masked = image_stream(pdf, photo, SMask=smask)
    stencil = image_stream(pdf, text_image((1200, 400)).convert("1", dither=Image.NONE))
    del stencil["/ColorSpace"]
    stencil.ImageMask = True
    pal = photo.resize((600, 400)).convert("P", palette=Image.ADAPTIVE, colors=64)
    palette = bytes(pal.getpalette()[: 64 * 3])
    indexed = pdf.make_indirect(pikepdf.Stream(pdf, zlib.compress(pal.tobytes())))
    for k, v in dict(Type=Name.XObject, Subtype=Name.Image, Width=600, Height=400, BitsPerComponent=8,
                     ColorSpace=[Name.Indexed, Name.DeviceRGB, 63, pikepdf.String(palette)],
                     Filter=Name.FlateDecode).items():
        indexed[Name("/" + k)] = v
    form = pdf.make_stream(b"q 200 0 0 150 0 0 cm /P Do Q")
    form.Type, form.Subtype, form.BBox = Name.XObject, Name.Form, [0, 0, 200, 150]
    form.Resources = Dictionary(XObject=Dictionary(P=masked))
    xo = {"M": masked, "St": stencil, "Ix": indexed, "Fm": form}
    add_page(pdf, font, xo, [("M", 50, 450, 300, 300 * photo.height / photo.width), ("Ix", 50, 50, 300, 200),
                             ("Fm", 380, 100, 1, 1)], "Masks")
    ops = b"q 0.8 0 0 rg 400 0 0 133 100 600 cm /St Do Q"
    page = pdf.pages[-1]
    page.contents_add(pdf.make_stream(ops))
    add_page(pdf, font, {"Fm": form}, [("Fm", 100, 100, 2, 2)], "Form reused")
    pdf.save(out / "masks.pdf")

    # 6. Text only, already compact: must not grow.
    pdf, font = new_pdf()
    for i in range(3):
        add_page(pdf, font, {}, [], f"Page {i + 1}")
    pdf.save(out / "text-only.pdf")

    # 7. Same image stored twice as separate objects.
    pdf, font = new_pdf()
    for i in range(3):
        add_page(pdf, font, {"D": image_stream(pdf, photo.resize((800, 600)), jpeg_quality=80)},
                 [("D", 50, 400, 400, 300)], f"Duplicate {i + 1}")
    pdf.save(out / "duplicates.pdf")
    return sorted(out.glob("*.pdf"))


if __name__ == "__main__":
    for path in build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None):
        print(path, path.stat().st_size)
