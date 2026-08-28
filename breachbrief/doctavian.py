"""Minimal Doctavian client for the calls this agent actually makes.

Authentication takes two headers, not one. The API key identifies the
subscription and a Doctavian-issued OAuth bearer identifies the caller; sending
only the key returns 401 with "Authorization header is missing". Both come from
the environment so neither is ever written to disk or passed on a command line.
"""
from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

DEFAULT_BASE_URL = "https://demo.api.doctavian.com"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIMEOUT_SECONDS = 60
MAX_DOCUMENT_BYTES = 25_000_000
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$"
)
_EN_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
DRIVE_SCOPE_CODES = frozenset({"COPY_FILE_GOOGLEDRIVE_FAILED"})
DRIVE_SCOPE_REMEDIATION = (
    "Do not retry this generation. The Doctavian tenant's document-storage "
    "identity lacks the Google Drive scope required by its copy step. Ask the "
    "sponsor to repair the tenant or issue a documented replacement "
    "authorization path, then run a fresh preflight."
)


class DoctavianError(RuntimeError):
    """An API call failed. Carries the codes Doctavian returns for triage."""

    def __init__(self, message: str, *, status: int | None = None,
                 codes: list[str] | None = None, event_ids: list[str] | None = None,
                 retryable: bool = True, remediation: str | None = None):
        super().__init__(message)
        self.status = status
        self.codes = codes or []
        self.event_ids = event_ids or []
        self.retryable = retryable
        self.remediation = remediation


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
        if error.get("code"):
            codes.append(str(error["code"]))
        for inner in error.get("innerErrors") or []:
            if inner.get("code"):
                codes.append(str(inner["code"]))
            if inner.get("eventId"):
                event_ids.append(str(inner["eventId"]))
        external = error.get("externalErrors") or []
        external_messages = [
            str(item.get("message") or item.get("userMessage") or item)
            if isinstance(item, dict) else str(item)
            for item in external
        ]
        detail = "; ".join(external_messages) or error.get("message") or detail
    except (ValueError, AttributeError):
        pass
    codes = list(dict.fromkeys(codes))
    event_ids = list(dict.fromkeys(event_ids))
    drive_scope_blocker = bool(DRIVE_SCOPE_CODES.intersection(codes))
    return DoctavianError(f"HTTP {status}: {detail}", status=status,
                          codes=codes, event_ids=event_ids,
                          retryable=not drive_scope_blocker,
                          remediation=DRIVE_SCOPE_REMEDIATION if drive_scope_blocker else None)


def _request(method: str, path: str, *, body: bytes | None = None,
             content_type: str | None = None, storage_type: str | None = None,
             base_url: str | None = None,
             expected_status: int | tuple[int, ...] | None = None) -> dict:
    api_key, token = _credentials()
    url = (base_url or os.environ.get("DOCTAVIAN_BASE_URL") or DEFAULT_BASE_URL) + path
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-Api-Key", api_key)
    request.add_header("Accept", "application/json")
    if content_type:
        request.add_header("Content-Type", content_type)
    if storage_type:
        request.add_header("X-Storage-Type", storage_type)
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as response:
            response_body = response.read()
            status = getattr(response, "status", None)
            allowed = ((expected_status,) if isinstance(expected_status, int)
                       else expected_status)
            if allowed is not None and status is not None and status not in allowed:
                raise DoctavianError(
                    f"Expected HTTP {' or '.join(str(item) for item in allowed)}, "
                    f"received HTTP {status}", status=status
                )
            return json.loads(response_body or b"{}")
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
        raise DoctavianError(f"{what} upload returned no file id")
    return str(files[0]["id"])


def upload_template(path: str | Path) -> str:
    """Store a .docx template and return its URN."""
    path = Path(path)
    body, content_type = _multipart("file", path.name, path.read_bytes())
    response = _request("POST", "/v1/documents/template/upload",
                        body=body, content_type=content_type,
                        storage_type="document-template")
    return _first_file_id(response, "Template")


def _format_en_date(value: str) -> str:
    match = _ISO_DATE.fullmatch(value)
    if match:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return f"{_EN_MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year}"

    match = _ISO_DATETIME.fullmatch(value)
    if not match:
        return value
    pattern = "%Y-%m-%d %H:%M:%S" if match.group(6) else "%Y-%m-%d %H:%M"
    parsed = datetime.strptime(value.replace("T", " "), pattern)
    hour = parsed.hour % 12 or 12
    suffix = "AM" if parsed.hour < 12 else "PM"
    seconds = f":{parsed.second:02d}" if match.group(6) else ""
    return (
        f"{_EN_MONTHS[parsed.month - 1]} {parsed.day}, {parsed.year}, "
        f"{hour}:{parsed.minute:02d}{seconds} {suffix}"
    )


