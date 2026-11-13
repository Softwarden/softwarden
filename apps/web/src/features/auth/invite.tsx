/** The invitation link's page, `/invite#<token>` (access and provisioning). The token
 *  is in the fragment, which browsers never send, so it is in no server or proxy log. The page reads it, takes it out
 *  of the address bar, and keeps it in this tab's sessionStorage, so a password or single sign-on round trip that
 *  comes back to `/invite` still has it; it is cleared once accepted or found unusable. The API takes it in the body
 *  (`auth/invitations/preview` and `auth/invitations/accept`, both CSRF-checked even when signed out).
 *
 *  Public: it never needs `auth/me`, because a single sign-on session started from this page has no membership yet
 *  (`me` answers 403 until the invitation is accepted). The preview says what the link is for and who this browser is
 *  signed in as; from that:
 *  - the email's account, signed in: Join;
 *  - a link that can't create an account (`can_create_account` false: the copy the inviting Admin was shown, where more
 *    than one organization shares the installation): sign in first if the address has an account, or open the
 *    invitation email, whose link proves the mailbox and creates the account. Nothing says whether an account exists;
 *  - no account for the email, nobody signed in: choose a name and password, which creates the account and signs in.
 *    When an organization made an account for the address through its single sign-on or SCIM (`organization_account`),
 *    say that creating one here retires it, and where to sign in to keep it;
 *  - the email's account, nobody signed in: sign in first (password, or the organization's single sign-on), coming back;
 *  - another account signed in: sign out first;
 *  - signed in through another organization's single sign-on (`other_sign_in`): sign out first, then the sign-in
 *    choices, because such a session is confined to that organization and can't accept.
 *  An unknown, expired, used or revoked link gets one answer. Loaded on first visit, outside the first bundle. */
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Problem, signOutRequest, when, type Me } from "@/api";
import { ErrorSummary, Field } from "@/components/app";
import { DetailList, DetailRow } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AuthCard } from "@/features/auth/auth-card";
import { HOME } from "@/features/auth/login";
import { typedApi, type Schemas } from "@/lib/openapi";
import { problemText } from "@/lib/query";
import { storedInvitation, storeInvitation } from "@/features/auth/invite-token";

type Preview = Schemas["InvitationPreview"];

const ROLE: Record<string, string> = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
const UNAVAILABLE = "/problems/invitation-unavailable";
const unavailable = (e: unknown) => e instanceof Problem && (e.status === 404 || e.type === UNAVAILABLE);
/** This tab's invitation token while the invitee signs in elsewhere and comes back (sessionStorage, an hour at most). */
const storedToken = () => storedInvitation();
const storeToken = storeInvitation;

/** The preview and the accept take the token in the body. Both are CSRF-checked, signed out too: make sure this
 *  browser has the cookie first, as first-run setup does. */
async function previewOf(token: string): Promise<Preview> {
  await api("auth/csrf");
  return typedApi("post", "auth/invitations/preview", { body: { token } });
}
async function accept(token: string, body: { name?: string; password?: string } = {}): Promise<Me> {
  await api("auth/csrf");
  return (await typedApi("post", "auth/invitations/accept", { body: { token, ...body } })) as Me;
}

