# BreachBrief

BreachBrief reconciles messy incident tickets into an auditable SLA credit memo, then calls Doctavian to render the result as a one-page PDF.

## The problem

An incident export cannot be billed as-is. One outage may have two ticket IDs. Rows may be duplicated, incidents can cross a billing boundary, and an open ticket may have no end time. Severity also arrives as `SEV1`, `sev-1`, `P1`, `critical`, `Sev 3`, or blank.

Those details change the money. Counting overlapping tickets twice inflates downtime. Dropping an open incident understates it. Including a service that is not covered by the agreement creates a credit that the customer was never owed.

## How the agent works

`breachbrief/reconcile.py` turns the incident feed into a defensible set of facts:

| Input problem | Resolution |
| --- | --- |
| Severity has six spellings | Map each known spelling to one value. Keep a blank value as `UNCLASSIFIED`. |
| Two tickets cover one outage | Merge overlapping or touching windows and retain every source ticket ID. |
| The exporter duplicated a row | Exclude the duplicate and record the reason. |
| An incident was never closed | End it at the billing-period boundary. |
| An outage crosses the month boundary | Count only the time inside the billing period. |
| A service is outside the SLA | Exclude it from the credit and keep it in the audit. |

The reconciler computes availability and the contract credit ladder for each service. `breachbrief/agent.py` then writes the variable-length service, outage, and exclusion audits as deterministic lines. It does not ask a language model to guess a severity, duration, or dollar amount.

The final upload follows Doctavian's data contract: one root `data` object, locale-formatted strings at every scalar leaf, and the original object and array structure left intact. The client uploads the DOCX template and data, checks for a terminal `201` result with a document URN, downloads the generated document by its GUID, and validates the returned PDF or DOCX before writing it.

## What Doctavian does

Doctavian does the document work: it binds the reconciled dataset to `template/sla-credit-memo.docx`, generates the customer-facing memo, stores it, and returns the downloadable PDF. The agent never assembles a PDF itself.

The same template handles both supplied fixtures:

| Fixture | Result |
| --- | --- |
| `incidents-messy.json` | 3 services in breach, 29,833 reconciled downtime minutes, 2 excluded rows, a regulatory notice, and a `$29,400.00` credit |
| `incidents-clean.json` | 0 services in breach, 9 downtime minutes, no excluded rows, and a `$0.00` credit |

Both paths were generated and downloaded from the sponsor's demo API. The messy case renders all three service lines, five reconciled outage records, and both exclusions on one page.

## Run it

Use Python 3.11 or newer. Obtain an API key and OAuth bearer through the sponsor's Doctavian onboarding flow, then set `DOCTAVIAN_API_KEY` and `DOCTAVIAN_TOKEN` in the environment. The demo tenant uses `https://demo.api.doctavian.com` by default; set `DOCTAVIAN_BASE_URL` only when the sponsor supplies another environment.

```bash
python3 -m breachbrief.agent fixtures/incidents-messy.json \
  --output out/sla-credit-memo.pdf
```

The CLI prints the reconciliation summary and stage status. It does not print credentials, upload identifiers, document URNs, or raw API responses. Generated files are written with owner-only permissions.

Run the reconciler without any network call:

```bash
python3 -m breachbrief.agent fixtures/incidents-messy.json --dry-run
```

`python-docx` is needed only to rebuild the committed template:

```bash
python3 -m pip install python-docx
python3 template/build_template.py
```

## Verify it

```bash
python3 -m unittest discover -s tests -v
```

The 34 network-free tests cover severity normalization, interval merging, boundary handling, every credit tier, presentation flattening, the sponsor's upload contract, generation status checks, download headers, document validation, CLI identifier redaction, and byte-reproducible template packaging.

## Repository layout

```text
breachbrief/reconcile.py    Incident feed to reconciled facts
breachbrief/agent.py        CLI and deterministic presentation fields
breachbrief/doctavian.py    Upload, generation, and download client
template/build_template.py  Reproducible DOCX template builder
template/sla-credit-memo.docx
fixtures/                   Messy and clean synthetic months
tests/                      34 network-free tests
```

## Safety boundaries

The fixtures contain synthetic data. Credentials stay in environment variables. The download client accepts only a GUID-backed Doctavian document, caps the response at 25 MB, and checks the file signature before saving it. This demo generates a memo; it does not send a signature request or email anyone.

## License

MIT.
