"""Minimal Doctavian client for the calls this agent actually makes.

Authentication takes two headers, not one. The API key identifies the
subscription and a Google bearer token identifies the caller; sending only the
key returns 401 with "Authorization header is missing". Both come from the
environment so neither is ever written to disk or passed on a command line.
"""
from __future__ import annotations

import json
import mimetypes
import os
import ssl
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE_URL = "https://demo.api.doctavian.com"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIMEOUT_SECONDS = 60


class DoctavianError(RuntimeError):
    """An API call failed. Carries the codes Doctavian returns for triage."""

    def __init__(self, message: str, *, status: int | None = None,
                 codes: list[str] | None = None, event_ids: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.codes = codes or []
        self.event_ids = event_ids or []


def _credentials() -> tuple[str, str]:
    api_key = os.environ.get("DOCTAVIAN_API_KEY", "").strip()
    token = os.environ.get("DOCTAVIAN_TOKEN", "").strip()
    missing = [n for n, v in (("DOCTAVIAN_API_KEY", api_key),
                              ("DOCTAVIAN_TOKEN", token)) if not v]
    if missing:
        raise DoctavianError(
            "Missing credentials: " + ", ".join(missing) +
            ". See README for how to obtain them."
        )
    return api_key, token


def _describe_error(status: int, body: bytes) -> DoctavianError:
    codes: list[str] = []
    event_ids: list[str] = []
    detail = body.decode("utf-8", "replace")[:400]
    try:
        payload = json.loads(body)
        error = payload.get("error") or {}
        for inner in error.get("innerErrors") or []:
            if inner.get("code"):
                codes.append(str(inner["code"]))
            if inner.get("eventId"):
                event_ids.append(str(inner["eventId"]))
        external = error.get("externalErrors") or []
        detail = "; ".join(str(e) for e in external) or error.get("message") or detail
    except (ValueError, AttributeError):
        pass
    return DoctavianError(f"HTTP {status}: {detail}", status=status,
                          codes=codes, event_ids=event_ids)


def _request(method: str, path: str, *, body: bytes | None = None,
             content_type: str | None = None, base_url: str | None = None) -> dict:
    api_key, token = _credentials()
    url = (base_url or os.environ.get("DOCTAVIAN_BASE_URL") or DEFAULT_BASE_URL) + path
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-Api-Key", api_key)
    request.add_header("Accept", "application/json")
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise _describe_error(error.code, error.read()) from None
    except urllib.error.URLError as error:
        raise DoctavianError(f"Could not reach Doctavian: {error.reason}") from None


def _multipart(field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = f"----breachbrief{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(filename)[0] or DOCX_MIME
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def _first_file_id(response: dict, what: str) -> str:
    files = ((response.get("result") or {}).get("data") or {}).get("files") or []
    if not files or not files[0].get("id"):
        raise DoctavianError(f"{what} upload returned no file id: {json.dumps(response)[:200]}")
    return str(files[0]["id"])


def upload_template(path: str | Path) -> str:
    """Store a .docx template and return its URN."""
    path = Path(path)
    body, content_type = _multipart("file", path.name, path.read_bytes())
    response = _request("POST", "/v1/documents/template/upload",
                        body=body, content_type=content_type)
    return _first_file_id(response, "Template")


def upload_data(payload: dict) -> str:
    """Store the reconciled facts and return their URN."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = _request("POST", "/v1/documents/data/upload",
                        body=raw, content_type="application/json")
    return _first_file_id(response, "Data")


def generate(template_urn: str, data_urn: str, *, name: str,
             file_format: str = "pdf", template_name: str = "sla-credit-memo.docx",
             external_id: str | None = None) -> dict:
    """Render the template against the data and return the generate result."""
    body = json.dumps({
        "externalContext": {"id": external_id or f"breachbrief-{uuid.uuid4().hex[:8]}"},
        "template": {"name": template_name, "urn": template_urn,
                     "fileFormat": "docx", "loadMethod": "Storage"},
        "data": {"urn": data_urn, "loadMethod": "Storage"},
        "document": {"timezone": "(GMT+00:00) UTC", "locale": "en_US_POSIX",
                     "name": name, "fileFormat": file_format,
                     "deliveryMethod": "Storage"},
    }).encode("utf-8")
    return _request("POST", "/v1/documents/document/generate",
                    body=body, content_type="application/json")


def upload_signature_document(path: str | Path) -> str:
    """Store a rendered PDF on the signatures side and return its URN."""
    path = Path(path)
    body, content_type = _multipart("file", path.name, path.read_bytes())
    response = _request("POST", "/v1/signatures/document/upload",
                        body=body, content_type=content_type)
    return _first_file_id(response, "Signature document")


def create_envelope(document_urn: str, *, document_name: str, signers: list[dict],
                    subject: str, message: str, anchor: str) -> dict:
    """Draft an envelope with a signature field anchored in the memo text.

    Reference ids are integers the payload uses to wire fields to signers and
    documents; role and field type must be lower case or the API rejects them.
    The envelope is created as a draft. Sending is a separate, deliberate call.
    """
    documents = [{"referenceDocumentId": 1, "name": document_name,
                  "loadMethod": "Storage", "urn": document_urn}]
    recipients = []
    fields = []
    for index, signer in enumerate(signers, start=1):
        recipients.append({
            "referenceSignerId": index,
            "name": signer["name"],
            "email": signer["email"],
            "role": "signer",
            "signOrder": index,
            "mandatory": True,
        })
        fields.append({
            "referenceSignerId": index,
            "referenceDocumentId": 1,
            "name": f"signature{index}",
            "type": "signature",
            "anchorString": signer.get("anchor", anchor),
        })
    body = json.dumps({
        "documents": documents,
        "recipients": recipients,
        "fields": fields,
        "envelope": {"name": document_name, "subject": subject, "message": message},
    }).encode("utf-8")
    return _request("POST", "/v1/signatures/envelope/create",
                    body=body, content_type="application/json")


def send_envelope(envelope_id: str) -> dict:
    """Send a drafted envelope to its recipients. This mails real people."""
    return _request("GET", f"/v1/signatures/envelope/{envelope_id}/send")
