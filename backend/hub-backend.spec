# PyInstaller build of the backend for the desktop app (a one-folder build: it starts much faster
# than a one-file build, which unpacks itself on every launch).
#   uv run pyinstaller hub-backend.spec --noconfirm   ->  dist/hub-backend/hub-backend.exe
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("app")  # app.main is imported by name ("app.main:app")
    + collect_submodules("uvicorn")
    + collect_submodules("app.migrations")
)

a = Analysis(
    ["hub_backend.py"],
    pathex=["."],
    # Alembic loads env.py and the revision files from disk, so ship them as files.
    datas=[("app/migrations", "app/migrations")],
    hiddenimports=hiddenimports,
    excludes=["pytest", "tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hub-backend",
    console=True,  # the shell starts it with no window and sends its output to a log file
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="hub-backend", upx=False)
