"""BreachBrief: read a messy incident feed, produce a signed-ready credit memo.

Run it against a feed and it reconciles the incidents, uploads the facts and the
template, and asks Doctavian to render the memo. The reconciliation is the
agent's job; deciding what the document says is the template's.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import doctavian
from .reconcile import reconcile

TEMPLATE = Path(__file__).resolve().parents[1] / "template" / "sla-credit-memo.docx"


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
    parser.add_argument("--dry-run", action="store_true",
                        help="reconcile and print the facts without calling Doctavian")
    parser.add_argument("--facts-out", type=Path,
                        help="also write the reconciled facts to this path")
    args = parser.parse_args(argv)

    feed = json.loads(args.feed.read_text(encoding="utf-8"))
    payload = reconcile(feed)
    print(summarise(payload))

    if args.facts_out:
        args.facts_out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nfacts written to {args.facts_out}")

    if args.dry_run:
        print("\ndry run: no Doctavian call made")
        return 0

    if not TEMPLATE.exists():
        print(f"\ntemplate missing: {TEMPLATE}\nrun: python3 template/build_template.py",
              file=sys.stderr)
        return 2

    try:
        template_urn = doctavian.upload_template(TEMPLATE)
        print(f"\ntemplate uploaded  {template_urn}")
        data_urn = doctavian.upload_data(payload)
        print(f"data uploaded      {data_urn}")
        result = doctavian.generate(template_urn, data_urn,
                                    name=args.name, file_format=args.format)
    except doctavian.DoctavianError as error:
        print(f"\nDoctavian call failed: {error}", file=sys.stderr)
        if error.codes:
            print(f"  codes:     {', '.join(error.codes)}", file=sys.stderr)
        if error.event_ids:
            print(f"  event ids: {', '.join(error.event_ids)}", file=sys.stderr)
        if not error.retryable:
            print("  retry:     no", file=sys.stderr)
        if error.remediation:
            print(f"  next:      {error.remediation}", file=sys.stderr)
        return 1

    print("document generated")
    print(json.dumps(result, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
