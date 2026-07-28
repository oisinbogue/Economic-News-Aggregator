from datetime import date, datetime, timezone
from unittest.mock import patch

from pipeline import timeutil


class TestUtcNow:
    def test_returns_utc_aware_datetime(self):
        result = timeutil.utc_now()
        assert result.tzinfo == timezone.utc


class TestUtcToday:
    def test_matches_utc_now_date(self):
        assert timeutil.utc_today() == timeutil.utc_now().date()

    def test_uses_utc_date_not_local_date_near_midnight(self):
        # 23:30 UTC on Jan 1 is already Jan 2 in timezones ahead of UTC (e.g.
        # local system clock set to UTC+2). utc_today() must resolve to the
        # UTC date (Jan 1) regardless of what the local clock would say.
        fixed = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
        with patch("pipeline.timeutil.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            assert timeutil.utc_today() == date(2026, 1, 1)
            mock_dt.now.assert_called_once_with(timezone.utc)
