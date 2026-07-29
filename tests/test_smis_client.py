import unittest
from concurrent.futures import ThreadPoolExecutor

from core.integrations.smis import SmisClient, SmisClientError

DETAIL = {
    "code": 200,
    "data": {
        "id": 61753,
        "appid": 730,
        "hashName": "Kilowatt Case",
        "cnName": "千瓦武器箱",
    },
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = DETAIL if payload is None else payload
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses, clock):
        self.headers = {}
        self.responses = list(responses)
        self.clock = clock
        self.request_times = []
        self.requests = []

    def request(self, *args, **kwargs):
        self.request_times.append(self.clock())
        self.requests.append((args, kwargs))
        return self.responses.pop(0)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client_with(responses, *, retries=3, interval=1.0):
    fake_time = FakeTime()
    session = FakeSession(responses, fake_time.clock)
    client = SmisClient(
        session=session,
        max_retries=retries,
        min_request_interval=interval,
    )
    client._clock = fake_time.clock
    client._sleep = fake_time.sleep
    return client, session, fake_time


class SmisClientTestCase(unittest.TestCase):
    def test_global_rate_limit_applies_across_threads(self):
        client, session, fake_time = client_with(
            [FakeResponse(), FakeResponse(), FakeResponse()], interval=1.5
        )

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(client.fetch_metadata, [61753] * 3))

        self.assertEqual(len(results), 3)
        self.assertEqual(session.request_times, [0.0, 1.5, 3.0])
        self.assertEqual(fake_time.sleeps, [1.5, 1.5])

    def test_access_denied_and_other_client_errors_do_not_retry(self):
        for status, message in ((403, "拒绝访问"), (404, "不可接受")):
            with self.subTest(status=status):
                client, session, _ = client_with(
                    [FakeResponse(status), FakeResponse()], retries=3
                )
                with self.assertRaisesRegex(SmisClientError, message):
                    client.fetch_metadata(61753)
                self.assertEqual(len(session.request_times), 1)

    def test_rate_limit_respects_retry_after_and_server_error_backs_off(self):
        client, session, fake_time = client_with([
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(503),
            FakeResponse(),
        ])

        result = client.fetch_metadata(61753)

        self.assertEqual(result["smis_id"], 61753)
        self.assertEqual(len(session.request_times), 3)
        self.assertEqual(fake_time.sleeps, [7.0, 2])

    def test_retryable_error_reports_exhaustion(self):
        client, session, _ = client_with(
            [FakeResponse(500), FakeResponse(502)], retries=2
        )

        with self.assertRaisesRegex(SmisClientError, "重试耗尽.*HTTP 502"):
            client.fetch_metadata(61753)
        self.assertEqual(len(session.request_times), 2)

    def test_exchange_candidates_use_required_filters_and_map_snapshots(self):
        payload = {
            "code": 200,
            "data": [{
                "id": 100,
                "appid": 730,
                "hashName": "Example Item",
                "cnName": "示例饰品",
                "updateTime": "2026-07-30 01:00:00",
                "buffSellPrice": 70,
                "buffSellNum": 10,
                "steamSellPrice": 115,
                "steamSellNum": 200,
                "steamTransactionQuantity": 100,
            }],
        }
        client, session, _ = client_with([FakeResponse(payload=payload)])

        rows = client.fetch_exchange_candidates(
            min_price=30,
            max_price=500,
            min_transaction_quantity=50,
            max_ratio=0.8,
            max_age_minutes=120,
            limit=50,
        )

        request_args, request_kwargs = session.requests[0]
        self.assertEqual(request_args[:2], ("POST", "https://smis.club/api/commodity/exchange"))
        self.assertEqual(request_kwargs["json"]["model"], 0)
        self.assertEqual(request_kwargs["json"]["maxRate"], 0.8)
        self.assertEqual(request_kwargs["json"]["buffTax"], 0.015)
        self.assertEqual(rows[0].smis_id, 100)
        self.assertEqual(rows[0].steam_transaction_quantity, 100)

    def test_exchange_candidates_skip_malformed_rows(self):
        payload = {
            "code": 200,
            "data": [None, {"id": "broken"}],
        }
        client, _, _ = client_with([FakeResponse(payload=payload)])

        rows = client.fetch_exchange_candidates(
            min_price=30,
            max_price=500,
            min_transaction_quantity=50,
            max_ratio=0.8,
            max_age_minutes=120,
        )

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
