/** Per-user preferences kept in this browser: table columns, density, the sidebar rail, single-key shortcuts
 *  and the palette's recent items. Keys carry the user's ID, so two accounts on one browser keep their own choices.
 *  Nothing here is sent to the API. Recent items are the only organisation data: record kinds and IDs (no names, no
 *  people), keyed by organisation too. A later phase may move these to a server-side user setting.
 *
 *  Key shape: `softwarden.<user id>.<name>`, for example `softwarden.7.table.applications`. */
import { useCallback, useSyncExternalStore } from "react";

const PREFIX = "softwarden";
export const prefKey = (userId: number, name: string) => `${PREFIX}.${userId}.${name}`;

const listeners = new Set<() => void>();
const notify = () => listeners.forEach((l) => l());
function subscribe(listener: () => void) {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => { if (!e.key || e.key.startsWith(`${PREFIX}.`)) listener(); };
  window.addEventListener("storage", onStorage);
  return () => { listeners.delete(listener); window.removeEventListener("storage", onStorage); };
}

function readRaw(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; } // storage blocked: defaults apply
}

export function readPref<T>(key: string, fallback: T): T {
  const raw = readRaw(key);
  if (raw === null) return fallback;
  try { return JSON.parse(raw) as T; } catch { return fallback; }
}

export function writePref<T>(key: string, value: T | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* storage blocked or full: the choice lasts until the page reloads */ }
  notify();
}

/** A preference as React state. `fallback` must be stable (a constant) so the snapshot stays referentially equal. */
export function usePref<T>(key: string, fallback: T): [T, (value: T | null) => void] {
  // The raw string is the snapshot (stable between reads); it is parsed below.
  const raw = useSyncExternalStore(subscribe, () => readRaw(key));
  let value = fallback;
  if (raw !== null) { try { value = JSON.parse(raw) as T; } catch { /* keep the fallback */ } }
  const set = useCallback((v: T | null) => writePref(key, v), [key]);
  return [value, set];
}