def _stringify_leaf(value: object, *, locale: str) -> object:
    if isinstance(value, dict):
        return {str(key): _stringify_leaf(item, locale=locale)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_leaf(item, locale=locale) for item in value]
    if isinstance(value, str):
        return _format_en_date(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DoctavianError("Data contains a non-finite number")
        return format(value, ".15g")
    raise DoctavianError(f"Unsupported data value: {type(value).__name__}")


def prepare_data(payload: dict, *, locale: str = "en") -> dict:
    """Apply the sponsor's upload contract without changing object/array shape."""
    if locale != "en":
        raise DoctavianError(f"Unsupported data locale: {locale}")
    if not isinstance(payload, dict):
        raise DoctavianError("Document data must be a JSON object")

    if "data" in payload:
        if len(payload) != 1 or not isinstance(payload["data"], dict):
            raise DoctavianError("Document data must contain exactly one root data object")
        data = payload["data"]
    else:
        data = payload
    if "data" in data:
        raise DoctavianError("Refusing an ambiguous data.data envelope")
    return {"data": _stringify_leaf(data, locale=locale)}


def upload_data(payload: dict, *, locale: str = "en") -> str:
    """Store the reconciled facts and return their URN."""
    raw = json.dumps(
        prepare_data(payload, locale=locale),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    response = _request("POST", "/v1/documents/data/upload",
                        body=raw, content_type="application/json",
                        storage_type="document-data")
    return _first_file_id(response, "Data")


def generate(template_urn: str, data_urn: str, *, name: str,
             file_format: str = "pdf", template_name: str = "sla-credit-memo.docx",
             external_id: str | None = None, locale: str = "en",
             timezone: str = "Europe/Dublin") -> dict:
    """Render the template against the data and return the generate result."""
    body = json.dumps({
        "externalContext": {"id": external_id or f"breachbrief-{uuid.uuid4().hex[:8]}"},
        "template": {"name": template_name, "urn": template_urn,
                     "fileFormat": "docx", "loadMethod": "Storage", "options": {}},
        "data": {"urn": data_urn, "loadMethod": "Storage"},
        "document": {"timezone": timezone, "locale": locale,
                     "name": name, "fileFormat": file_format,
                     "deliveryMethod": "Storage", "path": "root", "options": {}},
    }).encode("utf-8")
    response = _request(
        "POST",
        "/v1/documents/document/generate",
        body=body,
        content_type="application/json",
        expected_status=(200, 201),
    )
    result = response.get("result") or {}
    document = (result.get("data") or {}).get("document") or {}
    if str(result.get("statusCode") or "") != "201" or not document.get("urn"):
        raise DoctavianError("Generation response was not terminal Created with a document URN")
    return response


def document_urn(response: dict) -> str:
    document = (((response.get("result") or {}).get("data") or {}).get("document") or {})
    urn = str(document.get("urn") or "")
    if not urn:
        raise DoctavianError("Generation returned no document URN")
    return urn


def _document_id(urn: str) -> str:
    candidate = urn.split(":", 1)[0]
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        raise DoctavianError("Generation returned an invalid document URN") from None


def _validate_document(payload: bytes, file_format: str) -> None:
    if file_format == "pdf":
        if not payload.startswith(b"%PDF-"):
            raise DoctavianError("Downloaded document is not a PDF")
        return
    if file_format == "docx":
        try:
            with ZipFile(BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except BadZipFile:
            raise DoctavianError("Downloaded document is not a DOCX") from None
        required = {"[Content_Types].xml", "word/document.xml"}
        if not required.issubset(names):
            raise DoctavianError("Downloaded document is not a DOCX")
        return
    raise DoctavianError(f"Unsupported document format: {file_format}")


def download_document(urn: str, *, file_format: str = "pdf") -> bytes:
    """Download and structurally validate a generated Storage document."""
    api_key, token = _credentials()
    base_url = os.environ.get("DOCTAVIAN_BASE_URL") or DEFAULT_BASE_URL
    url = base_url + "/v1/documents/document/" + quote(_document_id(urn)) + "/download"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-Api-Key", api_key)
    request.add_header("X-Storage-Type", "document-data")
    request.add_header("Accept", "application/octet-stream")
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as response:
            payload = response.read(MAX_DOCUMENT_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise _describe_error(error.code, error.read()) from None
    except urllib.error.URLError as error:
        raise DoctavianError(f"Could not reach Doctavian: {error.reason}") from None
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DoctavianError("Downloaded document exceeds the 25 MB safety limit")
    _validate_document(payload, file_format)
    return payload


def upload_signature_document(path: str | Path) -> str:
    """Store a rendered PDF on the signatures side and return its URN."""
    path = Path(path)
    body, content_type = _multipart("file", path.name, path.read_bytes())
    response = _request("POST", "/v1/signatures/document/upload",
                        body=body, content_type=content_type,
                        storage_type="document-input")
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
