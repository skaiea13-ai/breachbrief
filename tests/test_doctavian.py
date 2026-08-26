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


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


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
            return_value=_Response({"result": {"data": {}}}),
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
        self.assertEqual(payload["data"]["loadMethod"], "Storage")
        self.assertEqual(payload["document"]["deliveryMethod"], "Storage")


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
