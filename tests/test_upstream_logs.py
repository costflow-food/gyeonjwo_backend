import unittest

import httpx

from app.core.request_context import new_request_id
from app.core.upstream_logs import observe_data_api


class UpstreamLogTests(unittest.TestCase):
    def test_data_api_failures_keep_original_result_and_request_id(self):
        for failure in (httpx.RemoteProtocolError, httpx.ConnectError,
                        httpx.ConnectTimeout, httpx.ReadTimeout, 504):
            with self.subTest(failure=failure):
                def respond(request):
                    if failure == 504:
                        return httpx.Response(504)
                    raise failure('private-error', request=request)

                request_id = new_request_id()
                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    observe_data_api(client)
                    url = 'https://example.test/rest/v1/work_cases?private=query'
                    with self.assertLogs('gyeonjwo', level='WARNING') as logs:
                        if failure == 504:
                            self.assertEqual(client.get(url).status_code, 504)
                        else:
                            with self.assertRaises(failure):
                                client.get(url)
                    status = '504' if failure == 504 else 'none'
                    self.assertEqual(len(logs.output), 1)
                    self.assertRegex(logs.output[0], rf'request_id={request_id} stage=data_api upstream_status={status} duration_ms=\d+\.\d+$')
                    self.assertNotIn('private', logs.output[0])
