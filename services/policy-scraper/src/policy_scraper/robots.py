"""Honouring robots.txt, with the matching rule the standard actually specifies.

``urllib.robotparser`` is not usable here. Its ``RuleLine.applies_to`` is a
prefix comparison with no wildcard handling, so a rule written ``Disallow: /*?``
is treated as the literal prefix ``/*?`` and matches nothing. Against CMS's file
it happens to reach the right answer for the wrong reason, which is worse than
being wrong: the next site with a wildcard rule would be crawled in breach of it
and nothing would say so.

So this implements the two rules that decide the question:

* ``*`` matches any run of characters and ``$`` anchors the end of the path.
* The **longest matching pattern wins**, and ``Allow`` wins a tie.

CMS's robots.txt is exactly the case that needs both. It carries::

    Allow: /medicare-coverage-database/*?
    Disallow: /*?

Both patterns match a Medicare Coverage Database URL with a query string. Under
longest-match the ``Allow`` wins and the database is fetchable, which is the
intent — the whole site is behind query strings. A naive "does any Disallow
match" check would refuse the entire database and look, from the outside, like a
scraper with nothing to do.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_USER_AGENT_LINE: Final = re.compile(r"^user-agent\s*:\s*(.*)$", re.IGNORECASE)
_RULE_LINE: Final = re.compile(r"^(allow|disallow)\s*:\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class _Rule:
    """One Allow or Disallow pattern."""

    allowed: bool
    pattern: str

    def matches(self, path: str) -> bool:
        """Report whether this rule's pattern covers a path."""
        return re.match(_to_regex(self.pattern), path) is not None

    @property
    def specificity(self) -> int:
        """Pattern length, which is what longest-match compares."""
        return len(self.pattern)


def _to_regex(pattern: str) -> str:
    """Return the regex for a robots.txt path pattern.

    Everything is escaped except the two metacharacters the standard defines, so
    a literal ``?`` or ``.`` in a path cannot behave as a regex operator.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    expression = "".join(".*" if char == "*" else re.escape(char) for char in body)
    return expression + ("$" if anchored_end else "")


class RobotsPolicy:
    """What one host's robots.txt permits, for one User-Agent."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str, user_agent: str) -> RobotsPolicy:
        """Return the policy for ``user_agent``, falling back to the ``*`` group.

        Groups are matched on the token before any slash or space, the way a
        crawler's own name is conventionally written, and a group naming this
        crawler specifically takes precedence over the wildcard group.
        """
        token = user_agent.split("/")[0].split()[0].casefold()
        groups: dict[str, list[_Rule]] = {}
        current: list[str] = []
        starting_group = True

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if agent := _USER_AGENT_LINE.match(line):
                if not starting_group:
                    current = []
                    starting_group = True
                current.append(agent.group(1).strip().casefold())
                groups.setdefault(current[-1], [])
                continue
            if rule := _RULE_LINE.match(line):
                starting_group = False
                path = rule.group(2).strip()
                if not path:
                    # "Disallow:" with no path is the documented way to say
                    # "nothing is disallowed", so it is not a rule at all.
                    continue
                parsed = _Rule(allowed=rule.group(1).casefold() == "allow", pattern=path)
                for name in current:
                    groups.setdefault(name, []).append(parsed)

        return cls(groups.get(token, groups.get("*", [])))

    def allows(self, url: str) -> bool:
        """Report whether this policy permits fetching a URL.

        A path matched by no rule is allowed — robots.txt is a list of
        exclusions, not an allowlist.
        """
        split = urlsplit(url)
        path = split.path or "/"
        if split.query:
            path = f"{path}?{split.query}"

        matching = [rule for rule in self._rules if rule.matches(path)]
        if not matching:
            return True
        # Longest pattern wins; Allow wins a tie, hence `rule.allowed` as the
        # tiebreaker in the sort key.
        best = max(matching, key=lambda rule: (rule.specificity, rule.allowed))
        return best.allowed


#: What a host with no robots.txt permits: everything. A missing or unreadable
#: file is not a prohibition, and treating it as one would silently stop the
#: scrape. downloads.cms.gov, where the exports live, serves no robots.txt at
#: all — the delay between requests still applies there.
ALLOW_ALL: Final = RobotsPolicy([])
