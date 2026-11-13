/** An edit form opened from a list (lists carry no revision). It reads the record with its
 *  ETag when it opens and renders the form from that read, so the save's If-Match names exactly the copy the form
 *  shows. A save refused with 412 (someone else saved first) shows "Load the latest version", which reads the record
 *  again and starts the form over from the new copy; nothing of theirs is overwritten. */
import { Fragment, useState } from "react";
import { ErrorBanner, isNotFound, Loading, NotFound } from "@/components/app";
import { StaleBanner } from "@/features/money/contract-ui";
import { ifMatch, useOpenedRecord } from "@/lib/if-match";
import { problemText } from "@/lib/query";

/** What the form gets beside the record: the save's If-Match, and what to call on a 412. */
export type OpenedWrite = {
  /** The If-Match header for the save (the opened read's ETag). */
  ifMatch: () => Promise<Record<string, string> | undefined>;
  /** The save was refused with 412: show "Load the latest version". */
  onStale: () => void;
};

export function OpenedEdit<T>({ path, what, back, children }: {
  /** The record's API path, for example `people/<id>`. */
  path: string;
  /** The record's noun for the messages ("person", "vendor"). */
  what: string;
  /** The list it belongs to: a record that isn't there (404) shows the not-found page with the way back. */
  back?: { to: string; label: string };
  children: (record: T, write: OpenedWrite) => React.ReactNode;
}) {
  const opened = useOpenedRecord<T>(path);
  const [round, setRound] = useState(0);
  const [stale, setStale] = useState(false);
  if (back && isNotFound(opened.error)) return <NotFound what={what} back={back} />;
  if (opened.error) return <ErrorBanner message={problemText(opened.error, `Couldn't load the ${what}.`)} onRetry={() => void opened.reload()} />;
  if (opened.data === undefined) return <Loading what={`the ${what}`} />;
  async function loadLatest() {
    await opened.reload();
    setStale(false);
    setRound((n) => n + 1);
  }
  return (
    <>
      {stale && <StaleBanner what={what} onLoad={() => void loadLatest()} />}
      <Fragment key={round}>{children(opened.data, { ifMatch: async () => ifMatch(await opened.etag()), onStale: () => setStale(true) })}</Fragment>
    </>
  );
}
