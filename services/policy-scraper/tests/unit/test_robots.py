"""robots.txt matching, including the case urllib gets wrong.

The rule that matters here is longest-match with Allow winning ties. CMS's file
is the reason: it disallows every query-string URL and then allows the Medicare
Coverage Database's, and the database is entirely behind query strings.
"""

from __future__ import annotations

import pytest

from policy_scraper.robots import ALLOW_ALL, RobotsPolicy

UA = "MedAuthAI-PolicyScraper/0.1 (+https://medauth.ai; scraper@medauth.ai)"

#: The shape of the real file, cut to the rules that decide our URLs.
CMS_ROBOTS = """
# CMS.gov robots.txt
User-agent: *
Allow: /core/*.css$
Disallow: /core/
Disallow: /node/
Allow: /medicare-coverage-database/*?
Disallow: /*?
Disallow: /admin/
Sitemap: https://www.cms.gov/sitemap.xml
"""


class TestCmsRules:
    def test_the_coverage_database_is_allowed_despite_the_query_string_rule(self) -> None:
        """Both patterns match; the longer one is the Allow, so the database is
        fetchable. A naive "any Disallow matches" check would refuse the whole
        site and look exactly like a scraper with nothing to do."""
        policy = RobotsPolicy.parse(CMS_ROBOTS, UA)

        assert policy.allows("https://www.cms.gov/medicare-coverage-database/view/lcd.aspx?lcdid=1")

    def test_other_query_string_urls_are_still_disallowed(self) -> None:
        """The Disallow is real; it just loses on this one path prefix."""
        policy = RobotsPolicy.parse(CMS_ROBOTS, UA)

        assert not policy.allows("https://www.cms.gov/search?q=policy")

    def test_a_plain_path_is_allowed(self) -> None:
        policy = RobotsPolicy.parse(CMS_ROBOTS, UA)

        assert policy.allows("https://www.cms.gov/medicare-coverage-database/")

    @pytest.mark.parametrize("path", ["/admin/", "/admin/settings", "/node/1"])
    def test_disallowed_prefixes_are_refused(self, path: str) -> None:
        policy = RobotsPolicy.parse(CMS_ROBOTS, UA)

        assert not policy.allows(f"https://www.cms.gov{path}")


class TestMatching:
    def test_the_longest_pattern_wins(self) -> None:
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /a/\nAllow: /a/b/\n", UA)

        assert not policy.allows("https://h/a/x")
        assert policy.allows("https://h/a/b/x")

    def test_allow_wins_a_tie(self) -> None:
        """Equal-length patterns, opposite verdicts — the standard says Allow."""
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /a/b\nAllow: /a/b\n", UA)

        assert policy.allows("https://h/a/b")

    def test_a_star_matches_any_run_of_characters(self) -> None:
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /files/*.zip\n", UA)

        assert not policy.allows("https://h/files/exports/current_lcd.zip")
        assert policy.allows("https://h/files/exports/current_lcd.csv")

    def test_a_dollar_anchors_the_end(self) -> None:
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /report$\n", UA)

        assert not policy.allows("https://h/report")
        assert policy.allows("https://h/reports/annual")

    def test_regex_characters_in_a_pattern_are_literal(self) -> None:
        """A path is not a regex. A '.' in a rule matches a dot, not any byte."""
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /a.zip\n", UA)

        assert not policy.allows("https://h/a.zip")
        assert policy.allows("https://h/axzip")

    def test_an_unmatched_path_is_allowed(self) -> None:
        """robots.txt is a list of exclusions, not an allowlist."""
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /private/\n", UA)

        assert policy.allows("https://h/anything/else")

    def test_an_empty_disallow_forbids_nothing(self) -> None:
        """ "Disallow:" with no path is the documented way to permit everything."""
        policy = RobotsPolicy.parse("User-agent: *\nDisallow:\n", UA)

        assert policy.allows("https://h/anything")

    def test_comments_are_ignored(self) -> None:
        policy = RobotsPolicy.parse("User-agent: *\nDisallow: /x/  # keep out\n", UA)

        assert not policy.allows("https://h/x/y")


class TestUserAgentGroups:
    ROBOTS = """
User-agent: *
Disallow: /

User-agent: MedAuthAI-PolicyScraper
Disallow: /private/
"""

    def test_a_group_naming_this_scraper_wins_over_the_wildcard(self) -> None:
        policy = RobotsPolicy.parse(self.ROBOTS, UA)

        assert policy.allows("https://h/public")
        assert not policy.allows("https://h/private/x")

    def test_another_crawlers_group_does_not_apply_to_us(self) -> None:
        policy = RobotsPolicy.parse(self.ROBOTS, "SomeoneElse/1.0 (them@example.com)")

        assert not policy.allows("https://h/public")

    def test_two_agents_sharing_one_group_both_get_its_rules(self) -> None:
        text = "User-agent: alpha\nUser-agent: beta\nDisallow: /x/\n"

        for agent in ("alpha/1.0", "beta/1.0"):
            assert not RobotsPolicy.parse(text, agent).allows("https://h/x/y")


class TestAllowAll:
    def test_a_host_with_no_robots_txt_permits_everything(self) -> None:
        """downloads.cms.gov, where the exports live, is exactly this case."""
        assert ALLOW_ALL.allows("https://downloads.cms.gov/anything/at/all.zip")
