/** The signed-in user (`GET auth/me`), provided by the app shell to every screen. */
import { createContext, useContext, useState } from "react";
import { useLocation } from "react-router-dom";
import type { Me } from "@/api";

export const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext)!;
export const canWrite = (me: Me) => me.role !== "viewer";
/** Why sign-in settings (auto-provisioning, SCIM roles) can't be changed while platform staff impersonate a member: the
 *  API refuses them (403 impersonation-restricted), since they outlive the impersonation. */
export const IMPERSONATING_SIGN_IN_SETTINGS = "Sign-in settings stay with this organization's Admins: they can't be changed while you impersonate someone. Stop impersonating to change them as yourself.";
/** Reload `auth/me` (the Admin banners: a webhook endpoint turned back on clears its notice). Provided by the shell. */
export const MeRefreshContext = createContext<() => void>(() => undefined);
export const useRefreshMe = () => useContext(MeRefreshContext);

/** History state for the page a form lands on after a save: the shell says "Saved <name>." and focuses the
 *  page's heading. `notice` is the page's own visible note, where it shows one. */
/** `announced`: the shell has said this entry's save (set when it drops `saved`). */
export type SavedState = { saved?: string; notice?: string; announced?: boolean };
export const savedState = (name: string, notice?: string): SavedState => ({ saved: name, ...(notice ? { notice } : {}) });

/** The note a page shows after a form landed on it (`savedState`), as state the page can replace with later notes.
 *  `quiet` while it is that note: the shell has already said it ("Saved <name>." and the note), so the page shows it
 *  without saying it a second time (SuccessNote `quiet`). */
export function useLandingNotice() {
  const location = useLocation();
  const [landed] = useState(() => location.state as SavedState | null);
  const [notice, setNotice] = useState(landed?.notice ?? "");
  return [notice, setNotice, !!(landed?.saved || landed?.announced) && notice === landed?.notice] as const;
}
