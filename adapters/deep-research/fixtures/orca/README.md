Snapshots replayed by the fake Orca runner in `test_dr.py`.

`signed_out_tools_menu.txt` is a real accessibility snapshot of gemini.google.com with the Upload & tools menu open, captured signed out (no account data). The `synthetic_*` files hand-follow that snapshot format for the signed-in states (plan, running, done, failed) that could not be captured while Orca's browser was signed out. Replace them with real captures when a signed-in run is available.
