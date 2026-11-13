-- Multi-Agent Hub 0.1.0 sample database (scripts/snapshot_release_db.py). Do not edit.
BEGIN TRANSACTION;
CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
INSERT INTO "alembic_version" VALUES('b7e41c2d9a10');
CREATE TABLE events (
	id INTEGER NOT NULL, 
	session_id VARCHAR NOT NULL, 
	agent VARCHAR(32) NOT NULL, 
	type VARCHAR NOT NULL, 
	payload JSON, 
	created_at DATETIME NOT NULL, raw JSON, stint_id INTEGER REFERENCES stints (id) ON DELETE CASCADE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
INSERT INTO "events" VALUES(1,'project-session','claude','user_message','{"text": "Fix the parser"}','2026-01-01 12:00:00.000000','null',1);
INSERT INTO "events" VALUES(2,'project-session','claude','message','{"text": "Looking at it."}','2026-01-01 12:00:01.000000','null',1);
INSERT INTO "events" VALUES(3,'project-session','claude','tool_call','{"name": "Edit", "input": {"file_path": "parser.py"}}','2026-01-01 12:00:02.000000','null',1);
INSERT INTO "events" VALUES(4,'project-session','claude','limit_reached','{"message": "usage limit"}','2026-01-01 12:00:03.000000','null',1);
INSERT INTO "events" VALUES(5,'project-session','codex','handoff','{"from": "claude", "to": "codex", "brief": true}','2026-01-01 12:00:04.000000','null',2);
INSERT INTO "events" VALUES(6,'project-session','codex','completed','{}','2026-01-01 12:00:05.000000','null',2);
INSERT INTO "events" VALUES(7,'chat-session','ollama','user_message','{"text": "What is a worktree?"}','2026-01-01 12:00:06.000000','null',3);
INSERT INTO "events" VALUES(8,'chat-session','ollama','message','{"text": "A second checkout of the same repository."}','2026-01-01 12:00:07.000000','null',3);
CREATE TABLE sessions (
	title VARCHAR NOT NULL, 
	project_path VARCHAR NOT NULL, 
	id VARCHAR NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, chain JSON, limit_policy VARCHAR(32), chat BOOLEAN DEFAULT 0 NOT NULL, 
	PRIMARY KEY (id)
);
INSERT INTO "sessions" VALUES('Fix the parser','C:\work\repo','project-session','completed','2026-01-01 12:00:00.000000','2026-01-01 12:00:00.000000','[{"agent": "claude"}, {"agent": "codex"}]',NULL,0);
INSERT INTO "sessions" VALUES('Question','C:\hub\chats\chat-session','chat-session','ready','2026-01-01 12:00:00.000000','2026-01-01 12:00:00.000000','null',NULL,1);
CREATE TABLE settings (
	"key" VARCHAR NOT NULL, 
	value JSON, 
	PRIMARY KEY ("key")
);
INSERT INTO "settings" VALUES('chaining_enabled','true');
INSERT INTO "settings" VALUES('default_chain','[{"agent": "claude", "model": null, "effort": null}, {"agent": "codex", "model": null, "effort": null}, {"agent": "ollama", "model": null, "effort": null}]');
INSERT INTO "settings" VALUES('limit_policy','"auto"');
INSERT INTO "settings" VALUES('default_agent','"claude"');
INSERT INTO "settings" VALUES('default_model','null');
INSERT INTO "settings" VALUES('default_effort','"medium"');
INSERT INTO "settings" VALUES('progress_layout','"panel"');
INSERT INTO "settings" VALUES('local_model','null');
CREATE TABLE stints (
	id INTEGER NOT NULL, 
	session_id VARCHAR NOT NULL, 
	seq INTEGER NOT NULL, 
	agent VARCHAR(32) NOT NULL, 
	model VARCHAR, 
	native_session_id VARCHAR, 
	reason VARCHAR(32) NOT NULL, 
	parent_stint_id INTEGER, 
	brief VARCHAR, 
	started_at DATETIME NOT NULL, 
	ended_at DATETIME, effort VARCHAR, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE, 
	FOREIGN KEY(parent_stint_id) REFERENCES stints (id) ON DELETE SET NULL, 
	UNIQUE (session_id, seq)
);
INSERT INTO "stints" VALUES(1,'project-session',1,'claude','claude-opus-5-5','claude-native','new',NULL,NULL,'2026-01-01 12:00:00.000000','2026-01-01 12:05:00.000000','high');
INSERT INTO "stints" VALUES(2,'project-session',2,'codex',NULL,'codex-thread','limit',NULL,'Task: fix the parser

continue','2026-01-01 12:00:00.000000',NULL,NULL);
INSERT INTO "stints" VALUES(3,'chat-session',1,'ollama','qwen3.5:9b-q4_K_M','local-uuid','new',NULL,NULL,'2026-01-01 12:00:00.000000',NULL,NULL);
CREATE TABLE summaries (
	id INTEGER NOT NULL, 
	session_id VARCHAR NOT NULL, 
	agent VARCHAR(32) NOT NULL, 
	model VARCHAR, 
	text VARCHAR NOT NULL, 
	tokens INTEGER, 
	fallback_from VARCHAR(32), 
	last_event_id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
);
INSERT INTO "summaries" VALUES(1,'project-session','codex',NULL,'Parser fixed.',120,NULL,6,'2026-01-01 12:00:00.000000');
CREATE TABLE worktrees (
	id INTEGER NOT NULL, 
	session_id VARCHAR NOT NULL, 
	stint_id INTEGER, 
	repo_path VARCHAR NOT NULL, 
	path VARCHAR NOT NULL, 
	subdir VARCHAR NOT NULL, 
	branch VARCHAR NOT NULL, 
	base_commit VARCHAR NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	removed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE, 
	FOREIGN KEY(stint_id) REFERENCES stints (id) ON DELETE CASCADE
);
INSERT INTO "worktrees" VALUES(1,'project-session',NULL,'C:\work\repo','C:\hub\worktrees\repo-12345678','','hub/fix-the-parser-12345678','0000000000000000000000000000000000000000','active','2026-01-01 12:00:00.000000',NULL);
CREATE INDEX ix_events_session_id ON events (session_id);
CREATE INDEX ix_stints_session_id ON stints (session_id);
CREATE INDEX ix_events_stint_id ON events (stint_id);
CREATE INDEX ix_worktrees_session_id ON worktrees (session_id);
CREATE INDEX ix_summaries_session_id ON summaries (session_id);
COMMIT;
