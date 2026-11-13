import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  AGENT_NAMES,
  api,
  type AgentKind,
  type FoundSkill,
  type LibrarySkill,
  type ShareableSkill,
  type SkillPreview,
  type SkillSwitches,
  type SkillUpdateCheck,
} from "../api";
import { AgentLogo } from "./AgentLogo";

const AGENTS: AgentKind[] = ["claude", "codex", "ollama"];
const SWITCH_NAMES: Record<AgentKind, string> = { claude: "Claude", codex: "Codex", ollama: "Local" };

interface Props {
  onClose: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * The hub's skill library: skills any agent can use from the `/` menu. Import from a folder, a zip or a git
 * repository, or share one of your own Claude or Codex skills; look at each skill before turning it on, and
 * choose which agents get it.
 */
export function SkillsDialog({ onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [skills, setSkills] = useState<LibrarySkill[] | null>(null);
  const [shareable, setShareable] = useState<ShareableSkill[] | null>(null);
  const [source, setSource] = useState("");
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [update, setUpdate] = useState<{ name: string; check: SkillUpdateCheck } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialog.current?.showModal();
    api.skills().then((r) => setSkills(r.skills), (e) => setError(message(e)));
    api.shareableSkills().then((r) => setShareable(r.skills), () => setShareable([]));
  }, []);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  function find(from: string) {
    if (!from.trim()) return;
    void run(async () => {
      setUpdate(null);
      setPreview(await api.previewSkills(from.trim()));
    });
  }

  async function toggle(skill: LibrarySkill, agent: AgentKind, on: boolean) {
    await run(async () => {
      const updated = await api.setSkillAgents(skill.name, { [agent]: on });
      setSkills((list) => list?.map((s) => (s.name === skill.name ? updated : s)) ?? null);
    });
  }

  async function remove(skill: LibrarySkill) {
    const what =
      skill.source === "link"
        ? "Its folder stays where it is; it is only taken out of the hub."
        : "The downloaded copy is deleted.";
    if (!window.confirm(`Remove ${skill.name} from the library? ${what}`)) return;
    await run(async () => {
      await api.removeSkill(skill.name);
      setSkills((list) => list?.filter((s) => s.name !== skill.name) ?? null);
      setShareable((list) => list?.map((s) => (s.path === skill.path ? { ...s, linked: false } : s)) ?? null);
    });
  }

  async function checkUpdate(skill: LibrarySkill) {
    await run(async () => {
      setPreview(null);
      setUpdate({ name: skill.name, check: await api.checkSkillUpdate(skill.name) });
    });
  }

