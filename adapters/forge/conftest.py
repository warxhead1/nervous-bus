def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs real Blender; CI runs -m 'not live'")


import os

# Never touch the real driver from tests: a wedged driver hangs the opener.
os.environ.setdefault("FORGE_GPU_PROBE_CMD", "true")
