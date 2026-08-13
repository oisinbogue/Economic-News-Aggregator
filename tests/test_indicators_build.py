"""Phase-1 smoke test (INDICATORS_BRIEF.md §10/§11/§13): builds a fresh
indicators.db from the real 5-country Phase-1 config
(config/indicators/*.yaml), fetches every active series against a
synthetic -- never live -- DBnomics response, runs derived-series
transforms, and asserts that every *active* core indicator for every
Phase-1 country ends up with at least one stored observation carrying a
valid reference period.

This is the brief's "prove the schema survives contact with five different
data environments" check: it exercises config -> db -> fetch -> transform
end to end. `unavailable: true` core entries (e.g. us.fx_vs_usd) are
excluded from the observation assertion -- an explicit unavailable marker
with no provider is the brief's sanctioned way for a country to lack a
core indicator, it can never have observations, and that's checked
separately.

Real config also has a handful of gb/ie/eu/de core series whose primary
`provider` is `ecb`/`eurostat`/`ons`/`boe`/`stooq` (INDICATORS_BRIEF.md §4
point 2: official APIs used where DBnomics coverage is thin) -- every one
of those provider modules now exists (indicators/providers/{ecb,eurostat,
ons,boe,stooq}.py), so this test mocks each of their httpx clients with a
small synthetic response too and asserts they land observations exactly
like the dbnomics-backed core series, rather than the old Phase-1
"no provider module" graceful-failure check.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

import indicators.config as config_mod
import indicators.db as db_mod
import indicators.fetch as fetch_mod
import indicators.transform as transform_mod
from indicators.providers import bis, bls, boe, dbnomics, ecb, eurostat, ons, stooq


def _synthetic_dbnomics_payload() -> dict:
    """A generic multi-point series response shape-compatible with every
    series in the real config regardless of its true frequency -- the
    smoke test only cares that *an* observation with a valid reference
    period lands in the db, not that the spacing matches D/W/M/Q/A exactly.
    """
    return {
        "errors": None,
        "series": {
            "docs": [
                {
                    "period_start_day": ["2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01"],
                    "value": [100.0, 101.0, 102.5, 103.0],
                }
            ]
        },
    }


_ECB_PAYLOAD = {
    # A single monthly observation -- valid regardless of whether the real
    # series is daily/monthly/quarterly (ecb._parse_time_period treats any
    # "YYYY-MM"-shaped id as a month, which is all this smoke test needs).
    "dataSets": [{"series": {"0:0:0:0:0:0:0": {"observations": {"0": [1.5, 0, 0, None, None]}}}}],
    "structure": {"dimensions": {"observation": [{"id": "TIME_PERIOD", "values": [{"id": "2024-01"}]}]}},
}
_EUROSTAT_PAYLOAD = {
    "value": {"0": 100.0},
    "dimension": {"time": {"category": {"index": {"2024-01": 0}}}},
}
_ONS_PAYLOAD = {"months": [{"year": "2024", "month": "January", "value": "100.0"}]}
_BOE_CSV = "DATE,IUMBEDR\n31 Jan 2024,4.5\n"
_STOOQ_CSV = "Date,Open,High,Low,Close,Volume\n2024-01-31,100,101,99,100.5,1000\n"
_BLS_PAYLOAD = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{"data": [{"year": "2024", "period": "M01", "value": "100.0", "footnotes": [{}]}]}]},
}
_BIS_CSV = (
    "FREQ,REF_AREA,VALUE,UNIT_MEASURE,UNIT_MULT,BREAKS,COVERAGE,TITLE_TS,TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK\n"
    "Q,US,R,628,0,,,,2024-Q1,100.0,A,F,\n"
)


def _install_all_provider_mocks(monkeypatch) -> None:
    """Installs one combined mock transport, routed by request host, for
    every provider module's httpx client.

    Every indicators/providers/*.py module does `import httpx` -- Python
    module imports are cached in sys.modules, so `dbnomics.httpx`,
    `ecb.httpx`, `eurostat.httpx`, etc. are literally the *same* module
    object, not per-module copies. Calling monkeypatch.setattr(module.httpx,
    "AsyncClient", ...) once per provider therefore doesn't give each
    provider its own mock -- each call clobbers the one before it, and (since
    each factory below captures "the current httpx.AsyncClient" as its
    fallback) they end up recursively wrapping each other and blowing up
    with "multiple values for keyword argument 'transport'". One combined
    handler, patched once, avoids that entirely.
    """
    dbnomics_payload = _synthetic_dbnomics_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.db.nomics.world":
            return httpx.Response(200, json=dbnomics_payload)
        if host == "data-api.ecb.europa.eu":
            return httpx.Response(200, json=_ECB_PAYLOAD)
        if host == "ec.europa.eu":
            return httpx.Response(200, json=_EUROSTAT_PAYLOAD)
        if host == "api.ons.gov.uk":
            return httpx.Response(200, json=_ONS_PAYLOAD)
        if host == "www.bankofengland.co.uk":
            return httpx.Response(200, text=_BOE_CSV)
        if host == "stooq.com":
            return httpx.Response(200, text=_STOOQ_CSV)
        if host == "api.bls.gov":
            return httpx.Response(200, json=_BLS_PAYLOAD)
        if host == "stats.bis.org":
            return httpx.Response(200, text=_BIS_CSV)
        raise AssertionError(f"unexpected live request in test: {request.url}")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, **kwargs)

    # All of these `.httpx` attributes resolve to the same module object
    # (see docstring above); setting it via any one of them would suffice,
    # but naming every provider explicitly documents which modules this
    # covers and survives a future provider being renamed/split.
    for module in (dbnomics, ecb, eurostat, ons, boe, stooq, bls, bis):
        monkeypatch.setattr(module.httpx, "AsyncClient", factory)


async def _no_ecb_rates():
    return []


async def _no_stooq_rate(_currency):
    return None


@pytest.fixture
def real_config_fresh_db(tmp_path, monkeypatch):
    """Uses the real config/indicators/*.yaml (INDICATORS_CONFIG_DIR is left
    untouched) but redirects indicators.db's get_db_path() at a throwaway
    file under tmp_path, so this test never touches the repo's real
    data/indicators.db.
    """
    db_path = tmp_path / "indicators.db"
    monkeypatch.setattr(db_mod, "get_db_path", lambda: db_path)
    config_mod.get_config.cache_clear()
    config_mod.get_countries.cache_clear()
    config_mod.get_core_indicators.cache_clear()
    config_mod.get_series.cache_clear()
    return db_path


def test_full_pipeline_smoke_five_phase1_countries(real_config_fresh_db, monkeypatch):
    _install_all_provider_mocks(monkeypatch)
    monkeypatch.setattr(fetch_mod.fx, "fetch_ecb_rates", _no_ecb_rates)
    monkeypatch.setattr(fetch_mod.fx, "fetch_stooq_rate", _no_stooq_rate)

    db_mod.init_db()
    n_synced = db_mod.sync_series_from_config()
    assert n_synced > 0

    results = asyncio.run(fetch_mod.fetch_all(concurrency=5, backfill=True))
    assert results, "expected at least one series to have been fetched"
    result_by_id = {sid: (status, detail) for sid, status, detail, _ in results}

    with db_mod.get_connection() as conn:
        derived_ids = [
            row["series_id"]
            for row in conn.execute("SELECT series_id FROM series WHERE is_derived = 1")
        ]
        for series_id in derived_ids:
            transform_mod.compute_derived(series_id, conn)  # must not raise

        countries = list(config_mod.get_countries().keys())
        assert set(countries) == {"us", "gb", "ie", "eu", "de"}

        core_rows = conn.execute(
            "SELECT series_id, active, provider, unavailable_reason FROM series WHERE tier = 'core'"
        ).fetchall()

        # Every Phase-1 country reports (or explicitly marks unavailable)
        # every one of the 16 core indicators (brief §6.1).
        seen_countries = {row["series_id"].split(".")[0] for row in core_rows}
        assert seen_countries == set(countries)

        active_rows = [row for row in core_rows if row["active"]]
        inactive_rows = [row for row in core_rows if not row["active"]]

        assert active_rows, "expected at least one active core series across the 5 countries"
        for row in inactive_rows:
            assert row["unavailable_reason"], f"{row['series_id']}: inactive with no unavailable_reason"

        dbnomics_rows = [row for row in active_rows if row["provider"] == "dbnomics"]
        other_provider_rows = [row for row in active_rows if row["provider"] != "dbnomics"]
        assert dbnomics_rows, "expected at least one active dbnomics-backed core series"
        assert other_provider_rows, "expected at least one active non-dbnomics-backed core series"

        # Every provider module (dbnomics, ecb, eurostat, ons, boe, stooq)
        # is mocked above, so every active core series -- regardless of
        # which provider it's assigned -- should land a real observation.
        for row in dbnomics_rows + other_provider_rows:
            series_id = row["series_id"]
            status, detail = result_by_id.get(series_id, (None, None))
            assert status == "ok", f"{series_id}: expected an ok fetch against the synthetic provider, got {status}/{detail}"

            obs = conn.execute(
                "SELECT period_start, period_end FROM observations WHERE series_id = ? "
                "ORDER BY period_start LIMIT 1",
                (series_id,),
            ).fetchone()
            assert obs is not None, f"{series_id}: active core series has no stored observation"

            start = date.fromisoformat(obs["period_start"])
            end = date.fromisoformat(obs["period_end"])
            assert start <= end, f"{series_id}: period_start ({start}) after period_end ({end})"
