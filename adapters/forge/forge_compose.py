"""Bundled compare script. Run only inside Blender:
blender -b --factory-startup --python forge_compose.py -- --sheet S --refs a.png,b.png --out FILE

Left panel: the reference images stacked and scaled to fit. Right: the full render sheet.
"""
import sys

import bpy
import numpy as np


def load(path):
    img = bpy.data.images.load(path)
    return img, img.size[0], img.size[1]


def pixels(img, w, h):
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(h, w, 4)


def main():
    a = sys.argv[sys.argv.index("--") + 1:]
    d = dict(zip([k.lstrip("-") for k in a[::2]], a[1::2]))
    sheet, sw, sh = load(d["sheet"])
    refs = d["refs"].split(",")
    panel_w = sh
    slot_h = sh // len(refs)
    canvas = np.full((sh, panel_w + sw, 4), 0.1, dtype=np.float32)
    canvas[..., 3] = 1
    canvas[:, panel_w:] = pixels(sheet, sw, sh)
    for i, r in enumerate(refs):
        img, w, h = load(r)
        s = min(panel_w / w, slot_h / h)
        nw, nh = max(1, int(w * s)), max(1, int(h * s))
        img.scale(nw, nh)
        px = pixels(img, nw, nh)
        y0 = sh - (i + 1) * slot_h + (slot_h - nh) // 2  # bottom-up rows: slot 0 is on top
        x0 = (panel_w - nw) // 2
        canvas[y0:y0 + nh, x0:x0 + nw] = px
    out = bpy.data.images.new("cmp", canvas.shape[1], canvas.shape[0])
    out.pixels.foreach_set(canvas.ravel())
    out.filepath_raw = d["out"]
    out.file_format = "PNG"
    out.save()


main()
