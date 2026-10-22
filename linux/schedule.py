#!/usr/bin/env python3
"""A calendar-day cadence in China time; no last-run state is written."""
import datetime as dt
import os
import sys

CHINA = dt.timezone(dt.timedelta(hours=8))


def is_due(today, anchor, days):
    if not 1 <= days <= 365:
        raise ValueError('CFST_SCHEDULE_DAYS must be between 1 and 365')
    elapsed = (today - anchor).days
    return elapsed >= 0 and elapsed % days == 0


def main():
    try:
        days = int(os.environ.get('CFST_SCHEDULE_DAYS', '3'))
        anchor = dt.date.fromisoformat(os.environ['CFST_SCHEDULE_ANCHOR'])
        return 0 if is_due(dt.datetime.now(CHINA).date(), anchor, days) else 1
    except (KeyError, ValueError):
        print('Invalid schedule: configure CFST_SCHEDULE_DAYS and CFST_SCHEDULE_ANCHOR in env.sh', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
