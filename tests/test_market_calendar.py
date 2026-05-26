import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime import market_calendar


class MarketCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        market_calendar._KR_HOLIDAY_CACHE.clear()

    def test_kis_holiday_pagination_includes_late_month_holidays(self) -> None:
        calls: list[dict[str, str]] = []

        def fake_get(_url: str, *, headers: dict[str, str], params: dict[str, str], timeout: int) -> object:
            calls.append(dict(params))
            if len(calls) == 1:
                payload = {
                    "ctx_area_fk": "20260501            ",
                    "ctx_area_nk": "20260524            ",
                    "output": [
                        {"bass_dt": "20260522", "opnd_yn": "Y"},
                        {"bass_dt": "20260523", "opnd_yn": "N"},
                        {"bass_dt": "20260524", "opnd_yn": "N"},
                    ],
                }
                return SimpleNamespace(
                    headers={"tr_cont": "M"},
                    json=lambda: payload,
                    raise_for_status=lambda: None,
                )

            payload = {
                "output": [
                    {"bass_dt": "20260525", "opnd_yn": "N"},
                    {"bass_dt": "20260526", "opnd_yn": "Y"},
                ],
            }
            return SimpleNamespace(
                headers={"tr_cont": ""},
                json=lambda: payload,
                raise_for_status=lambda: None,
            )

        fake_client = SimpleNamespace(
            URL_BASE="https://example.test",
            ACCESS_TOKEN="token",
            APP_KEY="key",
            APP_SECRET="secret",
        )

        with patch.object(market_calendar, "_kis_client", return_value=fake_client), patch.object(
            market_calendar.requests,
            "get",
            side_effect=fake_get,
        ):
            snapshot = market_calendar.build_snapshot(date(2026, 5, 25))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["CTX_AREA_FK"], "20260501")
        self.assertEqual(calls[1]["CTX_AREA_NK"], "20260524")
        self.assertFalse(snapshot.kr_open_today)
        self.assertEqual(snapshot.kr_last_trading_day, "2026-05-22")


if __name__ == "__main__":
    unittest.main()
