# ADR-0025: robots.txt matching is implemented here, not taken from `urllib`

**Status:** Accepted · **Task:** TASK-013

## Context

The scraper honours robots.txt. Python ships `urllib.robotparser`, which is the
obvious thing to use.

It is not usable here. Its `RuleLine.applies_to` is a **prefix comparison with no
wildcard handling**, so a rule written `Disallow: /*?` is treated as the literal
prefix `/*?` and matches nothing.

Against CMS's file it happens to reach the right answer for the wrong reason,
which is worse than being wrong: the next site with a wildcard rule would be
crawled in breach of it, and nothing would say so.

CMS's robots.txt is precisely the case that needs real matching:

```
Allow: /medicare-coverage-database/*?
Disallow: /*?
```

Both patterns match a Medicare Coverage Database URL with a query string. Under
the standard's longest-match rule the `Allow` wins and the database is
fetchable, which is the intent — the whole site is behind query strings. A naive
"does any Disallow match" check would refuse the entire database and look, from
the outside, like a scraper with nothing to do.

## Decision

Implement the two matching rules the standard actually specifies, in
`policy_scraper/robots.py`:

- `*` matches any run of characters; `$` anchors the end of the path.
- **The longest matching pattern wins**, and `Allow` wins a tie.

Alongside it, `fetch.py` carries three courtesies that are TASK-013 requirements
rather than nice-to-haves:

- **A User-Agent naming this scraper and a contact address.** Not decoration:
  `www.cms.gov` answers 403 to some clients purely on their User-Agent.
- **robots.txt honoured per host**, fetched once and remembered for the run. The
  database UI is `www.cms.gov` and the exports are on `downloads.cms.gov`, which
  are separate hosts with separate files — conflating them checks the wrong one.
  A host serving no robots.txt permits everything, which is `downloads.cms.gov`.
- **A delay between requests.** CMS's robots.txt sets no `Crawl-delay`, so this
  is our own policy. A nightly job against a government service has no reason to
  hurry, and there are only three requests in a run.

## Consequences

- The matcher is ~100 lines with its own unit suite, which is cheap next to
  breaching a government site's crawl policy.
- It is correct for the next source too, which matters because the payer
  sandboxes come later.

## References

- `services/policy-scraper/src/policy_scraper/robots.py`, `fetch.py`
