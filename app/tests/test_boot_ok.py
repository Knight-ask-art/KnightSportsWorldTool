"""Tests for the JHP_BOOT_OK_PATH startup marker in http_server.serve()."""

from __future__ import annotations

import json
import os

from replica import build_info
from replica.adapters.http_server import serve


def test_boot_ok_written_when_env_set(tmp_path, monkeypatch):
    marker = tmp_path / 'nested' / 'updates' / 'boot-ok.json'
    monkeypatch.setenv('JHP_BOOT_OK_PATH', str(marker))

    srv = serve(port=0, store_dir=str(tmp_path / 'store'), quiet=True)
    try:
        assert marker.is_file()
        data = json.loads(marker.read_text(encoding='utf-8'))
        assert data['version'] == build_info.VERSION
        assert data['pid'] == os.getpid()
        assert isinstance(data['at'], int) and data['at'] > 0
        assert set(data.keys()) == {'version', 'pid', 'at'}
    finally:
        srv.shutdown()
        srv.server_close()


def test_boot_ok_not_written_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv('JHP_BOOT_OK_PATH', raising=False)
    store = tmp_path / 'store'

    srv = serve(port=0, store_dir=str(store), quiet=True)
    try:
        assert not list(tmp_path.rglob('boot-ok.json'))
    finally:
        srv.shutdown()
        srv.server_close()


def test_boot_ok_unwritable_path_does_not_break_serve(tmp_path, monkeypatch):
    blocker = tmp_path / 'not_a_dir'
    blocker.write_text('file', encoding='utf-8')
    unwritable = blocker / 'sub' / 'boot-ok.json'
    monkeypatch.setenv('JHP_BOOT_OK_PATH', str(unwritable))

    srv = serve(port=0, store_dir=str(tmp_path / 'store'), quiet=True)
    try:
        assert srv.server_address[1] > 0
        assert not unwritable.exists()
    finally:
        srv.shutdown()
        srv.server_close()
