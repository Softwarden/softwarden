/** The path part of a request (before any `?` or `#`) never climbs out of /api/v1/. A `..` segment, a backslash,
 *  an encoded dot, slash or backslash, or a control character or space (the URL parser deletes tab, LF and CR before it
 *  collapses dot segments, so `.<tab>.` is `..`) can only come from an id taken from the address bar; refuse it before
 *  fetch(). No imports, so a Playwright spec can load it directly. */
// eslint-disable-next-line no-control-regex
const UNSAFE_PATH = /\.\.|[\\]|%2e|%2f|%5c|[\x00-\x20\x7f]/i;

export function assertSafePath(path: string): void {
  if (UNSAFE_PATH.test(path.split(/[?#]/)[0])) throw new Error("Refused an API path that is not a plain resource path.");
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const isUuid = (value: string | null | undefined): value is string => !!value && UUID.test(value);
/** An id read from the address bar (a search parameter) that goes into an API path: the id when it is a UUID, else "" (absent). */
export const uuidOrEmpty = (value: string | null | undefined): string => (isUuid(value) ? value : "");
