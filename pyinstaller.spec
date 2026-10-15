# PyInstaller spec — 把 FastAPI 后端打包成 Tauri sidecar 可执行文件。
#
#   pyinstaller pyinstaller.spec
#
# 产物 dist/auto-clip-video-server.exe；scripts/build_windows.ps1 会把它按
# Tauri 的命名要求复制到 src-tauri/binaries/auto-clip-video-server-<target-triple>.exe。

import importlib.util

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 与 src-tauri/tauri.conf.json 的 bundle.externalBin 末段保持一致
SIDECAR_NAME = "auto-clip-video-server"

datas = []
binaries = []
hiddenimports = ["app.main"]


def _collect(package: str) -> None:
    """收集包的 datas/binaries/hiddenimports；包没装就跳过而不是让打包失败。"""
    if importlib.util.find_spec(package) is None:
        return
    package_datas, package_binaries, package_imports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)


# 这些包的钩子/子模块大量靠动态导入，不显式收集会在冻结后 ImportError
for _pkg in (
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic_settings",
    "uvicorn",
    "structlog",
    "sqlalchemy",
    "aiosqlite",
    "anyio",
    "keyring",
    "httpx",
    "websockets",
    "multipart",
):
    _collect(_pkg)

# 本项目自己的模块：路由与服务里有 importlib 动态加载的 provider，全量收集最稳妥
hiddenimports += collect_submodules("app")

# SQLAlchemy 的 asyncio 扩展在首次 import 时才动态加载 greenlet，PyInstaller 追不到，
# 也没有现成钩子；漏掉会让 sidecar 启动即 ImportError（打包能成功但产物打不开）。
hiddenimports += ["greenlet", "greenlet._greenlet"]

# 可选能力：装了才收集，没装则 sidecar 里对应功能明确报告不可用
_collect("imageio_ffmpeg")
_collect("faster_whisper")
_collect("openai")
# ctranslate2 没有 PyInstaller 钩子，而它的推理全靠包目录里的原生 DLL（被 import 时按
# __file__ 相对路径加载），只靠追 faster_whisper 的静态 import 会漏掉整个 DLL 目录 ——
# 表现是打包成功、冻结后一加载模型就崩。
_collect("ctranslate2")

a = Analysis(
    ["app/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=SIDECAR_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压缩会触发杀软误报，且打包机上通常没有 upx
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 后台服务不该弹黑窗；排障靠 data_dir/logs/app.log
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
