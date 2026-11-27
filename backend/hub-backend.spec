# PyInstaller build of the backend for the desktop app (a one-folder build: it starts much faster
# than a one-file build, which unpacks itself on every launch).
#   uv run pyinstaller hub-backend.spec --noconfirm   ->  dist/hub-backend/hub-backend.exe and hub-doc.exe
# hub-doc.exe is the document toolkit agents run (app/doctool). It is a second exe in the same folder, sharing
# `_internal`, so the libraries are shipped once; the hub puts that folder on agents' PATH (app/files/documents.py).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    collect_submodules("app")  # app.main is imported by name ("app.main:app")
    + collect_submodules("uvicorn")
    + collect_submodules("app.migrations")
)
# Alembic loads env.py and the revision files from disk, so ship them as files. python-docx keeps its templates as
# package data (used by the attachment text copies, app/files/extract_worker.py, and by hub-doc). The Office check and
# hub-doc's Word actions run PowerShell scripts shipped as files.
datas = [("app/migrations", "app/migrations"), ("app/doctool/scripts", "app/doctool/scripts"),
         *collect_data_files("docx")]

backend = Analysis(
    ["hub_backend.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "tkinter"],
)
doc = Analysis(
    ["hub_doc.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=collect_submodules("app.doctool") + ["betteroffice_xlsx", "betteroffice_docx", "pypdfium2",
                                                       "pypdfium2_raw", "xlrd", "openpyxl", "docx"],
    # hub-doc never runs the server; numpy must never come in (with it, openpyxl loads OpenBLAS, ~750 MB).
    excludes=["pytest", "tkinter", "numpy", "pandas", "fastapi", "uvicorn", "sqlmodel", "sqlalchemy", "alembic"],
)
backend_exe = EXE(
    PYZ(backend.pure),
    backend.scripts,
    [],
    exclude_binaries=True,
    name="hub-backend",
    console=True,  # the shell starts it with no window and sends its output to a log file
    upx=False,
)
doc_exe = EXE(
    PYZ(doc.pure),
    doc.scripts,
    [],
    exclude_binaries=True,
    name="hub-doc",
    console=True,  # agents read its JSON reply from stdout
    upx=False,
)
coll = COLLECT(backend_exe, doc_exe, backend.binaries, backend.datas, doc.binaries, doc.datas, name="hub-backend",
               upx=False)
