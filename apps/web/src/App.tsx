import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Search as SearchIcon } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api, IMPERSONATION_ENDED, IMPERSONATION_STARTED, Me, NOT_SIGNED_IN, ORGANIZATION_SUSPENDED, Problem, PROBLEM_EVENT, sessionChange, setTabImpersonation, signOutRequest, type Membership, type PlatformSession } from "@/api";
import { ErrorBanner, PageHead, useReturnFocus, UuidGuard } from "@/components/app";
import { PageErrorBoundary } from "@/components/error-boundary";
import { IS_MAC, PALETTE_KEYS, ShortcutHelp, useShortcuts } from "@/components/shortcuts";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DeletionBanner, ImpersonationBanner, SuspendedPanel } from "@/components/org-banners";
import { Sidebar } from "@/components/sidebar";
import { ThemeMenu } from "@/components/theme-menu";
import { Button } from "@/components/ui/button";
import { ChangePassword } from "@/features/auth/auth-card";
import { HOME, Login, RedirectToLogin } from "@/features/auth/login";
import { forgetInvitation } from "@/features/auth/invite-token";
import { SESSION_ENDED_NOTICE, SessionEndedDialog, Unreachable } from "@/features/auth/session-ended";
import { useApiQuery } from "@/lib/query";
import { prefKey, usePref } from "@/lib/prefs";
import { MeContext, MeRefreshContext, type SavedState } from "@/lib/session";

/** What the console is told when the shell returns to it after an impersonation (App's `endedImpersonation`). */
export type ConsoleNotice = { notice?: string };

/** A session check's answer (App's `refresh`). */
type SessionCheck = { kind: "me"; me: Me } | { kind: "out" } | { kind: "unknown"; retryAfter?: number };
/** A 429's Retry-After (seconds), when the answer carried one. */
const retryAfterOf = (e: unknown) => (e instanceof Problem ? e.retryAfter : undefined);
/** A definite answer about the session: a 401 or 403. No answer, a 429 or a 5xx says nothing about it. */
const definite = (e: unknown) => e instanceof Problem && (e.status === 401 || e.status === 403);

