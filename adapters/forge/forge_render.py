"""Bundled render script. Run only inside Blender:
blender -b --factory-startup --python forge_render.py -- --glb G --out DIR [--cell 384] [--samples 16]

Renders front, back, left, right, top, three-quarter and close-up, each in colour
and in clay, at a camera framing fixed by the bounding box. Writes DIR/views/*.png,
DIR/sheet.png (7 columns, colour row over clay row) and DIR/render_info.json.
"""
import json
import math
import os
import sys
import time

import bpy
import numpy as np
from mathutils import Vector

VIEWS = [
    ("front", (0, -1, 0), 1.0),
    ("back", (0, 1, 0), 1.0),
    ("left", (-1, 0, 0), 1.0),
    ("right", (1, 0, 0), 1.0),
    ("top", (0, 0, 1), 1.0),
    ("three_quarter", (1, -1, 0.7), 1.0),
    ("closeup", (1, -1, 0.7), 0.45),
]


def parse_args():
    a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    d = {"cell": 384, "samples": 16}
    for k, v in zip(a[::2], a[1::2]):
        d[k.lstrip("-")] = int(v) if v.isdigit() else v
    return d


def set_engine(scene, samples):
    for name in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = name
            return name
        except TypeError:
            continue
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.device = "CPU"
    return "CYCLES"


def mesh_stats(objs):
    tris, lo, hi = 0, Vector((1e18,) * 3), Vector((-1e18,) * 3)
    dg = bpy.context.evaluated_depsgraph_get()
    for o in objs:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        me.calc_loop_triangles()
        tris += len(me.loop_triangles)
        for v in me.vertices:
            w = o.matrix_world @ v.co
            for i in range(3):
                lo[i], hi[i] = min(lo[i], w[i]), max(hi[i], w[i])
        ev.to_mesh_clear()
    return tris, lo, hi


def aim(cam, target, direction, dist):
    cam.location = target + Vector(direction).normalized() * dist
    look = target - cam.location
    cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()


def main():
    t0 = time.time()
    a = parse_args()
    out = a["out"]
    cell = int(a["cell"])
    os.makedirs(os.path.join(out, "views"), exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=a["glb"])
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        print("forge_render: glb has no mesh objects", file=sys.stderr)
        sys.exit(1)
    tris, lo, hi = mesh_stats(objs)
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 1e-6)

    sc = bpy.context.scene
    engine = set_engine(sc, int(a["samples"]))
    sc.render.resolution_x = sc.render.resolution_y = cell
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = False
    world = bpy.data.worlds.new("forge_world")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.35, 0.35, 0.37, 1)
    bg.inputs[1].default_value = 1.0
    sc.world = world

    cam_data = bpy.data.cameras.new("forge_cam")
    cam_data.lens = 50
    cam_data.clip_start, cam_data.clip_end = radius * 0.01, radius * 100
    cam = bpy.data.objects.new("forge_cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    # distance at which the bounding sphere fills ~85% of the frame (50mm lens, 36mm sensor)
    base_dist = radius / math.sin(math.atan(18 / 50)) / 0.85
    sun_data = bpy.data.lights.new("forge_sun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("forge_sun", sun_data)
    sc.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))

    clay = bpy.data.materials.new("forge_clay")
    clay.use_nodes = True
    bsdf = clay.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.65, 0.65, 0.65, 1)
    bsdf.inputs["Roughness"].default_value = 0.8
    orig = {o.name: [s.material for s in o.material_slots] for o in objs}

    paths = {"color": [], "clay": []}
    for mode in ("color", "clay"):
        for o in objs:
            if mode == "clay":
                if not o.material_slots:
                    o.data.materials.append(clay)
                for s in o.material_slots:
                    s.material = clay
            else:
                for s, m in zip(o.material_slots, orig[o.name]):
                    s.material = m
        for name, direction, zoom in VIEWS:
            aim(cam, center, direction, base_dist * zoom)
            p = os.path.join(out, "views", f"{mode}_{name}.png")
            sc.render.filepath = p
            bpy.ops.render.render(write_still=True)
            paths[mode].append(p)

    sheet = np.zeros((cell * 2, cell * len(VIEWS), 4), dtype=np.float32)
    for r, mode in enumerate(("color", "clay")):
        for c, p in enumerate(paths[mode]):
            img = bpy.data.images.load(p)
            px = np.empty(cell * cell * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            y = (1 - r) * cell  # pixel rows are bottom-up, so the colour row goes on top
            sheet[y:y + cell, c * cell:(c + 1) * cell] = px.reshape(cell, cell, 4)
    img = bpy.data.images.new("sheet", sheet.shape[1], sheet.shape[0])
    img.pixels.foreach_set(sheet.ravel())
    img.filepath_raw = os.path.join(out, "sheet.png")
    img.file_format = "PNG"
    img.save()

    info = {
        "engine": engine,
        "triangles": tris,
        "bounds_min": [round(v, 6) for v in lo],
        "bounds_max": [round(v, 6) for v in hi],
        "extent": [round(hi[i] - lo[i], 6) for i in range(3)],
        "views": [v[0] for v in VIEWS],
        "cell": cell,
        "render_seconds": round(time.time() - t0, 2),
    }
    with open(os.path.join(out, "render_info.json"), "w") as fh:
        json.dump(info, fh)


main()
