/** The assistant's decision dialogs in the discovery queue, loaded on first use: accept a dismiss
 *  suggestion (an optional reason, the suggestion's by default) and reject any suggestion (an optional note). Accepting
 *  a link needs no dialog and a create opens the queue's create form prefilled (discovery.tsx). A 409 (the suggestion
 *  went out of date or was decided meanwhile) is said in words; the queue then reloads. The assistant's reason is
 *  shown as plain text. */
import { useState } from "react";
import { api, Problem, type AssistantSuggestion, type Observation } from "@/api";
import { acceptedText } from "@/features/discovery/assistant-ui";
import { ErrorSummary, Field, Textarea, useReturnFocus } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export function AssistantNoteDialog({ mode, obs, suggestion, onClose, onDone }: {
  mode: "dismiss" | "reject"; obs: Observation; suggestion: AssistantSuggestion;
  onClose: () => void; onDone: (text: string, obs: Observation | null) => void;
}) {
  const focus = useReturnFocus(true);
  const [text, setText] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState("");
  const [busy, setBusy] = useState(false);
  const field = mode === "dismiss" ? "reason" : "note";
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErrors({});
    setRefusal("");
    try {
      // The suggestion this dialog shows: the API refuses (409) if a newer one replaced it meanwhile.
      const body = { suggestion: suggestion.id, ...(text.trim() ? { [field]: text.trim() } : {}) };
      const r = await api<Observation>(`discovery/observations/${obs.id}/assistant/${mode === "dismiss" ? "accept" : "reject"}`, { method: "POST", body });
      onDone(mode === "dismiss" ? acceptedText(obs.name, r) : `Rejected the assistant's suggestion for ${obs.name}. It is still to decide.`, mode === "dismiss" ? r : null);
    } catch (err) {
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else if (err instanceof Problem) setRefusal(err.status === 409 ? `${err.detail} Nothing was changed; close this and the queue shows the candidate as it is now.` : err.detail);
      else setRefusal("Something went wrong. Nothing was changed; try again.");
    } finally {
      setBusy(false);
    }
  }
  const title = mode === "dismiss" ? `Accept: dismiss ${obs.name}` : `Reject the suggestion for ${obs.name}`;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{title}</DialogTitle>
          <DialogDescription>
            {mode === "dismiss"
              ? `${obs.name} stays out of the candidates unless its name, state or owners change in the source. The decision is recorded as accepting the assistant's suggestion.`
              : `The candidate stays to decide; you can still link, create or dismiss it by hand. Your no is recorded against the suggestion.`}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate className="grid gap-2">
          <ErrorSummary errors={errors} />
          {refusal && <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{refusal}</p>}
          {suggestion.reason && (
            <p className="rounded-md border bg-muted px-3 py-2 text-sm [overflow-wrap:anywhere]">
              <span className="font-semibold">Assistant's reason: </span><span>{suggestion.reason}</span>
            </p>
          )}
          {mode === "dismiss" ? (
            <Field id="reason" label="Why dismiss it? (optional)" errors={errors} help="Saved with the decision. Leave it empty to save the assistant's reason, marked as accepted from the assistant.">
              <Textarea id="reason" value={text} maxLength={500} onChange={(e) => setText(e.target.value)} />
            </Field>
          ) : (
            <Field id="note" label="Why not? (optional)" errors={errors} help="Saved with your no, for whoever reviews the assistant's suggestions. At most 500 characters.">
              <Textarea id="note" value={text} maxLength={500} onChange={(e) => setText(e.target.value)} />
            </Field>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : mode === "dismiss" ? "Dismiss" : "Reject suggestion"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
