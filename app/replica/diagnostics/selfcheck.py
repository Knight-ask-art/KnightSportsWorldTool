"""One command that runs every module self-test.

    cd app && python -m replica.diagnostics.selfcheck
    cd app && python -m replica.selfcheck          # legacy alias

Exit code 0 only when every module passes.  Each module's `self_test()` is
offline: no network, no account, no submission.
"""

from __future__ import annotations

import importlib
import sys
import traceback

MODULES = [
    'domain.coords', 'transport.netsec', 'transport.gt4', 'domain.health', 'domain.safety', 'application.guard', 'transport.amap',
    'transport.obs', 'application.policy', 'application.points', 'domain.routes', 'domain.stadium', 'application.readonly', 'application.riskcheck',
    'diagnostics.collector', 'application.variation', 'application.identity', 'application.limits', 'transport.realclient', 'adapters.http_server',
    'application.qualification', 'application.tokenkeeper',
    'application.device_profile', 'application.presets',
    'application.stats', 'application.export_fit',
    'application.diagnostics_bundle', 'application.inspection',
]


def _load(name: str):
    """Import as a package member, falling back to the cwd-relative form."""
    root = __package__.rsplit('.', 1)[0] if '.' in __package__ else __package__
    for modname in ('%s.%s' % (root, name), 'replica.%s' % name):
        try:
            return importlib.import_module(modname)
        except ImportError:
            continue
    raise


def run() -> tuple[bool, list[tuple[str, int, int, str]]]:
    rows = []
    ok_all = True
    for name in MODULES:
        try:
            mod = _load(name)
        except Exception as e:  # noqa: BLE001
            rows.append((name, 0, 0, 'import failed: %s' % e))
            ok_all = False
            continue
        fn = getattr(mod, 'self_test', None)
        if not callable(fn):
            rows.append((name, 0, 0, 'no self_test'))
            ok_all = False
            continue
        try:
            results = fn()
        except Exception as e:  # noqa: BLE001
            rows.append((name, 0, 0, 'raised: %s' % e))
            ok_all = False
            if '-v' in sys.argv:
                traceback.print_exc()
            continue
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        failed = [n for n, ok, _ in results if not ok]
        detail = '' if not failed else 'FAIL: ' + ', '.join(failed[:3])
        rows.append((name, passed, total, detail))
        if passed != total:
            ok_all = False
    return ok_all, rows


def main() -> int:
    ok, rows = run()
    width = max(len(r[0]) for r in rows) + 2
    print('\n=== 模块自测 ===')
    for name, passed, total, detail in rows:
        mark = 'PASS' if passed == total and total else 'FAIL'
        print('  %-*s %-4s %2d/%-2d  %s' % (width, name, mark, passed, total,
                                            detail))
    passed_mods = sum(1 for _, p, t, _ in rows if p == t and t)
    print('-' * (width + 20))
    print('  %d/%d 模块通过' % (passed_mods, len(rows)))
    print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
