"""Desktop entry: pywebview window + embedded replica server.

Dev:  python app.py
Frozen exe: same code; server starts on a free port, window points at it.
"""
import argparse
import os
import socket

from replica.adapters.http_server import serve, FRONTEND_DIR


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
    try:
        webview.start()
    finally:
        srv.shutdown()


if __name__ == '__main__':
    main()
