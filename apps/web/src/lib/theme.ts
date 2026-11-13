/** Light and dark themes. The user's preference (System, Light or Dark) is kept in
 *  localStorage; System follows `prefers-color-scheme`. `<html data-theme>` holds the resolved theme, which the token
 *  CSS keys off. public/theme-init.js (loaded by index.html) runs the same resolution before first paint, so no
 *  flash of the wrong theme; keep THEME_KEY and the logic there in step with this file. */
import { useCallback, useSyncExternalStore } from "react";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";
export const THEME_KEY = "softwarden.theme";
export const THEME_PREFS: { value: ThemePref; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

const DARK_QUERY = "(prefers-color-scheme: dark)";

export function readPref(): ThemePref {
  let stored: string | null = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch { /* storage blocked: use the system setting */ }
  return stored === "light" || stored === "dark" ? stored : "system";
}

export function resolveTheme(pref: ThemePref, systemDark = window.matchMedia(DARK_QUERY).matches): Theme {
  return pref === "system" ? (systemDark ? "dark" : "light") : pref;
}

function apply(pref: ThemePref) {
  const root = document.documentElement;
  root.dataset.themePref = pref;
  root.dataset.theme = resolveTheme(pref);
}

const listeners = new Set<() => void>();
const notify = () => listeners.forEach((l) => l());

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Store the preference, apply it now and tell every `useTheme` caller. */
export function setThemePref(pref: ThemePref) {
  try {
    if (pref === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, pref);
  } catch { /* storage blocked: the choice lasts until the page reloads */ }
  apply(pref);
  notify();
}

/** Follow the operating system while the preference is System, and changes made in another tab. Called once at
 *  start-up (main.tsx), so it works on every screen, including sign-in. */
export function startThemeSync() {
  const media = window.matchMedia(DARK_QUERY);
  media.addEventListener("change", () => { if (readPref() === "system") { apply("system"); notify(); } });
  window.addEventListener("storage", (e) => { if (e.key === THEME_KEY || e.key === null) { apply(readPref()); notify(); } });
  apply(readPref());
}

/** The preference and the theme it resolves to. */
export function useTheme() {
  const pref = useSyncExternalStore(subscribe, () => (document.documentElement.dataset.themePref as ThemePref | undefined) ?? readPref());
  const theme = useSyncExternalStore(subscribe, () => (document.documentElement.dataset.theme as Theme | undefined) ?? resolveTheme(pref));
  const set = useCallback((p: ThemePref) => setThemePref(p), []);
  return { pref, theme, setPref: set };
}
