/**
 * Reading a query parameter off a custom-scheme URI.
 *
 * **Parsed by hand rather than with `URL`.** React Native ships a partial `URL`
 * implementation, and a custom-scheme URI is the shape it handles least
 * predictably — `medauth://launch?claim=...` has no host, and an implementation
 * that mis-splits it drops the query silently. On this path that presents as a
 * launch which completed and delivered nothing, which is exactly the failure the
 * claim handoff exists to make visible.
 *
 * One parser rather than two: both URIs this app reads are custom-scheme and
 * both arrive from outside it — the redirect ending an auth session, and an
 * EHR-initiated launch arriving as a deep link. Two hand-rolled parsers is how
 * one of them ends up handling an encoded value the other does not.
 */

/**
 * Return the first value of `name` in `url`'s query string, or null.
 *
 * Null covers every way the parameter is not usably present — no query at all,
 * no such parameter, or one whose value is empty — because no caller here can
 * act on the difference. A launch with an empty `iss` and a launch with no `iss`
 * are both a launch this app cannot perform.
 */
export function queryParam(url: string, name: string): string | null {
  const start = url.indexOf('?');
  if (start === -1) {
    return null;
  }
  const query = url.slice(start + 1).split('#')[0] ?? '';
  for (const pair of query.split('&')) {
    const separator = pair.indexOf('=');
    if (separator === -1) {
      continue;
    }
    if (decodeURIComponent(pair.slice(0, separator)) !== name) {
      continue;
    }
    const value = decodeURIComponent(pair.slice(separator + 1));
    return value === '' ? null : value;
  }
  return null;
}
