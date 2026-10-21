# Multi-Agent Hub frontend

React + TypeScript UI (Vite). Runs in the browser for now; the desktop shell comes later.

## Run (development)

Start the backend first (`uv run python -m app.main` in `backend/`), then:

```powershell
cd frontend
npm install   # first time only
npm run dev
```

Open http://localhost:5173. Vite forwards `/api` to the backend at `http://127.0.0.1:8765`
(override with the `HUB_API_URL` shell environment variable before starting Vite):

```powershell
$env:HUB_API_URL="http://127.0.0.1:8765"
npm run dev
```

The current Vite config reads this from the process environment, not from a
frontend `.env` file. It controls the development proxy only. The browser uses
relative `/api` URLs, including for event streaming. No frontend secrets are needed.

## Check and build

```powershell
npm run typecheck
npm run build      # output in dist/
```

## Layout

- `src/api.ts`: backend types and calls
- `src/useSessionStream.ts`: live session updates (server-sent events, auto-reconnect)
- `src/components/`: sidebar, session view, transcript, composer, new-session dialog
- `src/styles.css`: theme tokens (dark default, light via system setting) and layout
