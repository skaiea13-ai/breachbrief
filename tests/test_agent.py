from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from breachbrief import agent
from breachbrief.agent import presentation_payload
from breachbrief.reconcile import reconcile
from template.build_template import PACKAGE_TIMESTAMP, build


def facts(case: str) -> dict:
    feed = json.loads((ROOT / "fixtures" / f"incidents-{case}.json").read_text())
    return reconcile(feed)


class PresentationPayloadTests(unittest.TestCase):
    def test_messy_case_flattens_every_audit_record_without_losing_totals(self) -> None:
        payload = presentation_payload(facts("messy"))
        account = payload["Account"][0]

        self.assertEqual(account["ServiceCount"], 3)
        self.assertEqual(len(account["ServiceAudit"].splitlines()), 3)
        self.assertEqual(len(account["ExclusionAudit"].splitlines()), 2)
        self.assertIn("Credit review required", account["Outcome"])
        self.assertIn("Triggered", account["RegulatoryStatus"])
        self.assertEqual(
            account["TotalDowntimeMinutes"],
            sum(service["down_minutes"] for service in account["Services"]),
        )
        for service in account["Services"]:
            self.assertIn(service["name"], account["ServiceAudit"])
            for outage in service["outages"]:
                self.assertIn(outage["incident_ids"], account["OutageAudit"])

    def test_clean_case_has_explicit_empty_audit_messages(self) -> None:
        account = presentation_payload(facts("clean"))["Account"][0]

        self.assertEqual(account["ServiceCount"], 3)
        self.assertEqual(account["BreachCount"], 0)
        self.assertEqual(account["TotalCreditUsd"], "0.00")
        self.assertIn("All services met", account["Outcome"])
        self.assertIn("INC-2001", account["OutageAudit"])
        self.assertEqual(account["ExclusionAudit"], "No source rows were excluded.")

    def test_flattening_does_not_mutate_reconciled_facts(self) -> None:
        original = facts("messy")
        snapshot = json.loads(json.dumps(original))

        presentation_payload(original)

        self.assertEqual(original, snapshot)


class CliPrivacyTests(unittest.TestCase):
    def test_success_output_does_not_print_remote_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "memo.pdf"
            facts_output = pathlib.Path(directory) / "facts.json"
            stdout = io.StringIO()
            with (
                mock.patch.object(agent.doctavian, "upload_template", return_value="secret-template-urn"),
                mock.patch.object(agent.doctavian, "upload_data", return_value="secret-data-urn"),
                mock.patch.object(agent.doctavian, "generate", return_value={"result": "created"}),
                mock.patch.object(agent.doctavian, "document_urn", return_value="secret-document-urn"),
                mock.patch.object(agent.doctavian, "download_document", return_value=b"%PDF-1.7\n"),
                contextlib.redirect_stdout(stdout),
            ):
                status = agent.main([
                    str(ROOT / "fixtures" / "incidents-clean.json"),
                    "--output",
                    str(output),
                    "--facts-out",
                    str(facts_output),
                ])

            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertTrue(facts_output.is_file())
            self.assertNotIn("secret-", stdout.getvalue())
            self.assertNotIn(directory, stdout.getvalue())


class TemplatePrivacyTests(unittest.TestCase):
    def test_template_is_reproducible_and_has_no_identity_or_time_metadata(self) -> None:
        stored = ROOT / "template" / "sla-credit-memo.docx"
        with tempfile.TemporaryDirectory() as directory:
            rebuilt = build(pathlib.Path(directory) / "memo.docx")
            self.assertEqual(rebuilt.read_bytes(), stored.read_bytes())

        with ZipFile(stored) as archive:
            names = archive.namelist()
            self.assertFalse(any(name.startswith(("docProps/", "customXml/")) for name in names))
            self.assertTrue(all(item.date_time == PACKAGE_TIMESTAMP for item in archive.infolist()))
            xml = b"".join(
                archive.read(name) for name in names if name.endswith((".xml", ".rels"))
            )
        for forbidden in (b"docProps/", b"customXml/", b"w:rsid", b"docId", b"paraId", b"textId"):
            self.assertNotIn(forbidden, xml)


if __name__ == "__main__":
    unittest.main()
