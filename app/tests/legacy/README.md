# Legacy test runners

These are the pre-pytest suites. They still exercise the full mock bridge and
the HTTP server, but they are standalone `main()` runners: `pytest` does not
collect their checks.

Run them explicitly:

```powershell
cd 好想跑/app
python tests/legacy/legacy_bridge_suite.py    # 71-API bridge + mock servers
python tests/legacy/legacy_http_suite.py      # HTTP server + frontend smoke
python tests/legacy/legacy_realclient_smoke.py  # sidecar shapes (unidbg only)
```

`legacy_realclient_smoke.py` needs the retired unidbg sidecar; the other two are
offline. New tests belong in `tests/` and must be pytest-collectable.
