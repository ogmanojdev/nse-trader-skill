import datetime
import io
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import FIX, common, load

store = common("sanyamstore")
cal = common("nsecal")
nsedata = common("nsedata")
scan = load("nse-scanner", "scan")
tm = load("nse-timemachine", "timemachine")

DB = os.path.join(FIX, "nse-common", "sanyam-candles.db")
DAY = datetime.date(2026, 11, 9)
CLOSE = datetime.datetime(2026, 11, 9, 15, 30)


def window(day):
    o, c = cal.session_or_raise(day)
    to_ts = lambda t: int((cal.ist_to_utc(datetime.datetime.combine(day, t))
                           - cal.EPOCH).total_seconds())
    return to_ts(o), to_ts(c)


class Symbols(unittest.TestCase):
    def test_equities_pass_through_and_indices_are_translated(self):
        # Sanyam names indices its own way and translates at the broker boundary.
        self.assertEqual(store.sanyam_symbol("RELIANCE"), "RELIANCE")
        self.assertEqual(store.sanyam_symbol("reliance"), "RELIANCE")
        self.assertEqual(store.sanyam_symbol("NIFTY"), "NIFTY 50")
        self.assertEqual(store.sanyam_symbol("^NSEI"), "NIFTY 50")
        self.assertEqual(store.sanyam_symbol("^NSEBANK"), "BANKNIFTY")


class Reading(unittest.TestCase):
    def test_a_missing_database_says_where_it_looked(self):
        with self.assertRaises(store.StoreError) as cm:
            store.find_db("/nope/trading_system.db")
        self.assertIn("/nope/trading_system.db", str(cm.exception))

    def test_incomplete_bars_are_excluded_by_default(self):
        """Mirrors Sanyam's own default at app/candles/store.py:110. A bar still
        forming has a close that can still move."""
        done = store.read_bars(DB, "TCS", "5m")
        every = store.read_bars(DB, "TCS", "5m", complete_only=False)
        self.assertEqual(len(done), 74)
        self.assertEqual(len(every), 75)

    def test_an_unknown_symbol_is_empty_not_an_error(self):
        self.assertEqual(store.read_bars(DB, "NOSUCHCO", "5m"), [])

    def test_bars_come_back_ascending_with_their_source(self):
        bars = store.read_bars(DB, "TCS", "5m")
        self.assertEqual([b["ts"] for b in bars], sorted(b["ts"] for b in bars))
        self.assertEqual({b["source"] for b in bars}, {"HISTORY"})

    def test_the_window_is_half_open_like_sanyams(self):
        start, end = window(DAY)
        self.assertEqual(len(store.read_bars(DB, "TCS", "5m", start, end)), 74)
        self.assertEqual(len(store.read_bars(DB, "TCS", "5m", start, start)), 0)


class Trust(unittest.TestCase):
    def test_worst_source_wins(self):
        # An aggregate is only as good as its worst input.
        self.assertEqual(store.worst_source(["HISTORY", "LIVE_AGG"]), "LIVE_AGG")
        self.assertEqual(store.worst_source(["HISTORY", "HISTORY_DB"]), "HISTORY_DB")
        self.assertEqual(store.worst_source(["HISTORY"]), "HISTORY")
        self.assertEqual(store.worst_source([None]), "UNKNOWN")

    def test_live_agg_is_not_trusted(self):
        self.assertNotIn("LIVE_AGG", store.TRUSTED_SOURCES)
        self.assertIn("HISTORY", store.TRUSTED_SOURCES)


