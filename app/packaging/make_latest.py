"""Generate ``dist/latest.json`` for the in-app updater.

The manifest is deliberately small and boring: a client downloads it, compares
``version`` with its own build, then downloads the asset whose ``sha256`` it
verifies before replacing the onedir install.  Relative asset URLs are resolved
against the manifest URL, so the same file works on any host or local folder.

    python packaging/make_latest.py --dist dist
    python packaging/make_latest.py --dist dist --base-url https://example.com/hxp
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = APP_ROOT / 'dist'
DEFAULT_CHANNEL = 'stable'
#: Fallback when ``replica/build_info.py`` cannot be read; the file stays the
#: single source of truth, this only keeps standalone runs usable.
DEFAULT_VERSION = '0.2.0'
DEFAULT_MIN_VERSION = '0.1.4'
DEFAULT_ROLLBACK_VERSION = '0.1.9'
DEFAULT_ENTRY = '好想跑.exe'

#: Release notes are kept with the manifest generator so a rebuild cannot
#: publish a version with the previous version's notes.
RELEASE_NOTES = {
    '0.2.0': [
        '修复学校把一天拆成多段有效时间时，模型只认第一段、18:00 被误判不合格',
        '有效时段改为可添加多行：06:00–07:59、09:01–11:59 等会全部保存并逐段校验',
        '开始时间留空时只在学校有效时段内随机，不再落进 08:30 / 12:30 这类空档',
        '提交前会用服务器最新的 runModePolicy 刷新时段，升级后不必手动重填',
        '设置新增整套外观皮肤：纸感手账、赛场海报、极简白、复古档案',
        '皮肤同时更换字体、卡片、纹理、按钮和强调色；启动页、登录页、工作台一起生效',
        '主题只保存在本机 data/ui_state.json，不上传，不影响账号、数据或提交',
    ],
    '0.1.9': [
        '修复旧版共享设备指纹：不再让命中的老用户共用同一 DeviceId / x86_64 / isRoot=1',
        '旧指纹启动时一次性迁移为唯一、自洽的新指纹，先备份，并强制重新登录',
        '设备指纹不合法（共享常量、x86_64、root、机型系统像素不一致）时拒绝保存/提交',
        'Android 和 iOS 的 appVersion / UA 对齐到当前官方 7.4.0',
        '修正 Android 登录前 tokenSign 应为空、iOS 布尔头线上被写成 False',
        '只改设备身份但不重新登录的路径会被拦下，避免同一 token 中途换机',
        '关于 v0.1.7 / v0.1.8 的环境异常与损失，作者已在发布说明中致歉',
    ],
    '0.1.8': [
        '登录前必须阅读并勾选《免责声明》，旧会话会退出一次并要求重新确认',
        '登录页、侧栏和关于页固定显示「完全免费开源、禁止倒卖」提示',
        '免责声明全文可在登录页和关于页打开，仅保留右上角关闭按钮',
        '设置页把「真实性与兼容」改为「跑步数据效果」，常用项用直白说明',
        '旧服务器兼容收进折叠的高级项，默认行为不变',
        '海拔来源改为滑动切换；导航、跑步标签、弹窗和按钮加入纸墨风格动效',
        '按钮、筛选和勾选框统一视觉，支持系统「减少动态效果」设置',
        '修复浏览器自动填充/恢复复选框被误认为已同意免责声明的问题',
    ],
    '0.1.7': [
        '修复画线时点位反复加载失败：不再按每 10 m 目标距离重拉点位',
        '绘制路线时不再请求点位；路线定稿后按最终距离请求一次，自动请求最多 1 次/5 分钟',
        '目标距离默认 0，手绘后自动填写；点位与目标距离不一致时明确提示并可一键刷新',
        '10603 冷却按账号隔离，A 账号不再锁住 B 账号',
        '点位读取失败时保留上次加载的点位，不再清空地图和检查表',
        '提交复用地图上显示的那套点位，不再重新随机抽一组',
        '10603 时允许复用同账号/跑区 24 h 内的缓存点位，并明确标注来源距离',
        '后台刷新不再重置地图视野（画线时镜头不再被拉回围栏）',
        '点位限流/读取失败时显示冷却倒计时与真实原因',
        '会话 token 改为 DPAPI 封存（token_enc），旧明文启动时自动迁移',
        '单实例保护：第二个窗口会聚焦已有窗口并退出，避免两个进程互相覆盖 data/',
        '自动更新要求 zip 和安装包都带 sha256，且只接受 https（localhost 除外）',
        '手动选择的跑区按账号保存，下次登录/重启自动恢复；跑区下拉可一键切回「自动」',
        '围栏读取异常时停止提交，不再跳过区域校验',
        '提交页常驻结果卡：结果未知时锁定提交，直到完成只读回执核对',
        '检查表行可点击跳转；没有修复入口的提醒不再显示成按钮',
        '路线撤销改为 50 个快照栈，支持 Ctrl+Z / Ctrl+Shift+Z',
        '控制点悬停显示序号，可选中、Delete 删除、拖动微调',
        '点位冷却期间刷新按钮置灰并每秒更新「冷却 Ns」',
        '实测确认：点位接口每次随机返回 5 个点，距离只影响候选范围',
    ],
    '0.1.6': [
        '策略 geoFence 为 null 时回退 getGeoFenceForRun，地图定位到围栏中心',
        '点位接口 10603 限流时按账号/跑区/距离复用缓存，不再误报点位读取失败',
        '打卡点命中半径由后端下发（25 m），检查表不再用 40 m 误判全过',
        '补跑开始时间保留秒和毫秒，不再整分对齐',
        '提交前检查表新增：跑步冻结期、人脸抽检、距离达标',
        'iOS 全链统一 UA（含 OBS 直传），不再出现 Python-urllib 与平台 UA 混用',
        'iOS 设备指纹补兜底：appInstallTime 不发 0，DeviceId 不为空',
        '重做应用图标：16–256 多尺寸 .ico，任务栏/标题栏不再发糊',
        '账号页显示实际 UA / 屏幕倍率 / 安装天数；诊断包补客户端与最近回执',
    ],
    '0.1.5': [
        '提交前按官方风格预览数据：配速/步频/步幅/海拔 + 每公里表',
        '每公里配速强制有差异，不再出现相同配速',
        '修复官方 App 每公里表显示 200 多分钟（OBS 配速单位）',
        '学校没有围栏也能提交；没有围栏/多围栏时可用「设为校区」',
        '空打卡点默认阻止；确认学校无点位后需显式勾选「跳过打卡点」',
        '打卡点按当前目标距离拉取；跑区按账号校区名自动匹配，fenceIds 命中的围栏全部保留',
        '必经点改用官方 isFixed 标记，命中半径校正为 25 m',
        '顺序跑学校按点位编号排序；冻结期只提醒，人脸抽检会阻止提交',
        '改距离后自动重拉点位；多跑区学校可在跑步页切换跑区',
        '「设为校区」在无围栏/多围栏时显示；围栏读取失败不再把路线首点记成校区',
        '进入跑步页自动刷新校区点位',
        '每公里用时/海拔默认自动，可手动微调',
        '提交成功后询问：存路线库并清空 / 直接清空 / 暂时保留',
        '更新下载支持进度、断点续传、取消，失败保留旧版本',
        '设置页可自定义登录页横图封面',
        '新 Logo；字体 ZCOOL XiaoWei / Patrick Hand / JetBrains Mono；Bootstrap Icons；侧边栏与登录页改版',
    ],
    '0.1.4': [
        '每公里用时独立随机，参数面板可直接修改；合计严格等于预计时长',
        '10 秒窗口配速按真实记录重标（CV 0.19–0.52，随配速变化），采样间隔改为 2–4 秒',
        '每公里步频/步幅不再恒定：laps 的 avgCadence 曾每公里都除回 150.0',
        '海拔按距离定标：4 km 约 19–24 m、1.3 km 约 11–14 m，加入短坡与气压抖动',
        '默认步频 172 → 150，匹配账号历史记录（102–154）',
        '检查更新时对镜像测速，自动选最快的一条；下载失败自动换下一个',
        '修复慢配速下窗口噪声为负导致总距离回退、分段被静默摊平',
        '模型软阈值按真实记录重标（配速波动、停顿、累计爬升）',
        'v0.1.3 保留为回滚版本，latest.json 的 rollback 指向它的 zip',
    ],
    '0.1.3': [
        '提交坐标改为读取账号真实校区围栏，不再使用内置坐标',
        '切换校区会自动清空旧坐标；拿不到围栏时拒绝提交',
        '新增 NSIS 安装包：开始菜单快捷方式、卸载项、静默升级',
        '安装版与便携版分开更新：安装版走安装包，便携版原地替换',
        '发布资产改用 ASCII 文件名，避免 GitHub 改写下载地址',
        '极验失败时自动留下现场（w 明文、两张图、JS 版本），排障不用再猜',
        '极验被拒时可直接用官方验证码手动通过，不依赖本地解题器',
        '极验模块按图像/字段/RSA 拆开，字段变更不再需要重测图像',
    ],
    '0.1.2': [
        '新增应用内检查更新：下载校验后自动解压并重启安装',
        '新增海拔来源：自动 / 固定 / 区间 / 本机 GPX/JSON 模板',
        '围栏校验升级为整段检查，提示越界点与越界路段',
        '新增记录原始详情 JSON 调试命令与构建标识',
        '新增首次启动新手引导；推荐手绘路线，自动功能仍在开发中',
        '闭合路线默认关闭；导入轨迹不再自动首尾相连',
        '记录详情 OBS 回读支持旧版 uuid key（v0.1.2 修复）',
        '打包改用 Nuitka 编译，发布包不再包含核心 Python 源码',
        '新增 latest.json 更新元数据（版本 / SHA-256 / 回滚）',
    ],
}


def read_version(app_root: Path = APP_ROOT) -> str:
    path = app_root / 'replica' / 'build_info.py'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        print(f'warning: cannot read {path}; falling back to '
              f'{DEFAULT_VERSION}', file=sys.stderr)
        return DEFAULT_VERSION
    match = re.search(r"^VERSION\s*=\s*'([^']+)'", text, re.M)
    if not match:
        print(f'warning: cannot read VERSION from {path}; '
              f'falling back to {DEFAULT_VERSION}', file=sys.stderr)
        return DEFAULT_VERSION
    return match.group(1)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def asset_url(name: str, base_url: str = '') -> str:
    """Percent-encoded name, absolute when a base URL was supplied."""
    quoted = quote(name)
    base = (base_url or '').strip().rstrip('/')
    return f'{base}/{quoted}' if base else quoted


def _asset(path: Path, base_url: str, version: str, *,
           fmt: str = 'zip', layout: str = 'onedir',
           entry: str = DEFAULT_ENTRY, mirrors: tuple = ()) -> dict:
    """One downloadable file.  ``url`` stays first for old clients; ``urls``
    carries the extra mirrors and is only emitted when there is more than one.

    GitHub release assets are frequently unreachable from mainland China, so
    the manifest lists the same file behind several proxies.  Clients that
    predate this field read ``url`` and ignore the rest.
    """
    urls = []
    for base in (base_url,) + tuple(mirrors or ()):
        value = asset_url(path.name, base)
        if value and value not in urls:
            urls.append(value)
    asset = {
        'file': path.name,
        'url': urls[0],
        'size': path.stat().st_size,
        'sha256': sha256_file(path),
        'format': fmt,
        'layout': layout,
        'entry': entry,
        'version': version,
    }
    if len(urls) > 1:
        asset['urls'] = urls
    return asset


def build_manifest(version: str, zip_path: Path, *,
                   base_url: str = '',
                   mirrors: tuple = (),
                   channel: str = DEFAULT_CHANNEL,
                   min_version: str = DEFAULT_MIN_VERSION,
                   released_at: str | None = None,
                   notes: list[str] | None = None,
                   rollback_version: str | None = None,
                   rollback_zip: Path | None = None,
                   rollback_base_url: str = '',
                   rollback_mirrors: tuple = (),
                   setup_path: Path | None = None,
                   entry: str = DEFAULT_ENTRY) -> dict:
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise SystemExit(f'zip not found: {zip_path}')
    if notes is None:
        notes = list(RELEASE_NOTES.get(version, []))
    if released_at is None:
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
        released_at = now.replace(microsecond=0).isoformat()

    assets = {'windows-x64': _asset(zip_path, base_url, version, entry=entry,
                                    mirrors=mirrors)}
    # The installer is the primary asset for new installs and for updating an
    # installed copy.  `windows-x64` stays a zip so that v0.1.2 clients (whose
    # updater only understands zip) can still auto-update instead of failing
    # with "更新包不是有效 zip".
    if setup_path:
        setup_path = Path(setup_path)
        if setup_path.is_file():
            assets['windows-x64-setup'] = _asset(
                setup_path, base_url, version, mirrors=mirrors,
                fmt='installer', layout='installer', entry=entry)
        else:
            print(f'warning: setup not found: {setup_path}', file=sys.stderr)

    manifest = {
        'schema': 1,
        'app': 'haoxiangpao',
        'name': '好想跑',
        'channel': channel,
        'version': version,
        'min_version': min_version,
        'released_at': released_at,
        'notes': notes,
        'assets': assets,
        'generated_by': 'packaging/make_latest.py',
    }

    if rollback_version and rollback_zip:
        rollback_zip = Path(rollback_zip)
        if rollback_zip.is_file():
            manifest['rollback'] = _asset(
                rollback_zip, rollback_base_url or base_url,
                rollback_version, entry=entry,
                mirrors=rollback_mirrors or mirrors)
        else:
            print(f'warning: rollback zip not found: {rollback_zip}',
                  file=sys.stderr)
    return manifest


def find_zip(dist: Path, version: str) -> Path:
    """ASCII name first; the Chinese name is the pre-0.1.3 convention."""
    for name in (f'haoxiangpao_v{version}_win64.zip',
                 f'好想跑_v{version}_win64.zip'):
        candidate = dist / name
        if candidate.is_file():
            return candidate
    return dist / f'haoxiangpao_v{version}_win64.zip'


def find_setup(dist: Path, version: str) -> Path | None:
    candidate = dist / f'haoxiangpao_v{version}_win64_setup.exe'
    return candidate if candidate.is_file() else None


def find_rollback(dist: Path, version: str | None) -> Path | None:
    if not version:
        return None
    candidate = find_zip(dist, version)
    return candidate if candidate.is_file() else None


def write_manifest(manifest: dict, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    with out.open('w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='generate dist/latest.json')
    ap.add_argument('--dist', default=str(DEFAULT_DIST))
    ap.add_argument('--version', default=None,
                    help='defaults to replica/build_info.py VERSION')
    ap.add_argument('--zip', default=None, help='asset zip path')
    ap.add_argument('--setup', default=None,
                    help='NSIS installer path; auto-detected when omitted')
    ap.add_argument('--out', default=None, help='defaults to <dist>/latest.json')
    ap.add_argument('--base-url', default='',
                    help='absolute download base; omit for relative URLs')
    ap.add_argument('--mirror-base-url', action='append', default=[],
                    metavar='URL',
                    help='extra download base (repeatable); written to urls[]')
    ap.add_argument('--channel', default=DEFAULT_CHANNEL)
    ap.add_argument('--min-version', default=DEFAULT_MIN_VERSION,
                    help=f'oldest version that may upgrade directly '
                         f'(default: {DEFAULT_MIN_VERSION})')
    ap.add_argument('--released-at', default=None,
                    help='ISO-8601; defaults to now in Beijing time')
    ap.add_argument('--notes-file', default=None,
                    help='JSON array of release notes; defaults to built-in notes')
    ap.add_argument('--rollback-version', default=DEFAULT_ROLLBACK_VERSION,
                    help='include this older zip as rollback; empty disables '
                         f'(default: {DEFAULT_ROLLBACK_VERSION})')
    ap.add_argument('--rollback-base-url', default='',
                    help='download base for the rollback zip; defaults to --base-url')
    ap.add_argument('--rollback-mirror-base-url', action='append', default=[],
                    metavar='URL',
                    help='extra download base for the rollback zip (repeatable)')
    args = ap.parse_args(argv)

    dist = Path(args.dist).resolve()
    version = args.version or read_version()
    zip_path = Path(args.zip) if args.zip else find_zip(dist, version)
    if not zip_path.is_absolute():
        zip_path = (Path.cwd() / zip_path).resolve()

    if args.setup:
        setup_path = Path(args.setup)
        if not setup_path.is_absolute():
            setup_path = (Path.cwd() / setup_path).resolve()
    else:
        setup_path = find_setup(dist, version)

    notes = None
    if args.notes_file:
        notes = json.loads(Path(args.notes_file).read_text(encoding='utf-8'))
        if not isinstance(notes, list):
            raise SystemExit('--notes-file must contain a JSON array')

    rollback_version = (args.rollback_version or '').strip() or None
    rollback_zip = find_rollback(dist, rollback_version)
    if (rollback_version and rollback_zip and args.base_url
            and not (args.rollback_base_url or '').strip()):
        # The rollback zip lives in the *previous* GitHub release, not this
        # one.  Reusing the new base silently writes a 404 into the manifest.
        print('warning: --rollback-base-url is empty; the rollback URL will '
              'point at the current release and may 404', file=sys.stderr)
    manifest = build_manifest(
        version, zip_path,
        base_url=args.base_url,
        mirrors=tuple(args.mirror_base_url or ()),
        channel=args.channel,
        min_version=args.min_version,
        released_at=args.released_at,
        notes=notes,
        rollback_version=rollback_version,
        rollback_zip=rollback_zip,
        rollback_base_url=args.rollback_base_url,
        rollback_mirrors=tuple(args.rollback_mirror_base_url or ()),
        setup_path=setup_path,
    )
    out = Path(args.out) if args.out else dist / 'latest.json'
    write_manifest(manifest, out)

    asset = manifest['assets']['windows-x64']
    print(f'latest.json: {out}')
    print(f'version:     {manifest["version"]} ({manifest["channel"]})')
    print(f'asset:       {asset["file"]} · {asset["size"]} bytes')
    print(f'sha256:      {asset["sha256"]}')
    setup = manifest['assets'].get('windows-x64-setup')
    if setup:
        print(f'installer:   {setup["file"]} · {setup["size"]} bytes')
        print(f'             {setup["sha256"]}')
    if manifest.get('rollback'):
        print(f'rollback:    {manifest["rollback"]["version"]} · '
              f'{manifest["rollback"]["file"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
