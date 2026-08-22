"""The three courtesies: identifying ourselves, asking permission, pacing.

Every request goes through a mock transport, so nothing here touches the
network. What the live check covers is whether CMS still answers the same way;
what these cover is whether we would behave if it did.
"""

from __future__ import annotations

import httpx
import pytest

from policy_scraper.fetch import PoliteClient, RobotsDisallowed

UA = "MedAuthAI-PolicyScraper/0.1 (+https://medauth.ai; scraper@medauth.ai)"

CMS_ROBOTS = "User-agent: *\nAllow: /medicare-coverage-database/*?\nDisallow: /*?\n"


class Recorder:
    """A transport that records requests and answers from a script."""

    def __init__(self, robots: str | None = CMS_ROBOTS, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.robots = robots
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            if self.robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=self.robots)
        return httpx.Response(self.status, content=b"payload")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler),
            headers={"User-Agent": UA},
        )

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def polite(recorder: Recorder, *, delay: float = 0.0) -> PoliteClient:
    return PoliteClient(
        user_agent=UA,
        delay_seconds=delay,
        timeout_seconds=5.0,
        client=recorder.client(),
    )


async def test_a_permitted_url_is_fetched() -> None:
    recorder = Recorder()

    async with polite(recorder) as client:
        body = await client.get("https://www.cms.gov/medicare-coverage-database/exports/x.zip?v=1")

    assert body == b"payload"


async def test_robots_is_fetched_before_the_first_request() -> None:
    recorder = Recorder()

    async with polite(recorder) as client:
        await client.get("https://downloads.cms.gov/exports/current_lcd.zip")

    assert recorder.paths[0] == "/robots.txt"


async def test_robots_is_fetched_once_per_host() -> None:
    """Three archives from one host is one robots.txt, not three."""
    recorder = Recorder()

    async with polite(recorder) as client:
        for name in ("a.zip", "b.zip", "c.zip"):
            await client.get(f"https://downloads.cms.gov/exports/{name}")

    assert recorder.paths.count("/robots.txt") == 1


async def test_a_second_host_gets_its_own_robots_check() -> None:
    recorder = Recorder()

    async with polite(recorder) as client:
        await client.get("https://downloads.cms.gov/a.zip")
        await client.get("https://www.cms.gov/medicare-coverage-database/view/lcd.aspx?lcdid=1")

    assert recorder.paths.count("/robots.txt") == 2


async def test_a_disallowed_url_raises_rather_than_being_skipped() -> None:
    """Loud, because a run that quietly fetched nothing looks like a run with
    nothing to fetch."""
    recorder = Recorder(robots="User-agent: *\nDisallow: /\n")

    async with polite(recorder) as client:
        with pytest.raises(RobotsDisallowed):
            await client.get("https://www.cms.gov/anything")


async def test_a_disallowed_url_is_not_requested() -> None:
    recorder = Recorder(robots="User-agent: *\nDisallow: /\n")

    async with polite(recorder) as client:
        with pytest.raises(RobotsDisallowed):
            await client.get("https://www.cms.gov/anything")

    assert recorder.paths == ["/robots.txt"]


async def test_a_host_with_no_robots_txt_is_treated_as_allow_all() -> None:
    """downloads.cms.gov, where the exports live, serves none."""
    recorder = Recorder(robots=None)

    async with polite(recorder) as client:
        assert await client.get("https://downloads.cms.gov/exports/current_lcd.zip") == b"payload"


async def test_an_unreachable_robots_txt_is_treated_as_allow_all() -> None:
    """A file we could not read is not a prohibition."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, content=b"payload")

    client = PoliteClient(
        user_agent=UA,
        delay_seconds=0.0,
        timeout_seconds=5.0,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with client:
        assert await client.get("https://downloads.cms.gov/x.zip") == b"payload"


async def test_the_user_agent_identifies_this_scraper() -> None:
    """Load-bearing: cms.gov answers 403 to some clients on User-Agent alone."""
    recorder = Recorder()

    async with polite(recorder) as client:
        await client.get("https://downloads.cms.gov/x.zip")

    assert all(request.headers["user-agent"] == UA for request in recorder.requests)


async def test_an_error_status_raises() -> None:
    recorder = Recorder(status=503)

    async with polite(recorder) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("https://downloads.cms.gov/x.zip")


class TestPacing:
    async def test_requests_are_spaced_by_the_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("policy_scraper.fetch.asyncio.sleep", record)
        recorder = Recorder()

        async with polite(recorder, delay=1.5) as client:
            await client.get("https://downloads.cms.gov/a.zip")
            await client.get("https://downloads.cms.gov/b.zip")

        assert slept == [1.5, 1.5]  # robots, then each document after the first

    async def test_the_first_request_does_not_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing to be polite about before the first request."""
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("policy_scraper.fetch.asyncio.sleep", record)
        recorder = Recorder(robots=None)

        async with polite(recorder, delay=1.5) as client:
            await client.get("https://downloads.cms.gov/a.zip")

        assert slept == [1.5]  # only between robots.txt and the document

    async def test_a_zero_delay_never_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("policy_scraper.fetch.asyncio.sleep", record)
        recorder = Recorder()

        async with polite(recorder, delay=0.0) as client:
            await client.get("https://downloads.cms.gov/a.zip")
            await client.get("https://downloads.cms.gov/b.zip")

        assert slept == []
