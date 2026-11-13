/** The assistant in the discovery queue (capability `assistant`): each candidate's suggestion
 *  (the action, the record, the confidence as glyph plus percent plus words, and the assistant's reason), what was
 *  decided about it, and "Ask the assistant" with the latest batch's progress. Shared with the settings and usage
 *  pages (assistant-admin.tsx): money in USD, batch and call states as chips.
 *
 *  The reason and detail are model-written text. They are rendered as React text children only, labelled as the
 *  assistant's, never as links or markup. Nothing here applies a suggestion on its own: Accept goes through the API
 *  (discovery/observations/{id}/assistant/accept), which performs the suggested link, create or dismiss as an ordinary
 *  audited decision. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api, ASSISTANT_CAPABILITY, when, type AssistantBatch, type AssistantBatchStatus, type AssistantSuggestion, type Me, type Observation,
  type SuggestionAction, type SuggestionOutcome,
} from "@/api";
import { RecordLink } from "@/components/record-link";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";

export const hasAssistant = (me: Me) => me.capabilities.includes(ASSISTANT_CAPABILITY);

type Tone = "confirmed" | "overdue" | "pending" | "unreviewed" | "neutral";

// ---- money ----------------------------------------------------------------------------------------------------------

/** "0.004200" as "0.0042 USD": the API's decimal string, never through a float; at least two decimals, trailing zeros
 *  after the cents dropped. */
export function usd(value: string | null | undefined) {
  if (!value) return "—";
  const [whole, frac = ""] = value.split(".");
  const kept = frac.replace(/0+$/, "").padEnd(2, "0");
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${kept} USD`;
}

/** What accepting a suggestion did, from the decision the API answered with (never from the suggestion). */
export function acceptedText(name: string, r: Observation) {
  const d = r.decision;
  const record = d?.record?.name ?? "the record";
  if (d?.decision === "linked") return `Accepted the assistant's suggestion: linked ${name} to ${record}. Nothing on ${record} changed.`;
  if (d?.decision === "created") return `Accepted the assistant's suggestion: created ${record} and linked it. It has no owners yet, so it is in Needs attention.`;
  if (d?.decision === "dismissed") return `Accepted the assistant's suggestion: dismissed ${name}. It comes back only if its name, state or owners change in the source.`;
  return `Accepted the assistant's suggestion for ${name}.`;
}

// ---- suggestions ----------------------------------------------------------------------------------------------------

const ACTION_TEXT: Record<SuggestionAction, string> = { link: "Link to an existing record", create: "Create a record", dismiss: "Dismiss" };
const DECIDED_TEXT: Record<SuggestionAction, string> = { link: "link it", create: "create a record", dismiss: "dismiss it" };

/** Confidence as a glyph, the percentage and words, never the colour or the number alone. */
export function confidenceText(c: number | null) {
  if (c === null) return { glyph: "", text: "No confidence given" };
  const pct = `${Math.round(c * 100)}%`;
  if (c >= 0.85) return { glyph: "●", text: `${pct}, high confidence` };
  if (c >= 0.6) return { glyph: "◐", text: `${pct}, medium confidence` };
  return { glyph: "○", text: `${pct}, low confidence` };
}

const OUTCOME: Record<Exclude<SuggestionOutcome, "pending">, { tone: Tone; glyph: string; label: string }> = {
  accepted: { tone: "confirmed", glyph: "✓", label: "Accepted" },
  overridden: { tone: "neutral", glyph: "↷", label: "Overridden" },
  ignored: { tone: "neutral", glyph: "○", label: "Ignored" },
  rejected: { tone: "neutral", glyph: "✕", label: "Rejected" },
  superseded: { tone: "neutral", glyph: "↻", label: "Superseded" },
};

