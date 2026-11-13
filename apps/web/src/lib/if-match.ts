/** If-Match on every write the API compares a revision on.
 *
 *  The API answers a signed-in session's PATCH or DELETE (and the actions that take If-Match) without If-Match with
 *  428 (`SOFTWARDEN_REQUIRE_IF_MATCH`, on by default), so every such write sends the ETag of the read the form or
 *  confirmation was opened with; a change someone else saved since is refused with 412 and the screen offers the
 *  latest version instead of overwriting it.
 *
 *  - A detail page or editor that already reads its record with `apiWithEtag` sends that ETag (`ifMatch(etag)`).
 *  - A dialog opened from a list (lists carry no revision) reads its record when it opens: `useOpenedRecord(path)`;
 *    `await opened.etag()` is that read's ETag (waiting for it when the save is quicker than the read).
 *  - A write that names its whole change and sends back nothing read earlier (one click: Mark active, undo of the
 *    write just made) sends `ANY_REVISION` (`If-Match: *`, RFC 9110: any current revision). */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiWithEtag, Problem } from "@/api";

/** The If-Match header for `etag`; none when there is no ETag (a create). */
export function ifMatch(etag: string | null | undefined): Record<string, string> | undefined {
  return etag ? { "If-Match": etag } : undefined;
}

/** For a one-click write that names its whole change: whatever the record's current revision is. */
export const ANY_REVISION: Record<string, string> = { "If-Match": "*" };

/** The 412 an If-Match write gets when the record changed since it was read. */
export const isStaleWrite = (e: unknown) => e instanceof Problem && e.status === 412;

/** What a delete (or remove) confirmation says on a 412. */
export const staleDeleteText = (what: string, verb: "delete" | "remove" = "delete") =>
  `Someone else changed this ${what} after you opened this, so it wasn't ${verb}d. Close this and check it, then ${verb} it again if you still want to.`;

/** The record at `path` read with its ETag while `path` is set (a dialog that edits or deletes it is open). Not cached
 *  across openings and kept out of the API cache's keys, so a refetch after some other write never swaps in a newer
 *  ETag behind the person's back: only `reload()` (Load the latest version) does. */
export function useOpenedRecord<T>(path: string | null) {
  const client = useQueryClient();
  const key = ["if-match", path];
  const query = useQuery({
    queryKey: key, queryFn: () => apiWithEtag<T>(path!), enabled: path !== null, gcTime: 0, staleTime: Infinity, retry: false,
  });
  return {
    /** The record as read when the dialog opened (or by the last reload). */
    data: query.data?.data,
    error: query.error,
    /** The ETag to send as If-Match: the opened read's (waiting for it when it is still on its way). */
    etag: async () => (path === null ? null : (await client.ensureQueryData({ queryKey: key, queryFn: () => apiWithEtag<T>(path) })).etag),
    /** The copy last known with its ETag: the opened read, or the answer of a save adopted since. */
    latest: () => client.ensureQueryData({ queryKey: key, queryFn: () => apiWithEtag<T>(path!) }),
    /** A save's answer (apiWriteWithEtag) becomes the copy and ETag the next save starts from, without a second read
     *  that could absorb a change someone else made in between. */
    adopt: (saved: { data: T; etag: string | null }) => client.setQueryData(key, saved),
    /** Read it again (Load the latest version); resolves to the new copy. */
    reload: async () => (await query.refetch()).data?.data,
  };
}
