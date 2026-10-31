"""Agent sources by name. Add an agent: drop one module here that defines a
Source subclass with a `name`; it is picked up automatically."""
import importlib
import inspect
import os
import pkgutil

from model import Source

SOURCES: dict = {}
for _m in sorted(pkgutil.iter_modules([os.path.dirname(__file__)]), key=lambda m: m.name):
    _mod = importlib.import_module(f"sources.{_m.name}")
    for _obj in vars(_mod).values():
        if inspect.isclass(_obj) and issubclass(_obj, Source) and _obj is not Source and _obj.name:
            SOURCES.setdefault(_obj.name, _obj())
