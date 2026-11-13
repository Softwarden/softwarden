/** The invitation token this tab keeps while the invitee signs in elsewhere and comes back to `/invite`
 *  (sessionStorage, so this tab only). Kept apart from the invitation page, which is loaded
 *  on first use, because the shell's sign-out clears it too and must not pull that page
 *  into the first bundle.
 *
 *  Stored as `{"token": "...", "at": <milliseconds>}` and forgotten an hour after it was stored: a sign-in round trip
 *  takes minutes, and a token left behind in a shared browser shouldn't outlive that. A value in the older plain-string
 *  form, or one that can't be read, counts as nothing kept. */
export const INVITATION_KEY = "softwarden.invitation";
/** How long a kept token lasts: one hour. */
export const INVITATION_TTL_MS = 60 * 60 * 1000;

export function storedInvitation(now = Date.now()): string {
  try {
    const raw = sessionStorage.getItem(INVITATION_KEY);
    if (!raw) return "";
    const kept = JSON.parse(raw) as { token?: unknown; at?: unknown };
    const fresh = typeof kept.token === "string" && typeof kept.at === "number" && now - kept.at >= 0 && now - kept.at < INVITATION_TTL_MS;
    if (fresh) return kept.token as string;
    sessionStorage.removeItem(INVITATION_KEY);
    return "";
  } catch {
    forgetInvitation();
    return "";
  }
}

export function storeInvitation(token: string) {
  if (!token) return forgetInvitation();
  try { sessionStorage.setItem(INVITATION_KEY, JSON.stringify({ token, at: Date.now() })); } catch { /* storage off: this visit only */ }
}

export function forgetInvitation() {
  try { sessionStorage.removeItem(INVITATION_KEY); } catch { /* storage off: nothing kept */ }
}
