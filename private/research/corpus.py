"""样本库存储层（私有研究用，不对外）。

一行一个 JSON 样本，按账号分文件：``private/data/corpus/<uid>.jsonl``。
可用环境变量 ``JHP_CORPUS_DIR`` 指向其他目录（测试必须这样做）。

本模块只负责存取，不做业务筛选；筛选与采集在 :mod:`collection`。
"""

from __future__ import annotations

import json
import os
import tempfile

#: 进入样本库的最低距离（米）。
MIN_DISTANCE_M = 3000


def corpus_dir() -> str:
    override = os.environ.get('JHP_CORPUS_DIR')
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, 'private', 'data', 'corpus')


def corpus_path(uid) -> str:
    return os.path.join(corpus_dir(), '%s.jsonl' % uid)


def load_samples(uid):
    """Yield stored samples, skipping blank or malformed lines."""
    try:
        with open(corpus_path(uid), encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def write_samples(uid, samples) -> str:
    """Atomically replace the sample file; returns its path."""
    path = corpus_path(uid)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.corpus-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + '\n')
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
