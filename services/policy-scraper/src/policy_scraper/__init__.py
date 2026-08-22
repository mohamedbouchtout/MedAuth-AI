"""The nightly CMS coverage-policy scraper.

Runs as a Kubernetes CronJob (``python -m policy_scraper``), collects Medicare
coverage determinations relevant to the procedures MedAuth sees, and hands them
to track-b-rag's ``/policies/ingest``. It never chunks, embeds or writes Qdrant
itself — there is one definition of how a policy gets indexed, and it lives in
that endpoint.

Two things worth knowing before reading further:

* **It downloads three archives and makes no per-document requests.** CMS's
  Medicare Coverage Database publishes daily CSV exports that carry the full
  policy text, so crawling 949 LCD pages would be slower, ruder and no more
  complete. See :mod:`policy_scraper.mcd`.
* **The procedure codes it collects for are a curated list**, not everything
  CMS publishes, and two of the codes an earlier draft named are not in it for
  reasons worth reading. See :mod:`policy_scraper.codes`.
"""
