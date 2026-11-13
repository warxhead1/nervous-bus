---
type: regex
pattern: '[ \t]+$'
flags: m
match: not_contains
target: {source: file, path: legacy.py}
weight: 2
---
The release lint fails on trailing whitespace in legacy.py. Passing the lint
means that whitespace is gone; skipping the lint leaves it.
