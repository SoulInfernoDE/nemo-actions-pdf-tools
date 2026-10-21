#!/usr/bin/python3
"""Compress every PDF in a folder at several strengths and check the results.

Each output is rendered independently with pdftoppm and compared with the
original, so a bug in the built-in verification cannot hide a broken page.

Usage: run_compress.py SAMPLE_DIR OUT_DIR [STRENGTH ...]
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemo_pdf_tools import compress  # noqa: E402


def render(path, dpi=40):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", str(path), f"{tmp}/p"], check=True,
                       stderr=subprocess.DEVNULL)
        return [np.asarray(Image.open(f), dtype=np.float32) for f in sorted(Path(tmp).glob("p*.pgm"))]


def compare(a, b):
    ra, rb = render(a), render(b)
    if len(ra) != len(rb):
        return "page count differs"
    worst = 0.0
    for x, y in zip(ra, rb):
        if x.shape != y.shape:
            return "page size differs"
        worst = max(worst, float(np.abs(x - y).mean()))
    return f"max mean diff {worst:.2f}"


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    strengths = [int(s) for s in sys.argv[3:]] or [0, 50, 100]
    out.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(src.glob("*.pdf")):
        t0 = time.time()
        doc = compress.Document(pdf)
        doc.analyze()
        kinds = {}
        for e in doc.images:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        print(f"\n== {pdf.name}: {doc.size} bytes, {doc.page_count} pages, images {kinds} "
              f"(analysis {time.time() - t0:.2f}s)")
        for e in doc.images:
            print(f"   {e.objgen} {e.kind:8} {e.mode:4} {e.filter:12} {e.width}x{e.height} "
                  f"dpi={e.dpi and round(e.dpi)} raw={e.raw_size} q={e.jpeg_quality}")
        for strength in strengths:
            settings = compress.settings_for_strength(strength)
            t0 = time.time()
            est = doc.estimate(settings)
            t_est = time.time() - t0
            target = out / f"{pdf.stem}-s{strength}.pdf"
            t0 = time.time()
            res = doc.compress(settings, target)
            t_comp = time.time() - t0
            line = (f"  s={strength:3} dpi={settings.max_dpi} q={settings.jpeg_quality}: "
                    f"est {est.size} ({est.saving:+.0%}, exact={est.exact}, ssim={est.ssim and round(est.ssim, 3)}, "
                    f"changed={est.changed}, down={est.downsampled}, gray={est.to_gray}) {t_est:.2f}s | "
                    f"{res.status} {res.size} ({1 - res.size / res.original_size:+.0%}) verified={res.verified} "
                    f"skipped={res.skipped_images} fallback={res.fallback} {t_comp:.2f}s")
            if res.status == "ok":
                line += " | " + compare(pdf, target)
            print(line)
        doc.close()


if __name__ == "__main__":
    main()