export function InvitePage({ onSignedIn, onSignedOut }: { onSignedIn: (me: Me) => void; onSignedOut: () => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  // The link's fragment wins (a new link replaces one kept from before); otherwise the one kept for this tab.
  const fromLink = location.hash.replace(/^#/, "");
  const [token] = useState(() => fromLink || storedToken());
  useEffect(() => {
    if (!fromLink) return;
    storeToken(fromLink);
    // Out of the address bar (and so out of history, bookmarks and screenshots) as soon as it's read.
    navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // The token isn't in the cache key: it stays in this component and sessionStorage only. Never cached past the page.
  const preview = useQuery({ queryKey: ["invitation-preview"], queryFn: () => previewOf(token), enabled: token !== "", gcTime: 0 });
  // Set when accepting found the link no longer usable (it expired, or was revoked, while this page was open).
  const [gone, setGone] = useState(false);
  // Why accepting was refused when the page then changed step (the session or the link changed since the preview):
  // kept above the new step, which the reloaded preview shows.
  const [refused, setRefused] = useState("");
  const stale = (why: string) => { setRefused(why); void preview.refetch(); };
  const isGone = !token || gone || (preview.isError && unavailable(preview.error));
  useEffect(() => { if (isGone) storeToken(""); }, [isGone]);

  /** A member now: the session's `me` again (a new account was signed in; a single sign-on session that had no
   *  membership has one now), then everyone's home. Django rotates the CSRF token when it signs someone in. */
  async function joined(answer: Me) {
    storeToken("");
    await api("auth/csrf").catch(() => undefined);
    const me = await api<Me>("auth/me").catch(() => answer);
    onSignedIn(me);
    navigate(HOME, { replace: true });
  }

  if (isGone) return <Unavailable />;
  if (preview.isError && !preview.isFetching) {
    return (
      <AuthCard title="Couldn't load this invitation">
        <div className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          {problemText(preview.error, "Softwarden didn't answer. Check your connection and try again.")}
        </div>
        <Button type="button" variant="outline" onClick={() => void preview.refetch()}>Try again</Button>
      </AuthCard>
    );
  }
  if (!preview.data) {
    return <AuthCard title="Invitation"><p className="text-muted-foreground" role="status">Loading the invitation…</p></AuthCard>;
  }
  const p = preview.data;
  return (
    <AuthCard title={`Join ${p.organization.name}`}>
      <p className="mb-3 text-sm text-muted-foreground">You've been invited to Softwarden, where {p.organization.name}'s IT team keeps track of its software.</p>
      <DetailList className="mb-4 rounded-md border px-3">
        <DetailRow label="Organization">{p.organization.name}</DetailRow>
        <DetailRow label="Email"><span className="[overflow-wrap:anywhere]">{p.email}</span></DetailRow>
        <DetailRow label="Role">{ROLE[p.role] ?? p.role}</DetailRow>
        <DetailRow label="Link expires"><span className="tabular">{when(p.expires_at)}</span></DetailRow>
      </DetailList>
      {refused && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert">{refused}</p>}
      {p.session === "other_sign_in" ? (
        <OtherAccount preview={p} viaSso onSignedOut={() => { onSignedOut(); setRefused(""); void preview.refetch(); }} />
      ) : p.session === "other_account" ? (
        <OtherAccount preview={p} onSignedOut={() => { onSignedOut(); setRefused(""); void preview.refetch(); }} />
      ) : p.session === "invitee" ? (
        <JoinAsInvitee token={token} preview={p} onJoined={joined} onGone={() => setGone(true)} onStale={stale} />
      ) : !p.can_create_account ? (
        <SignInOrUseEmail preview={p} />
      ) : p.account_exists === false ? (
        <NewAccount token={token} preview={p} onJoined={joined} onGone={() => setGone(true)} onStale={stale} />
      ) : (
        <SignInFirst preview={p} />
      )}
    </AuthCard>
  );
}

/** One answer for every link that can't be used, so the page never says which (and never whether an account exists). */
function Unavailable() {
  return (
    <AuthCard title="This invitation link can't be used">
      <p className="mb-4">It may have expired, been used already, or been replaced by a newer invitation. Ask the administrator who invited you to send a new link.</p>
      <p className="text-sm text-muted-foreground">Already have an account? <Link className="underline" to="/login">Sign in</Link></p>
    </AuthCard>
  );
}

type StepProps = { token: string; preview: Preview; onJoined: (me: Me) => Promise<void>; onGone: () => void; onStale: (why: string) => void };

/** Accepting with a link that doesn't prove the mailbox while nobody is signed in. */
const EMAILED_LINK_REQUIRED = "/problems/invitation-emailed-link-required";

/** What a refused accept means for the page: an unusable link, or a session or link that changed since the preview
 *  loaded (signed out elsewhere, another account, a single sign-on session confined to another organization, or a link
 *  that no longer creates accounts because a second organization appeared): the page reloads the preview and keeps the
 *  problem's own sentence above the step it shows then. Anything else stays in the step. */
function refusal(e: unknown, { onGone, onStale }: Pick<StepProps, "onGone" | "onStale">): string | null {
  if (unavailable(e)) { onGone(); return null; }
  if (e instanceof Problem && e.type === EMAILED_LINK_REQUIRED) {
    onStale(`${e.detail} Open the link in the invitation email to create your account.`);
    return null;
  }
  if (e instanceof Problem && (e.status === 403 || e.status === 409)) { onStale(e.detail); return null; }
  return problemText(e, "Couldn't accept the invitation. Try again.");
}

function NewAccount({ token, preview, onJoined, onGone, onStale }: StepProps) {
  const [form, setForm] = useState({ name: preview.name, password: "", confirm: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    const next: Record<string, string[]> = {};
    if (!form.name.trim()) next.name = ["Enter your name."];
    if (!form.password) next.password = ["Choose a password."];
    else if (form.confirm !== form.password) next.confirm = ["The passwords don't match."];
    if (Object.keys(next).length) return setErrors(next);
    setErrors({});
    setSaving(true);
    try {
      await onJoined(await accept(token, { name: form.name.trim(), password: form.password }));
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      const message = refusal(err, { onGone, onStale });
      if (message) setErrors({ password: [message] });
    }
  }
  const made = preview.organization_account;
  return (
    <form onSubmit={submit} noValidate aria-label="Create your account">
      <h2 className="mb-1 font-display text-section font-semibold">Create your account</h2>
      <p className="mb-3 text-sm text-muted-foreground">You sign in with the invited email and this password.</p>
      {made && (
        // An organization's single sign-on or SCIM made an account for this address, which never proved it.
        <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground" role="note" data-testid="organization-account">
          <p><span aria-hidden="true">! </span>{made.organization} already made an account for this address through its single sign-on. Creating an account here retires that one: it stops working in {made.organization}.</p>
          <p className="mt-1">To keep it, don&rsquo;t create an account here: <a href={made.sign_in_url} className="font-semibold text-state-pending-foreground underline">sign in through {made.organization}</a> instead.</p>
        </div>
      )}
      <ErrorSummary errors={errors} />
      <Field id="name" label="Your name (required)" errors={errors}>
        <Input id="name" autoComplete="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={200} />
      </Field>
      <Field id="password" label="Password (required)" errors={errors} help="At least 12 characters, not a common password.">
        <Input id="password" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
      </Field>
      <Field id="confirm" label="Confirm password (required)" errors={errors}>
        <Input id="confirm" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
      </Field>
      <Button type="submit" className="mt-1 w-full" disabled={saving}>{saving ? "Joining…" : `Create account and join ${preview.organization.name}`}</Button>
    </form>
  );
}

function JoinAsInvitee({ token, preview, onJoined, onGone, onStale }: StepProps) {
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  async function join() {
    if (saving) return;
    setError("");
    setSaving(true);
    try {
      await onJoined(await accept(token));
    } catch (err) {
      setSaving(false);
      const message = refusal(err, { onGone, onStale });
      if (message) setError(message);
    }
  }
  return (
    <>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <p className="mb-3 text-sm">You're signed in with the invited account.</p>
      <Button type="button" className="w-full" disabled={saving} onClick={() => void join()}>{saving ? "Joining…" : `Join ${preview.organization.name}`}</Button>
    </>
  );
}

/** The link the inviting Admin was shown, where it can't create an account: someone with an account signs in and
 *  comes back to join; a newcomer opens the invitation email, whose link proves the address is theirs. Nothing here
 *  says whether the address has an account. */
function SignInOrUseEmail({ preview }: { preview: Preview }) {
  return (
    <>
      <SignInChoices preview={preview} lead={`If ${preview.email} already has a Softwarden account, sign in with it and you'll come back here to join.`} />
      <div className="mt-4 border-t pt-4" data-testid="use-invitation-email">
        <h2 className="mb-1 font-display text-section font-semibold">New to Softwarden?</h2>
        <p className="text-sm text-muted-foreground">
          This copy of the link can&rsquo;t create an account. Open the link in the invitation email sent to {preview.email}: it shows the address is yours, and you create your account there.
          No email? Ask the administrator who invited you to send it again.
        </p>
      </div>
    </>
  );
}

/** The email has an account and nobody is signed in: sign in as it, then come back here to accept. */
function SignInFirst({ preview }: { preview: Preview }) {
  return <SignInChoices preview={preview} lead={`${preview.email} already has a Softwarden account. Sign in with it and you'll come back here to join.`} />;
}

function SignInChoices({ preview, lead }: { preview: Preview; lead: string }) {
  // Back to /invite with no token in the address: this tab kept it.
  const back = "/invite";
  return (
    <>
      <h2 className="mb-1 font-display text-section font-semibold">Sign in to accept</h2>
      <p className="mb-3 text-sm text-muted-foreground">{lead}</p>
      <Button asChild className="w-full">
        <Link to={`/login?${new URLSearchParams({ next: back })}`}>Sign in with your password</Link>
      </Button>
      {preview.sso && (
        <div className="mt-3">
          {/* A full-page navigation: the identity provider, then the API's callback, which comes back to this page. */}
          <Button asChild variant="outline" className="w-full">
            <a href={preview.sso.start_url}>{preview.sso.provider === "entra" ? "Sign in with Microsoft Entra ID" : `Sign in with ${preview.sso.label}`}</a>
          </Button>
        </div>
      )}
    </>
  );
}

/** Another account is signed in in this browser: accepting would add the wrong person, so sign out first. `viaSso`: the
 *  browser is signed in through another organization's single sign-on, a session confined to that organization, which
 *  can't accept (even as the invited account); after signing out the page offers the sign-in choices. */
function OtherAccount({ preview, onSignedOut, viaSso = false }: { preview: Preview; onSignedOut: () => void; viaSso?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function signOut() {
    setBusy(true);
    setError("");
    try {
      await signOutRequest();
      await api("auth/csrf").catch(() => undefined); // a fresh token for the signed-out session
      onSignedOut();
      // The button is gone with this step: the page's heading takes focus while the invitation reloads.
      document.querySelector<HTMLElement>("main h1")?.focus();
    } catch (err) {
      setBusy(false);
      setError(problemText(err, "Couldn't sign out. Try again."));
    }
  }
  return (
    <>
      <h2 className="mb-1 font-display text-section font-semibold">{viaSso ? "Signed in through another organization" : "A different account is signed in"}</h2>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <p className="mb-3 text-sm text-muted-foreground">
        {viaSso
          ? `This browser is signed in through another organization's single sign-on, which can only be used in that organization. Sign out here, then sign in again to accept this invitation for ${preview.email}.`
          : `This invitation is for ${preview.email}, but this browser is signed in to another Softwarden account. Sign out here, then accept it as that account.`}
      </p>
      <Button type="button" className="w-full" disabled={busy} onClick={() => void signOut()}>{busy ? "Signing out…" : "Sign out"}</Button>
    </>
  );
}
