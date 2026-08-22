"""An HTTP client that identifies itself, asks permission, and paces itself.

Three courtesies, all of them TASK-013 requirements rather than nice-to-haves:

* **A User-Agent naming this scraper and a contact address.** Not decoration —
  ``www.cms.gov`` answers 403 to some clients purely on their User-Agent, so a
  request without a plausible one does not get an answer at all.
* **robots.txt, honoured per host**, fetched once and remembered for the run. A
  host that serves no robots.txt permits everything; ``downloads.cms.gov``,
  where the exports live, is exactly that case.
* **A delay between requests.** CMS's robots.txt sets no ``Crawl-delay``, so
  this is our own policy. A nightly job against a government service has no
  reason to hurry, and there are only three requests in a run.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from urllib.parse import urlsplit

import httpx

from policy_scraper.robots import ALLOW_ALL, RobotsPolicy

logger = logging.getLogger(__name__)


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids a URL this scraper wanted to fetch.

    Loud rather than silent. A scrape that quietly skipped every document
    because a rule changed would look exactly like a scrape that found nothing
    to do, which is the failure mode this whole service is written to avoid.
    """


class PoliteClient:
    """Fetches over HTTP, within what each host permits."""

    def __init__(
        self,
        *,
        user_agent: str,
        delay_seconds: float,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._delay = delay_seconds
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._policies: dict[str, RobotsPolicy] = {}
        self._made_a_request = False

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client, unless it was handed to us."""
        if self._owns_client:
            await self._client.aclose()

    async def get(self, url: str) -> bytes:
        """Fetch a URL, after checking robots.txt and waiting out the delay.

        Raises:
            RobotsDisallowed: The host's robots.txt forbids this URL.
            httpx.HTTPStatusError: The host answered with an error status.
        """
        policy = await self._policy_for(url)
        if not policy.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        await self._wait_turn()
        logger.info("Fetching %s", url)
        response = await self._client.get(url)
        response.raise_for_status()
        return response.content

    async def _wait_turn(self) -> None:
        """Sleep between requests, but not before the first one."""
        if self._made_a_request and self._delay:
            await asyncio.sleep(self._delay)
        self._made_a_request = True

    async def _policy_for(self, url: str) -> RobotsPolicy:
        """Return the host's robots policy, fetching it at most once per run."""
        split = urlsplit(url)
        origin = f"{split.scheme}://{split.netloc}"
        if origin in self._policies:
            return self._policies[origin]

        await self._wait_turn()
        policy = ALLOW_ALL
        try:
            response = await self._client.get(f"{origin}/robots.txt")
            if response.status_code == httpx.codes.OK:
                policy = RobotsPolicy.parse(response.text, self._user_agent)
                logger.info("Loaded robots.txt for %s", origin)
            else:
                logger.info(
                    "%s has no robots.txt (HTTP %s); treating as allow-all",
                    origin,
                    response.status_code,
                )
        except httpx.HTTPError as exc:
            # A robots.txt we could not fetch is not a prohibition. Saying so in
            # the log matters, because the alternative reading — that we crawled
            # in breach of a rule we never read — is the one a site owner cares
            # about.
            logger.warning(
                "Could not fetch robots.txt for %s (%s); treating as allow-all", origin, exc
            )

        self._policies[origin] = policy
        return policy
