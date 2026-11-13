/** What the shell shows when the session ends under a signed-in page, and when the first
 *  session check on load gets no answer. Loaded with the shell (both are needed exactly when the server is in doubt).
 *
 *  - SessionEndedDialog: the session ended (it timed out, the server restarted, the password was reset elsewhere) while
 *    a page was open. The page stays mounted underneath, so a form keeps what was typed; signing in again here takes
 *    the same account back to the same organization and the page reloads what it couldn't. An account that signs in
 *    only through single sign-on gets that button (a full-page round trip, back to this address).
 *  - Unreachable: the first `auth/me` got no answer, a 429 or a 5xx, which says nothing about the session: retry with
 *    a growing wait instead of showing the sign-in page to someone who is signed in. */
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api, Problem, sessionChange, type Me, type PlatformSession } from "@/api";
import { Field, useReturnFocus } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { AuthCard } from "@/features/auth/auth-card";
import type { Schemas } from "@/lib/openapi";

/** Said on the sign-in page when the shell had to leave the page (an impersonation's session, the staff console). */
export const SESSION_ENDED_NOTICE = "Your session ended. Sign in again to continue where you were.";

type Options = Schemas["AuthOptions"];

/** `auth/login` answers `me`, or a platform staff account's console session (no `organization`). */
const isMe = (body: Me | PlatformSession): body is Me => "organization" in body;

export function SessionEndedDialog({ me, onSignedIn, onSignOut }: {
  /** The account and organization the page was showing. */
  me: Me;
  /** Signed in again: App takes it back to the page's organization and reloads what the page couldn't. */
  onSignedIn: (next: Me) => Promise<void>;
  /** Leave the page for the sign-in page (another account, or no password at hand). */
  onSignOut: () => void;
}) {
  const location = useLocation();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sso, setSso] = useState<Options["sso"]>(null);
  // Focus goes back to whatever opened this (the Save button, the link): the page is the same page.
  const returnFocus = useReturnFocus(true);
  useEffect(() => {
    let live = true;
    api<Options>(`auth/options?${new URLSearchParams({ email: me.user.email })}`).then((o) => { if (live) setSso(o.sso); }, () => undefined);
    return () => { live = false; };
  }, [me.user.email]);
  const here = location.pathname + location.search + location.hash;
  const ssoHref = sso ? `${sso.start_url}${sso.start_url.includes("?") ? "&" : "?"}${new URLSearchParams({ next: here })}` : "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!password) return setError("Enter your password.");
    setBusy(true);
    setError("");
    try {
      // Signing in gives a new session key: alone, after the page's requests in flight (api.ts sessionChange).
      const body = await sessionChange(() => api<Me | PlatformSession>("auth/login", { method: "POST", exclusive: true, body: { email: me.user.email, password } }));
      await api("auth/csrf").catch(() => undefined); // Django rotates the CSRF token at sign-in
      if (!isMe(body)) return onSignOut(); // a staff account that is no member any more: the console's own sign-in
      await onSignedIn(body);
    } catch (err) {
      setBusy(false);
      setError(err instanceof Problem ? err.detail : "Couldn't sign in. Check your connection and try again.");
    }
  }
  return (
    <Dialog open onOpenChange={() => undefined}>
      {/* An opaque overlay: the page underneath stays mounted (typed values survive) but isn't readable while signed out. */}
      <DialogContent showCloseButton={false} className="max-w-md" overlayClassName="bg-background" data-testid="session-ended" {...returnFocus}
        onInteractOutside={(e) => e.preventDefault()} onEscapeKeyDown={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Your session ended</DialogTitle>
          <DialogDescription>
            Sign in again to carry on. This page stays as it is and keeps what you typed; anything you tried to save since the session ended wasn&rsquo;t saved, so save it again.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate aria-label="Sign in again">
          {error && <p id="session-password-error" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
          <p className="mb-3 text-sm">Signed in as <span className="font-semibold [overflow-wrap:anywhere]">{me.user.email}</span> in {me.organization.name}.</p>
          <Field id="session-password" label="Password">
            <Input id="session-password" type="password" autoComplete="current-password" autoFocus value={password}
              onChange={(e) => setPassword(e.target.value)} aria-invalid={error ? true : undefined} aria-describedby={error ? "session-password-error" : undefined} />
          </Field>
          {sso && (
            <p className="mb-3 text-sm">
              No password? <a href={ssoHref} className="font-semibold">{sso.provider === "entra" ? "Sign in with Microsoft Entra ID" : `Sign in with ${sso.label}`}</a> (this page reloads afterward, so copy anything you typed first).
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onSignOut}>Sign out</Button>
            <Button type="submit" aria-busy={busy || undefined}>{busy ? "Signing in…" : "Sign in again"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** The first session check got no clear answer: say so and try again, `retryIn` seconds from now (null while trying). */
export function Unreachable({ retryIn, onRetry }: { retryIn: number | null; onRetry: () => void }) {
  return (
    <AuthCard title="Softwarden isn't reachable right now">
      <p className="mb-3">The server didn&rsquo;t answer. If you were signed in, you still are: this page tries again on its own.</p>
      <p className="mb-4 text-sm text-muted-foreground" role="status">
        {retryIn === null ? "Trying again…" : `Trying again in ${retryIn} ${retryIn === 1 ? "second" : "seconds"}.`}
      </p>
      <Button type="button" variant="outline" className="w-full" disabled={retryIn === null} onClick={onRetry}>Try now</Button>
    </AuthCard>
  );
}