function outcomeSentence(s: AssistantSuggestion) {
  const by = s.outcome_by?.name ?? "Someone";
  const at = s.outcome_at ? `, ${when(s.outcome_at)}` : "";
  switch (s.outcome) {
    case "accepted": return `${by}${s.via_accept ? " accepted it" : " decided the same by hand"}${at}.`;
    case "overridden": return `${by} decided to ${s.decided_action ? DECIDED_TEXT[s.decided_action] : "do something else"} instead${at}.`;
    case "ignored": return `${by} decided after the candidate had changed in the source${at}.`;
    case "rejected": return `${by} said no${at}.${s.decided_action ? ` Then decided to ${DECIDED_TEXT[s.decided_action]}.` : " The candidate is still to decide."}`;
    case "superseded": return "A newer suggestion replaced it: the candidate changed in the source.";
    default: return "";
  }
}

/** Model-written text, labelled as the assistant's: a React text child, so markup and addresses stay text. */
function AssistantText({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <p className="mt-1 text-sm [overflow-wrap:anywhere]" data-testid="assistant-text">
      <span className="font-semibold">{label}: </span><span>{text}</span>
    </p>
  );
}

export type AssistantAction = { kind: "accept" | "reject"; obs: Observation; suggestion: AssistantSuggestion };

/** The suggestion for one candidate, as a queue cell. Editors accept or reject a current, undecided, ready suggestion;
 *  everyone else (and every decided or out-of-date one) reads it. */
export function SuggestionCell({ o, write, onAction, busy = false }: { o: Observation; write: boolean; onAction: (a: AssistantAction) => void; busy?: boolean }) {
  const s = o.assistant_suggestion;
  const undecided = o.status === "new" || o.status === "changed";
  if (!s) return <span className="text-sm text-muted-foreground">{undecided ? "No suggestion yet" : "—"}</span>;
  const model = <p className="mt-1 text-xs text-muted-foreground">{s.model}, {when(s.created_at)}</p>;
  if (s.state !== "ready") {
    const refused = s.state === "refused";
    return (
      <div className="max-w-xs" data-testid="assistant-suggestion" data-state={s.state}>
        <span className={chip({ state: refused ? "neutral" : "pending" })}><span aria-hidden="true">{refused ? "⊘" : "!"}</span>{refused ? "No suggestion: declined by the model" : "No usable suggestion"}</span>
        <AssistantText label={refused ? "Details from the assistant" : "Why it was discarded"} text={s.detail} />
        {undecided && <p className="mt-1 text-sm text-muted-foreground">Decide it by hand. {refused ? "The assistant isn't asked again unless the candidate changes." : "The next batch asks again."}</p>}
        {model}
      </div>
    );
  }
  const action = s.action as SuggestionAction;
  const conf = confidenceText(s.confidence);
  const stale = s.outcome === "pending" && !s.current;
  const actionable = write && undecided && s.current && s.outcome === "pending";
  const outcome = s.outcome !== "pending" ? OUTCOME[s.outcome] : null;
  return (
    <div className="max-w-xs" data-testid="assistant-suggestion" data-state={s.state} data-outcome={s.outcome}>
      <p className="font-semibold">
        <span className="sr-only">The assistant suggests: </span>{ACTION_TEXT[action]}
        {action === "link" && (s.record ? <>: <RecordLink record={s.record} className="underline" /></> : <span className="font-normal text-muted-foreground"> (the record no longer exists)</span>)}
      </p>
      <p className="text-sm tabular" data-testid="assistant-confidence">{conf.glyph && <span aria-hidden="true">{conf.glyph} </span>}{conf.text}</p>
      <AssistantText label="Assistant's reason" text={s.reason} />
      {outcome && (
        <div className="mt-1.5" data-testid="assistant-outcome">
          <span className={chip({ state: outcome.tone })}><span aria-hidden="true">{outcome.glyph}</span>{outcome.label}</span>
          <span className="mt-0.5 block text-sm text-muted-foreground">{outcomeSentence(s)}</span>
          {s.outcome === "rejected" && s.note && <span className="block text-sm [overflow-wrap:anywhere]">Note: “{s.note}”</span>}
        </div>
      )}
      {stale && (
        <p className="mt-1.5 text-sm">
          <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Out of date</span>
          <span className="mt-0.5 block text-muted-foreground">Made before the candidate last changed in the source, so it can't be accepted. The next batch suggests again.</span>
        </p>
      )}
      {actionable && (
        <span className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction({ kind: "accept", obs: o, suggestion: s })}>
            {busy ? "Accepting…" : action === "link" ? "Accept suggestion" : "Accept suggestion…"}<span className="sr-only"> for {o.name}</span>
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction({ kind: "reject", obs: o, suggestion: s })}>Reject…<span className="sr-only"> the suggestion for {o.name}</span></Button>
        </span>
      )}
      {model}
    </div>
  );
}

