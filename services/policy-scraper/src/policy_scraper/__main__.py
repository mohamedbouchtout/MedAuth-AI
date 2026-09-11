"""Entry point for the nightly CronJob: ``python -m policy_scraper``.

A one-shot process, not a service. It exits non-zero when the run could not
complete or when any document failed to ingest, so Kubernetes marks the job
failed rather than reporting a green run that quietly indexed nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from logging_policy import install_logging_policy
from policy_scraper.config import get_settings
from policy_scraper.scrape import run


def main() -> int:
    """Run one scrape and return the process exit code."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # After basicConfig, which sets the root level this raises the libraries
    # above. LOG_LEVEL=DEBUG is a real thing to do when a nightly run failed, and
    # it is exactly when urllib3 and botocore start writing request detail — so
    # the floors have to be applied to the configuration a human just chose, not
    # to the default they replaced. This job's own URLs are public payer
    # publications, but what a library may log is settled platform-wide rather
    # than per service. See packages/logging-policy.
    install_logging_policy()
    logger = logging.getLogger(__name__)

    try:
        summary = asyncio.run(run(get_settings()))
    except Exception:
        # The traceback is the point: a nightly job that fails needs to say why
        # in the log a human will read tomorrow morning.
        logger.exception("Scrape failed")
        return 1

    if summary.failed:
        logger.error("%s document(s) failed to ingest", summary.failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