export function App() {
  const [me, setMeState] = useState<Me | null | undefined>(undefined);
  // A platform staff account with no membership is signed in for the console only (auth/me is 403 for it).
  const [staff, setStaffState] = useState<PlatformSession | null>(null);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState("");
  // Said once in the shell when this tab learned the session changed under it (an impersonation started or changed in
  // another tab), with why a change it tried wasn't made.
  const [sessionNotice, setSessionNotice] = useState("");
  // The session ended under a signed-in page (the dialog asks to sign in again over the page), and what the
  // sign-in page says when the shell had to leave the page for it.
  const [ended, setEnded] = useState(false);
  const [loginNotice, setLoginNotice] = useState("");
  // The first session check got no clear answer: when it tries again (seconds), null while it is trying.
  const [unreachable, setUnreachable] = useState<{ retryIn: number | null } | null>(null);
  const retryFirst = useRef<() => void>(() => undefined);
  const navigate = useNavigate();
  // Cached data belongs to one account in one organization. Drop it as the identity changes (sign-in, sign-out,
  // switching organization, starting or stopping an impersonation), before the new screens subscribe: an effect would
  // run after the page's queries started and make each fetch twice.
  const queryClient = useQueryClient();
  const identity = useRef<string | null>(null);
  // The impersonation this tab shows (and so says on every write, api.ts setTabImpersonation): null for none.
  const belief = useRef<string | null>(null);
  const setIdentity = (next: Me | null, nextStaff: PlatformSession | null) => {
    const key = next ? `${next.user.id}/${next.organization.id}/${next.impersonation ? `as:${next.impersonation.id}` : "own"}` : nextStaff ? `staff/${nextStaff.user.id}` : null;
    if (key !== identity.current) {
      queryClient.clear();
      setSessionNotice(""); // a notice is about the identity it was shown for
      setEnded(false);
    }
    if (key) setLoginNotice("");
    identity.current = key;
    belief.current = next?.impersonation?.id ?? null;
    setTabImpersonation(belief.current);
    setStaffState(nextStaff);
    setMeState(next);
  };
  const setMe = (next: Me | null) => setIdentity(next, null);
  const setStaff = (session: PlatformSession) => setIdentity(null, session);

  /** The session changed under this tab while it only read it (a reload, a poll, another tab's message): another
   *  impersonation started, replaced the one shown, or ended. The tab must not keep showing what it showed (a form filled
   *  for one identity would save as another), so it moves with the session and says why: home with the banner and a
   *  notice, or back to the console. Nothing was refused, so the notice doesn't say a change wasn't made. */
  function sessionMoved(next: Me | null, session: PlatformSession | null) {
    const was = belief.current;
    const shownOrganization = latest.current.me?.organization.id;
    if (next) setMe(next);
    else if (session) setStaff(session);
    else setMe(null);
    if (next?.impersonation) {
      setSessionNotice(was
        ? `This tab now shows the impersonation of ${next.user.name}, which replaced the one it showed (started in another tab of this browser).`
        : `Platform staff started impersonating ${next.user.name} in another tab of this browser, so this tab now shows that, with the banner.`);
      navigate(HOME, { replace: true });
    } else if (next || session) {
      navigate(shownOrganization ? `/platform/organizations/${shownOrganization}` : "/platform", { replace: true, state: { notice: "The impersonation ended in another tab of this browser." } satisfies ConsoleNotice });
    }
  }
  /** Take a `me` this tab only read (a passive reload): shown as it is when its impersonation is the one this tab
   *  believes in; otherwise the tab moves with it (sessionMoved). Every passive reload goes through here. */
  function takeMe(next: Me) {
    if ((next.impersonation?.id ?? null) === belief.current) setMe(next);
    else sessionMoved(next, null);
  }
  /** `auth/me`, or for a staff account with no membership (403, but signed in) `platform/session`. Three answers:
   *  signed in (`me`), signed out (a definite 401 or 403, or the staff console session), or couldn't tell (no answer, a
   *  429 or a 5xx, such as a 502 during a deploy), which keeps the current state: one failed check must never sign the
   *  screen out. `passive`: the tab only reads the session (a poll, another tab's message), so a different
   *  impersonation moves the tab (takeMe) instead of being taken silently; the deliberate callers (sign-in, Start,
   *  Stop, the checks below) handle what they find themselves. */
  /** `auth/me`, read once more after a fresh CSRF read when the first answer is 403 not-signed-in: a request that
   *  carried a session key rotated a moment before (another tab's Start or Stop) gets that answer, though this
   *  browser's cookie is fine by now (docs/API.md "Session key changes"). A second not-signed-in is the answer. */
  const readMe = async (): Promise<Me> => {
    try {
      return await api<Me>("auth/me");
    } catch (e) {
      if (!(e instanceof Problem && e.status === 403 && e.type === "/problems/not-signed-in")) throw e;
      await api("auth/csrf").catch(() => undefined);
      return api<Me>("auth/me");
    }
  };
  const refresh = async (passive = false): Promise<SessionCheck> => {
    try {
      const next = await readMe();
      if (passive) takeMe(next);
      else setMe(next);
      return { kind: "me", me: next };
    } catch (e) {
      if (!definite(e)) return { kind: "unknown", retryAfter: retryAfterOf(e) };
      if ((e as Problem).status === 403 && (e as Problem).type !== "/problems/not-signed-in") {
        try {
          const session = await api<PlatformSession>("platform/session");
          if (session.platform_staff) {
            if (passive && belief.current) sessionMoved(null, session);
            else setStaff(session);
            return { kind: "out" };
          }
        } catch (other) {
          if (!definite(other)) return { kind: "unknown", retryAfter: retryAfterOf(other) };
        }
      }
      setMe(null);
      return { kind: "out" };
    }
  };
  // On first load only a definite answer decides: signed in, or signed out (the sign-in page). No answer, a 429
  // or a 5xx (a deploy, a tripped rate limit) says nothing about the session, so the check is tried again after 1, 2,
  // 4 ... up to 30 seconds, saying so, instead of showing the sign-in page to someone who is signed in.
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    const run = async () => {
      clearTimeout(timer);
      setUnreachable((u) => (u ? { retryIn: null } : u));
      await api("auth/csrf").catch(() => undefined);
      const result = await refresh();
      if (!live) return;
      if (result.kind !== "unknown") return setUnreachable(null);
      // Never sooner than a 429's Retry-After; up to a quarter more at random, so tabs reloaded together (a deploy)
      // don't all ask again in the same second.
      const backoff = Math.max(Math.min(30, 2 ** attempt++), result.retryAfter ?? 0);
      const wait = Math.ceil(backoff * (1 + Math.random() * 0.25));
      setUnreachable({ retryIn: wait });
      timer = setTimeout(() => void run(), wait * 1000);
    };
    retryFirst.current = () => void run();
    void run();
    return () => { live = false; clearTimeout(timer); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /** Check whether the impersonation this tab shows has ended (stopped, timed out, ended by a request the server
   *  answered, or a write refused with impersonation-ended): reload the session, and only on a definite answer without
   *  an impersonation (the staff account's own session, its CSRF token changed) go back to the console with `notice`.
   *  Still the same one, or no clear answer: stay, with `me` (and the server clock) fresh. A different one: home, with a
   *  notice saying so (and, for a refused write, that the change wasn't made).
   *
   *  One check at a time. A periodic check (timer, focus) asked for while one runs shares it. A check that must see the
   *  state after its own change (`after`: Stop, a refused write) waits for the running one and then runs, and the
   *  `after` checks asked for while one waits share it. Your own Stop's words win over a refused write's. */
  const inFlight = useRef<Promise<void> | null>(null);
  const queuedAfter = useRef<Promise<void> | null>(null);
  const stopNotice = useRef<string | null>(null);
  type Check = { notice: string; organizationId?: string; after?: boolean; refused?: boolean };
  function runCheck({ notice, organizationId, refused }: Check) {
    const run = (async () => {
      const believed = belief.current;
      await api("auth/csrf").catch(() => undefined);
      const result = await refresh();
      if (result.kind === "unknown") return;
      if (result.kind === "me" && result.me.impersonation) {
        if (result.me.impersonation.id === believed) return; // still the same one: stay
        setSessionNotice(`This tab now shows the impersonation of ${result.me.user.name}, which replaced the one it showed (started in another tab of this browser).${refused ? " Your change wasn't made." : ""}`);
        navigate(HOME, { replace: true });
        return;
      }
      const said = stopNotice.current ?? notice;
      stopNotice.current = null;
      navigate(organizationId ? `/platform/organizations/${organizationId}` : "/platform", { replace: true, state: { notice: said } satisfies ConsoleNotice });
    })();
    inFlight.current = run;
    return run.finally(() => { if (inFlight.current === run) inFlight.current = null; });
  }
  function endedImpersonation(check: Check): Promise<void> {
    if (!check.after) return inFlight.current ?? runCheck(check);
    if (queuedAfter.current) return queuedAfter.current;
    const queued = (async () => {
      while (inFlight.current) await inFlight.current.catch(() => undefined);
      queuedAfter.current = null;
      await runCheck(check);
    })();
    queuedAfter.current = queued;
    return queued;
  }
  /** Other tabs of this browser share the session: tell them an impersonation started or stopped, and reload ours when
   *  they say so (Shell's own messages on the channel are about organization switches). */
  const channel = useRef<BroadcastChannel | null>(null);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(SESSION_CHANNEL);
    channel.current = ch;
    ch.onmessage = (e: MessageEvent<{ type?: string }>) => {
      if (e.data?.type !== "impersonation") return;
      const { me: current, endedImpersonation: ended, refresh: reload } = latest.current;
      // A tab showing an impersonation checks whether it is still the same one (and returns to the console if not);
      // any other tab just reloads its session.
      if (current?.impersonation) void ended({ notice: "The impersonation was stopped in another tab of this browser.", organizationId: current.organization.id, after: true });
      else void reload(true);
    };
    return () => { ch.close(); channel.current = null; };
  }, []);
  const tellOtherTabs = () => channel.current?.postMessage({ type: "impersonation" });
  async function stopImpersonating() {
    if (!me?.impersonation || stopping) return;
    const organizationId = me.organization.id;
    const name = me.user.name;
    setStopping(true);
    setStopError("");
    try {
      await sessionChange(() => api("platform/impersonation", { method: "DELETE", exclusive: true }));
    } catch (e) {
      // 409: it had already ended, so the staff session is back anyway. Anything else: still impersonating.
      if (!(e instanceof Problem && e.status === 409)) {
        setStopping(false);
        setStopError("Couldn't stop the impersonation. Try again.");
        return;
      }
    }
    tellOtherTabs();
    // Your own Stop's words, even if a refused write's check is the one that runs.
    stopNotice.current = `You stopped impersonating ${name}.`;
    await endedImpersonation({ notice: stopNotice.current, organizationId, after: true });
    setStopping(false);
  }
  // Problems the shell acts on whatever screen made the request (api.ts PROBLEM_EVENT).
  /** The session changed under this tab without an impersonation it knew of ending (409 impersonation-started): reload
   *  and say so. One at a time. */
  const resyncing = useRef(false);
  async function resync() {
    if (resyncing.current) return;
    resyncing.current = true;
    try {
      await api("auth/csrf").catch(() => undefined);
      const result = await refresh();
      if (result.kind !== "me") return;
      if (result.me.impersonation) {
        setSessionNotice(`Platform staff started impersonating ${result.me.user.name} in another tab of this browser, so this tab now shows that, with the banner. Your change wasn't made.`);
        navigate(HOME, { replace: true });
      } else {
        setSessionNotice("This browser's session changed in another tab, so your change wasn't made. Check the page and try again.");
      }
    } finally {
      resyncing.current = false;
    }
  }
  /** A request was refused as not signed in (403 not-signed-in, or a 401) while this tab shows a signed-in page.
   *  Ask the server once more after a fresh CSRF read: a request that carried a session key rotated a moment before
   *  gets that answer though the browser is signed in. A definite "signed out" keeps the page and asks to sign in again
   *  over it; an impersonation, or the staff console, goes to the sign-in page saying why, and comes back to this page
   *  after. No clear answer: nothing changes. One check at a time; a sign-in, sign-out or switch meanwhile wins. */
  const checkingSession = useRef(false);
  async function sessionRefused() {
    const { me: shown, staff: consoleSession, ended: asking } = latest.current;
    if (checkingSession.current || asking || (!shown && !consoleSession)) return;
    checkingSession.current = true;
    const before = identity.current;
    try {
      await api("auth/csrf").catch(() => undefined);
      let answer: unknown;
      try {
        answer = await api<Me | PlatformSession>(shown ? "auth/me" : "platform/session");
      } catch (e) {
        answer = e;
      }
      if (identity.current !== before) return; // a sign-in, sign-out or switch meanwhile
      if (shown && !(answer instanceof Problem) && answer && typeof answer === "object" && "organization" in answer) {
        // Signed in: as this page shows (a request that carried a just-rotated key), or as someone or somewhere else
        // (another tab of this browser signed in as another account, switched organization or impersonation). Then
        // the tab moves with the session (sessionMoved through a passive reload), and leaves a page whose form was
        // filled for the other identity, saying why.
        const now = answer as Me;
        const same = now.user.id === shown.user.id && now.organization.id === shown.organization.id && (now.impersonation?.id ?? null) === (shown.impersonation?.id ?? null);
        if (same) return;
        const otherUser = now.user.id !== shown.user.id;
        await latest.current.refresh(true);
        if (otherUser && !now.impersonation) {
          navigate(HOME, { replace: true });
          setSessionNotice(`This browser is now signed in as ${now.user.name} (signed in in another tab), so this tab shows their dashboard. Your change wasn't made.`);
        }
        return;
      }
      if (!definite(answer)) return; // no clear answer
      if ((answer as Problem).type !== NOT_SIGNED_IN) {
        // Signed in, but not as this page shows (the membership is gone): reload what the session is.
        void latest.current.refresh(true);
        return;
      }
      if (shown && !shown.impersonation) return setEnded(true);
      setMe(null);
      setLoginNotice(SESSION_ENDED_NOTICE);
    } finally {
      checkingSession.current = false;
    }
  }
  /** Signed in again from the dialog: back to the page's organization (a sign-in lands in the account's default one),
   *  then every query the page holds is read again, so what failed while signed out loads and a form keeps its values. */
  async function signedInAgain(next: Me) {
    const shown = latest.current.me;
    let now = next;
    if (shown && next.user.id === shown.user.id && next.organization.id !== shown.organization.id) {
      now = await api<Me>("auth/me/organization", { method: "POST", body: { organization_id: shown.organization.id } }).catch(() => next);
    }
    const same = !!shown && now.user.id === shown.user.id && now.organization.id === shown.organization.id;
    setMe(now);
    setEnded(false);
    if (same) await queryClient.invalidateQueries();
    else navigate(HOME, { replace: true }); // not where the page was: start from home (setMe cleared the cache)
  }
  const latest = useRef({ me, staff, ended, endedImpersonation, refresh, resync, sessionRefused });
  latest.current = { me, staff, ended, endedImpersonation, refresh, resync, sessionRefused };
  const reloading = useRef(false);
  useEffect(() => {
    const on = (e: Event) => {
      const problem = (e as CustomEvent<Problem>).detail;
      const { me: current, endedImpersonation: ended, refresh: reload } = latest.current;
      // endedImpersonation runs one check at a time; a burst of suspended answers reloads `me` once.
      if (problem.type === IMPERSONATION_ENDED && current?.impersonation) void ended({ notice: "Your impersonation ended before that change, so it wasn't made.", organizationId: current.organization.id, after: true, refused: true });
      // This tab didn't know an impersonation started (in another tab): reload, show what the session is now, and say
      // the change wasn't made.
      if (problem.type === IMPERSONATION_STARTED || (problem.type === IMPERSONATION_ENDED && !current?.impersonation)) void latest.current.resync();
      if (problem.type === NOT_SIGNED_IN || problem.status === 401) void latest.current.sessionRefused();
      if (problem.type === ORGANIZATION_SUSPENDED && current && !current.organization_suspended && !reloading.current) {
        reloading.current = true;
        void reload(true).finally(() => { reloading.current = false; });
      }
    };
    window.addEventListener(PROBLEM_EVENT, on);
    return () => window.removeEventListener(PROBLEM_EVENT, on);
  }, []);
  // While impersonating, re-check the session when the window gets focus and every 20 seconds: a GET can end an
  // impersonation on the server (its time limit) without this page being told, and the next write would then run as the
  // staff account.
  const impersonatedOrg = me?.impersonation ? me.organization.id : null;
  useEffect(() => {
    if (!impersonatedOrg) return;
    const check = () => void latest.current.endedImpersonation({ notice: "Your impersonation ended.", organizationId: impersonatedOrg });
    const t = setInterval(check, 20_000);
    window.addEventListener("focus", check);
    return () => { clearInterval(t); window.removeEventListener("focus", check); };
  }, [impersonatedOrg]);

  if (me === undefined) {
    return unreachable ? <Unreachable retryIn={unreachable.retryIn} onRetry={() => retryFirst.current()} /> : <p className="p-6" aria-live="polite">Loading Softwarden…</p>;
  }
  if (me?.must_change_password) return <ChangePassword me={me} onChanged={() => void refresh()} />;
  const banner = me?.impersonation
    ? <ImpersonationBanner me={me} onStop={() => void stopImpersonating()} stopping={stopping} stopError={stopError}
        onCheck={() => void endedImpersonation({ notice: `Your impersonation of ${me.user.name} reached its time limit and ended.`, organizationId: me.organization.id })} />
    : null;
  const signedOut = () => setMe(null);
  // The console: platform staff (with or without a membership), and anyone impersonating (who is told to stop first).
  const inConsole = !!(staff || me?.platform_staff || me?.impersonation);
  return (
    <>
    <Routes>
      <Route path="/login" element={me ? <Navigate to={HOME} replace /> : staff ? <Navigate to="/platform" replace /> : <Login onSignedIn={setMe} onStaffSignedIn={setStaff} notice={loginNotice} />} />
      {/* First-run setup is public: nobody can sign in before the first account exists. */}
      <Route path="/setup" element={<Suspense fallback={<p className="p-6" aria-live="polite">Loading setup…</p>}><Setup signedIn={!!me} onSignedIn={setMe} /></Suspense>} />
      {/* An invitation link is public too: the invitee may have no account yet, or a single sign-on session with no
          membership (auth/me answers 403 until they accept). */}
      <Route path="/invite" element={<Suspense fallback={<p className="p-6" aria-live="polite">Loading the invitation…</p>}><InvitePage onSignedIn={setMe} onSignedOut={signedOut} /></Suspense>} />
      {/* Public signup and its confirmation link (signed out; a signed-in visitor goes home). */}
      <Route path="/signup" element={me ? <Navigate to={HOME} replace /> : <Suspense fallback={<p className="p-6" aria-live="polite">Loading signup…</p>}><SignupPage /></Suspense>} />
      <Route path="/signup/verify" element={<VerifyRoute onSignedIn={setMe} />} />
      {/* Self-service password reset. Public, signed in or not: while impersonating the API refuses it, and the
          page says so. Using a link ends every session of the account, perhaps this tab's: read the session again. */}
      <Route path="/forgot-password" element={<Suspense fallback={<p className="p-6" aria-live="polite">Loading…</p>}><ForgotPasswordPage /></Suspense>} />
      <Route path="/reset-password" element={<ResetPasswordRoute onReset={async () => { await refresh(); }} />} />
      {/* The platform admin console, its own area outside the organization shell. */}
      <Route path="/platform/*" element={inConsole
        ? <Suspense fallback={<p className="p-6" aria-live="polite">Loading the platform console…</p>}>
            <PlatformConsole me={me} staff={staff} banner={banner} onSignedOut={signedOut}
              onStarted={async (id) => {
                // This tab is in the new impersonation from its answer on: its belief and the header it sends change
                // together, before `me` is reloaded (so a passive reload in between isn't taken as another tab's).
                belief.current = id;
                setTabImpersonation(id);
                await api("auth/csrf").catch(() => undefined);
                const result = await refresh();
                tellOtherTabs();
                if (result.kind === "me") navigate(HOME);
              }} />
          </Suspense>
        : me ? <Navigate to={HOME} replace /> : <RedirectToLogin />} />
      <Route path="*" element={me ? <Shell me={me} banner={banner} onSignedOut={signedOut} onMeChange={takeMe} sessionNotice={sessionNotice} onDismissSessionNotice={() => setSessionNotice("")} /> : staff ? <Navigate to="/platform" replace /> : <RedirectToLogin />} />
    </Routes>
    {/* Over the page, which stays mounted with whatever was typed. */}
    {ended && me && <SessionEndedDialog me={me} onSignedIn={signedInAgain} onSignOut={() => { forgetInvitation(); setMe(null); }} />}
    </>
  );
}

