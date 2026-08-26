from __future__ import annotations

import json
import pathlib
import unittest

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from breachbrief.reconcile import (
    UNCLASSIFIED,
    Window,
    credit_rate,
    merge_windows,
    normalise_severity,
    parse_instant,
    reconcile,
)

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "incidents-messy.json"


def w(start: str, end: str, *ids: str, severity: str = "SEV2") -> Window:
    return Window(parse_instant(start), parse_instant(end), list(ids), severity, "")


class SeverityTests(unittest.TestCase):
    def test_the_six_spellings_collapse_to_one_vocabulary(self) -> None:
        for raw in ("SEV1", "sev-1", "P1", "critical", "  Sev 1 ", "p_1"):
            self.assertEqual(normalise_severity(raw), "SEV1", raw)
        self.assertEqual(normalise_severity("SEV2"), "SEV2")
        self.assertEqual(normalise_severity("Sev 3"), "SEV3")

    def test_missing_severity_is_named_rather_than_guessed(self) -> None:
        for raw in ("", None, "   ", "banana"):
            self.assertEqual(normalise_severity(raw), UNCLASSIFIED)


class MergeTests(unittest.TestCase):
    """Two tickets for one outage must bill once, not twice."""

    def test_overlapping_windows_become_one_outage(self) -> None:
        merged = merge_windows([
            w("2026-07-03T02:14:00Z", "2026-07-03T03:41:00Z", "INC-1042"),
            w("2026-07-03T03:10:00Z", "2026-07-03T04:02:00Z", "INC-1043"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].minutes, 108)
        self.assertEqual(merged[0].incident_ids, ["INC-1042", "INC-1043"])

    def test_merged_outage_keeps_the_worst_severity(self) -> None:
        merged = merge_windows([
            w("2026-07-03T02:00:00Z", "2026-07-03T03:00:00Z", "A", severity="SEV3"),
            w("2026-07-03T02:30:00Z", "2026-07-03T04:00:00Z", "B", severity="SEV1"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].severity, "SEV1")

    def test_touching_windows_merge_but_separated_ones_do_not(self) -> None:
        touching = merge_windows([
            w("2026-07-03T02:00:00Z", "2026-07-03T03:00:00Z", "A"),
            w("2026-07-03T03:00:00Z", "2026-07-03T04:00:00Z", "B"),
        ])
        self.assertEqual(len(touching), 1)
        apart = merge_windows([
            w("2026-07-03T02:00:00Z", "2026-07-03T03:00:00Z", "A"),
            w("2026-07-03T03:01:00Z", "2026-07-03T04:00:00Z", "B"),
        ])
        self.assertEqual(len(apart), 2)

    def test_a_window_fully_inside_another_adds_no_minutes(self) -> None:
        merged = merge_windows([
            w("2026-07-03T02:00:00Z", "2026-07-03T06:00:00Z", "OUTER"),
            w("2026-07-03T03:00:00Z", "2026-07-03T04:00:00Z", "INNER"),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].minutes, 240)


class CreditLadderTests(unittest.TestCase):
    def test_meeting_the_contract_owes_nothing(self) -> None:
        self.assertEqual(credit_rate(99.95, 99.9), 0)
        self.assertEqual(credit_rate(99.9, 99.9), 0)

    def test_each_rung_of_the_ladder(self) -> None:
        self.assertEqual(credit_rate(99.5, 99.9), 10)
        self.assertEqual(credit_rate(98.0, 99.9), 25)
        self.assertEqual(credit_rate(80.0, 99.9), 50)


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.feed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.account = reconcile(self.feed)["Account"][0]

    def _service(self, name: str) -> dict:
        return next(s for s in self.account["Services"] if s["name"] == name)

    def test_duplicate_ticket_row_is_dropped_with_a_reason(self) -> None:
        reasons = {(e["id"], e["reason"]) for e in self.account["Excluded"]}
        self.assertIn(("INC-1043", "duplicate ticket row"), reasons)

    def test_service_outside_the_sla_never_reaches_the_credit(self) -> None:
        reasons = {(e["id"], e["reason"]) for e in self.account["Excluded"]}
        self.assertIn(("INC-1077", "service is not under SLA"), reasons)
        self.assertNotIn("internal-wiki", [s["name"] for s in self.account["Services"]])

    def test_two_tickets_for_one_outage_are_billed_once(self) -> None:
        render = self._service("render-api")
        first = render["outages"][0]
        self.assertEqual(first["merged_from"], 2)
        self.assertEqual(first["minutes"], 108)
        self.assertEqual(render["ticket_count"], 3)
        self.assertEqual(render["outage_count"], 2)

    def test_an_outage_straddling_the_boundary_is_clamped(self) -> None:
        cdn = self._service("asset-cdn")
        straddler = next(o for o in cdn["outages"] if "INC-1080" in o["incident_ids"])
        self.assertTrue(straddler["start"].startswith("2026-07-01 00:00"))
        self.assertEqual(straddler["minutes"], 270)

    def test_an_incident_nobody_closed_runs_to_the_period_end(self) -> None:
        cdn = self._service("asset-cdn")
        open_one = next(o for o in cdn["outages"] if "INC-1051" in o["incident_ids"])
        self.assertTrue(open_one["end"].startswith("2026-08-01 00:00"))

    def test_a_service_with_no_incidents_still_appears_and_owes_nothing(self) -> None:
        feed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        feed["incidents"] = []
        account = reconcile(feed)["Account"][0]
        self.assertEqual(len(account["Services"]), 3)
        self.assertEqual(account["BreachCount"], 0)
        self.assertEqual(account["TotalCreditUsd"], "0.00")
        self.assertFalse(account["RegulatoryNotice"])
        for service in account["Services"]:
            self.assertEqual(service["availability_pct"], "100.0000")

    def test_regulatory_notice_needs_a_long_sev1_not_merely_a_long_outage(self) -> None:
        self.assertTrue(self.account["RegulatoryNotice"])
        feed = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for incident in feed["incidents"]:
            if incident["service"] == "transcode-queue":
                incident["severity"] = "SEV3"
        # asset-cdn's long outage is SEV2, so downgrading the long SEV1 should
        # clear the notice even though plenty of downtime remains.
        account = reconcile(feed)["Account"][0]
        self.assertFalse(account["RegulatoryNotice"])
        self.assertGreater(account["BreachCount"], 0)

    def test_totals_agree_with_the_per_service_lines(self) -> None:
        line_sum = sum(float(s["credit_usd"]) for s in self.account["Services"])
        self.assertEqual(f"{line_sum:.2f}", self.account["TotalCreditUsd"])
        self.assertEqual(
            self.account["BreachCount"],
            sum(1 for s in self.account["Services"] if s["breached"]),
        )


if __name__ == "__main__":
    unittest.main()
