# BreachBrief

An agent that turns a month of messy incident tickets into an SLA credit memo, and hands it to Doctavian to sign.

The agent decides what is true. The Doctavian template decides what the document says. That split is deliberate: the hard part of a credit memo is not merging fields into a letter, it is that the same template has to produce a one-line "nothing owed" note for a clean month and a six-section, signature-bearing memo for a bad one, without the caller branching.

## The problem

An on-call rota does not produce clean data. A real month looks like this:

- Severity written six ways: `SEV1`, `sev-1`, `P1`, `critical`, `Sev 3`, and blank.
- One outage, two tickets, because the second on-call opened their own. Summing both bills the customer twice for the same downtime.
- A duplicate row from the exporter.
- An incident nobody ever closed.
- An outage that started last month and ended in this one.
- A service that was never under SLA in the first place.

You cannot add up downtime until that is resolved, and you cannot put a number in front of a customer that you are unable to defend line by line.

## What the agent does

`breachbrief/reconcile.py` normalises the feed and produces the facts:

| Input problem | Resolution |
| --- | --- |
| Six spellings of severity | Collapsed to one vocabulary. A blank severity becomes `UNCLASSIFIED` rather than being guessed. |
| Two tickets, one outage | Overlapping and touching windows merge into a single billable outage that keeps the worst severity and every contributing ticket id. |
| Duplicate exporter row | Dropped, with the reason recorded. |
| Never-closed incident | Runs to the end of the billing period. |
| Outage straddling the month boundary | Clamped to the period that actually carried it. |
| Service outside the SLA | Excluded from the credit, still listed so the figure can be audited. |

It then computes availability per service, walks the credit ladder for the contract tier, and totals what is owed.

## Where Doctavian does the real work

Everything about the shape of the output lives in `template/sla-credit-memo.docx`, not in Python. The template holds:

- **7 conditional paragraphs.** The "no credit owed" line and the "credit owed, signature required" line are mutually exclusive and both live in the template, keyed off `Account[0].BreachCount`. The regulatory notice appears only when a SEV1 ran over four hours. The signature block does not exist at all when the credit is zero.
- **3 repeaters, one nested.** Services repeat; inside each service, its merged outages repeat. The exclusions list repeats separately.
- **Conditional inline text.** The "merged from N separate tickets" note only renders on outages that were actually merged, using `hidden` on an `mdoc:text` element.
- **Calculation in the document.** `$sum` over per-service downtime, `$count` of services, `$format` for currency and dates, `$toDecimal` so numbers add instead of concatenating.

One template, two extremes, no branching in the caller:

```
messy month:  3 of 3 services breached, $29,400.00 credit, regulatory notice, 2 rows excluded
clean month:  0 breaches, $0.00, no notice, no exclusions, no signature block
```

Then, when a credit is owed, the memo goes to `signatures/envelope/create` with a signature field anchored to the approval line in the generated text. A number this size should not leave the building without a countersignature.

## Run it

Python 3.11 or newer. No third-party dependencies for the agent itself; `python-docx` is only needed if you want to rebuild the template.

```bash
export DOCTAVIAN_API_KEY=your_key
export DOCTAVIAN_TOKEN=your_doctavian_oauth_token
python3 -m breachbrief.agent fixtures/incidents-messy.json
```

Reconcile without calling the API at all:

```bash
python3 -m breachbrief.agent fixtures/incidents-messy.json --dry-run
```

Rebuild the template from source:

```bash
python3 template/build_template.py
```

### Credentials

Doctavian needs **two** headers, not one. `X-Api-Key` identifies the subscription and `Authorization: Bearer` carries the OAuth token issued for that Doctavian environment. Sending only the key returns `401 Authorization header is missing`.

Use only the API key and bearer supplied through the sponsor-approved Doctavian onboarding or OAuth flow. Demo credentials are scoped to the demo host; a generic Google Cloud access token is not a substitute. Do not scrape tokens from browser storage. The client reads both values from the environment and never writes them to disk or passes them on a command line.

## Verify

```bash
python3 -m unittest discover -s tests -v
```

21 tests, no network. They cover the severity vocabulary, the merge rules (overlapping, touching, fully contained, worst-severity-wins), every rung of the credit ladder, each messy-input case end to end, the documented upload headers and generation payload, and terminal classification of the known demo-tenant storage failure.

## Layout

```
breachbrief/reconcile.py    incident feed -> defensible facts
breachbrief/doctavian.py    API client: template, data, generate, envelope
breachbrief/agent.py        CLI tying it together
template/build_template.py  builds the .docx template from source
template/sla-credit-memo.docx
fixtures/                   a messy month and a clean one
tests/                      16 offline tests
```

## Known issue on the demo environment

`documents/document/generate` currently returns 500 on the hackathon demo tenant:

```
COPY_FILE_GOOGLEDRIVE_FAILED
"The service drive has thrown an exception. HttpStatusCode is Forbidden.
 Request had insufficient authentication scopes."
```

Uploads succeed with the documented `X-Storage-Type` values and the Signatures path works, but generation still fails inside Doctavian's Google Drive-backed copy step. The client treats this exact code as a non-retryable external blocker so operators do not spend calls repeating it. A live retry is appropriate only after Doctavian repairs the demo tenant or documents and issues a replacement authorization path.

## Licence

MIT.
