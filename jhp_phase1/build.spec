# PyInstaller spec: 好想跑 (onedir, console off)
# Build:  pyinstaller build.spec --noconfirm
# Output: dist/好想跑/  (exe + _internal)
# Runtime deps kept EXTERNAL next to the exe (not bundled):
#   runtime/jre/      - bundled JRE (jlink runtime)
#   runtime/unidbg/   - oracle sidecar (out/, libs/, oracle.apk)
# or set env JHP_JAVA / JHP_UNIDBG to point elsewhere.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.dirname(os.path.abspath(SPEC))

datas = [
    (os.path.join(ROOT, 'frontend'), 'frontend'),
]
datas += collect_data_files('webview')
datas += collect_data_files('pythonnet')

hiddenimports = [
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr_loader',
    'pythonnet',
    'cv2',
    'numpy',
    'Crypto',
    'requests',
]
hiddenimports += collect_submodules('replica')

a = Analysis(
    ['app.py'],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=['tkinter', 'matplotlib', 'PIL'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='好想跑',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(ROOT, 'assets', 'logo.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='好想跑',
)
