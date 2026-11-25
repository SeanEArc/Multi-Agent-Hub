# Settings and app data

## Settings

Settings are `HUB_*` env vars or `backend/.env` (`HUB_DATABASE_URL`, `HUB_HOST`, `HUB_PORT`, `HUB_RELOAD`, `HUB_LOG_LEVEL`, `HUB_CORS_ORIGINS`, `HUB_WORKTREES_DIR`, `HUB_CHATS_DIR`, `HUB_TOKEN_FILE`, `HUB_TOKEN`, `HUB_LOCK_FILE`, `HUB_OLLAMA_URL`, `HUB_OLLAMA_MODEL`, `HUB_OLLAMA_NUM_CTX`, `HUB_OLLAMA_THINK`, `HUB_OLLAMA_MAX_ROUNDS`, `HUB_OLLAMA_COMMAND_TIMEOUT`, `HUB_LOCAL_DIR`, `HUB_SKILLS_DIR`, `HUB_ATTACHMENTS_DIR`, `HUB_TOOLS_BIN`, `HUB_OFFICE_CHECK`; `HUB_OFFICE_REGISTRY`; `hub-doc` itself reads `HUB_DOC_OUTPUTS`, `HUB_DOC_READ_ROOTS` and `HUB_DOC_MEMORY_LIMIT`). All app data lives in `%LOCALAPPDATA%\MultiAgentHub` (`data\hub.db`, `data\token`, `worktrees`, `chats`, `local`, `skills`, `attachments`, `bin`, `logs`), shared by `uv run` and the desktop app; `app/server.py` holds `data\server.lock` while a backend runs, so a second one exits with code 3.