// ---- batches --------------------------------------------------------------------------------------------------------

const BATCH: Record<AssistantBatchStatus, { tone: Tone; glyph: string; label: string }> = {
  queued: { tone: "neutral", glyph: "◔", label: "Queued" },
  running: { tone: "pending", glyph: "▸", label: "Working" },
  succeeded: { tone: "confirmed", glyph: "✓", label: "Finished" },
  stopped: { tone: "pending", glyph: "◐", label: "Stopped early" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
};
export const batchActive = (b: AssistantBatch | undefined | null) => !!b && (b.status === "queued" || b.status === "running");

export function BatchStatusChip({ status }: { status: AssistantBatchStatus }) {
  const s = BATCH[status];
  return <span className={chip({ state: s.tone })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

const n = (v: number | undefined) => v ?? 0;
const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;
const TRIGGER: Record<AssistantBatch["trigger"], (b: AssistantBatch) => string> = {
  after_run: () => "after a discovery run",
  manual: (b) => `asked for by ${b.requested_by?.name ?? "an Editor"}`,
  follow_up: () => "a follow-up for the candidates left over",
};

/** A batch in words: what it looked at, what came back, and what is left. */
export function batchSentence(b: AssistantBatch) {
  const c = b.counts;
  const scope = b.connector ? ` for ${b.connector.name}` : "";
  if (b.status === "queued") return `Queued ${when(b.queued_at)}${scope}, ${TRIGGER[b.trigger](b)}. The worker starts it within a minute.`;
  if (b.status === "running") return `Working since ${when(b.started_at)}${scope}: ${plural(n(c.suggested), "suggestion")} so far.`;
  const parts = [
    `${n(c.suggested)} suggested`,
    n(c.refused) ? `${n(c.refused)} declined by the model` : "",
    n(c.failed) ? `${n(c.failed)} without a usable answer` : "",
    n(c.unchanged) ? `${n(c.unchanged)} unchanged since their last suggestion (not sent again)` : "",
    n(c.skipped) ? `${n(c.skipped)} decided while it worked` : "",
  ].filter(Boolean);
  const left = n(c.remaining) ? ` ${plural(n(c.remaining), "candidate")} left for a follow-up batch.` : "";
  return `${BATCH[b.status].label} ${when(b.finished_at)}${scope}, ${TRIGGER[b.trigger](b)}: looked at ${plural(n(c.candidates), "candidate")}; ${parts.join(", ")}; ${plural(n(c.calls), "call")}.${left}`;
}

/** "Ask the assistant" (Editors and Admins) and the latest batch: polled every two seconds while one is queued or
 *  running, every 15 seconds otherwise and on returning to the window (so a batch queued after a run, or by someone
 *  else, shows up), with its counts, why it stopped ("Cap reached") and any error. `connector` narrows a new batch to
 *  the queue's connector filter. `onFinished` reloads the queue whenever a batch seen active has ended, including one
 *  that queued a follow-up. */
export function AssistantPanel({ me, connector, onFinished }: { me: Me; connector: { id: string; name: string } | null; onFinished: () => void }) {
  const write = me.role !== "viewer";
  const batches = useApiQuery<AssistantBatch[]>("assistant/batches", {
    refetchInterval: (q) => (q.state.status === "error" ? false : (q.state.data as AssistantBatch[] | undefined)?.some(batchActive) ? 2000 : 15_000),
    refetchOnWindowFocus: true,
  });
  const invalidate = useInvalidate();
  const [asking, setAsking] = useState(false);
  const [refusal, setRefusal] = useState("");
  const latest = batches.data?.[0] ?? null;
  const active = batchActive(latest);
  // A batch that has ended since the last look (seen queued or running, or new and already done): its suggestions are
  // in, so the queue reloads once.
  const seenActive = useRef<Set<string>>(new Set());
  const known = useRef<Set<string> | null>(null);
  useEffect(() => {
    const list = batches.data;
    if (!list) return;
    const ended = list.some((b) => !batchActive(b) && (seenActive.current.has(b.id) || (known.current !== null && !known.current.has(b.id))));
    seenActive.current = new Set(list.filter(batchActive).map((b) => b.id));
    known.current = new Set(list.map((b) => b.id));
    if (ended) onFinished();
  }, [batches.data]); // eslint-disable-line react-hooks/exhaustive-deps
  async function ask() {
    setAsking(true);
    setRefusal("");
    try {
      await api<AssistantBatch>("assistant/batches", { method: "POST", body: connector ? { connector: connector.id } : {} });
      await invalidate("assistant/batches");
    } catch (e) {
      setRefusal(problemText(e, "Couldn't ask the assistant."));
    } finally {
      setAsking(false);
    }
  }
  const capped = latest?.stop_reason === "cap_reached";
  return (
    <section aria-labelledby="assistant-h" className="mb-4 rounded-lg border bg-card px-4 py-3" data-testid="assistant-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id="assistant-h" className="font-display text-section font-semibold">Assistant</h2>
        {latest && <BatchStatusChip status={latest.status} />}
        {capped && <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Cap reached</span>}
        <span className="ml-auto flex flex-wrap gap-2">
          {write && (
            <Button size="sm" variant="outline" onClick={() => void ask()} disabled={asking || active}>
              {asking ? "Asking…" : active ? "Assistant working" : "Ask the assistant"}{connector && !active && <span className="sr-only"> about {connector.name}</span>}
            </Button>
          )}
          {me.role === "admin" && <Button asChild size="sm" variant="outline"><Link to="/admin/assistant">Assistant settings</Link></Button>}
        </span>
      </div>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        Claude suggests linking, creating or dismissing each undecided candidate, with a confidence and its reason{connector ? `; asking now covers ${connector.name} only` : ""}. Nothing changes until {write ? "you or another Editor" : "an Editor"} accepts a suggestion, and every decision is recorded against it.
      </p>
      <div role="status" className="mt-1 text-sm" data-testid="assistant-batch">
        {batches.isError ? "" : latest ? batchSentence(latest) : batches.data ? "The assistant hasn't looked at any candidates yet." : ""}
      </div>
      {latest?.error && (latest.status === "failed" || latest.status === "stopped") && (
        <p className={`mt-2 rounded-md border border-l-4 px-3 py-2 text-sm ${latest.status === "failed" ? "border-error-border bg-error-bg" : "border-state-pending-border bg-state-pending text-state-pending-foreground"}`} data-testid="assistant-stop">
          <span aria-hidden="true">{latest.status === "failed" ? "! " : "◐ "}</span>{latest.error}
          {capped && me.role === "admin" && <> <Link className="underline" to="/admin/assistant">Raise the cap</Link></>}
        </p>
      )}
      {batches.isError && (
        <p className="mt-2 text-sm" role="alert"><span aria-hidden="true">! </span>{problemText(batches.error, "Couldn't load the assistant's batches.")} <Button size="sm" variant="outline" onClick={() => void batches.refetch()}>Retry</Button></p>
      )}
      {refusal && <p className="mt-2 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert"><span aria-hidden="true">! </span>Not asked: {refusal}</p>}
    </section>
  );
}
