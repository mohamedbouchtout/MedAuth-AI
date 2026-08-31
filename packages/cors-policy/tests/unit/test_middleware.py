"""Unit tests for installing the policy on a FastAPI app.

These drive a real app through a real ``TestClient`` rather than asserting on
the middleware's constructor arguments: what the task asks for is that a
preflight is *answered*, and an assertion about how the middleware was
configured would pass just as happily if it were never reached.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cors_policy import ALLOWED_METHODS, install_cors

ALLOWED_ORIGIN = "https://app.example.com"
OTHER_ORIGIN = "https://evil.example.com"


def build_app(origins: tuple[str, ...] = (ALLOWED_ORIGIN,)) -> FastAPI:
    app = FastAPI()
    install_cors(app, origins)

    @app.get("/thing")
    async def read_thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.patch("/thing")
    async def patch_thing() -> dict[str, str]:
        return {"ok": "patched"}

    return app


class TestPreflight:
    def test_configured_origin_gets_the_routes_methods(self) -> None:
        response = TestClient(build_app()).options(
            "/thing",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        granted = response.headers["access-control-allow-methods"]
        for method in ALLOWED_METHODS:
            assert method in granted

    def test_content_type_is_granted_since_it_is_what_makes_json_preflight(self) -> None:
        response = TestClient(build_app()).options(
            "/thing",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )

        assert "content-type" in response.headers["access-control-allow-headers"].lower()

    def test_the_launch_id_header_is_granted(self) -> None:
        """fhir-integration's chart reads carry launch_id in a custom header.

        A custom request header is preflighted, so without this grant every
        ``GET /fhir/patient/{id}/context`` from apps/web fails in the browser
        before it reaches the service — and it fails as a CORS error rather than
        as anything naming the header, which is why it is asserted rather than
        left to be discovered in a console.
        """
        response = TestClient(build_app()).options(
            "/thing",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-medauth-launch-id",
            },
        )

        assert response.status_code == 200
        granted = response.headers["access-control-allow-headers"].lower()
        assert "x-medauth-launch-id" in granted

    def test_origin_outside_the_configured_list_is_not_granted(self) -> None:
        response = TestClient(build_app()).options(
            "/thing",
            headers={
                "Origin": OTHER_ORIGIN,
                "Access-Control-Request-Method": "PATCH",
            },
        )

        assert "access-control-allow-origin" not in response.headers


class TestActualRequest:
    def test_configured_origin_is_echoed_on_a_real_response(self) -> None:
        response = TestClient(build_app()).get("/thing", headers={"Origin": ALLOWED_ORIGIN})

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

    def test_unconfigured_origin_gets_no_grant_header(self) -> None:
        """The request itself still succeeds — CORS is enforced by the browser,
        which will withhold the response from the page. The server's job is to
        withhold the grant, not the answer."""
        response = TestClient(build_app()).get("/thing", headers={"Origin": OTHER_ORIGIN})

        assert "access-control-allow-origin" not in response.headers

    def test_credentials_are_never_granted(self) -> None:
        """Nothing here authenticates with a cookie, and the WebSocket origin
        reasoning in CLAUDE.md depends on that staying true."""
        response = TestClient(build_app()).get("/thing", headers={"Origin": ALLOWED_ORIGIN})

        assert "access-control-allow-credentials" not in response.headers


class TestOriginsComeFromConfiguration:
    def test_a_different_configured_list_grants_a_different_origin(self) -> None:
        """The task's third test: the allowed origins come from configuration
        rather than a literal. Two apps built from two different lists must
        grant two different origins — a hardcoded list would make these agree."""
        first = TestClient(build_app(("https://one.example.com",)))
        second = TestClient(build_app(("https://two.example.com",)))

        one = {"Origin": "https://one.example.com"}
        two = {"Origin": "https://two.example.com"}

        assert first.get("/thing", headers=one).headers.get("access-control-allow-origin")
        assert not first.get("/thing", headers=two).headers.get("access-control-allow-origin")
        assert second.get("/thing", headers=two).headers.get("access-control-allow-origin")
        assert not second.get("/thing", headers=one).headers.get("access-control-allow-origin")

    def test_no_configured_origins_installs_nothing(self) -> None:
        """A service configured with no origins answers no browser. Installing
        middleware that rejects everything would report the same outcome less
        clearly, so nothing is installed at all."""
        app = build_app(())
        assert not app.user_middleware

        response = TestClient(app).get("/thing", headers={"Origin": ALLOWED_ORIGIN})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
