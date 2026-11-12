"""The shared India clock and NSE calendar (PLAN_INDIA_ZERODHA.md, B1).

Every tool carries the same block; test_repo checks the copies are byte-identical,
and these tests run the behaviour against each copy as loaded."""

import datetime
import unittest

from _load import load

CLOCK_TOOLS = [("trader-alerts", "alerts"), ("trader-broker", "ticket"), ("trader-budget", "size"),
               ("trader-entry", "entry"), ("trader-exit", "exit"), ("trader-news", "news"),
               ("trader-risk", "check"), ("trader-scanner", "scan"), ("trader-timemachine", "timemachine")]
MODS = [(f, load(f, n)) for f, n in CLOCK_TOOLS]
DT = datetime.datetime
D = datetime.date

# NSE's 2026 list as published (api/holiday-master, read 2026-11-12): 16 weekday closures
WEEKDAY_HOLIDAYS = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
                    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
                    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"]
WEEKEND_HOLIDAYS = ["2026-02-15", "2026-03-21", "2026-08-15", "2026-11-08"]


class IndiaClock(unittest.TestCase):
    def each(self):
        for folder, mod in MODS:
            with self.subTest(tool=folder):
                yield mod

    def test_utc_to_india_is_plus_five_thirty_all_year(self):
        for m in self.each():
            self.assertEqual(m.to_ist(DT(2026, 11, 12, 3, 45)), DT(2026, 11, 12, 9, 15))
            self.assertEqual(m.to_ist(DT(2026, 1, 5, 9, 30)), DT(2026, 1, 5, 15, 0))   # winter
            self.assertEqual(m.to_ist(DT(2026, 7, 6, 9, 30)), DT(2026, 7, 6, 15, 0))   # summer: no DST
            self.assertEqual(m.to_ist(DT(2026, 12, 31, 20, 0)), DT(2027, 1, 1, 1, 30))  # crosses the date
            aware = DT(2026, 11, 12, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
            self.assertEqual(m.to_ist(aware), DT(2026, 11, 12, 9, 0))
            self.assertIsNone(m.to_ist(aware).tzinfo)

    def test_india_to_utc_is_the_inverse(self):
        for m in self.each():
            self.assertEqual(m.ist_to_utc(DT(2026, 11, 12, 15, 0)), DT(2026, 11, 12, 9, 30))
            self.assertEqual(m.ist_to_utc(DT(2027, 1, 1, 1, 30)), DT(2026, 12, 31, 20, 0))

    def test_parse_reads_india_time_unless_an_offset_is_given(self):
        for m in self.each():
            self.assertEqual(m.parse_ist("2026-11-12 09:30"), DT(2026, 11, 12, 4, 0))
            self.assertEqual(m.parse_ist("2026-11-12T09:30:00"), DT(2026, 11, 12, 4, 0))
            self.assertEqual(m.parse_ist("2026-11-12T04:00Z"), DT(2026, 11, 12, 4, 0))
            self.assertEqual(m.parse_ist("2026-11-12T09:30+05:30"), DT(2026, 11, 12, 4, 0))
            self.assertEqual(m.parse_ist("2026-11-11T23:00-05:00"), DT(2026, 11, 12, 4, 0))
            self.assertIsNone(m.parse_ist(" 2026-11-12 09:30 ").tzinfo)
            with self.assertRaises(ValueError):
                m.parse_ist("half past nine")

    def test_the_2026_holidays_are_closed_with_their_names(self):
        for m in self.each():
            self.assertEqual(sorted(m.NSE_HOLIDAYS), sorted(WEEKDAY_HOLIDAYS + WEEKEND_HOLIDAYS))
            for day in WEEKDAY_HOLIDAYS:
                d = D.fromisoformat(day)
                self.assertLess(d.weekday(), 5, day)
                ok, why = m.nse_trading_day(d)
                self.assertFalse(ok, day)
                self.assertEqual(why, "NSE holiday: " + m.NSE_HOLIDAYS[day])
            self.assertEqual(m.nse_trading_day(D(2026, 10, 20)), (False, "NSE holiday: Dussehra"))

    def test_ordinary_weekdays_trade_and_weekends_do_not(self):
        for m in self.each():
            self.assertEqual(m.nse_trading_day(D(2026, 11, 12)), (True, ""))   # Thursday
            self.assertEqual(m.nse_trading_day(D(2026, 11, 9)), (True, ""))    # Monday between the Diwali days
            self.assertEqual(m.nse_trading_day(D(2026, 10, 19)), (True, ""))   # the day before Dussehra
            self.assertEqual(m.nse_trading_day(D(2026, 11, 14)), (False, "weekend"))
            self.assertEqual(m.nse_trading_day(D(2026, 11, 15)), (False, "weekend"))
            trading = sum(m.nse_trading_day(D(2026, 1, 1) + datetime.timedelta(days=i))[0] for i in range(365))
            self.assertEqual(trading, 261 - 16)  # 261 weekdays in 2026, less the 16 weekday closures

    def test_special_sessions_are_not_trading_days_for_this_pack(self):
        for m in self.each():
            ok, why = m.nse_trading_day(D(2026, 2, 1))
            self.assertFalse(ok)
            self.assertEqual(why, "NSE special session (no entries): Union Budget live session (Sunday)")
            ok, why = m.nse_trading_day(D(2026, 11, 8))
            self.assertFalse(ok)
            self.assertIn("Muhurat", why)

    def test_a_datetime_is_checked_like_its_date(self):
        # R11: a datetime's isoformat() carries the time and would miss both tables
        for m in self.each():
            self.assertEqual(m.nse_trading_day(DT(2026, 10, 20, 10, 0)), (False, "NSE holiday: Dussehra"))
            self.assertEqual(m.nse_trading_day(DT(2026, 11, 8, 18, 15))[0], False)
            self.assertIn("Muhurat", m.nse_trading_day(DT(2026, 11, 8, 18, 15))[1])
            self.assertEqual(m.nse_trading_day(DT(2026, 11, 12, 9, 30)), (True, ""))

    def test_the_session_constants_and_rupees(self):
        for m in self.each():
            self.assertEqual((m.NSE_OPEN, m.NSE_CLOSE, m.FO_CLOSE), (datetime.time(9, 15), datetime.time(15, 30),
                                                                      datetime.time(15, 15)))
            self.assertEqual((m.ENTRIES_FROM, m.ENTRIES_UNTIL), (datetime.time(9, 30), datetime.time(15, 0)))
            self.assertEqual((m.SQUARE_OFF, m.SQUARE_OFF_FO), (datetime.time(15, 25), datetime.time(15, 12)))
            self.assertEqual(m.ts_to_ist(1794368700), DT(2026, 11, 11, 9, 15))
            self.assertEqual(m.inr(1234567.8), "₹12,34,567.80")
            self.assertEqual(m.inr(-0.004), "₹0.00")
            self.assertEqual(m.inr(-1234.5), "-₹1,234.50")
            self.assertEqual(m.inr(100000000, 0), "₹10,00,00,000")

    def test_a_year_with_no_calendar_fails_closed(self):
        for m in self.each():
            self.assertEqual(m.NSE_CALENDAR_YEARS, (2026,))
            for d in (D(2027, 1, 4), D(2025, 12, 31)):  # a Monday and a Wednesday
                ok, why = m.nse_trading_day(d)
                self.assertFalse(ok)
                self.assertEqual(why, "no NSE holiday calendar for %d in this pack yet" % d.year)

    def test_maintenance_this_year_has_a_calendar(self):
        # Fails on 1 January of a year NSE's circular has not been added for. Add the year's
        # holidays from NSE's own circular (one data batch), then extend NSE_CALENDAR_YEARS.
        today = MODS[0][1].to_ist(DT.now(datetime.timezone.utc)).date()
        for m in self.each():
            self.assertIn(today.year, m.NSE_CALENDAR_YEARS,
                          "NSE holiday calendar for %d is missing: add it from NSE's circular" % today.year)


if __name__ == "__main__":
    unittest.main()
