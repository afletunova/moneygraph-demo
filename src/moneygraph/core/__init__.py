"""The importable library surface — zero FastAPI dependency.

Everything here is pure logic over the Postgres schema: entity resolution,
enrichment, ticker/price lookups, and syndicate-round / re-resolve sweeps.
`from moneygraph.core import resolve, enrich, lookup_ticker` is the intended
entry point for using this outside the FastAPI app.
"""

from .enrichment import check_acquisition_demotion_evidence, enrich, enrich_all_nodes
from .reresolve import run_reresolve_sweep
from .resolve import is_generic_entity, normalize, resolve
from .stockprice import get_price_history, yahoo_symbol
from .syndicate import detect_syndicate_clusters, flag_syndicate_clusters
from .ticker_lookup import lookup_ticker, lookup_ticker_and_cik

__all__ = [
    "check_acquisition_demotion_evidence",
    "detect_syndicate_clusters",
    "enrich",
    "enrich_all_nodes",
    "flag_syndicate_clusters",
    "get_price_history",
    "is_generic_entity",
    "lookup_ticker",
    "lookup_ticker_and_cik",
    "normalize",
    "resolve",
    "run_reresolve_sweep",
    "yahoo_symbol",
]
