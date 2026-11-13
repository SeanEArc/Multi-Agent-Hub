// The list that opens above the text box when a message starts with "/": the current agent's skills and commands.
import { useEffect, useRef } from "react";
import type { SlashCommand } from "../api";

const KIND_LABELS: Record<SlashCommand["kind"], string> = {
  skill: "Skill",
  library: "Library",
  builtin: "Command",
  hub: "Hub",
};

interface Props {
  // null while loading.
  commands: SlashCommand[] | null;
  error: string | null;
  query: string;
  active: number;
  onPick: (command: SlashCommand) => void;
  onHover: (index: number) => void;
}

/** The commands whose name contains the typed text, names that start with it first. */
export function matchCommands(commands: SlashCommand[], query: string): SlashCommand[] {
  const q = query.toLowerCase();
  const hits = commands.filter((c) => c.name.toLowerCase().includes(q));
  return [...hits.filter((c) => c.name.toLowerCase().startsWith(q)), ...hits.filter((c) => !c.name.toLowerCase().startsWith(q))];
}

export function SlashMenu({ commands, error, query, active, onPick, onHover }: Props) {
  const list = useRef<HTMLDivElement>(null);
  const shown = commands ? matchCommands(commands, query) : [];

  // Keep the highlighted row in view while the arrow keys move it.
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(".slash-item.active")?.scrollIntoView({ block: "nearest" });
  }, [active]);

  return (
    <div className="slash-menu" role="listbox" aria-label="Skills and commands" ref={list}>
      {error ? (
        <div className="slash-note warning">{error}</div>
      ) : commands === null ? (
        <div className="slash-note muted">Looking up skills and commands…</div>
      ) : shown.length === 0 ? (
        <div className="slash-note muted">
          {commands.length === 0 ? "No skills or commands for this agent yet. Add some under Skills." : `Nothing matches /${query}.`}
        </div>
      ) : (
        shown.map((c, i) => (
          <button
            key={`${c.kind}:${c.name}`}
            type="button"
            role="option"
            aria-selected={i === active}
            className={`slash-item${i === active ? " active" : ""}`}
            // Before the text box loses focus, so the pick lands in it.
            onMouseDown={(e) => {
              e.preventDefault();
              onPick(c);
            }}
            onMouseEnter={() => onHover(i)}
          >
            <span className="slash-name">/{c.name}</span>
            <span className="slash-kind">{KIND_LABELS[c.kind]}</span>
            {c.description && <span className="slash-description muted">{c.description}</span>}
          </button>
        ))
      )}
    </div>
  );
}
