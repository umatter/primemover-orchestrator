"""Scheduling the standing treated canary.

The canary is one uploaded experiment carrying a queue per day, because
`start_at` already schedules work and nothing external needs to fire. Two
things can quietly ruin it, and both are what these tests are for.

It could be built from the wrong scenario. A canary whose crawler is
pi=psi=kappa=tau=0 passes for ever and means nothing -- the same trap that made
a green `--youtube` run look like evidence the treatments worked.

Or its schedule could drift. `start_at` is Europe/Zurich WALL CLOCK: the API
strips the offset and reads the naive datetime as local time. Generating
sessions by adding 24h to an instant gives "05:00 every day until 25 October,
then 04:00" -- which nobody would notice, because a canary an hour early still
runs.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "treatment-canary.py"


def load_script():
    spec = importlib.util.spec_from_file_location("treatment_canary", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canary = load_script()


class TestSchedule:
    def test_one_session_per_day(self):
        starts = canary.sessions(date(2026, 9, 10), 28, time(5, 0))

        assert len(starts) == 28
        assert [s[:10] for s in starts][:3] == ["2026-09-10", "2026-09-11", "2026-09-12"]
        assert len(set(starts)) == 28

    def test_the_wall_clock_is_what_stays_fixed_across_dst(self):
        # 2026-10-25 is the CEST -> CET change. The hour must not move.
        starts = canary.sessions(date(2026, 10, 23), 4, time(5, 0))

        assert [s[11:19] for s in starts] == ["05:00:00"] * 4
        assert [s[-6:] for s in starts] == ["+02:00", "+02:00", "+01:00", "+01:00"]

    def test_the_offset_it_writes_matches_the_wall_clock_it_encodes(self):
        # The API strips the offset, so a wrong one would never bite here -- it
        # would bite whoever reads the JSON later and believes it.
        assert canary.local_start(date(2026, 7, 1), time(5, 0)).endswith("+02:00")
        assert canary.local_start(date(2026, 1, 15), time(5, 0)).endswith("+01:00")

    @pytest.mark.parametrize("at", [time(5, 0), time(23, 30), time(0, 0)])
    def test_any_hour_survives_the_round_trip(self, at):
        start = canary.local_start(date(2026, 9, 10), at)

        assert start[11:16] == at.strftime("%H:%M")


class TestItRefusesAControlScenario:
    def test_a_control_crawler_is_rejected(self, tmp_path, monkeypatch):
        # The whole point of the canary. pi=0/kappa=0 would pass for ever.
        scenario = json.loads(canary.SCENARIO.read_text())
        scenario[0]["configuration"]["params"]["pi"] = 0
        control = tmp_path / "control.json"
        control.write_text(json.dumps(scenario))
        monkeypatch.setattr(canary, "SCENARIO", control)

        with pytest.raises(SystemExit, match="control crawler"):
            canary.build(3, time(5, 0), date(2026, 9, 10))


class TestItReusesTheTestScenario:
    def test_the_workflow_is_not_duplicated(self):
        # If the canary carried its own copy of the workflow the two would drift,
        # and the canary would stop testing what the e2e tests.
        built = canary.build(2, time(5, 0), date(2026, 9, 10))
        source = json.loads(canary.SCENARIO.read_text())

        built_types = [j["type"] for j in built[0]["queues"][0]["jobs"]]
        source_types = [j["type"] for j in source[0]["queues"][0]["jobs"]]

        assert built_types == source_types
        assert "lambdasingleselectjob" in built_types

    def test_every_session_carries_the_whole_workflow(self):
        built = canary.build(5, time(5, 0), date(2026, 9, 10))
        queues = built[0]["queues"]

        assert len(queues) == 5
        assert len({len(q["jobs"]) for q in queues}) == 1, "sessions differ in length"

    def test_the_treated_params_survive(self):
        built = canary.build(2, time(5, 0), date(2026, 9, 10))
        params = built[0]["configuration"]["params"]

        assert int(params["pi"]) == 1
        assert int(params["kappa"]) == 1


class TestProxy:
    """A canary shipping PROXY_*_PLACEHOLDER never starts a browser."""

    def test_placeholders_are_substituted(self):
        class _Settings:
            e2e_proxy_configured = True
            e2e_proxy_host = "gate.example.net"
            e2e_proxy_username = "user"
            e2e_proxy_password = "secret"
            e2e_proxy_port = 5432

        proxy = canary.build(2, time(5, 0), date(2026, 9, 10), settings=_Settings())[0]["proxy"]

        assert proxy["hostname"] == "gate.example.net"
        assert not str(proxy["password"]).endswith("_PLACEHOLDER")

    def test_it_refuses_rather_than_shipping_a_placeholder(self):
        # Multilogin validates the proxy on profile start, so an unsubstituted
        # canary would fail every session with GET_PROXY_CONNECTION_IP_ERROR --
        # an error about IP lookup, nothing about treatments or credentials.
        class _Settings:
            e2e_proxy_configured = False
            e2e_proxy_host = None
            e2e_proxy_username = None
            e2e_proxy_password = None
            e2e_proxy_port = None

        with pytest.raises(RuntimeError, match="residential proxy"):
            canary.build(2, time(5, 0), date(2026, 9, 10), settings=_Settings())
