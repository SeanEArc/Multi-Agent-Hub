"""Entry point for the bundled `hub-doc.exe` (PyInstaller), the document toolkit agents run (app/doctool).

From source the same program is the `hub-doc` launcher `uv sync` makes from `[project.scripts]`. The bundled exe
also runs its own capped child (`--hub-doc-child`), since the bundle has no separate Python."""

from app.doctool.cli import main

if __name__ == "__main__":
    main()