class Equivalence(unittest.TestCase):
    """Sanyam's HISTORY bars must give the same answer as the Yahoo chart they came
    from, because they are the same session from the same exchange."""

    def setUp(self):
        with open(os.path.join(FIX, "nse-scanner", "chart-TCS.NS-1d-5m.json"),
                  encoding="utf-8") as fh:
            self.yahoo = nsedata.series_from_chart(fh.read())
        start, end = window(DAY)
        self.hist = store.as_series(store.read_bars(DB, "TCS", "5m", start, end))
        self.live = store.as_series(store.read_bars(DB, "TCSLIVE", "5m", start, end))

    def orb(self, ser):
        return scan.orb_for(scan.session_bars(ser, DAY, CLOSE), 15)

    def test_the_opening_range_is_identical_to_the_chart(self):
        y, h = self.orb(self.yahoo), self.orb(self.hist)
        self.assertEqual((h["range_high"], h["range_low"]),
                         (y["range_high"], y["range_low"]))
        self.assertEqual(h["hit"], y["hit"])

    def test_the_same_number_of_usable_bars(self):
        self.assertEqual(len(scan.session_bars(self.hist, DAY, CLOSE)),
                         len(scan.session_bars(self.yahoo, DAY, CLOSE)))

    def test_as_series_produces_the_shape_the_rest_of_the_code_speaks(self):
        # Deliberate: one code path for grid checks and arithmetic, not two.
        for key in ("t", "o", "h", "l", "c", "v", "meta"):
            self.assertIn(key, self.hist)
        self.assertEqual(self.hist["meta"]["exchangeTimezoneName"], "Asia/Kolkata")
        self.assertEqual(self.hist["meta"]["gmtoffset"], 19800)
        self.assertTrue(self.hist["meta"]["_from_sanyam"])

    def test_live_agg_narrows_the_range_which_is_why_it_is_refused(self):
        """The whole reason the guard exists, in numbers.

        A ~1 Hz price poll misses intrabar spikes. On this session TCS's 09:15 bar
        closed at 2456.70 and reached 2495.60, so a poll-built bar loses 39 rupees
        of the high. The range comes out 36% narrower, and a close that is inside
        the real range reads as a breakout.
        """
        y, l = self.orb(self.yahoo), self.orb(self.live)
        true_width = y["range_high"] - y["range_low"]
        live_width = l["range_high"] - l["range_low"]
        self.assertLess(live_width, true_width * 0.7)
        # the concrete false signal
        false_breakout = l["range_high"] + 0.05
        self.assertGreater(false_breakout, l["range_high"])
        self.assertLess(false_breakout, y["range_high"])


class Aggregation(unittest.TestCase):
    def test_1m_rolls_up_to_5m_on_the_ist_grid(self):
        one = store.read_bars(DB, "TCSONEM", "1m")
        self.assertEqual(len(one), 60)
        five = store.aggregate(one, 300)
        self.assertEqual(len(five), 12)
        for b in five:
            # IST is +19800s, a multiple of 300, so epoch buckets land on the IST grid
            self.assertEqual(b["ts"] % 300, 0)
            local = cal.to_ist(cal.utc_of(b["ts"]))
            self.assertIsNotNone(cal.slot_of(local))

    def test_an_aggregate_keeps_the_extremes_and_sums_volume(self):
        one = store.read_bars(DB, "TCSONEM", "1m")
        five = store.aggregate(one, 300)
        first = [b for b in one if b["ts"] < five[1]["ts"]]
        self.assertEqual(five[0]["h"], max(b["h"] for b in first))
        self.assertEqual(five[0]["l"], min(b["l"] for b in first))
        self.assertEqual(five[0]["c"], first[-1]["c"])
        self.assertEqual(five[0]["v"], sum(b["v"] for b in first))

    def test_an_aggregate_inherits_the_worst_source(self):
        mixed = [{"ts": 0, "o": 1, "h": 2, "l": 1, "c": 2, "v": 1, "source": "HISTORY"},
                 {"ts": 60, "o": 2, "h": 3, "l": 2, "c": 3, "v": 1, "source": "LIVE_AGG"}]
        self.assertEqual(store.aggregate(mixed, 300)[0]["source"], "LIVE_AGG")


