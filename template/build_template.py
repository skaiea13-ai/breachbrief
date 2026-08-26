"""Build the SLA credit memo template as a .docx Doctavian can generate from.

The template carries the document logic, not the agent. The agent hands over a
reconciled set of facts; every decision about what appears on the page — which
sections exist, how many rows, what the totals are — is expressed here in
Doctavian elements and expressions, so one template renders a clean month and a
catastrophic one correctly without the caller branching.
"""
from __future__ import annotations

import pathlib

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

OUT = pathlib.Path(__file__).resolve().parent / "sla-credit-memo.docx"


def para(doc, text: str, *, bold: bool = False, size: int = 10, space_after: int = 4):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    p.paragraph_format.space_after = Pt(space_after)
    return p


def build() -> pathlib.Path:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)

    # ---- Header -----------------------------------------------------------
    title = doc.add_heading("Service Level Credit Memo", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para(doc, "{!Account[0].Name} — {!Account[0].Tier} agreement", bold=True, size=12)
    para(doc,
         "Billing period {!$format(date(Account[0].PeriodStart), 'date', 'medium')} "
         "to {!$format(date(Account[0].PeriodEnd), 'date', 'medium')}  ·  "
         "contract availability {!Account[0].ContractUptimePct}%  ·  "
         "monthly fee {!$format($toDecimal(Account[0].MonthlyFeeUsd), 'number', 'currency')}")

    # ---- Outcome line: one of two mutually exclusive paragraphs -----------
    doc.add_paragraph()
    para(doc, '<mdoc:paragraph name="cleanMonth" hidden="{!$toDecimal(Account[0].BreachCount) > 0}">')
    para(doc,
         "Every service under this agreement met its availability commitment for the "
         "period. No credit is owed and no signature is required.", bold=True)
    para(doc, "</mdoc:paragraph>")

    para(doc, '<mdoc:paragraph name="breachMonth" hidden="{!$toDecimal(Account[0].BreachCount) == 0}">')
    para(doc,
         "{!Account[0].BreachCount} of {!$count(Account[0].Services)} services fell below "
         "the {!Account[0].ContractUptimePct}% commitment. A credit of "
         "{!$format($toDecimal(Account[0].TotalCreditUsd), 'number', 'currency')} is owed "
         "and requires countersignature below.", bold=True)
    para(doc, "</mdoc:paragraph>")

    # ---- Regulatory notice: only for a long SEV1 --------------------------
    para(doc, '<mdoc:paragraph name="regulatoryNotice" hidden="{!$Account[0].RegulatoryNotice == false}">')
    para(doc,
         "REGULATORY NOTICE — this period contains a severity 1 outage exceeding four "
         "hours. Notification obligations under the master agreement are triggered.",
         bold=True)
    para(doc, "</mdoc:paragraph>")

    # ---- Per-service breakdown -------------------------------------------
    doc.add_paragraph()
    doc.add_heading("Availability by service", level=1)
    para(doc, '<mdoc:repeater name="services" value="Account[0].Services" variable="svc">')

    para(doc, "{!#svc#.name}", bold=True, size=11)
    para(doc,
         "Availability {!#svc#.availability_pct}%  ·  "
         "{!#svc#.down_minutes} minutes down  ·  "
         "{!#svc#.outage_count} outages reconciled from {!#svc#.ticket_count} tickets  ·  "
         "worst severity {!#svc#.worst_severity}")

    para(doc, '<mdoc:paragraph name="svcCredit" hidden="{!$#svc#.breached == false}">')
    para(doc,
         "Below commitment. Credit at {!#svc#.credit_rate_pct}% of the monthly fee = "
         "{!$format($toDecimal(#svc#.credit_usd), 'number', 'currency')}.")
    para(doc, "</mdoc:paragraph>")

    para(doc, '<mdoc:paragraph name="svcOk" hidden="{!$#svc#.breached == true}">')
    para(doc, "Met commitment. No credit for this service.")
    para(doc, "</mdoc:paragraph>")

    # Nested repeater: the merged outages behind this service's number.
    para(doc, '<mdoc:repeater name="outages" value="#svc#.outages" variable="out">')
    para(doc,
         "    {!#out#.incident_ids}  ·  {!#out#.severity}  ·  "
         "{!#out#.start} to {!#out#.end}  ·  {!#out#.minutes} min  ·  {!#out#.summary}")
    para(doc, '<mdoc:text name="mergedNote" italic="true" hidden="{!$toDecimal(#out#.merged_from) &lt; 2}">'
              "        merged from {!#out#.merged_from} separate tickets covering one outage"
              "</mdoc:text>")
    para(doc, "</mdoc:repeater>")

    doc.add_paragraph()
    para(doc, "</mdoc:repeater>")

    # ---- What was excluded, and why --------------------------------------
    para(doc, '<mdoc:paragraph name="exclusions" hidden="{!$toDecimal(Account[0].ExcludedCount) == 0}">')
    doc.add_heading("Excluded from this calculation", level=1)
    para(doc,
         "{!Account[0].ExcludedCount} reported rows did not contribute to the credit. "
         "They are listed so the figure can be audited rather than trusted.")
    para(doc, "</mdoc:paragraph>")

    para(doc, '<mdoc:repeater name="excluded" value="Account[0].Excluded" variable="ex">')
    para(doc, "    {!#ex#.id}  ·  {!#ex#.service}  ·  {!#ex#.reason}")
    para(doc, "</mdoc:repeater>")

    # ---- Totals -----------------------------------------------------------
    doc.add_paragraph()
    doc.add_heading("Total", level=1)
    para(doc,
         "Services under agreement: {!$count(Account[0].Services)}  ·  "
         "in breach: {!Account[0].BreachCount}  ·  "
         "total downtime {!$sum(Account[0].Services, \"down_minutes\")} minutes of "
         "{!Account[0].PeriodMinutes} in the period.")
    para(doc,
         "Credit payable: {!$format($toDecimal(Account[0].TotalCreditUsd), 'number', 'currency')}",
         bold=True, size=12)

    # ---- Signature block, only when money changes hands -------------------
    para(doc, '<mdoc:paragraph name="signatureBlock" hidden="{!$toDecimal(Account[0].TotalCreditUsd) == 0}">')
    doc.add_paragraph()
    para(doc, "Approved for credit", bold=True, size=11)
    para(doc, "Service provider representative: ________________________    Date: ____________")
    para(doc, "{!Account[0].Name} representative: ________________________    Date: ____________")
    para(doc, "</mdoc:paragraph>")

    para(doc, " ")
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"template written: {path} ({path.stat().st_size} bytes)")
