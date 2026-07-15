# PyInstaller spec for the standalone oikb-gui executable.
#
# Build (on Windows, for a Windows exe):
#   pip install .[all] pyinstaller
#   pyinstaller oikb-gui.spec
#
# Produces dist/oikb-gui.exe — a single windowed binary that is both the
# Tkinter GUI and its own CLI backend (src/oikb/gui.py `main()` falls
# through to the Click CLI when arguments are present).

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    # Connectors and CLI subcommands are imported lazily inside functions,
    # so PyInstaller's static analysis misses them.
    collect_submodules("oikb")
    # uvicorn[standard] loads its loops/protocols/lifespan modules by string.
    + collect_submodules("uvicorn")
)

a = Analysis(
    ["src/oikb/gui.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hooksconfig={},
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="oikb-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: no console window
    disable_windowed_traceback=False,
)
