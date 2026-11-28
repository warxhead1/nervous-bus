def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs real Blender; CI runs -m 'not live'")