class ScannerIntegration(unittest.TestCase):
    def run_orb(self, extra):
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = scan.main(["orb", "--sanyam", DB, "--now", "2026-11-09 15:30",
                            "--no-write"] + extra)
        return rc, buf.getvalue(), err.getvalue()

    def test_history_bars_are_read_with_no_network_call(self):
        rc, out, err = self.run_orb(["--symbols", "TCS"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("sanyam 5m", out)
        self.assertIn("1 charts read", out)

    def test_live_agg_is_refused_by_default(self):
        _rc, out, err = self.run_orb(["--symbols", "TCSLIVE"])
        self.assertIn("LIVE_AGG", err)
        self.assertIn("0 charts read", out)

    def test_live_agg_can_be_forced(self):
        _rc, out, _err = self.run_orb(["--symbols", "TCSLIVE", "--allow-live-agg"])
        self.assertIn("1 charts read", out)

    def test_1m_is_aggregated_when_5m_is_absent(self):
        _rc, out, _err = self.run_orb(["--symbols", "TCSONEM"])
        self.assertIn("1m aggregated", out)

    def test_an_empty_store_explains_how_to_fill_it(self):
        _rc, _out, err = self.run_orb(["--symbols", "NOSUCHCO"])
        self.assertIn("app.candles.fetch", err)
        self.assertIn("KITE_ACCESS_TOKEN", err)


class TimeMachineIntegration(unittest.TestCase):
    def test_too_little_history_is_refused_rather_than_dressed_up(self):
        """With one session each slot's pool holds one bar, so every draw is the same
        bar and 10,000 trials are one replay reported as a simulation."""
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = tm.main(["run", "--sanyam", DB, "--symbol", "TCS", "--entry", "2472.30",
                          "--stop", "2466.00", "--target", "2485.00", "--qty", "100",
                          "--at", "09:30", "--now", "2026-11-10 09:30", "--no-write"])
        self.assertEqual(rc, 1)
        self.assertIn("only 1 past session", err.getvalue())
        self.assertIn("deterministic replay", err.getvalue())

    def test_the_floor_can_be_lowered_on_purpose(self):
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = tm.main(["run", "--sanyam", DB, "--symbol", "TCS", "--entry", "2472.30",
                          "--stop", "2466.00", "--target", "2485.00", "--qty", "100",
                          "--at", "09:30", "--now", "2026-11-10 09:30", "--trials", "100",
                          "--min-sessions", "1", "--no-write"])
        self.assertEqual(rc, 0, buf.getvalue() + err.getvalue())
        self.assertIn("from sanyam 5m", buf.getvalue())



class EmptyHelp(unittest.TestCase):
    """The long explanation is for the first missing symbol, not every one.

    A watchlist of twenty names with an empty store printed fifteen lines of
    setup instructions twenty times, which buried the scan's own output.
    """

    def setUp(self):
        store.reset_empty_help()

    def tearDown(self):
        store.reset_empty_help()

    def test_the_first_miss_explains_itself(self):
        msg = store.empty_help("RELIANCE", "5m or 1m", "2026-11-13")
        self.assertIn("RELIANCE", msg)
        self.assertIn("local SQLite file", msg)
        self.assertIn("app.candles.fetch", msg)
        self.assertIn("KITE_ACCESS_TOKEN", msg)
        self.assertGreater(len(msg.splitlines()), 10)

    def test_every_miss_after_the_first_is_one_line(self):
        store.empty_help("RELIANCE", "5m or 1m", "2026-11-13")
        for sym in ("TCS", "SBIN", "INFY"):
            msg = store.empty_help(sym, "5m or 1m", "2026-11-13")
            self.assertEqual(len(msg.splitlines()), 1, msg)
            self.assertIn(sym, msg)
            self.assertNotIn("app.candles.fetch", msg)

    def test_a_fresh_run_explains_again(self):
        store.empty_help("RELIANCE", "5m or 1m", "2026-11-13")
        store.reset_empty_help()
        self.assertIn("local SQLite file", store.empty_help("TCS", "5m", "2026-11-13"))

if __name__ == "__main__":
    unittest.main()