  return (
    <dialog ref={dialog} className="dialog skills-dialog" onClose={onClose}>
      <h2>Skills</h2>
      <p className="muted small">
        Skills any agent can use: type <code>/</code> in the text box to pick one. Each agent also keeps its own skills,
        which show in its <code>/</code> menu without being added here.
      </p>
      {error && <div className="composer-error">{error}</div>}

      {preview ? (
        <PreviewPanel
          preview={preview}
          busy={busy}
          onCancel={() => setPreview(null)}
          onAdd={(picks) =>
            void run(async () => {
              const result = await api.addSkills({ source: preview.source, staging_id: preview.staging_id, picks });
              setSkills(result.skills);
              setShareable((list) =>
                list?.map((s) => (result.skills.some((k) => k.path === s.path) ? { ...s, linked: true } : s)) ?? null,
              );
              setPreview(null);
              setSource("");
            })
          }
        />
      ) : update ? (
        <UpdatePanel
          name={update.name}
          check={update.check}
          busy={busy}
          onClose={() => setUpdate(null)}
          onApply={() =>
            void run(async () => {
              const updated = await api.applySkillUpdate(update.name, update.check.staging_id!);
              setSkills((list) => list?.map((s) => (s.name === updated.name ? updated : s)) ?? null);
              setUpdate(null);
            })
          }
        />
      ) : (
        <>
          <section className="skills-section">
            <h3>Library</h3>
            {skills === null ? (
              <span className="muted small">Loading…</span>
            ) : skills.length === 0 ? (
              <span className="muted small">No skills yet. Add one below.</span>
            ) : (
              <ul className="skill-list">
                {skills.map((skill) => (
                  <li key={skill.name} className={`skill-row${skill.present ? "" : " missing"}`}>
                    <div className="skill-row-head">
                      <strong>{skill.name}</strong>
                      <span className="skill-badge">
                        {!skill.present ? "Missing" : skill.source === "link" ? "Linked" : skill.repo ? "From git" : "Copied"}
                      </span>
                      <span className="skill-row-actions">
                        {skill.repo && (
                          <button type="button" className="button subtle" disabled={busy} onClick={() => void checkUpdate(skill)}>
                            Update
                          </button>
                        )}
                        <button type="button" className="button subtle" disabled={busy} onClick={() => void remove(skill)}>
                          Remove
                        </button>
                      </span>
                    </div>
                    <span className="muted small">
                      {skill.present ? skill.description : `The folder is gone: ${skill.path}`}
                    </span>
                    <AgentSwitches
                      value={skill.agents}
                      disabled={busy || !skill.present}
                      onChange={(agent, on) => void toggle(skill, agent, on)}
                      native={skill.origin}
                    />
                    {skill.warnings.map((w) => (
                      <span key={w} className="hint warning">
                        {w}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="skills-section">
            <h3>Add skills</h3>
            <form
              className="skill-source"
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                find(source);
              }}
            >
              <input
                value={source}
                onChange={(e) => setSource(e.target.value)}
                placeholder="A folder, a .zip file, or a git repository (https://…)"
                aria-label="Folder, zip or repository"
              />
              <button type="submit" className="button" disabled={busy || !source.trim()}>
                {busy ? "Looking…" : "Find skills"}
              </button>
            </form>
            <span className="hint">
              Folders on this PC are linked, so your edits carry over. Repositories and zips are copied in, and a repository
              skill can be updated later. Nothing in a skill runs when it is added; you see it first.
            </span>
          </section>

          <section className="skills-section">
            <h3>Share your own skills</h3>
            <span className="hint">Your Claude and Codex skills, to use with the other agents too.</span>
            {shareable === null ? (
              <span className="muted small">Looking…</span>
            ) : shareable.length === 0 ? (
              <span className="muted small">No Claude or Codex skills found.</span>
            ) : (
              <ul className="skill-list compact">
                {shareable.map((s) => (
                  <li key={s.path} className="skill-share-row">
                    <span className={`agent-${s.agent}`}>
                      <AgentLogo agent={s.agent} />
                    </span>
                    <span className="skill-share-text">
                      <strong>{s.name}</strong>
                      <span className="muted small">{s.description}</span>
                    </span>
                    <button type="button" className="button subtle" disabled={busy || s.linked} onClick={() => find(s.path)}>
                      {s.linked ? "Shared" : "Share"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}

      <div className="dialog-actions">
        <button type="button" className="button" onClick={() => dialog.current?.close()}>
          Close
        </button>
      </div>
    </dialog>
  );
}

function AgentSwitches({
  value,
  onChange,
  disabled,
  native,
}: {
  value: SkillSwitches;
  onChange: (agent: AgentKind, on: boolean) => void;
  disabled?: boolean;
  // The agent whose own folder the skill is in: it has the skill already.
  native?: AgentKind | null;
}) {
  return (
    <div className="skill-switches" role="group" aria-label="Agents that can use it">
      {AGENTS.map((agent) => (
        <label
          key={agent}
          className={`checkbox skill-switch agent-${agent}`}
          title={native === agent ? `${AGENT_NAMES[agent]} already has this skill in its own folder` : undefined}
        >
          <input
            type="checkbox"
            checked={native === agent || value[agent]}
            disabled={disabled || native === agent}
            onChange={(e) => onChange(agent, e.target.checked)}
          />
          {SWITCH_NAMES[agent]}
          {native === agent && <span className="muted"> (its own)</span>}
        </label>
      ))}
    </div>
  );
}

function PreviewPanel({
  preview,
  busy,
  onCancel,
  onAdd,
}: {
  preview: SkillPreview;
  busy: boolean;
  onCancel: () => void;
  onAdd: (picks: { path: string; agents: SkillSwitches }[]) => void;
}) {
  // Each found skill: whether to add it and the switches (starting from the hub's suggestion).
  const [choices, setChoices] = useState(() =>
    preview.skills.map((s) => ({ include: !s.exists, agents: { ...s.suggested } })),
  );
  const picks = preview.skills
    .map((s, i) => ({ skill: s, choice: choices[i] }))
    .filter(({ choice, skill }) => choice.include && !skill.exists);
  const where = preview.kind === "folder" ? "linked from this folder" : preview.kind === "git" ? "copied from the repository" : "copied from the zip";

  function set(i: number, change: Partial<(typeof choices)[number]>) {
    setChoices((list) => list.map((c, j) => (j === i ? { ...c, ...change } : c)));
  }

  return (
    <section className="skills-section">
      <h3>
        Found {preview.skills.length} skill{preview.skills.length === 1 ? "" : "s"}
      </h3>
      <span className="hint">
        Read each one before adding it: an agent will follow its instructions. They will be {where}.
      </span>
      {preview.problems.map((p) => (
        <span key={p} className="hint warning">
          Skipped: {p}
        </span>
      ))}
      <ul className="skill-list">
        {preview.skills.map((skill: FoundSkill, i) => (
          <li key={skill.path} className="skill-row">
            <label className="checkbox skill-row-head">
              <input
                type="checkbox"
                checked={choices[i].include && !skill.exists}
                disabled={skill.exists}
                onChange={(e) => set(i, { include: e.target.checked })}
              />
              <strong>{skill.name}</strong>
              {skill.exists && <span className="muted"> (already in the library)</span>}
            </label>
            <span className="muted small">{skill.description}</span>
            <AgentSwitches
              value={choices[i].agents}
              disabled={busy || skill.exists || !choices[i].include}
              onChange={(agent, on) => set(i, { agents: { ...choices[i].agents, [agent]: on } })}
            />
            {skill.warnings.map((w) => (
              <span key={w} className="hint warning">
                {w}
              </span>
            ))}
            {skill.scripts.length > 0 && (
              <span className="hint">
                Scripts: <code>{skill.scripts.join(", ")}</code>
              </span>
            )}
            <details className="skill-content">
              <summary>Show SKILL.md</summary>
              <pre>{skill.content}</pre>
            </details>
          </li>
        ))}
      </ul>
      <div className="dialog-actions">
        <button type="button" className="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          type="button"
          className="button primary"
          disabled={busy || picks.length === 0}
          onClick={() => onAdd(picks.map(({ skill, choice }) => ({ path: skill.subpath ?? skill.path, agents: choice.agents })))}
        >
          Add {picks.length} skill{picks.length === 1 ? "" : "s"}
        </button>
      </div>
    </section>
  );
}

function UpdatePanel({
  name,
  check,
  busy,
  onClose,
  onApply,
}: {
  name: string;
  check: SkillUpdateCheck;
  busy: boolean;
  onClose: () => void;
  onApply: () => void;
}) {
  return (
    <section className="skills-section">
      <h3>Update {name}</h3>
      {check.up_to_date ? (
        <span className="muted small">It is up to date with its repository.</span>
      ) : (
        <>
          <span className="hint">Check what changed before using the new version.</span>
          {check.skill_diff && <pre className="skill-diff">{check.skill_diff}</pre>}
          {[
            ["Added", check.added],
            ["Removed", check.removed],
            ["Changed", check.changed],
          ].map(([label, files]) =>
            (files as string[]).length ? (
              <span key={label as string} className="hint">
                {label as string}: <code>{(files as string[]).join(", ")}</code>
              </span>
            ) : null,
          )}
        </>
      )}
      <div className="dialog-actions">
        <button type="button" className="button" onClick={onClose} disabled={busy}>
          {check.up_to_date ? "Back" : "Keep the current version"}
        </button>
        {!check.up_to_date && (
          <button type="button" className="button primary" onClick={onApply} disabled={busy || !check.staging_id}>
            Use the new version
          </button>
        )}
      </div>
    </section>
  );
}