/** `/signup/verify#<token>`: the token is read here, before the page's code loads, and taken out of the address bar at
 *  once (out of history, bookmarks and screenshots); it is then only a prop, sent in the request body. */
function VerifyRoute({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => location.hash.replace(/^#/, ""));
  useEffect(() => {
    if (location.hash) navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <Suspense fallback={<p className="p-6" aria-live="polite">Loading…</p>}><VerifyPage token={token} onSignedIn={onSignedIn} /></Suspense>;
}

/** A password reset link's token, read when the app's code loads and taken out of the address bar at once, before
 *  the router starts and before the first session check (which can take seconds, or retry while the server is away):
 *  so the token is never in history, a bookmark or a screenshot of the loading page. */
const RESET_TOKEN = typeof window !== "undefined" && window.location.pathname === "/reset-password" ? window.location.hash.replace(/^#/, "") : "";
if (RESET_TOKEN) window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);

/** `/reset-password#<token>`: the token read at start-up, or (a link followed inside an open tab) from the
 *  fragment here, taken out of the address bar at once. */
function ResetPasswordRoute({ onReset }: { onReset: () => Promise<void> }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => location.hash.replace(/^#/, "") || RESET_TOKEN);
  useEffect(() => {
    if (location.hash) navigate({ pathname: location.pathname, search: location.search }, { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <Suspense fallback={<p className="p-6" aria-live="polite">Loading…</p>}><ResetPasswordPage token={token} onReset={onReset} /></Suspense>;
}

/** Password reset, loaded on first visit. */
type ResetModule = typeof import("@/features/auth/password-reset");
const ForgotPasswordPage = lazy(() => import("@/features/auth/password-reset").then((m) => ({ default: m.ForgotPasswordPage })).catch(() => ({ default: PageUnavailable })));
const ResetPasswordPage = lazy(() => import("@/features/auth/password-reset").then((m) => ({ default: m.ResetPasswordPage })).catch((): { default: ResetModule["ResetPasswordPage"] } => ({ default: PageUnavailable })));

/** Signup and its confirmation, loaded on first visit. */
type SignupModule = typeof import("@/features/auth/signup");
const SignupPage = lazy(() => import("@/features/auth/signup").then((m) => ({ default: m.SignupPage })).catch(() => ({ default: PageUnavailable })));
const VerifyPage = lazy(() => import("@/features/auth/signup").then((m) => ({ default: m.VerifyPage })).catch((): { default: SignupModule["VerifyPage"] } => ({ default: PageUnavailable })));
/** The platform admin console, loaded on first visit. */
type ConsoleModule = typeof import("@/features/platform/console");
const PlatformConsole = lazy(() => import("@/features/platform/console").then((m) => ({ default: m.PlatformConsole })).catch((): { default: ConsoleModule["PlatformConsole"] } => ({ default: PageUnavailable })));
/** Usage and limits (every role), loaded on first visit. */
const LimitsPage = lazy(() => import("@/features/admin/org-lifecycle").then((m) => ({ default: m.LimitsPage })).catch(() => ({ default: PageUnavailable })));

/** The invitation link's page, loaded when an invitee opens a link. */
const InvitePage = lazy(() => import("@/features/auth/invite").then((m) => ({ default: m.InvitePage })).catch(() => ({ default: PageUnavailable })));

/** Catalog reference pages (AI models and providers, the models-in-use report, software categories, tags) are loaded
 *  when first opened, so they stay out of the first bundle. */
type ReferenceModule = typeof import("@/features/catalog/reference");
/** Shown in place of a lazily loaded page whose code can't be loaded (offline, or the app was updated since this page
 *  loaded), so the shell stays usable. */
function PageUnavailable() {
  return (
    <>
      <PageHead title="This page isn't available" />
      <div className="max-w-prose rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
        <p className="font-semibold">Couldn't load this page. Reload the page to try again.</p>
        <p className="mt-1">Nothing you saved is affected; the rest of Softwarden still works.</p>
        <div className="mt-3"><Button type="button" variant="outline" onClick={() => window.location.reload()}>Reload the page</Button></div>
      </div>
    </>
  );
}
/** A page loaded on first visit; a chunk that can't be loaded shows PageUnavailable, so the shell stays usable. */
function lazyPage<P extends object>(load: () => Promise<React.ComponentType<P>>) {
  return lazy(() => load().then((C) => ({ default: C })).catch(() => ({ default: PageUnavailable as unknown as React.ComponentType<P> })));
}

/** Every page is loaded on first visit: only the shell, sign-in and the forced password change are in the first
 *  bundle, so a Viewer's first load doesn't carry the Admin screens and a change to one page can't push the initial JS
 *  over its budget. Modules several routes share (record-form, renewals, imports) load once and are cached. */
const NeedsAttention = lazyPage(() => import("@/features/dashboard/needs-attention").then((m) => m.NeedsAttention));
const Setup = lazyPage(() => import("@/features/setup/setup").then((m) => m.Setup));
const admin = () => import("@/features/admin/admin");
const PeopleAndTeams = lazyPage(() => admin().then((m) => m.PeopleAndTeams));
const VendorsPage = lazyPage(() => admin().then((m) => m.VendorsPage));
const UsersAndRoles = lazyPage(() => admin().then((m) => m.UsersAndRoles));
const OrganizationSettings = lazyPage(() => admin().then((m) => m.OrganizationSettings));
const Profile = lazyPage(() => admin().then((m) => m.Profile));
const DeliveryLog = lazyPage(() => admin().then((m) => m.DeliveryLog));
const AuditLog = lazyPage(() => import("@/features/admin/audit").then((m) => m.AuditLog));
const AuditEventPage = lazyPage(() => import("@/features/admin/audit").then((m) => m.AuditEventPage));
const SystemStatusPage = lazyPage(() => import("@/features/admin/system").then((m) => m.SystemStatusPage));
const ApiTokens = lazyPage(() => import("@/features/admin/tokens").then((m) => m.ApiTokens));
const AssessmentDetail = lazyPage(() => import("@/features/catalog/assessment-detail").then((m) => m.AssessmentDetail));
const AssessmentsList = lazyPage(() => import("@/features/catalog/assessments").then((m) => m.AssessmentsList));
const NewAssessment = lazyPage(() => import("@/features/catalog/assessments").then((m) => m.NewAssessment));
const Applications = lazyPage(() => import("@/features/catalog/catalog-list").then((m) => m.Applications));
const AgentsList = lazyPage(() => import("@/features/catalog/catalog-list").then((m) => m.AgentsList));
const detail = () => import("@/features/catalog/detail");
const ApplicationDetail = lazyPage(() => detail().then((m) => m.ApplicationDetail));
const ComponentDetail = lazyPage(() => detail().then((m) => m.ComponentDetail));
const AgentDetail = lazyPage(() => detail().then((m) => m.AgentDetail));
const Explorer = lazyPage(() => import("@/features/catalog/explorer").then((m) => m.Explorer));
const integrations = () => import("@/features/catalog/integrations");
const IntegrationsList = lazyPage(() => integrations().then((m) => m.IntegrationsList));
const NewIntegration = lazyPage(() => integrations().then((m) => m.NewIntegration));
const EditIntegration = lazyPage(() => integrations().then((m) => m.EditIntegration));
const IntegrationDetail = lazyPage(() => integrations().then((m) => m.IntegrationDetail));
const recordForm = () => import("@/features/catalog/record-form");
const NewRecord = lazyPage(() => recordForm().then((m) => m.NewRecord));
const NewComponentRedirect = lazyPage(() => recordForm().then((m) => m.NewComponentRedirect));
const EditApplication = lazyPage(() => recordForm().then((m) => m.EditApplication));
const EditComponent = lazyPage(() => recordForm().then((m) => m.EditComponent));
const NewAgent = lazyPage(() => recordForm().then((m) => m.NewAgent));
const EditAgent = lazyPage(() => recordForm().then((m) => m.EditAgent));
const DiscoveryPage = lazyPage(() => import("@/features/discovery/discovery").then((m) => m.DiscoveryPage));
const imports = () => import("@/features/imports/imports");
const Imports = lazyPage(() => imports().then((m) => m.Imports));
const ImportRun = lazyPage(() => imports().then((m) => m.ImportRun));
const ImportSources = lazyPage(() => imports().then((m) => m.ImportSources));
const RenewalDetail = lazyPage(() => import("@/features/money/renewal-detail").then((m) => m.RenewalDetail));
const renewals = () => import("@/features/money/renewals");
const RenewalsList = lazyPage(() => renewals().then((m) => m.RenewalsList));
const NewRenewal = lazyPage(() => renewals().then((m) => m.NewRenewal));
const EditRenewal = lazyPage(() => renewals().then((m) => m.EditRenewal));

const UNAVAILABLE = { AIModelsPage: PageUnavailable, AIProvidersPage: PageUnavailable, ModelsInUsePage: PageUnavailable, CategoriesPage: PageUnavailable, TagsPage: PageUnavailable };
const reference = () => import("@/features/catalog/reference").catch((): Pick<ReferenceModule, keyof typeof UNAVAILABLE> => UNAVAILABLE);
const AIModelsPage = lazy(() => reference().then((m) => ({ default: m.AIModelsPage })));
const AIProvidersPage = lazy(() => reference().then((m) => ({ default: m.AIProvidersPage })));
const ModelsInUsePage = lazy(() => reference().then((m) => ({ default: m.ModelsInUsePage })));
const CategoriesPage = lazy(() => reference().then((m) => ({ default: m.CategoriesPage })));
const TagsPage = lazy(() => reference().then((m) => ({ default: m.TagsPage })));

/** Custom field definitions: an Admin page every role can read, loaded on first use. */
const CustomFieldsPage = lazy(() => import("@/features/admin/custom-fields-admin")
  .then((m) => ({ default: m.CustomFieldsPage }))
  .catch(() => ({ default: PageUnavailable })));

/** Relationship types: an Admin page every role can read, loaded on first use. */
const RelationshipTypesPage = lazy(() => import("@/features/admin/relationship-types-admin")
  .then((m) => ({ default: m.RelationshipTypesPage }))
  .catch(() => ({ default: PageUnavailable })));

/** Pages, loaded on first use: search results and data quality (its tabs and exception dialog). */
const SearchPage = lazy(() => import("@/features/search/search-page").then((m) => ({ default: m.SearchPage })).catch(() => ({ default: PageUnavailable })));
const DataQualityPage = lazy(() => import("@/features/quality/data-quality-page").then((m) => ({ default: m.DataQualityPage })).catch(() => ({ default: PageUnavailable })));
/** The dashboard (every block, Recharts) and the Reports page (spend, renewals calendar and its feed,
 *  licence utilisation; no charts), each its own chunk. */
const DashboardPage = lazy(() => import("@/features/reports/dashboard").then((m) => ({ default: m.DashboardPage })).catch(() => ({ default: PageUnavailable })));
const ReportsPage = lazy(() => import("@/features/reports/reports").then((m) => ({ default: m.ReportsPage })).catch(() => ({ default: PageUnavailable })));

/** The vendor page (details, the vendor risk file, attachments) and the vendor risk overview and documents
 *  register, each loaded on first use. */
const VendorDetailPage = lazy(() => import("@/features/vendors/vendor-detail").then((m) => ({ default: m.VendorDetailPage })).catch(() => ({ default: PageUnavailable })));
const VendorRiskOverviewPage = lazy(() => import("@/features/vendors/risk-overview").then((m) => ({ default: m.VendorRiskOverviewPage })).catch(() => ({ default: PageUnavailable })));
const VendorRiskDocumentsPage = lazy(() => import("@/features/vendors/risk-overview").then((m) => ({ default: m.VendorRiskDocumentsPage })).catch(() => ({ default: PageUnavailable })));
/** The end-of-life calendar and the data-flow map (its graph loads Cytoscape in that chunk of its own), each
 *  loaded on first use. */
const EndOfLifePage = lazy(() => import("@/features/eol/eol-calendar").then((m) => ({ default: m.EndOfLifePage })).catch(() => ({ default: PageUnavailable })));
const DataFlowsPage = lazy(() => import("@/features/flows/data-flows").then((m) => ({ default: m.DataFlowsPage })).catch(() => ({ default: PageUnavailable })));

/** Contracts and licence pools, loaded on first use: the list and form, the contract page, the renew
 *  page, and the licence pool pages with the seat review. Only with the `contracts` capability. */
const contracts = () => import("@/features/money/contracts");
const ContractsList = lazy(() => contracts().then((m) => ({ default: m.ContractsList })).catch(() => ({ default: PageUnavailable })));
const NewContract = lazy(() => contracts().then((m) => ({ default: m.NewContract })).catch(() => ({ default: PageUnavailable })));
const EditContract = lazy(() => contracts().then((m) => ({ default: m.EditContract })).catch(() => ({ default: PageUnavailable })));
const ContractDetailPage = lazy(() => import("@/features/money/contract-detail").then((m) => ({ default: m.ContractDetailPage })).catch(() => ({ default: PageUnavailable })));
const RenewContract = lazy(() => import("@/features/money/contract-renew").then((m) => ({ default: m.RenewContract })).catch(() => ({ default: PageUnavailable })));
const pools = () => import("@/features/money/license-pools");
const LicensePoolsList = lazy(() => pools().then((m) => ({ default: m.LicensePoolsList })).catch(() => ({ default: PageUnavailable })));
const LicensePoolDetail = lazy(() => pools().then((m) => ({ default: m.LicensePoolDetail })).catch(() => ({ default: PageUnavailable })));
const NewLicensePool = lazy(() => pools().then((m) => ({ default: m.NewLicensePool })).catch(() => ({ default: PageUnavailable })));
const EditLicensePool = lazy(() => pools().then((m) => ({ default: m.EditLicensePool })).catch(() => ({ default: PageUnavailable })));

/** Notification rules, channels and the notification log: Admin pages, each its own chunk. */
const NotificationRulesPage = lazy(() => import("@/features/notify/rules-admin").then((m) => ({ default: m.NotificationRulesPage })).catch(() => ({ default: PageUnavailable })));
const NewNotificationRulePage = lazy(() => import("@/features/notify/rules-admin").then((m) => ({ default: m.NewNotificationRulePage })).catch(() => ({ default: PageUnavailable })));
const NotificationRulePage = lazy(() => import("@/features/notify/rules-admin").then((m) => ({ default: m.NotificationRulePage })).catch(() => ({ default: PageUnavailable })));
const NotificationChannelsPage = lazy(() => import("@/features/notify/channels-admin").then((m) => ({ default: m.NotificationChannelsPage })).catch(() => ({ default: PageUnavailable })));
const NotificationLogPage = lazy(() => import("@/features/notify/log").then((m) => ({ default: m.NotificationLogPage })).catch(() => ({ default: PageUnavailable })));
const DeliveryPage = lazy(() => import("@/features/notify/log").then((m) => ({ default: m.DeliveryPage })).catch(() => ({ default: PageUnavailable })));

/** Webhooks and their delivery log (Admin only, capability `webhooks`), and the API reference (Scalar, its
 *  own chunk again inside the page) and guide (every role): each its own chunk on first use. */
const WebhooksPage = lazy(() => import("@/features/notify/webhooks-admin").then((m) => ({ default: m.WebhooksPage })).catch(() => ({ default: PageUnavailable })));
const WebhookLogPage = lazy(() => import("@/features/notify/webhook-log").then((m) => ({ default: m.WebhookLogPage })).catch(() => ({ default: PageUnavailable })));
const WebhookDeliveryPage = lazy(() => import("@/features/notify/webhook-log").then((m) => ({ default: m.WebhookDeliveryPage })).catch(() => ({ default: PageUnavailable })));
const ApiReferencePage = lazy(() => import("@/features/api-docs/api-docs").then((m) => ({ default: m.ApiReferencePage })).catch(() => ({ default: PageUnavailable })));
const ApiGuidePage = lazy(() => import("@/features/api-docs/api-docs").then((m) => ({ default: m.ApiGuidePage })).catch(() => ({ default: PageUnavailable })));

/** Discovery set-up and operation, each its own chunk: connections, the guided walkthrough, connector
 *  settings, run history and the permissions reference (docs/PERMISSIONS.md, bundled with its page). */
const SourcePickerPage = lazy(() => import("@/features/discovery/setup").then((m) => ({ default: m.SourcePickerPage })).catch(() => ({ default: PageUnavailable })));
const WalkthroughPage = lazy(() => import("@/features/discovery/setup").then((m) => ({ default: m.WalkthroughPage })).catch(() => ({ default: PageUnavailable })));
const ConnectionsPage = lazy(() => import("@/features/discovery/connections").then((m) => ({ default: m.ConnectionsPage })).catch(() => ({ default: PageUnavailable })));
const ConnectorSettingsPage = lazy(() => import("@/features/discovery/connector-settings").then((m) => ({ default: m.ConnectorSettingsPage })).catch(() => ({ default: PageUnavailable })));
const RunHistoryPage = lazy(() => import("@/features/discovery/runs").then((m) => ({ default: m.RunHistoryPage })).catch(() => ({ default: PageUnavailable })));
const RunDetailPage = lazy(() => import("@/features/discovery/runs").then((m) => ({ default: m.RunDetailPage })).catch(() => ({ default: PageUnavailable })));
const OldRunDetailRedirect = lazy(() => import("@/features/discovery/runs").then((m) => ({ default: m.OldRunDetailRedirect })).catch(() => ({ default: PageUnavailable })));
/** The assistant's settings and usage log (Admin only, capability `assistant`), one chunk on first use. */
const AssistantSettingsPage = lazy(() => import("@/features/discovery/assistant-admin").then((m) => ({ default: m.AssistantSettingsPage })).catch(() => ({ default: PageUnavailable })));
const AssistantUsagePage = lazy(() => import("@/features/discovery/assistant-admin").then((m) => ({ default: m.AssistantUsagePage })).catch(() => ({ default: PageUnavailable })));
/** Single sign-on settings (Admin only, capability `sso.oidc`): draft, test sign-in, activate. */
const SsoSettingsPage = lazy(() => import("@/features/admin/sso-settings").then((m) => ({ default: m.SsoSettingsPage })).catch(() => ({ default: PageUnavailable })));
/** SCIM provisioning (Admin only, capability `scim`, from the optional `ee` package): tokens, roles, what was provisioned. */
const ProvisioningPage = lazy(() => import("@/features/admin/provisioning").then((m) => ({ default: m.ProvisioningPage })).catch(() => ({ default: PageUnavailable })));
const PermissionsReferencePage = lazy(() => import("@/features/discovery/permissions-doc").then((m) => ({ default: m.PermissionsReferencePage })).catch(() => ({ default: PageUnavailable })));
/** Shadow IT (capability `discovery.entra`, every role reads), its own chunk. */
const ShadowItPage = lazy(() => import("@/features/discovery/shadow-it").then((m) => ({ default: m.ShadowItPage })).catch(() => ({ default: PageUnavailable })));
const SsoCoveragePage = lazy(() => import("@/features/discovery/sso-coverage").then((m) => ({ default: m.SsoCoveragePage })).catch(() => ({ default: PageUnavailable })));
/** Offboarding: a person's footprint and reassign flow, and the access review, each its own chunk. */
const PersonPage = lazy(() => import("@/features/offboarding/person-page").then((m) => ({ default: m.PersonPage })).catch(() => ({ default: PageUnavailable })));
/** Catalog as code (Admin only, capability `catalog_code`): export, plan and apply, its own chunk. */
const CatalogCodePage = lazy(() => import("@/features/admin/catalog-code").then((m) => ({ default: m.CatalogCodePage })).catch(() => ({ default: PageUnavailable })));
/** Ticket connections (Admin only, capability `ticket_links`), its own chunk. */
const TicketConnectionsPage = lazy(() => import("@/features/tickets/ticket-connections").then((m) => ({ default: m.TicketConnectionsPage })).catch(() => ({ default: PageUnavailable })));
const AccessReviewPage = lazy(() => import("@/features/offboarding/access-review").then((m) => ({ default: m.AccessReviewPage })).catch(() => ({ default: PageUnavailable })));

/** The Admin banner's words for webhook endpoints Softwarden turned off (`auth/me.webhook_problem`): one endpoint by
 *  name with its reason, or how many, each sentence ending once. */
function webhookProblemText(p: NonNullable<Me["webhook_problem"]>) {
  const sentence = (s: string) => (/[.!?]$/.test(s.trim()) ? s.trim() : `${s.trim()}.`);
  if (p.disabled === 1) {
    const one = p.endpoints[0];
    const head = `Softwarden turned off the webhook ${one?.name ?? ""}`.trim();
    return `${one?.reason ? `${head}: ${sentence(one.reason)}` : `${head}.`} Its events aren't sent until it is turned on again.`;
  }
  return `Softwarden turned off ${p.disabled} webhooks. Their events aren't sent until they are turned on again.`;
}

/** A lazily loaded page: a polite loading line while its code arrives. */
function LazyPage({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<p className="text-muted-foreground" aria-live="polite">Loading page…</p>}>{children}</Suspense>;
}

/** Focus a page's heading once that page is shown: `focus(path, heading)` waits (up to ten seconds) until the address is
 *  `path` and a `main h1` reading `heading` is in the page, so a lazily loaded chunk (the Dashboard after an organisation
 *  switch, which shows "Loading page…" first) gets focus when it arrives, and the previous page's heading never does.
 *  It gives up as soon as the address is anything else. A later call, or the shell unmounting, cancels a pending one. */
function useHeadingFocus() {
  const pending = useRef<(() => void) | null>(null);
  useEffect(() => () => pending.current?.(), []);
  return (path: string, heading: string) => {
    pending.current?.();
    const timers: ReturnType<typeof setTimeout>[] = [];
    const observer = new MutationObserver(() => attempt());
    const cancel = () => {
      observer.disconnect();
      timers.forEach(clearTimeout);
      if (pending.current === cancel) pending.current = null;
    };
    function attempt() {
      if (window.location.pathname !== path) return cancel();
      // The heading reads `heading`, or starts with it (a record's h1 can carry a chip after its name).
      const h1 = [...document.querySelectorAll<HTMLElement>("main h1")].find((h) => h.textContent?.trim().startsWith(heading));
      if (!h1) return;
      cancel();
      h1.focus();
    }
    pending.current = cancel;
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    // One check in case the page is already in place and nothing changes; give up after ten seconds.
    timers.push(setTimeout(attempt), setTimeout(cancel, 10_000));
  };
}

/** The command palette is loaded on first use, so cmdk stays out of the first bundle. */
type PaletteModule = typeof import("@/components/command-palette");
type PaletteProps = React.ComponentProps<PaletteModule["default"]>;

/** Shown instead of the palette when its chunk can't be loaded (offline, or the app was updated since this page loaded). */
function PaletteUnavailable({ open, onOpenChange }: PaletteProps) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Search isn't available</DialogTitle>
          <DialogDescription>Couldn't load search. Reload the page to try again; nothing you were doing is lost by closing this.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          <Button type="button" onClick={() => window.location.reload()}>Reload the page</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The command palette is loaded on first use, so cmdk stays out of the first bundle. A failed load shows why. */
const CommandPalette = lazy(() => import("@/components/command-palette").catch((): { default: PaletteModule["default"] } => ({ default: PaletteUnavailable })));

/** Tabs of one browser tell each other about an organisation switch: the session (and so every tab) is now in the new
 *  organisation, and a tab still showing the old one would mix them. */
const SESSION_CHANNEL = "softwarden.session";
type SessionMessage = { type: "organization"; userId: number; organizationId: string; name: string };

function Shell({ me, banner, onSignedOut, onMeChange, sessionNotice, onDismissSessionNotice }: {
  me: Me; banner: React.ReactNode; onSignedOut: () => void; onMeChange: (me: Me) => void;
  /** App's word that the session changed under this tab (an impersonation started or changed in another tab). */
  sessionNotice: string; onDismissSessionNotice: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const focusHeading = useHeadingFocus();
  // The API counts every queue (ownership gaps, reviews, links to verify, stale assessments, renewals, discovery, data
  // quality) for each scope, so the badge and the page always agree. Same query key as the page's Assigned to me scope, so they share one request.
  const mine = useApiQuery<{ counts: { mine: number } }>("needs-attention?scope=mine", { enabled: !me.organization_suspended });
  const badge = mine.data?.counts?.mine ?? 0;
  const isAdmin = me.role === "admin";
  const hasContracts = me.capabilities.includes("contracts");
  const [rail, setRail] = usePref<boolean>(prefKey(me.user.id, "sidebar.rail"), false);
  const [palette, setPalette] = useState<{ open: boolean; search: string; loaded: boolean }>({ open: false, search: "", loaded: false });
  const [help, setHelp] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [status, setStatus] = useState("");
  const [switchError, setSwitchError] = useState("");
  const [tabNotice, setTabNotice] = useState("");
  // Cleared first, so the same message said twice is announced twice.
  const announce = (text: string) => { setStatus(""); setTimeout(() => setStatus(text), 60); };
  // App's session notice is said through the announcer, once per notice.
  useEffect(() => { if (sessionNotice) announce(sessionNotice); }, [sessionNotice]);
  // A form that saved navigates with `savedState(name)` (WCAG 4.1.3 and 2.4.3): say "Saved <name>." and put focus
  // on the page's heading, not the document. The flag is then dropped from the history entry, so a reload or Back
  // doesn't say it again; the rest of the state (a page's visible notice) stays.
  const location = useLocation();
  const saved = (location.state as SavedState | null)?.saved;
  useEffect(() => {
    if (!saved) return;
    // One announcement: "Saved <name>." and the page's note when it says more than that; the page shows its note
    // without saying it again (useLandingNotice).
    const note = (location.state as SavedState).notice;
    announce(`Saved ${saved}.${note && note !== `${saved} saved.` ? ` ${note}` : ""}`);
    focusHeading(location.pathname, saved);
    const { saved: _drop, ...rest } = location.state as SavedState;
    // `announced` stays, so a page that mounts again on this entry still shows its note without saying it.
    navigate({ pathname: location.pathname, search: location.search, hash: location.hash }, { replace: true, state: { ...rest, announced: true } });
  }, [location.key]); // eslint-disable-line react-hooks/exhaustive-deps
  /** A dismissed notice's button goes away: focus moves to the page's heading (or main), not to the document. */
  const focusPage = () => {
    const target = document.querySelector<HTMLElement>("main h1") ?? document.getElementById("main");
    target?.focus();
  };
  // Another tab switched the organisation: reload `me` (App clears the cache) and say so.
  const latest = useRef({ me, onMeChange });
  latest.current = { me, onMeChange };
  const channel = useRef<BroadcastChannel | null>(null);
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(SESSION_CHANNEL);
    channel.current = ch;
    ch.onmessage = (e: MessageEvent<SessionMessage>) => {
      const { me: current, onMeChange: change } = latest.current;
      if (e.data?.type !== "organization" || e.data.userId !== current.user.id || e.data.organizationId === current.organization.id) return;
      api<Me>("auth/me").then((next) => {
        change(next);
        const text = `This tab switched to ${next.organization.name}, because you switched organization in another tab.`;
        setTabNotice(text);
        announce(text);
      }).catch(() => undefined);
    };
    return () => { ch.close(); channel.current = null; };
  }, []);
  const reloadMe = () => api<Me>("auth/me").then(onMeChange).catch(() => undefined);
  const refreshMe = () => { void reloadMe(); };
  // An Admin's deletion banner shows the final export: reload `me` while it is still being made, so it turns into the
  // download (or says it failed) without a page reload.
  const finalExport = me.role === "admin" ? me.organization_deletion?.final_export?.status : undefined;
  useEffect(() => {
    if (finalExport !== "queued" && finalExport !== "running") return;
    const t = setInterval(() => { void reloadMe(); }, 15_000);
    return () => clearInterval(t);
  }, [finalExport]); // eslint-disable-line react-hooks/exhaustive-deps
  const openPalette = (search = "") => setPalette({ open: true, search, loaded: true });
  const closePalette = () => setPalette((p) => ({ ...p, open: false }));
  useShortcuts({ me, paletteOpen: palette.open, openPalette, closePalette, openHelp: () => setHelp(true), toggleRail: () => setRail(!rail), announce });

  async function signOut() {
    await signOutRequest().catch(() => undefined);
    // Nothing the previous account loaded may show to the next one, including an invitation token kept in this tab.
    queryClient.clear();
    forgetInvitation();
    onSignedOut();
  }
  /** Switch the session's organisation (audited by the API), then reload `me` through App, which clears every cached
   *  query before the new organisation's screens load, and land on its dashboard (everyone's home). */
  async function switchOrganization(org: Membership) {
    setSwitching(true);
    setSwitchError("");
    try {
      const next = await api<Me>("auth/me/organization", { method: "POST", body: { organization_id: org.id } });
      onMeChange(next);
      setTabNotice("");
      channel.current?.postMessage({ type: "organization", userId: next.user.id, organizationId: next.organization.id, name: next.organization.name } satisfies SessionMessage);
      navigate(HOME);
      announce(`Switched to ${next.organization.name}.`);
      focusHeading(HOME, "Dashboard");
    } catch (e) {
      setSwitchError(e instanceof Problem ? `Couldn't switch to ${org.name}: ${e.detail}` : `Couldn't switch to ${org.name}. You're still in ${me.organization.name}. Try again.`);
    } finally {
      setSwitching(false);
    }
  }
  return (
    <MeContext.Provider value={me}><MeRefreshContext.Provider value={refreshMe}>
      <a className="absolute -top-12 left-2 z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:top-2" href="#main">Skip to main content</a>
      {banner}
      {/* A live region, not role=status: pages keep their own status messages (one per page). */}
      <p className="sr-only" aria-live="polite" data-testid="shell-announcer">{status}</p>
      <div className={`grid min-h-screen ${rail ? "lg:grid-cols-[var(--rail-width)_minmax(0,1fr)]" : "lg:grid-cols-[var(--sidebar-width)_minmax(0,1fr)]"}`}>
        <Sidebar me={me} rail={rail} onToggleRail={() => setRail(!rail)} badge={badge} onSwitchOrganization={(o) => void switchOrganization(o)} switching={switching}
          onShowShortcuts={() => setHelp(true)} onSignOut={() => void signOut()} onProfile={() => navigate("/profile")} onApiDocs={() => navigate("/admin/api")}
          onLimits={() => navigate("/admin/limits")} onPlatform={me.platform_staff && !me.impersonation ? () => navigate("/platform") : undefined} />
        <div className="min-w-0">
          <header className="flex flex-wrap items-center gap-3 border-b bg-card px-4 py-2.5 sm:px-6">
            <span className="font-semibold">{me.organization.name}</span>
            <button type="button" onClick={() => openPalette()} aria-keyshortcuts="Control+K Meta+K"
              className="ml-auto inline-flex h-8 min-w-48 items-center gap-2 rounded-md border border-input bg-background px-2.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground">
              <SearchIcon aria-hidden="true" className="size-4" />
              <span className="flex-1 text-left">Search or jump to…</span>
              <kbd aria-hidden="true" className="rounded-sm border border-border-strong bg-muted px-1 font-mono text-[11px] text-foreground">{PALETTE_KEYS.join(IS_MAC ? "" : " ")}</kbd>
            </button>
            <ThemeMenu />
          </header>
          {switchError && <div className="border-b px-4 pt-3 sm:px-6"><ErrorBanner message={switchError} /></div>}
          {sessionNotice && (
            <div className="flex flex-wrap items-center gap-3 border-b border-l-4 border-info-border bg-info-bg px-4 py-2 sm:px-6" data-testid="session-notice">
              <span aria-hidden="true">ⓘ</span><span>{sessionNotice}</span>
              <Button size="sm" variant="outline" className="ml-auto" onClick={() => { onDismissSessionNotice(); focusPage(); }}>Dismiss<span className="sr-only"> this notice</span></Button>
            </div>
          )}
          {tabNotice && (
            <div className="flex flex-wrap items-center gap-3 border-b border-l-4 border-info-border bg-info-bg px-4 py-2 sm:px-6" data-testid="tab-notice">
              <span aria-hidden="true">ⓘ</span><span>{tabNotice}</span>
              <Button size="sm" variant="outline" className="ml-auto" onClick={() => { setTabNotice(""); focusPage(); }}>Dismiss</Button>
            </div>
          )}
          {me.digest_problem && (
            // Admins only: a failed digest must never be silent. Actions stay in Needs attention either way.
            <div className="flex flex-wrap items-center gap-3 border-b border-l-4 border-error-border bg-error-bg px-4 py-2 sm:px-6" role="status">
              <span><span aria-hidden="true">! </span>Digest email failed for {me.digest_problem.failed} {me.digest_problem.failed === 1 ? "user" : "users"} on {me.digest_problem.for_date}. Every item is still in Needs attention.</span>
              <Link to="/admin/digests?status=failed" className="font-semibold">View delivery log</Link>
            </div>
          )}
          {me.organization_deletion && <DeletionBanner me={me} />}
          {me.webhook_problem && (
            // Admins only: an endpoint Softwarden turned off must never be silent; its events aren't sent.
            <div className="flex flex-wrap items-center gap-3 border-b border-l-4 border-error-border bg-error-bg px-4 py-2 sm:px-6" role="status" data-testid="webhook-problem">
              <span><span aria-hidden="true">! </span>{webhookProblemText(me.webhook_problem)}</span>
              <Link to="/admin/webhooks" className="font-semibold">Review webhooks</Link>
            </div>
          )}
          <main id="main" tabIndex={-1} className="w-full min-w-0 p-4 sm:p-6">
            {/* Keyed by organisation: switching remounts every page, so nothing from the previous one stays on screen.
                A suspended organization shows why instead of any page: the API refuses them all. */}
            {me.organization_suspended ? <SuspendedPanel me={me} onSignOut={() => void signOut()} onCheck={reloadMe} /> : <PageErrorBoundary><LazyPage><Routes key={me.organization.id}>
              <Route element={<UuidGuard back={{ to: "/", label: "Needs attention" }} />}>
              <Route path="/" element={<NeedsAttention />} />
              <Route path="/dashboard" element={<LazyPage><DashboardPage /></LazyPage>} />
              <Route path="/reports" element={<LazyPage><ReportsPage /></LazyPage>} />
              <Route path="/data-quality" element={<LazyPage><DataQualityPage /></LazyPage>} />
              <Route path="/search" element={<LazyPage><SearchPage /></LazyPage>} />
              <Route path="/applications" element={<Applications />} />
              <Route path="/applications/new" element={<NewRecord />} />
              <Route path="/components/new" element={<NewComponentRedirect />} />
              <Route path="/applications/:id/edit" element={<EditApplication />} />
              <Route path="/components/:id/edit" element={<EditComponent />} />
              <Route path="/applications/:id" element={<ApplicationDetail />} />
              <Route path="/components/:id" element={<ComponentDetail />} />
              <Route path="/ai-agents" element={<AgentsList />} />
              <Route path="/ai-agents/new" element={<NewAgent />} />
              <Route path="/ai-agents/:id/edit" element={<EditAgent />} />
              <Route path="/ai-agents/:id" element={<AgentDetail />} />
              <Route path="/ai-models" element={<LazyPage><AIModelsPage /></LazyPage>} />
              <Route path="/ai-models/in-use" element={<LazyPage><ModelsInUsePage /></LazyPage>} />
              <Route path="/ai-providers" element={<LazyPage><AIProvidersPage /></LazyPage>} />
              <Route path="/categories" element={<LazyPage><CategoriesPage /></LazyPage>} />
              <Route path="/tags" element={<LazyPage><TagsPage /></LazyPage>} />
              <Route path="/custom-fields" element={<LazyPage><CustomFieldsPage /></LazyPage>} />
              <Route path="/relationship-types" element={<LazyPage><RelationshipTypesPage /></LazyPage>} />
              <Route path="/integrations" element={<IntegrationsList />} />
              <Route path="/integrations/new" element={<NewIntegration />} />
              <Route path="/integrations/:id/edit" element={<EditIntegration />} />
              <Route path="/integrations/:id" element={<IntegrationDetail />} />
              <Route path="/dependencies" element={<Explorer />} />
              <Route path="/vendors" element={<VendorsPage />} />
              <Route path="/vendors/:id" element={<LazyPage><VendorDetailPage /></LazyPage>} />
              <Route path="/vendor-risk" element={<LazyPage><VendorRiskOverviewPage /></LazyPage>} />
              <Route path="/vendor-risk/documents" element={<LazyPage><VendorRiskDocumentsPage /></LazyPage>} />
              <Route path="/end-of-life" element={<LazyPage><EndOfLifePage /></LazyPage>} />
              <Route path="/data-flows" element={<LazyPage><DataFlowsPage /></LazyPage>} />
              <Route path="/assessments" element={<AssessmentsList />} />
              <Route path="/assessments/new" element={<NewAssessment />} />
              <Route path="/assessments/:id" element={<AssessmentDetail />} />
              <Route path="/renewals" element={<RenewalsList />} />
              <Route path="/renewals/new" element={<NewRenewal />} />
              <Route path="/renewals/:id/edit" element={<EditRenewal />} />
              <Route path="/renewals/:id" element={<RenewalDetail />} />
              {hasContracts && <>
                <Route path="/contracts" element={<LazyPage><ContractsList /></LazyPage>} />
                <Route path="/contracts/new" element={<LazyPage><NewContract /></LazyPage>} />
                <Route path="/contracts/:id/edit" element={<LazyPage><EditContract /></LazyPage>} />
                <Route path="/contracts/:id/renew" element={<LazyPage><RenewContract /></LazyPage>} />
                <Route path="/contracts/:id" element={<LazyPage><ContractDetailPage /></LazyPage>} />
                <Route path="/license-pools" element={<LazyPage><LicensePoolsList /></LazyPage>} />
                <Route path="/license-pools/new" element={<LazyPage><NewLicensePool /></LazyPage>} />
                <Route path="/license-pools/:id/edit" element={<LazyPage><EditLicensePool /></LazyPage>} />
                <Route path="/license-pools/:id" element={<LazyPage><LicensePoolDetail /></LazyPage>} />
              </>}
              <Route path="/discovery" element={<DiscoveryPage />} />
              {/* The old Add connector address opens the guided setup. Admin-only pages explain the refusal to other roles. */}
              <Route path="/discovery/connectors/new" element={<Navigate to="/discovery/setup" replace />} />
              <Route path="/discovery/connectors/:id" element={<LazyPage><ConnectorSettingsPage /></LazyPage>} />
              <Route path="/discovery/setup" element={<LazyPage><SourcePickerPage /></LazyPage>} />
              <Route path="/discovery/setup/:source" element={<LazyPage><WalkthroughPage /></LazyPage>} />
              <Route path="/discovery/connections" element={<LazyPage><ConnectionsPage /></LazyPage>} />
              <Route path="/discovery/runs" element={<LazyPage><RunHistoryPage /></LazyPage>} />
              <Route path="/discovery/runs/:runId" element={<LazyPage><RunDetailPage /></LazyPage>} />
              <Route path="/discovery/runs/:connectorId/:runId" element={<LazyPage><OldRunDetailRedirect /></LazyPage>} />
              <Route path="/discovery/permissions" element={<LazyPage><PermissionsReferencePage /></LazyPage>} />
              <Route path="/discovery/shadow-it" element={<LazyPage><ShadowItPage /></LazyPage>} />
              <Route path="/discovery/sso-coverage" element={<LazyPage><SsoCoveragePage /></LazyPage>} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/admin/people" element={<PeopleAndTeams />} />
              <Route path="/people/:id" element={<LazyPage><PersonPage /></LazyPage>} />
              <Route path="/access-review" element={<LazyPage><AccessReviewPage /></LazyPage>} />
              {isAdmin && <Route path="/admin/users" element={<UsersAndRoles />} />}
              {isAdmin && <Route path="/admin/organization" element={<OrganizationSettings />} />}
              {/* Every role reads the organization's limits and usage. */}
              <Route path="/admin/limits" element={<LazyPage><LimitsPage /></LazyPage>} />
              {/* Admin only; other roles see who may open it (AdminOnly). The test sign-in comes back here. */}
              <Route path="/admin/sign-in" element={<LazyPage><SsoSettingsPage /></LazyPage>} />
              <Route path="/admin/provisioning" element={<LazyPage><ProvisioningPage /></LazyPage>} />
              <Route path="/admin/catalog-code" element={<LazyPage><CatalogCodePage /></LazyPage>} />
              <Route path="/admin/ticket-connections" element={<LazyPage><TicketConnectionsPage /></LazyPage>} />
              {/* Admin-only pages that explain the refusal to other roles (a shared link) rather than "Page not found". */}
              <Route path="/admin/audit" element={<AuditLog />} />
              <Route path="/admin/audit/:eventId" element={<AuditEventPage />} />
              <Route path="/admin/system" element={<SystemStatusPage />} />
              {/* Admin only; other roles see who may open them (AdminOnly). */}
              <Route path="/admin/assistant" element={<LazyPage><AssistantSettingsPage /></LazyPage>} />
              <Route path="/admin/assistant/usage" element={<LazyPage><AssistantUsagePage /></LazyPage>} />
              {isAdmin && <Route path="/admin/digests" element={<DeliveryLog />} />}
              {/* Admin only; other roles see who may open them (AdminOnly), as with the audit log. */}
              <Route path="/admin/notification-rules" element={<LazyPage><NotificationRulesPage /></LazyPage>} />
              <Route path="/admin/notification-rules/new" element={<LazyPage><NewNotificationRulePage /></LazyPage>} />
              <Route path="/admin/notification-rules/:id" element={<LazyPage><NotificationRulePage /></LazyPage>} />
              <Route path="/admin/notification-channels" element={<LazyPage><NotificationChannelsPage /></LazyPage>} />
              <Route path="/admin/notification-log" element={<LazyPage><NotificationLogPage /></LazyPage>} />
              <Route path="/admin/notification-log/:id" element={<LazyPage><DeliveryPage /></LazyPage>} />
              {/* Admin only; other roles see who may open it (AdminOnly). */}
              <Route path="/admin/tokens" element={<ApiTokens />} />
              <Route path="/admin/webhooks" element={<LazyPage><WebhooksPage /></LazyPage>} />
              <Route path="/admin/webhooks/deliveries" element={<LazyPage><WebhookLogPage /></LazyPage>} />
              <Route path="/admin/webhooks/deliveries/:id" element={<LazyPage><WebhookDeliveryPage /></LazyPage>} />
              <Route path="/admin/api" element={<LazyPage><ApiReferencePage /></LazyPage>} />
              <Route path="/admin/api/guide" element={<LazyPage><ApiGuidePage /></LazyPage>} />
              {isAdmin && <Route path="/admin/import-sources" element={<ImportSources />} />}
              <Route path="/imports" element={<Imports />} />
              <Route path="/imports/:id" element={<ImportRun />} />
              <Route path="*" element={<>
                <PageHead title="Page not found" desc="This page doesn't exist, or it needs a different role." />
                <Button asChild variant="outline"><Link to="/">Go to Needs attention</Link></Button>
              </>} />
              </Route>
            </Routes></LazyPage></PageErrorBoundary>}
          </main>
        </div>
      </div>
      {palette.loaded && (
        <Suspense fallback={<p className="fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-md border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md" aria-live="polite">Loading search…</p>}>
          <CommandPalette open={palette.open} onOpenChange={(o) => (o ? openPalette(palette.search) : closePalette())} me={me} initialSearch={palette.search}
            onSwitchOrganization={(o) => void switchOrganization(o)} onShowShortcuts={() => setHelp(true)} />
        </Suspense>
      )}
      <ShortcutHelp open={help} onOpenChange={setHelp} me={me} />
    </MeRefreshContext.Provider></MeContext.Provider>
  );
}
