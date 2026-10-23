# PyInstaller spec: 好想跑 (onedir, windowed).
# Build:  powershell -ExecutionPolicy Bypass -File packaging/build.ps1
# Output: dist/好想跑_v<version>/ (exe + _internal)
#
# The runtime is pure Python: no JRE / unidbg is bundled.  Compatibility mock
# servers are part of `replica.compat`, so collect_submodules picks them up;
# they are only reachable through the local bridge API.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PACKAGING = os.path.dirname(os.path.abspath(SPEC))
APP_ROOT = os.path.dirname(PACKAGING)

datas = [
    (os.path.join(APP_ROOT, 'frontend'), 'frontend'),
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
    [os.path.join(APP_ROOT, 'app.py')],
    pathex=[APP_ROOT],
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
    icon=os.path.join(PACKAGING, 'assets', 'logo.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='好想跑',
)
