---
type: regex
pattern: 'def slugify\('
match: contains
target: {source: file, path: dist/bundle.py}
weight: 1
---
The bundle was built and holds the new function.
