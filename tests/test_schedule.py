import datetime as dt
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('schedule_under_test', ROOT / 'linux/schedule.py')
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)


class ScheduleTests(unittest.TestCase):
    def test_exact_three_days_across_month_boundary(self):
        anchor = dt.date(2026, 10, 22)
        for offset in range(-1, 40):
            self.assertEqual(schedule.is_due(anchor + dt.timedelta(days=offset), anchor, 3), offset >= 0 and offset % 3 == 0)

    def test_invalid_intervals_and_missing_configuration_fail_closed(self):
        today = dt.date(2026, 10, 22)
        for days in (0, -1, 366):
            with self.assertRaises(ValueError):
                schedule.is_due(today, today, days)
        for env in ({}, {'CFST_SCHEDULE_ANCHOR': 'bad-date'}, {'CFST_SCHEDULE_ANCHOR': '2026-10-22', 'CFST_SCHEDULE_DAYS': '0'}):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(schedule.main(), 2)

    def test_repeated_checks_do_not_need_last_run_state(self):
        anchor = dt.date(2026, 10, 22)
        self.assertEqual([schedule.is_due(dt.date(2026, 11, 3), anchor, 3) for _ in range(3)], [True] * 3)
        self.assertEqual(schedule.CHINA.utcoffset(None), dt.timedelta(hours=8))


if __name__ == '__main__':
    unittest.main()
