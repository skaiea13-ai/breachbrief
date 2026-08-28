"""BreachBrief: read a messy incident feed and produce an SLA credit memo.

Run it against a feed and it reconciles the incidents, uploads the facts and the
template, and asks Doctavian to render the memo. The reconciliation is the
agent's job; deciding what the document says is the template's.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

from . import doctavian
from .reconcile import reconcile

TEMPLATE = Path(__file__).resolve().parents[1] / "template" / "sla-credit-memo.docx"


def presentation_payload(payload: dict) -> dict:
    """Flatten variable-length audit records for the proven direct-field renderer."""
    rendered = copy.deepcopy(payload)
    account = rendered["Account"][0]
    breach_count = int(account["BreachCount"])
    credit = str(account["TotalCreditUsd"])
    account["Outcome"] = (
        f"Credit review required: {breach_count} services missed the SLA; ${credit} is payable."
        if breach_count
        else "All services met the SLA. No credit is payable."
    )
    account["RegulatoryStatus"] = (
        "Triggered by a severity 1 outage longer than four hours."
        if account["RegulatoryNotice"]
        else "Not triggered for this billing period."
    )
    account["ServiceCount"] = len(account["Services"])
    account["TotalDowntimeMinutes"] = sum(
        int(service["down_minutes"]) for service in account["Services"]
    )
    account["ServiceAudit"] = "\n".join(
        (
            f"{index}. {service['name']}: availability {service['availability_pct']}%; "
            f"{service['down_minutes']} minutes down across {service['outage_count']} "
            f"outages from {service['ticket_count']} tickets; worst severity "
            f"{service['worst_severity']}; breach {str(service['breached']).lower()}; "
            f"credit {service['credit_rate_pct']}% (${service['credit_usd']})."
        )
        for index, service in enumerate(account["Services"], start=1)
    ) or "No contracted services were present."
    outage_lines = []
    for service in account["Services"]:
        for outage in service["outages"]:
            outage_lines.append(
                f"{service['name']} / {outage['incident_ids']}: {outage['start']} to "
                f"{outage['end']}; {outage['minutes']} minutes; severity "
                f"{outage['severity']}; merged from {outage['merged_from']} tickets; "
                f"{outage['summary']}"
            )
    account["OutageAudit"] = "\n".join(outage_lines) or "No billable outages were present."
    account["ExclusionAudit"] = "\n".join(
        f"{item['id']} / {item['service']}: {item['reason']}"
        for item in account["Excluded"]
    ) or "No source rows were excluded."
    return rendered


def summarise(payload: dict) -> str:
    account = payload["Account"][0]
    lines = [
        f"{account['Name']} — {account['Tier']} agreement",
        f"  period        {account['PeriodStart']} to {account['PeriodEnd']}",
        f"  services      {len(account['Services'])} under SLA, {account['BreachCount']} in breach",
        f"  credit        ${account['TotalCreditUsd']}",
        f"  excluded      {account['ExcludedCount']} rows",
    ]
    if account["RegulatoryNotice"]:
        lines.append("  regulatory    notice triggered by a SEV1 over four hours")
    for service in account["Services"]:
        state = f"BREACH {service['credit_rate_pct']}%" if service["breached"] else "ok"
        lines.append(
            f"    {service['name']:<18}{service['availability_pct']:>10}%  "
            f"{service['down_minutes']:>6} min  "
            f"{service['outage_count']} outages from {service['ticket_count']} tickets  {state}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="breachbrief", description=__doc__)
    parser.add_argument("feed", type=Path, help="incident feed JSON")
    parser.add_argument("--name", default="sla-credit-memo",
                        help="name of the generated document")
    parser.add_argument("--format", default="pdf", choices=("pdf", "docx"),
                        help="output format")
    parser.add_argument("--output", type=Path,
                        help="where to save the generated document")
    parser.add_argument("--dry-run", action="store_true",
                        help="reconcile and print the facts without calling Doctavian")
    parser.add_argument("--facts-out", type=Path,
                        help="also write the reconciled facts to this path")
    args = parser.parse_args(argv)

    feed = json.loads(args.feed.read_text(encoding="utf-8"))
    payload = reconcile(feed)
    print(summarise(payload))

    if args.facts_out:
        args.facts_out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.facts_out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        args.facts_out.chmod(0o600)
        print(f"\nfacts written to {args.facts_out.name}")

    if args.dry_run:
        print("\ndry run: no Doctavian call made")
        return 0

    if not TEMPLATE.exists():
        print("\ntemplate missing: template/sla-credit-memo.docx"
              "\nrun: python3 template/build_template.py",
              file=sys.stderr)
        return 2

    try:
        template_urn = doctavian.upload_template(TEMPLATE)
        print("\ntemplate uploaded")
        data_urn = doctavian.upload_data(presentation_payload(payload))
        print("data uploaded")
        result = doctavian.generate(template_urn, data_urn,
                                    name=args.name, file_format=args.format)
        document = doctavian.download_document(
            doctavian.document_urn(result), file_format=args.format
        )
    except doctavian.DoctavianError as error:
        print(f"\nDoctavian call failed: {error}", file=sys.stderr)
        if error.codes:
            print(f"  codes:     {', '.join(error.codes)}", file=sys.stderr)
        if not error.retryable:
            print("  retry:     no", file=sys.stderr)
        if error.remediation:
            print(f"  next:      {error.remediation}", file=sys.stderr)
        return 1

    output = args.output or Path(f"{args.name}.{args.format}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_bytes(document)
    temporary.chmod(0o600)
    temporary.replace(output)
    output.chmod(0o600)
    print(
        "document generated  "
        f"{output.name}  {len(document)} bytes  sha256={hashlib.sha256(document).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
