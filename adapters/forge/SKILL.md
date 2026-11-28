---
name: forge
description: Make reference images with Gemini (agy) and build 3D assets in headless Blender with a JSON CLI, a six-view render sheet, and a geometry gate. Use when an agent must generate concept references, write and run a bpy script to model an asset, look at a reproducible colour and clay review sheet, compare it to the references, or gate the exported glb.
---

# forge

Run `forge` from `adapters/forge/`. JSON on stdout, diagnostics on stderr, `forge <cmd> --help` is the reference. Needs `agy` (for `ref`) and `blender` (for `blend`, `compare`) on `PATH`.

| Question | Command |
|---|---|
| Give me reference images of the asset | `forge ref --prompt-file P --out DIR [--n 3] [--model M]` |
| Does my bpy script build the asset, and what does it look like | `forge blend --script S.py --out DIR [--timeout 300]` |
| Does the mesh pass the geometry budget | `forge gate DIR/model.glb --profile budget.toml` |
| How far is the model from the reference | `forge compare DIR/sheet.png REF_DIR` |
| Let the owner judge the model on the phone | `hearth-ask post ... --option-media ID=forge:DIR[,refs=REF_DIR]` |

Exit codes: `0` ok, `1` negative result (bad or missing image, script failed or timed out, gate failed), `2` usage, `3` backend unreachable (agy or blender missing, mesh_gate dependency missing).

## Files

- `ref` writes `ref_01.png` and up, plus `asset.json` (tool, model, prompt, `ai_generated: true`, `player_facing: false`, sha256 per file). References are never shipped.
- `blend` writes `model.glb` (your script), then `sheet.png`, `views/*.png`, `render_info.json`, `blend.log`, `render.log`. The sheet has seven columns (front, back, left, right, top, three-quarter, close-up). The top row is colour and the bottom row is clay. The camera framing comes from the bounding box, so renders of one model are comparable across revisions.
- `compare` writes `compare.png` beside the sheet: references stacked on the left, the sheet on the right.

## Asking the owner

`hearth-ask` (Hearth's ask CLI) reads a `forge blend` output directory as it is. `forge:DIR` attaches the model (`model.glb`, with `views/color_three_quarter.png` as the still shown where 3D cannot load), a turntable of the front, right, back and left colour views, and, with `refs=REF_DIR`, the first reference beside the render. The owner gets a 3D viewer with orbit, zoom, turntable and studio or clay light on the phone, the web and Kiln.

One option per look, so the owner taps the look. Three looks are three options:

```sh
hearth-ask post --project P --title "Which look?" \
  --option a:"Look A" --option-media a=forge:out/a,refs=refs \
  --option b:"Look B" --option-media b=forge:out/b,refs=refs \
  --option c:"Look C" --option-media c=forge:out/c,refs=refs \
  --input pins,changes
```

A plain pick stays one tap. `--input` asks for more only when the call is worth it: `pins` (pins with comments on the model or a still), `rank` (order the looks), `notes` (a note per look), `changes` (pick, with changes). The answer is typed into the asking session as one line, and `hearth-ask wait ID` prints it as JSON with `feedback` (pins on the model carry `pos` in model space). Other media specs: `model:GLB,poster=IMG`, `seq:DIR`, `compare:Reference=A.png,Render=B.png`. See `hearth-ask post --help`.

Never post a test ask: it notifies the owner's phone.

## Writing the bpy script

The script runs as `blender -b --factory-startup --python S.py -- --out DIR` with no network and a timeout. It must export `DIR/model.glb`. Read the directory from env `FORGE_OUT` or from the argument after `--`. Start with `bpy.ops.wm.read_factory_settings(use_empty=True)`. Export with `bpy.ops.export_scene.gltf(filepath=..., export_format="GLB")`. The JSON field `network_isolation` says whether `unshare -rn` was available. When it is not, the script ran with network access.

## Loop

1. `forge ref` with a prompt that states the object, a plain background and the camera angle. Look at the images and drop bad ones.
2. Write the bpy script from the references.
3. `forge blend`. On exit 1 read `reason` and `stderr_tail`, fix the script, rerun.
4. Look at `sheet.png` and `compare.png` yourself, or hand them to a fresh reviewer that never saw the build. Ask binary defect questions (holes, floating parts, wrong proportions, mismatch with the reference). Revise the script and go back to step 3.
5. `forge gate` when the sheet looks right. Fix the script on a failed check. The gate runs last because it is cheap but cannot see proportions.

There is no similarity score. CLIP-style scores perform at chance on 3D quality, so judging stays with agents.

## Gate setup

`forge gate` calls the `mesh_gate.py` named by env `FORGE_MESH_GATE` and passes its JSON through (`mesh_gate` needs `trimesh`). With the variable unset it prints `{"status": "skipped"}` and exits 0, which is not a pass. Treat `skipped` as an unrun gate.

Other env: `FORGE_NETNS=off` disables the network namespace.
