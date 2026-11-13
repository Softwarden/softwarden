/** Where an address sends what goes to it, as the API compares it (core/outbound.origin):
 *  the scheme, the host (lowercase, international names in punycode, one trailing dot dropped) and the port (the
 *  scheme's usual one when none is written). null for an empty or unparseable address, which sends nothing.
 *
 *  Where the browser and the server can disagree: the WHATWG URL parser maps international names with UTS #46
 *  (so `ß` becomes `ss`, case and full-width characters fold), while the server uses Python's IDNA encoding, which
 *  keeps `ß` (`xn--...`) and refuses some names the browser accepts; the browser decodes percent-encoded hosts
 *  (`%65xample.com` is `example.com`) and the server doesn't; IPv6 hosts keep their brackets and compressed form here
 *  (`[::1]`) but not in Python's `hostname`; and `new URL` accepts some addresses Python's urlsplit refuses, and back.
 *  This copy only decides when to open the secret field early. When it guesses "same place" and the server disagrees,
 *  the server answers 400 on the secret, and that refusal opens the field too (the forms' catch), so the Admin is
 *  never left without the field the error names. */
export function origin(url: string): string | null {
  if (!url.trim()) return null;
  try {
    const u = new URL(url.trim());
    const host = u.hostname.replace(/\.$/, "");
    if (!host) return null;
    const port = u.port || ({ "https:": "443", "http:": "80" } as Record<string, string>)[u.protocol] || "0";
    return `${u.protocol}//${host}:${port}`;
  } catch {
    return null;
  }
}

/** Whether changing an address from `before` to `after` makes the API ask for the stored secret again
 *  (core/outbound.secret_must_follow): the new address sends somewhere, and somewhere else than before. A path-only
 *  change keeps it; emptying the address sends nothing and needs nothing. */
export function destinationMoved(before: string, after: string): boolean {
  const next = origin(after);
  return next !== null && next !== origin(before);
}
