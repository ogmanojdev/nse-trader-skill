#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan the whole NSE cash market for setups.
Standard library only, Python 3.8+.

    python3 scan.py universe                              # EQUITY_L.csv -> trader/universe.txt
    python3 scan.py daily                                 # bhavcopy ladder -> trader/watchlist.json
    python3 scan.py daily --limit 300 --min-turnover 500
    python3 scan.py orb --at 10:00                        # opening-range breakouts so far
    python3 scan.py bands                                 # price bands + ASM/GSM -> trader/bands.json

universe  NSE's own EQUITY_L.csv, every listed security with its series, lot
          and ISIN. Keeps SERIES == EQ: the rolling-settlement equity book that
          an intraday trade can actually use. What that drops, and why:
            BE / BZ   trade-for-trade, 100% margin, no intraday netting
            SM / ST   SME platform, thin and lot-bound
            GS / GB   government securities
            IV / RR   partly paid, rights entitlements
          Series is the fact that decides whether a stock is tradeable
          intraday, so it is a hard filter, not a score.

daily     Pre-market scan from the exchange's own end-of-day bhavcopy
          (sec_bhavdata_full), not a chart API. One 400 KB request covers all
          ~2,700 EQ names, so a whole-market scan costs one request a day
          instead of 140 chart calls, it is official settled data, and it
          carries two columns no US feed has:

            DELIV_QTY / DELIV_PER   shares actually delivered, not churned

          Delivery percentage is the Indian signal the US original cannot have.
          A breakout on 70% delivery is being bought by people taking the
          shares home; the same breakout on 15% delivery is intraday churn that
          tends to give the move back. Setups, from daily closes:
            breakout_20d     last close above every close of the 20 before it
            bull_flag        up over 10% in the 10 sessions before the last 5,
                             then 5 closes inside a 5% range
            reclaim_ma20     closed under the 20-day average, then above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 under 30 yesterday, closed up today
            delivery_surge   delivery % at least 1.5x its own 20-day average
                             AND above 50%, on an up close
          Liquidity floors: --min-price 20 (penny names gap through stops),
          --min-turnover 200 (lakh, the bhavcopy's own TURNOVER_LACS).

orb       Opening-range breakouts on 5-minute bars, Yahoo chart, .NS symbols.
          The range is the first --or-minutes (15) of the session, so 09:15 to
          09:30. A hit is the first COMPLETED bar to close above the range
          high. The session is 09:15 to 15:30 IST, exactly 75 five-minute bars,
          and a bar off that grid is Yahoo's live partial tick and is dropped.

bands     Two files the exchange publishes that have no US equivalent and that
          decide whether your stop can fill at all:
            sec_list.csv   each symbol's price band (2 / 5 / 10 / 20 %)
            reportASM      names under Additional Surveillance Measure
          A stop placed past the day's band is not a wide stop, it is an order
          that cannot execute, because the exchange will not accept a price
          through the band. On a 2% band name an intraday stop has almost no
          room, and an ASM name can carry 100% margin. The scanner flags both
          and daily refuses to emit a setup on a band-locked name.

Times are IST and the calendar is the exchange's own (nsecal.py), so a holiday
is a holiday: today, 2026-11-10, is Diwali-Balipratipada and the market is
shut. --now pins the clock ("2026-11-09 10:00", IST; ...Z for UTC).

The bhavcopy filename is NOT authoritative. Asking the archive for a date it
has no file for serves the most recent file it does have, so a request for
10 Nov answers with 09 Nov data and the same 200 OK. Every fetch therefore
checks the DATE1 column against the date asked for and refuses a mismatch.

Not financial advice. A setup is a chart that looks a certain way, nothing
more. Nothing here can place an order: no order leaves this machine without
nse-risk passing it and you typing yes.
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nsecal       # noqa: E402  (vendored; see tools/vendor.py)
import nsedata      # noqa: E402
import sanyamstore  # noqa: E402

ScanError = nsedata.DataError
num = nsedata.num
TRADEABLE_SERIES = nsedata.TRADEABLE_SERIES
SLEEP = time.sleep

# ---------------------------------------------------------------- indicators

def rsi(closes, n=14):
    """Wilder's RSI, the same smoothing every Indian charting package uses."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def setups_for(closes, deliv, last_row):
    """Daily closes (oldest first) + delivery percentages -> list of setup names."""
    out = []
    n = len(closes)
    if n >= 21:
        if closes[-1] > max(closes[-21:-1]):
            out.append("breakout_20d")
        ma20 = mean(closes[-21:-1])
        if ma20 and closes[-2] < ma20 <= closes[-1]:
            out.append("reclaim_ma20")
    if n >= 16:
        run = closes[-16:-5]
        if run[0] > 0 and (run[-1] - run[0]) / run[0] > 0.10:
            tail = closes[-5:]
            lo, hi = min(tail), max(tail)
            if lo > 0 and (hi - lo) / lo < 0.05:
                out.append("bull_flag")
    r = rsi(closes) if n >= 15 else None
    if r is not None and r < 30:
        out.append("oversold")
    if n >= 16:
        prev = rsi(closes[:-1])
        if prev is not None and prev < 30 and closes[-1] > closes[-2]:
            out.append("oversold_bounce")
    d_now = num((last_row or {}).get("DELIV_PER"))
    hist = [x for x in deliv[-21:-1] if x is not None]
    if d_now is not None and len(hist) >= 10 and closes[-1] > closes[-2]:
        avg = mean(hist)
        if avg and d_now >= 50.0 and d_now >= 1.5 * avg:
            out.append("delivery_surge")
    return out, r, d_now


def cmd_universe(a):
    t0 = time.time()
    if a.offline:
        with open(a.offline, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        text = nsedata.nse_get(nsedata.EQUITY_L, timeout=60)
    kept, counts = nsedata.parse_universe(text)
    if not a.no_write:
        nsedata.write_universe(kept, a.out)
    dropped = {k: v for k, v in counts.items() if k not in TRADEABLE_SERIES}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": sum(v for k, v in counts.items()
                                                          if k != "fund"),
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
        return 0
    print("NSE EQUITY_L.csv: %s listed securities."
          % "{:,}".format(sum(v for k, v in counts.items() if k != "fund")))
    print("Kept %s in series EQ, the rolling-settlement equity book." % "{:,}".format(len(kept)))
    if counts.get("fund"):
        print("  dropped %d ETFs and index funds that also trade in EQ." % counts["fund"])
    print("Dropped: " + ", ".join("%d %s" % (v, k) for k, v in
                                  sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
    if not a.no_write:
        print("Wrote %s." % a.out)
    return 0


def universe_for_scan(a):
    """The symbol set the daily scan may consider.

    The bhavcopy carries SYMBOL and SERIES but no company name, so an ETF sitting
    in series EQ is indistinguishable there. The universe is what makes the fund
    filter possible at all, and most ETFs are caught for free because NSE lists
    them outside EQUITY_L.csv entirely.
    """
    return nsedata.names_for(offline=a.offline, universe_path=a.universe)


# ---------------------------------------------------------------- bands and ASM

def cmd_bands(a):
    if a.offline:
        bands = nsedata.parse_bands(open(os.path.join(a.offline, "sec_list.csv"),
                                 encoding="utf-8", errors="replace").read())
        asm_path = os.path.join(a.offline, "reportASM.json")
        asm = nsedata.parse_asm(open(asm_path, encoding="utf-8").read()) if os.path.exists(asm_path) else {}
    else:
        bands = nsedata.parse_bands(nsedata.nse_get(nsedata.SEC_LIST, timeout=60))
        try:
            asm = nsedata.parse_asm(nsedata.nse_api("/api/reportASM"))
        except ScanError as e:
            print("note: %s" % e, file=sys.stderr)
            asm = {}
    narrow = {s: b for s, b in bands.items() if b < nsedata.STANDARD_BAND}
    tight = {s: b for s, b in bands.items() if b <= 5}
    out = {"as_of": nsecal.ist_now().isoformat(timespec="seconds"),
           "bands": bands, "asm": asm}
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
    if a.json:
        print(json.dumps({"banded": len(bands), "narrow": len(narrow),
                          "tight": len(tight), "asm": len(asm),
                          "out": None if a.no_write else a.out}, indent=2))
    else:
        print("Price bands: %s names listed, %s with a band tighter than the standard %g%%."
              % ("{:,}".format(len(bands)), "{:,}".format(len(narrow)), nsedata.STANDARD_BAND))
        print("  %s sit at 5%% or tighter, where an intraday stop has almost no room."
              % "{:,}".format(len(tight)))
        print("ASM surveillance: %s names (%s long term, %s short term)." % (
            "{:,}".format(len(asm)),
            sum(1 for v in asm.values() if v == "longterm"),
            sum(1 for v in asm.values() if v == "shortterm")))
        if not a.no_write:
            print("Wrote %s." % a.out)
    return 0


def band_block(sym, row, bands, asm):
    """Why this name should not take a new intraday entry today, or None."""
    if sym in asm:
        return "ASM %s" % asm[sym]
    band = bands.get(sym)
    if band is None:
        return None
    prev = num((row or {}).get("PREV_CLOSE"))
    close = num((row or {}).get("CLOSE_PRICE"))
    if prev and close:
        move = abs(close - prev) / prev * 100.0
        if move >= band - 0.25:
            return "at the %g%% band" % band
    if band <= 2:
        return "%g%% band" % band
    return None


# ---------------------------------------------------------------- daily

def cmd_daily(a):
    t0 = time.time()
    end = nsecal.parse_ist(a.now, "--now").date() if a.now else nsecal.ist_now().date()
    if not nsecal.is_trading_day(end):
        prev = nsecal.prev_session_day(end)
        why = nsecal.HOLIDAYS.get(end.isoformat()) or end.strftime("%A")
        print("note: %s is not a session (%s). Scanning the last one, %s."
              % (end.isoformat(), why, prev.isoformat()), file=sys.stderr)
        end = prev
    cache = a.cache
    if a.offline:
        cache = a.offline
    days, series, misses = nsedata.bhav_ladder(end, a.sessions, cache,
                                           nsecal.is_trading_day, a.pause)
    if not days:
        raise ScanError("no bhavcopy for any of the last %d sessions. %s"
                        % (a.sessions, misses[0][1] if misses else ""))
    bands, asm = {}, {}
    if not a.no_bands:
        try:
            if a.offline:
                p = os.path.join(a.offline, "sec_list.csv")
                bands = nsedata.parse_bands(open(p, encoding="utf-8", errors="replace").read()) \
                    if os.path.exists(p) else {}
            else:
                bands = nsedata.parse_bands(nsedata.nse_get(nsedata.SEC_LIST, timeout=60))
                asm = nsedata.parse_asm(nsedata.nse_api("/api/reportASM"))
        except (ScanError, OSError, ValueError) as e:
            print("note: band/ASM data unavailable, scanning without it (%s)" % e, file=sys.stderr)

    allowed = universe_for_scan(a)
    last_idx = len(days) - 1
    hits, scanned, floors = [], 0, {"price": 0, "turnover": 0, "short": 0, "banded": 0, "fund": 0}
    only = set(s.strip().upper() for s in a.symbols.split(",")) if a.symbols else None
    for sym, by_idx in series.items():
        if only and sym not in only:
            continue
        if allowed is not None and sym not in allowed:
            floors["fund"] += 1
            continue
        last = by_idx.get(last_idx)
        if not last:
            continue
        closes, deliv = [], []
        for i in range(len(days)):
            r = by_idx.get(i)
            c = num((r or {}).get("CLOSE_PRICE"))
            if c is None:
                closes, deliv = [], []
                continue
            closes.append(c)
            deliv.append(num((r or {}).get("DELIV_PER")))
        if len(closes) < 16:
            floors["short"] += 1
            continue
        price = closes[-1]
        turn = num(last.get("TURNOVER_LACS"), 0.0)
        if price < a.min_price:
            floors["price"] += 1
            continue
        if turn < a.min_turnover:
            floors["turnover"] += 1
            continue
        scanned += 1
        names, r14, d_now = setups_for(closes, deliv, last)
        if not names:
            continue
        block = band_block(sym, last, bands, asm)
        if block:
            floors["banded"] += 1
            continue
        hits.append({"symbol": sym, "setups": names, "close": round(price, 2),
                     "prev_close": num(last.get("PREV_CLOSE")),
                     "turnover_lacs": round(turn, 1),
                     "delivery_pct": d_now, "rsi14": round(r14, 1) if r14 is not None else None,
                     "band_pct": nsedata.band_flag(bands.get(sym)), "asm": asm.get(sym),
                     "name": (allowed or {}).get(sym, "")})
    hits.sort(key=lambda h: -h["turnover_lacs"])
    if a.limit:
        hits = hits[:a.limit]
    out = {"as_of": end.isoformat(), "sessions": len(days),
           "from": days[0].isoformat(), "to": days[-1].isoformat(),
           "scanned": scanned, "hits": hits,
           "floors": floors, "missing_sessions": [m[0] for m in misses]}
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print("NSE bhavcopy, %d sessions %s to %s." % (len(days), days[0].isoformat(), days[-1].isoformat()))
    if misses:
        print("  missing: %s" % ", ".join(m[0] for m in misses))
    print("Scanned %s EQ names past the floors (price >= %g, turnover >= %g lakh)."
          % ("{:,}".format(scanned), a.min_price, a.min_turnover))
    print("Dropped %s under price, %s under turnover, %s too short a history, %s not in the "
          "equity universe (ETFs and funds), %s band or ASM blocked."
          % tuple("{:,}".format(floors[k]) for k in ("price", "turnover", "short", "fund", "banded")))
    print("%s setups:" % "{:,}".format(len(hits)))
    for h in hits[:a.show]:
        extra = []
        if h["delivery_pct"] is not None:
            extra.append("deliv %.0f%%" % h["delivery_pct"])
        if h["rsi14"] is not None:
            extra.append("RSI %.0f" % h["rsi14"])
        if h["band_pct"]:
            extra.append("band %g%%" % h["band_pct"])
        print("  %-14s %9.2f  %-44s %s" % (h["symbol"], h["close"], ",".join(h["setups"]),
                                           "  ".join(extra)))
    if len(hits) > a.show:
        print("  ... and %d more" % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s in %.1fs." % (a.out, time.time() - t0))
    return 0


# ---------------------------------------------------------------- orb

def session_bars(ser, day, now_ist, minutes=5):
    """Completed, on-grid bars of one IST session, oldest first.

    Three things get dropped, each for a reason that bites in live data:
      - a bar with a null close (a halt, or no trade in the window)
      - a bar off the 5-minute grid, which is Yahoo's partial live tick
      - a bar whose window has not closed yet at now_ist
    """
    out = []
    for i, ts in enumerate(ser["t"]):
        local = nsecal.to_ist(nsecal.utc_of(ts))
        if local.date() != day:
            continue
        if ser["c"][i] is None:
            continue
        slot = nsecal.slot_of(local, minutes)
        if slot is None:
            continue
        if local + datetime.timedelta(minutes=minutes) > now_ist:
            continue
        out.append({"slot": slot, "time": local, "o": ser["o"][i], "h": ser["h"][i],
                    "l": ser["l"][i], "c": ser["c"][i], "v": ser["v"][i]})
    out.sort(key=lambda b: b["slot"])
    return out


def orb_for(bars, or_minutes, minutes=5):
    """The opening range and the first completed bar to close above it."""
    k = max(1, or_minutes // minutes)
    if len(bars) < k:
        return None
    rng = bars[:k]
    if [b["slot"] for b in rng] != list(range(k)):
        return None  # a gap inside the range: a halt, so the range is not knowable
    highs = [b["h"] for b in rng if b["h"] is not None]
    lows = [b["l"] for b in rng if b["l"] is not None]
    if not highs or not lows:
        return None
    hi, lo = max(highs), min(lows)
    for b in bars[k:]:
        if b["c"] is not None and b["c"] > hi:
            return {"range_high": hi, "range_low": lo, "range_mid": (hi + lo) / 2.0,
                    "hit": True, "at": b["time"].strftime("%H:%M"), "close": b["c"],
                    "over_pct": (b["c"] - hi) / hi * 100.0 if hi else None,
                    "bars": len(bars)}
    return {"range_high": hi, "range_low": lo, "range_mid": (hi + lo) / 2.0,
            "hit": False, "at": None, "close": bars[-1]["c"], "over_pct": None,
            "bars": len(bars)}


def intraday_series(sym, day, a):
    """(series, origin) for one symbol's session. Three sources, one shape.

    All three return what nsedata.series_from_chart returns, so the grid checks,
    the completeness rule and the range arithmetic below are identical whichever
    one answered. Order of preference is the order of trust: a saved fixture is
    reproducible, Sanyam's store is the broker's own bars and needs no network,
    Yahoo is the fallback that throttles.
    """
    if a.offline:
        p = os.path.join(a.offline, "chart-%s-1d-5m.json"
                         % nsedata.to_yahoo(sym).replace("^", "idx-"))
        if not os.path.exists(p):
            raise ScanError("no fixture %s" % os.path.basename(p))
        with open(p, encoding="utf-8") as fh:
            return nsedata.series_from_chart(fh.read()), "fixture"
    if a.sanyam is not None:
        db = sanyamstore.find_db(a.sanyam or None)
        open_t, close_t = nsecal.session_or_raise(day)
        start = int((nsecal.ist_to_utc(datetime.datetime.combine(day, open_t))
                     - nsecal.EPOCH).total_seconds())
        end = int((nsecal.ist_to_utc(datetime.datetime.combine(day, close_t))
                   - nsecal.EPOCH).total_seconds())
        bars = sanyamstore.read_bars(db, sym, "5m", start, end)
        origin = "sanyam 5m"
        if not bars:
            # 1m is Sanyam's base interval, so 5m is usually derived rather than stored.
            one = sanyamstore.read_bars(db, sym, "1m", start, end)
            if one:
                bars = sanyamstore.aggregate(one, 300)
                origin = "sanyam 1m aggregated"
        if not bars:
            raise ScanError(sanyamstore.empty_help(
                sanyamstore.sanyam_symbol(sym), "5m or 1m", day.isoformat()))
        return sanyamstore.as_series(bars), origin
    return nsedata.series_from_chart(
        nsedata.yahoo_get(nsedata.chart_path(sym, "1d", "5m"))), "yahoo"


def cmd_orb(a):
    now = nsecal.parse_ist(a.now, "--now") if a.now else nsecal.ist_now()
    if a.at:
        t = nsecal.parse_ist("%s %s" % (now.date().isoformat(), a.at), "--at")
        now = t
    day = now.date()
    try:
        open_t, close_t = nsecal.session_or_raise(day)
    except nsecal.CalendarError as e:
        raise ScanError(str(e))
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif os.path.exists(a.watchlist):
        with open(a.watchlist, encoding="utf-8") as fh:
            wl = json.load(fh)
        syms = [h["symbol"] for h in (wl.get("hits") or [])][:a.max_charts]
        if not syms:
            raise ScanError("%s has no hits. Run: python3 scan.py daily" % a.watchlist)
    else:
        raise ScanError("nothing to scan. Pass --symbols, or run scan.py daily first.")
    rows, errors, throttled, origins = [], [], None, {}
    for k, sym in enumerate(syms[:a.max_charts]):
        try:
            if k and a.pause and a.sanyam is None and not a.offline:
                SLEEP(a.pause)      # only the network source needs pacing
            ser, origin = intraday_series(sym, day, a)
            origins[origin] = origins.get(origin, 0) + 1
            worst = (ser.get("meta") or {}).get("_worst_source")
            if worst and worst not in sanyamstore.TRUSTED_SOURCES and not a.allow_live_agg:
                errors.append((sym, "bars are %s: aggregated from a ~1 Hz price poll, so "
                                    "the intrabar high and low are approximate and the "
                                    "opening range would be too narrow. Pass "
                                    "--allow-live-agg to use them anyway." % worst))
                continue
            bars = session_bars(ser, day, now)
            if not bars:
                errors.append((sym, "no completed on-grid bars for %s by %s"
                               % (day.isoformat(), now.strftime("%H:%M"))))
                continue
            o = orb_for(bars, a.or_minutes)
            if not o:
                errors.append((sym, "opening range incomplete (a halt, or too early)"))
                continue
            last = bars[-1]["c"]
            if last is not None and last < a.min_price:
                continue
            o["symbol"] = sym
            rows.append(o)
        except nsedata.ThrottledError as e:
            # Stop here on purpose: see nsedata.ThrottledError. Whatever was read
            # before the ban is still reported.
            throttled = str(e)
            break
        except ScanError as e:
            errors.append((sym, str(e)))
    hit = [r for r in rows if r["hit"]]
    hit.sort(key=lambda r: -(r["over_pct"] or 0))
    out = {"as_of": now.isoformat(timespec="minutes"), "day": day.isoformat(),
           "or_minutes": a.or_minutes, "session": "%s to %s" % (open_t.strftime("%H:%M"),
                                                                close_t.strftime("%H:%M")),
           "scanned": len(rows), "breakouts": hit, "throttled": throttled,
           "bar_sources": origins,
           "errors": [{"symbol": s, "why": w} for s, w in errors]}
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    if origins:
        print("bars from %s" % ", ".join("%s (%d)" % (o, n) for o, n in sorted(origins.items())))
    print("Opening range %s to %s IST, clock at %s. %d charts read, %d broke out."
          % (open_t.strftime("%H:%M"),
             (datetime.datetime.combine(day, open_t) +
              datetime.timedelta(minutes=a.or_minutes)).strftime("%H:%M"),
             now.strftime("%H:%M"), len(rows), len(hit)))
    for r in hit[:a.show]:
        print("  %-14s range %9.2f to %9.2f  closed %9.2f at %s  %+.2f%% over"
              % (r["symbol"], r["range_low"], r["range_high"], r["close"], r["at"], r["over_pct"]))
    for s, w in errors[:5]:
        print("  note %-12s %s" % (s, w), file=sys.stderr)
    if throttled:
        print("\nSTOPPED after %d of %d charts: %s" % (len(rows), len(syms[:a.max_charts]),
                                                       throttled), file=sys.stderr)
    if not a.no_write:
        print("Wrote %s." % a.out)
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="scan.py", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="NSE EQUITY_L.csv -> trader/universe.txt")
    p.add_argument("--out", default="trader/universe.txt")
    p.add_argument("--offline", metavar="PATH", help="a saved EQUITY_L.csv")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="whole-market daily setups from the bhavcopy")
    p.add_argument("--out", default="trader/watchlist.json")
    p.add_argument("--cache", default="trader/cache", help="where bhavcopies are kept")
    p.add_argument("--universe", default="trader/universe.txt",
                   help="symbol list used to drop ETFs; fetched if absent")
    p.add_argument("--sessions", type=int, default=25, help="how many sessions of history (25)")
    p.add_argument("--symbols", help="only these, comma separated")
    p.add_argument("--min-price", type=float, default=20.0)
    p.add_argument("--min-turnover", type=float, default=200.0, help="lakh, TURNOVER_LACS (200)")
    p.add_argument("--limit", type=int, default=0, help="keep at most this many hits")
    p.add_argument("--show", type=int, default=25)
    p.add_argument("--no-bands", action="store_true", help="skip the band and ASM filter")
    p.add_argument("--pause", type=float, default=0.4)
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-09 18:30"')
    p.add_argument("--offline", metavar="DIR", help="a directory of bhav-YYYY-MM-DD.csv files")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("orb", help="opening-range breakouts so far")
    p.add_argument("--out", default="trader/orb.json")
    p.add_argument("--symbols")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--at", metavar="HH:MM", help="cut the clock at this IST time")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-09 10:00"')
    p.add_argument("--min-price", type=float, default=20.0)
    p.add_argument("--max-charts", type=int, default=40, help="Yahoo throttles; keep this small")
    p.add_argument("--sanyam", nargs="?", const="", metavar="DB",
                   help="read 5-minute bars from a local candle store rather than Yahoo: "
                        "your own broker data, no network call, no rate limit. Sanyam "
                        "writes one (free, sanyam.app). Path found automatically if "
                        "omitted.")
    p.add_argument("--allow-live-agg", action="store_true",
                   help="accept LIVE_AGG bars, whose intrabar extremes are approximate")
    p.add_argument("--pause", type=float, default=1.0)
    p.add_argument("--show", type=int, default=25)
    p.add_argument("--offline", metavar="DIR", help="a directory of chart-SYM.NS-1d-5m.json files")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bands", help="price bands and ASM names")
    p.add_argument("--out", default="trader/bands.json")
    p.add_argument("--offline", metavar="DIR")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb,
                "bands": cmd_bands}[a.cmd](a)
    except (ScanError, nsecal.CalendarError) as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
