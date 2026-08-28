from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from breachbrief import doctavian


FILE_RESPONSE = {
    "result": {"data": {"files": [{"id": "urn:doctavian:test-file"}]}}
}
GENERATED_RESPONSE = {
    "result": {
        "statusCode": "201",
        "message": "Created",
        "data": {
            "document": {
                "urn": "c72f4a1e-9d3b-4c5f-8a6e-1b2c3d4e5f6a:credit-memo.pdf"
            }
        },
    }
}


class _Response:
    def __init__(self, payload: dict | bytes, *, status: int = 200):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int | None = None) -> bytes:
        return self.payload if size is None else self.payload[:size]


def _headers(request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


class RequestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "DOCTAVIAN_API_KEY": "test-api-key",
                "DOCTAVIAN_TOKEN": "test-oauth-token",
                "DOCTAVIAN_BASE_URL": "https://example.invalid",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_authentication_headers_are_sent_without_changing_the_values(self) -> None:
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(FILE_RESPONSE),
        ) as urlopen:
            doctavian.upload_data({"Account": []})

        headers = _headers(urlopen.call_args.args[0])
        self.assertEqual(headers["authorization"], "Bearer test-oauth-token")
        self.assertEqual(headers["x-api-key"], "test-api-key")
        self.assertEqual(headers["accept"], "application/json")

    def test_data_upload_wraps_once_and_stringifies_locale_formatted_leaves(self) -> None:
        payload = {
            "Account": [{
                "Date": "2026-08-28",
                "Start": "2026-08-28 13:05",
                "Count": 2,
                "Rate": 3.5,
                "Active": True,
                "Missing": None,
                "Items": [{"Value": 1}],
            }]
        }
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(FILE_RESPONSE),
        ) as urlopen:
            doctavian.upload_data(payload)

        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {
            "data": {"Account": [{
                "Date": "Aug 28, 2026",
                "Start": "Aug 28, 2026, 1:05 PM",
                "Count": "2",
                "Rate": "3.5",
                "Active": "true",
                "Missing": "",
                "Items": [{"Value": "1"}],
            }]}
        })
        self.assertEqual(_headers(request)["content-type"], "application/json")
        self.assertEqual(_headers(request)["x-storage-type"], "document-data")

    def test_data_upload_does_not_double_wrap_the_sponsor_envelope(self) -> None:
        prepared = doctavian.prepare_data({"data": {"Account": [{"Count": 2}]}})
        self.assertEqual(prepared, {"data": {"Account": [{"Count": "2"}]}})

    def test_ambiguous_nested_data_envelope_is_rejected(self) -> None:
        with self.assertRaisesRegex(doctavian.DoctavianError, "data.data"):
            doctavian.prepare_data({"data": {"data": {"Account": []}}})

    def test_each_upload_uses_its_documented_storage_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            template = pathlib.Path(directory) / "template.docx"
            signature = pathlib.Path(directory) / "memo.pdf"
            template.write_bytes(b"docx")
            signature.write_bytes(b"pdf")
            cases = (
                (lambda: doctavian.upload_template(template), "document-template"),
                (lambda: doctavian.upload_data({"Account": []}), "document-data"),
                (lambda: doctavian.upload_signature_document(signature), "document-input"),
            )

            for call, expected in cases:
                with self.subTest(storage_type=expected), mock.patch.object(
                    doctavian.urllib.request,
                    "urlopen",
                    return_value=_Response(FILE_RESPONSE),
                ) as urlopen:
                    self.assertEqual(call(), "urn:doctavian:test-file")
                    headers = _headers(urlopen.call_args.args[0])
                    self.assertEqual(headers["x-storage-type"], expected)

    def test_generate_keeps_storage_load_and_delivery_methods(self) -> None:
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(GENERATED_RESPONSE, status=201),
        ) as urlopen:
            doctavian.generate(
                "urn:template",
                "urn:data",
                name="credit-memo",
                external_id="fixed-test-id",
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["externalContext"]["id"], "fixed-test-id")
        self.assertEqual(payload["template"]["loadMethod"], "Storage")
        self.assertEqual(payload["template"]["options"], {})
        self.assertEqual(payload["data"]["loadMethod"], "Storage")
        self.assertEqual(payload["document"]["deliveryMethod"], "Storage")
        self.assertEqual(payload["document"]["locale"], "en")
        self.assertEqual(payload["document"]["timezone"], "Europe/Dublin")
        self.assertEqual(payload["document"]["path"], "root")
        self.assertEqual(payload["document"]["options"], {})

    def test_generate_accepts_transport_200_only_with_inner_created_status(self) -> None:
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(GENERATED_RESPONSE, status=200),
        ):
            response = doctavian.generate("urn:template", "urn:data", name="memo")
        self.assertEqual(doctavian.document_urn(response), GENERATED_RESPONSE["result"]["data"]["document"]["urn"])

    def test_generate_requires_inner_created_status_and_document_urn(self) -> None:
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response({"result": {"data": {}}}, status=200),
        ), self.assertRaisesRegex(doctavian.DoctavianError, "terminal Created"):
            doctavian.generate("urn:template", "urn:data", name="memo")

    def test_generate_rejects_unexpected_transport_status(self) -> None:
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(GENERATED_RESPONSE, status=202),
        ), self.assertRaisesRegex(doctavian.DoctavianError, "Expected HTTP 200 or 201"):
            doctavian.generate("urn:template", "urn:data", name="memo")

    def test_download_uses_guid_and_required_storage_header(self) -> None:
        pdf = b"%PDF-1.7\ncanary"
        urn = "c72f4a1e-9d3b-4c5f-8a6e-1b2c3d4e5f6a:credit memo.pdf"
        with mock.patch.object(
            doctavian.urllib.request,
            "urlopen",
            return_value=_Response(pdf),
        ) as urlopen:
            self.assertEqual(doctavian.download_document(urn), pdf)

        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith(
            "/v1/documents/document/c72f4a1e-9d3b-4c5f-8a6e-1b2c3d4e5f6a/download"
        ))
        headers = _headers(request)
        self.assertEqual(headers["x-storage-type"], "document-data")
        self.assertEqual(headers["accept"], "application/octet-stream")

    def test_download_rejects_an_invalid_urn_before_network(self) -> None:
        with mock.patch.object(doctavian.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(doctavian.DoctavianError, "invalid document URN"):
                doctavian.download_document("not-a-guid:file.pdf")
        urlopen.assert_not_called()


class ErrorClassificationTests(unittest.TestCase):
    def test_drive_scope_failure_is_terminal_until_the_tenant_is_repaired(self) -> None:
        body = json.dumps({
            "error": {
                "innerErrors": [{
                    "code": "COPY_FILE_GOOGLEDRIVE_FAILED",
                    "eventId": "event-for-test",
                }],
                "externalErrors": [{
                    "message": "Request had insufficient authentication scopes."
                }],
            }
        }).encode("utf-8")

        error = doctavian._describe_error(500, body)

        self.assertFalse(error.retryable)
        self.assertIn("COPY_FILE_GOOGLEDRIVE_FAILED", error.codes)
        self.assertIn("Do not retry", error.remediation or "")
        self.assertEqual(error.event_ids, ["event-for-test"])

    def test_an_unknown_server_error_is_not_misclassified_as_the_known_blocker(self) -> None:
        body = json.dumps({
            "error": {
                "innerErrors": [{"code": "UNEXPECTED_FAILURE"}],
                "message": "Unexpected failure",
            }
        }).encode("utf-8")

        error = doctavian._describe_error(500, body)

        self.assertTrue(error.retryable)
        self.assertIsNone(error.remediation)


if __name__ == "__main__":
    unittest.main()
