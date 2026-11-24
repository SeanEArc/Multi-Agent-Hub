import { useEffect, useState } from "react";

export type ThemeChoice = "system" | "light" | "dark";

const THEME_KEY = "hub.theme";
const systemLight = window.matchMedia("(prefers-color-scheme: light)");

export function readTheme(): ThemeChoice {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // storage unavailable; follow the system
  }
  return "system";
}

/** Sets `data-theme` on <html>, which picks the light or dark tokens in styles.css. */
export function applyTheme(choice: ThemeChoice) {
  const resolved = choice === "system" ? (systemLight.matches ? "light" : "dark") : choice;
  document.documentElement.dataset.theme = resolved;
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);

  useEffect(() => {
    applyTheme(choice);
    try {
      if (choice === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, choice);
    } catch {
      // storage unavailable; the choice just won't be remembered
    }
    if (choice !== "system") return;
    // Follow the system while it changes.
    const onChange = () => applyTheme("system");
    systemLight.addEventListener("change", onChange);
    return () => systemLight.removeEventListener("change", onChange);
  }, [choice]);

  return [choice, setChoice];
}

/** The theme on screen now ("light" or "dark", from `data-theme`), following every change. */
export function useResolvedTheme(): "light" | "dark" {
  const read = () => (document.documentElement.dataset.theme === "light" ? "light" : "dark");
  const [theme, setTheme] = useState<"light" | "dark">(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}
