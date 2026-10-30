"""Desktop entry: pywebview window + embedded replica server.

Dev:  python app.py
Frozen exe: same code; server starts on a free port, window points at it.
"""
import argparse
import os
import socket

from replica.adapters.http_server import serve, FRONTEND_DIR


_MUTEX = None


def _focus_existing(title: str) -> None:
    """Bring the already-running window to the foreground."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.UINT]
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        user32.ShowWindow(hwnd, 9)            # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return
    user32.MessageBoxW(None, '好想跑已经在运行。', title, 0x40)


MUTEX_NAME = 'Local\\Haoxiangpao.SingleInstance.v1'


def _acquire_single_instance(title: str = '好想跑',
                             name: str = MUTEX_NAME) -> bool:
    """One GUI/server per Windows session.

    Two instances pick different random ports but share ``data/``; the session
    and campus-centre writes are read-modify-write, so the second window can
    overwrite the first one's login.  A named mutex makes the second start a
    no-op that focuses the existing window instead.
    """
    global _MUTEX
    if os.name != 'nt':
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True                  # cannot tell; never block startup
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        _focus_existing(title)
        return False
    _MUTEX = handle                  # hold it for the process lifetime
    return True


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--no-window', action='store_true',
                    help='server only (tests / headless)')
    ap.add_argument('--allow-multi', action='store_true',
                    help='skip the single-instance mutex (development only)')
    ap.add_argument('--check', action='store_true',
                    help='import frozen runtime pieces and exit 0 (build smoke test)')
    args = ap.parse_args()
    if args.check:
        # Import the login/GT4/OpenCV path and verify the packaged frontend is
        # present.  The exe is windowed, so the exit code is the signal.
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import requests  # noqa: F401
        import webview  # noqa: F401
        from replica.transport import gt4, ios_headers, netsec, realclient  # noqa: F401
        from replica.application import login_flow  # noqa: F401
        from replica.adapters import bridge_api, http_server  # noqa: F401
        if not all(ok for _, ok, _ in gt4.self_test()):
            raise SystemExit(3)
        index = os.path.join(FRONTEND_DIR, 'index.html')
        if not os.path.isfile(index):
            raise SystemExit(2)
        raise SystemExit(0)
    if not args.allow_multi and not _acquire_single_instance():
        raise SystemExit(0)
    port = args.port or _free_port()
    srv = serve(port, quiet=True)
    url = f'http://127.0.0.1:{port}/'
    print(f'serving {url}', flush=True)

    if args.no_window:
        import time
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
        return

    import webview
    webview.create_window('好想跑', url,
                          width=1280, height=820, min_size=(980, 640))
    # pywebview's WinForms backend loads this into Form.Icon.  Without it the
    # title bar falls back to ExtractIconW(exe), which Windows has usually
    # cached at a small size; passing the multi-frame .ico keeps it sharp.
    icon = os.path.join(FRONTEND_DIR, 'app.ico')
    try:
        webview.start(icon=icon if os.path.isfile(icon) else None)
    finally:
        srv.shutdown()


if __name__ == '__main__':
    main()
