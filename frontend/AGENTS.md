# frontend/AGENTS.md

The React and TypeScript UI (`src/`) and the Tauri desktop shell (`src-tauri/`). The root `AGENTS.md` rules apply here too.

## Commands

From `frontend/` (Node.js 22; Rust in `%USERPROFILE%\.cargo\bin`, only for the desktop shell):

```powershell
npm ci                 # install locked dependencies
npm run dev            # Vite on port 5173; run the backend separately (the Vite proxy adds the token)
npm run typecheck      # tsc -b
npm run build          # typecheck, then the production build
npm run tauri dev      # desktop app: backend from source via uv run, Vite hot reload
npm run tauri build    # PyInstaller bundle, UI, NSIS installer in src-tauri/target/release/bundle/nsis/
```

There are no UI unit tests: check UI changes with `npm run typecheck` and `npm run build`, then look at them in the app.

## Rules for UI changes

- **API access:** call the backend through `apiFetch` / `openEventStream` (`src/connection.ts`); in the desktop app they add the token. Load files as Blobs through `apiFetch` (`useApiBlob`, `useAttachmentUrl`), never with `src=` pointing at the API.
- **Styling:** use the theme tokens in `src/styles.css`; style agent-tinted parts with `var(--agent)` (set by `agentClass`), not `--accent`. Check light and dark themes and a narrow window.
- **Links and security:** the shell allows navigation only inside the app; outside links go through `POST /api/links/open`. Don't loosen the Tauri CSP or navigation rules without a reason recorded in `docs/architecture/desktop.md` or `frontend.md`.
- **Preview engines** (pdf.js, DOCX, XLSX) stay lazy-loaded chunks and are unmounted on close.
- **Style:** two-space indentation, double-quoted strings, semicolons, strict TypeScript; `PascalCase` component files, `camelCase` helpers and hooks. Follow the surrounding code.

## Where to read before changing something

Read the matching file in `docs/architecture/` before changing that part:

| If you're changing… | Read |
|---|---|
| Theme, colors, markdown, the preview pane | `frontend.md` |
| The desktop shell, its CSP, links, backend supervision | `desktop.md` |
| How the UI connects and authenticates | `api-and-auth.md` |
| Attachment chips, uploads and file rows | `attachments.md` |
| Output cards and the files-from-agents list | `documents.md` |
| The model picker | `models.md` |
| The `/` menu and command rows | `commands.md`, `skills.md` |
| Diffs and command rows in the transcript | `adapters.md` |
| Summaries and the session's progress panel | `sessions.md` |
