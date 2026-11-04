"""Explicit backend composition using the existing active graph scope/store.

No settings loading, startup hook, graph replacement or DB/API call at import.
"""
from contextlib import closing

from app.features.supplier_crawl.application.candidates import CrawlSupplierCandidates
from app.features.supplier_crawl.application.trial import SupplierCrawlTrial
from app.features.supplier_crawl.domain.registration import PayloadBuilder
from app.agents.tools import Fetcher
from app.features.supplier_crawl.infrastructure.registration import WebSupplierRegistrar


class _BudgetFetcher(Fetcher):
    def __init__(self, settings, scope, **kwargs):
        self.scope = scope
        super().__init__(settings, **kwargs)

    def _wait(self, host):
        self.scope.remaining_seconds()
        super()._wait(host)
        self.scope.remaining_seconds()

    def get(self, url, *, timeout=None, **kwargs):
        remaining = self.scope.remaining_seconds()
        response = super().get(url, timeout=min(timeout or self.settings.crawl_timeout_seconds, remaining), **kwargs)
        self.scope.remaining_seconds()
        return response


def make_crawl_service(*, scope, settings, transport=None, sleep=None):
    """Compose explicitly; DB v3 resolves keys from real observed item inputs.

    No local normalization, stored-case key copying or resolver is involved.
    This factory does not activate collection or call the DB.
    """
    def collect(options):
        kwargs = {'transport': transport}
        if sleep is not None:
            kwargs['sleep'] = sleep
        with closing(_BudgetFetcher(settings, scope, **kwargs)) as fetcher:
            return SupplierCrawlTrial(settings, fetcher).run(options)

    return CrawlSupplierCandidates(collect=collect, builder=PayloadBuilder(settings.secrets()),
        registrar=WebSupplierRegistrar(scope), store=scope.agent_store)
