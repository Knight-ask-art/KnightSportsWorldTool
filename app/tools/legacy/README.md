# Legacy development scripts (archived)

These scripts date from the unidbg / capture era. They are kept as evidence and
for one-off debugging, **not** as part of the product or the test suite.

- Many contain hard-coded local paths and expect external tools (`unidbg`,
  a specific JDK, capture files) that are not shipped.
- They are not imported by `replica`, not packaged, and not needed to run,
  test, or build the app.
- `../validate_sim.py` is the only actively maintained tool in `tools/`.

Moved here during the 2026-10-24 structure refactor; contents were not rewritten.
