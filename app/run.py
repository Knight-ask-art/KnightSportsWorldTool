"""Entry point: python run.py [--port 65520] [--no-browser]"""
import argparse
import webbrowser

from replica.adapters.http_server import serve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=65520)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--store', default=None)
    args = ap.parse_args()
    kwargs = {} if args.store is None else {'store_dir': args.store}
    serve(args.port, **kwargs)
    if not args.no_browser:
        webbrowser.open(f'http://127.0.0.1:{args.port}/')
    print('Ctrl+C to stop.')
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print('bye.')


if __name__ == '__main__':
    main()
