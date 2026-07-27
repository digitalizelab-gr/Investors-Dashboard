#!/usr/bin/env python3
"""
Super Investors Radar - 13F fetcher.

Pulls the two most recent 13F-HR filings per tracked investor straight from
SEC EDGAR, parses the information tables, maps CUSIPs to tickers via OpenFIGI,
diffs the two quarters into buy/sell trades, and writes data.json for the
dashboard.

Stdlib only - no pip installs needed.

Environment variables (all optional):
  SEC_USER_AGENT    Required by SEC in practice. Format: "Name email@domain.com".
                    A default is set below - REPLACE IT with your own contact.
  OPENFIGI_API_KEY  Free key from openfigi.com -> much higher CUSIP-mapping
                    rate limits. Without it the script still works, just slower
                    on the first run (results are cached in cusip_map.json).
  FINNHUB_API_KEY   Free key from finnhub.io -> adds a sector per ticker
                    (cached in sector_map.json) and the last trade price per
                    holding (refetched every run, never cached). Without it
                    sectors show "—" and prices are blank.

Run modes:
  python fetch_13f.py           normal refresh, writes data.json
  python fetch_13f.py --check   only verifies the CIK list (prints entity names)
"""

import json
import urllib.parse
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "SuperInvestorsRadar personal-project youremail@example.com",  # <-- change me
)
OPENFIGI_KEY = os.environ.get("OPENFIGI_API_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

MAX_HOLDINGS_PER_INVESTOR = 25   # dashboard shows top N by value
MAX_TRADES_PER_INVESTOR = 14

# The 10 tracked investors. CIK = the SEC id of the FILING ENTITY (the fund).
# Verify with:  python fetch_13f.py --check
# or search the entity at https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
INVESTORS = [
    {"id": "buffett", "name": "Warren Buffett", "cik": "0001067983",
     "style": "Value, long-term",
     "bio": "Chairman of Berkshire Hathaway. Buys durable businesses with wide moats and holds them for decades."},
    {"id": "dalio", "name": "Ray Dalio (Bridgewater)", "cik": "0001350694",
     "style": "Macro, diversified",
     "bio": "Founder of Bridgewater Associates. Diversification across assets, geographies and inflation regimes."},
    {"id": "ackman", "name": "Bill Ackman", "cik": "0001336528",
     "style": "Concentrated, activist",
     "bio": "CEO of Pershing Square. Ultra-concentrated large-cap book, often with an activist angle."},
    {"id": "smith", "name": "Terry Smith", "cik": "0001569205",
     "style": "Quality growth, buy & hold",
     "bio": "Founder of Fundsmith. Buy good companies, don't overpay, do nothing."},
    # Michael Burry (Scion, CIK 0001649339) was dropped: Scion deregistered in
    # late 2025, so its last 13F covers 2025-09-30 and no newer one is coming.
    {"id": "klarman", "name": "Seth Klarman", "cik": "0001061768",
     "style": "Deep value, special situations",
     "bio": "CEO of Baupost Group. Mispriced, complex or unloved assets with a heavy cash cushion."},
    {"id": "tepper", "name": "David Tepper", "cik": "0001656456",
     "style": "Opportunistic, macro equities",
     "bio": "Founder of Appaloosa Management. Concentrated equity bets on macro dislocations."},
    {"id": "marks", "name": "Howard Marks (Oaktree)", "cik": "0000949509",
     "style": "Credit, distressed value",
     "bio": "Co-founder of Oaktree Capital. Cycle-aware distressed and credit investing."},
    {"id": "lilu", "name": "Li Lu", "cik": "0001709323",
     "style": "Focused value, Munger school",
     "bio": "Founder of Himalaya Capital. Ultra-concentrated, decade-long horizons."},
    {"id": "icahn", "name": "Carl Icahn", "cik": "0000921669",
     "style": "Activist, event-driven",
     "bio": "Chairman of Icahn Enterprises. Large stakes, board fights, forced value events."},
]

# ---------------------------------------------------------------------------
# HTTP helpers (polite: SEC asks for <=10 req/s; we do far less)
# ---------------------------------------------------------------------------


def _get(url, headers=None, data=None, retries=3):
    hdrs = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs, data=data)
    for attempt in range(retries):
        try:
            time.sleep(0.25)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"unreachable: {url}")


def get_json(url):
    return json.loads(_get(url))


# ---------------------------------------------------------------------------
# Caches (committed to the repo so lookups shrink over time)
# ---------------------------------------------------------------------------


def load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)


CUSIP_MAP = load_cache("cusip_map.json")     # cusip -> ticker (or "" if unknown)
SECTOR_MAP = load_cache("sector_map.json")   # ticker -> sector


# ---------------------------------------------------------------------------
# EDGAR: find + parse the two latest 13F-HR filings for a CIK
# ---------------------------------------------------------------------------


def latest_13f_filings(cik, count=2):
    """Return up to `count` filings as [{accession, period, filed}], newest first.
    Amendments (13F-HR/A) replace the original for the same period."""
    subs = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    entity_name = subs.get("name", "?")
    recent = subs["filings"]["recent"]
    by_period = {}
    for form, acc, period, filed in zip(
        recent["form"], recent["accessionNumber"],
        recent["reportDate"], recent["filingDate"],
    ):
        if form not in ("13F-HR", "13F-HR/A"):
            continue
        # newest filing per period wins (list is newest-first, so keep first seen)
        if period not in by_period:
            by_period[period] = {"accession": acc, "period": period, "filed": filed}
    filings = sorted(by_period.values(), key=lambda f: f["period"], reverse=True)
    return entity_name, filings[:count]


DEBT_MARKERS = ("NOTE", "BOND", "DBCV", "DEBENTURE")


def is_debt(title_of_class):
    """13F tables mix convertible notes and bonds in with the equities.
    The dashboard charts stock positions only, so those rows are dropped."""
    t = title_of_class.upper()
    return any(m in t for m in DEBT_MARKERS)


def normalize_ticker(ticker):
    """Turn an OpenFIGI symbol into one a US quote API will accept.
    Returns "" when there is no usable symbol, which is the signal to show
    the company name instead and skip the price lookup."""
    t = (ticker or "").strip().upper().replace("/", ".")
    if not t or " " in t:
        return ""                       # blank, or a bond line like "AWK 3.625 06/15/26"
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t):
        return ""                       # CUSIP fragments and foreign venue codes
    return t


# OpenFIGI exchange codes for the US composite and the individual US venues.
US_EXCH_CODES = {"US", "UN", "UQ", "UA", "UR", "UW", "UP", "UV", "UB", "UD"}


def pick_us_ticker(records):
    """Choose the US listing out of an unfiltered OpenFIGI response.
    Falling back to records[0] is what previously turned Philip Morris into
    its Frankfurt line, so anything non-US is ignored outright."""
    for r in records:
        if r.get("exchCode") in US_EXCH_CODES and normalize_ticker(r.get("ticker")):
            return normalize_ticker(r["ticker"])
    return ""


# Foreign-domiciled issuers identify themselves with a CINS rather than a plain
# CUSIP, and OpenFIGI resolves none of them, filtered or not. Their US listings
# are pinned here by issuer name. Extend this when a sizeable holding turns up
# with no ticker. Where an issuer has several US share classes the most liquid
# one is used, so the price is the class price rather than the exact line held.
ISSUER_OVERRIDES = {
    "anglogold ashanti": "AU",
    "aon": "AON",
    "asml": "ASML",
    "chubb": "CB",
    "credo technology": "CRDO",
    "herbalife": "HLF",
    "liberty global": "LBTYK",
    "liberty latin america": "LILAK",
    "norwegian cruise line": "NCLH",
    "torm": "TRMD",
    "willis towers watson": "WTW",
    "xp inc": "XP",
}


def override_ticker(issuer):
    name = (issuer or "").lower()
    for prefix, ticker in ISSUER_OVERRIDES.items():
        if name.startswith(prefix):
            return ticker
    return ""


def parse_infotable(cik, accession):
    """Download a filing's information table XML and aggregate holdings by CUSIP."""
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}"
    index = get_json(f"{base}/index.json")
    xml_name = None
    for item in index["directory"]["item"]:
        n = item["name"].lower()
        if n.endswith(".xml") and "primary_doc" not in n:
            xml_name = item["name"]
            if "infotable" in n or "form13f" in n:
                break  # best match
    if not xml_name:
        raise RuntimeError(f"no infotable xml in {base}")

    root = ET.fromstring(_get(f"{base}/{xml_name}"))

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    holdings = {}
    for el in root.iter():
        if local(el.tag) != "infoTable":
            continue
        row = {}
        for child in el.iter():
            row[local(child.tag)] = (child.text or "").strip()
        if row.get("putCall"):
            continue  # skip options, keep common shares only
        if is_debt(row.get("titleOfClass", "")):
            continue  # convertible notes and bonds are not equity positions
        cusip = row.get("cusip", "").upper()
        if not cusip:
            continue
        value = float(row.get("value", 0) or 0)  # USD (post-2023 filings)
        shares = float(row.get("sshPrnamt", 0) or 0)
        h = holdings.setdefault(cusip, {
            "cusip": cusip,
            "issuer": row.get("nameOfIssuer", "").title(),
            "value": 0.0,
            "shares": 0.0,
        })
        h["value"] += value
        h["shares"] += shares
    return rescale_thousands(holdings)


def rescale_thousands(holdings):
    """The SEC moved 13F value reporting to whole dollars in 2023, but some
    filers still report thousands (Baupost does). Left uncorrected their market
    values come out a thousand times too small. A portfolio whose median implied
    share price is under a dollar is the giveaway, since no book of US equities
    trades there."""
    implied = sorted(h["value"] / h["shares"]
                     for h in holdings.values() if h["shares"] > 0)
    if implied and implied[len(implied) // 2] < 1.0:
        for h in holdings.values():
            h["value"] *= 1000
    return holdings


# ---------------------------------------------------------------------------
# CUSIP -> ticker (OpenFIGI) and ticker -> sector (Finnhub), both cached
# ---------------------------------------------------------------------------


def resolve_tickers(cusips):
    todo = [c for c in cusips if c not in CUSIP_MAP]
    if not todo:
        return
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
        batch, pause = 100, 0.3
    else:
        batch, pause = 5, 3.0  # keyless limits are tight
    print(f"  mapping {len(todo)} new CUSIPs via OpenFIGI "
          f"({'with key' if OPENFIGI_KEY else 'keyless, slow first run'})")
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        # exchCode US keeps OpenFIGI from handing back the Frankfurt or London
        # line for a dual-listed name (Philip Morris came back as "4I1").
        payload = json.dumps(
            [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        ).encode()
        try:
            resp = json.loads(_get("https://api.openfigi.com/v3/mapping",
                                   headers=headers, data=payload))
            for cusip, result in zip(chunk, resp):
                data = result.get("data") or []
                CUSIP_MAP[cusip] = data[0].get("ticker", "") if data else ""
        except Exception as e:
            print(f"  ! OpenFIGI batch failed ({e}); issuer names used as fallback")
            for cusip in chunk:
                CUSIP_MAP.setdefault(cusip, "")
        time.sleep(pause)

    # Foreign-domiciled issuers with a US listing (Chubb, TORM, ASML) carry a
    # CINS rather than a plain CUSIP, and the exchCode filter returns nothing
    # for them. Ask again unfiltered and pick a US venue out of the results.
    missed = [c for c in todo if not CUSIP_MAP.get(c)]
    if missed:
        print(f"  retrying {len(missed)} unmatched CUSIPs without the exchange filter")
        for i in range(0, len(missed), batch):
            chunk = missed[i:i + batch]
            payload = json.dumps(
                [{"idType": "ID_CUSIP", "idValue": c} for c in chunk]
            ).encode()
            try:
                resp = json.loads(_get("https://api.openfigi.com/v3/mapping",
                                       headers=headers, data=payload))
                for cusip, result in zip(chunk, resp):
                    CUSIP_MAP[cusip] = pick_us_ticker(result.get("data") or [])
            except Exception as e:
                print(f"  ! OpenFIGI retry batch failed ({e})")
            time.sleep(pause)

    save_cache("cusip_map.json", CUSIP_MAP)


def resolve_sector(ticker):
    if not ticker or not FINNHUB_KEY:
        return "—"
    if ticker in SECTOR_MAP:
        return SECTOR_MAP[ticker]
    try:
        prof = json.loads(_get(
            f"https://finnhub.io/api/v1/stock/profile2"
            f"?symbol={urllib.parse.quote(ticker)}&token={FINNHUB_KEY}"))
        SECTOR_MAP[ticker] = prof.get("finnhubIndustry") or "—"
    except Exception:
        SECTOR_MAP[ticker] = "—"
    time.sleep(1.1)  # free tier allows 60 calls/min
    return SECTOR_MAP[ticker]


def fetch_prices(investors):
    """Fill in the last trade price per holding. Prices are deliberately not
    cached between runs, and each ticker is fetched once per run no matter how
    many investors hold it."""
    tickers = sorted({h["ticker"] for inv in investors
                      for h in inv["holdings"] if h["ticker"]})
    if not FINNHUB_KEY:
        print(f"\nNo FINNHUB_API_KEY set - {len(tickers)} tickers left unpriced.")
        return
    print(f"\nPricing {len(tickers)} tickers via Finnhub")
    prices = {}
    for t in tickers:
        try:
            quote = json.loads(_get(
                f"https://finnhub.io/api/v1/quote"
                f"?symbol={urllib.parse.quote(t)}&token={FINNHUB_KEY}"))
            prices[t] = quote.get("c") or None   # c is 0 for unknown symbols
        except Exception:
            prices[t] = None
        time.sleep(1.1)  # free tier allows 60 calls/min
    for inv in investors:
        for h in inv["holdings"]:
            h["price"] = prices.get(h["ticker"])
    print(f"  priced {sum(1 for v in prices.values() if v)}/{len(tickers)}")

    # A symbol no quote API recognises is usually a stale mapping rather than an
    # untraded stock, so drop it and let the next run resolve that CUSIP again.
    # If OpenFIGI returns the same symbol the cache file is byte-identical and
    # nothing churns, so this is safe to leave running every day.
    unpriced = {t for t, v in prices.items() if not v}
    if unpriced:
        stale = [c for c, t in CUSIP_MAP.items() if t in unpriced]
        for c in stale:
            del CUSIP_MAP[c]
        save_cache("cusip_map.json", CUSIP_MAP)
        print(f"  dropped {len(stale)} unpriceable mappings for retry: "
              f"{', '.join(sorted(unpriced))}")


# ---------------------------------------------------------------------------
# Assemble per-investor payload
# ---------------------------------------------------------------------------


def quarter_label(period):  # "2026-03-31" -> "2026 Q1"
    y, m, _ = period.split("-")
    return f"{y} Q{(int(m) - 1) // 3 + 1}"


def build_investor(meta):
    entity, filings = latest_13f_filings(meta["cik"])
    if not filings:
        raise RuntimeError(f"no 13F filings found for CIK {meta['cik']} ({entity})")
    print(f"  entity: {entity} | latest period: {filings[0]['period']}")

    latest = parse_infotable(meta["cik"], filings[0]["accession"])
    prev = (parse_infotable(meta["cik"], filings[1]["accession"])
            if len(filings) > 1 else {})

    resolve_tickers(list(latest.keys()) + list(prev.keys()))

    total_value = sum(h["value"] for h in latest.values()) or 1.0

    # Holdings: top N by value
    top = sorted(latest.values(), key=lambda h: h["value"], reverse=True)
    holdings = []
    for h in top[:MAX_HOLDINGS_PER_INVESTOR]:
        ticker = (normalize_ticker(CUSIP_MAP.get(h["cusip"], ""))
                  or override_ticker(h["issuer"]))
        holdings.append({
            "ticker": ticker,
            "company": h["issuer"],
            "sector": resolve_sector(ticker),
            "weight": round(h["value"] / total_value * 100, 2),
            "value": round(h["value"]),
            "shares": round(h["shares"]),
            "since": "",  # 13Fs don't carry entry dates; fill by hand if you care
            "price": None,  # filled in by fetch_prices once all investors are built
        })

    # Trades: diff latest vs previous quarter, biggest moves first
    q = quarter_label(filings[0]["period"])
    trades = []
    for cusip, h in latest.items():
        p = prev.get(cusip)
        ticker = (normalize_ticker(CUSIP_MAP.get(cusip, ""))
                  or override_ticker(h["issuer"]))
        if p is None:
            trades.append({"quarter": q, "action": "New Position",
                           "ticker": ticker, "company": h["issuer"],
                           "note": f"New stake worth ${h['value'] / 1e6:,.0f}M.",
                           "_size": h["value"]})
        elif p["shares"] > 0:
            chg = (h["shares"] - p["shares"]) / p["shares"] * 100
            if chg > 1:
                trades.append({"quarter": q, "action": "Increase",
                               "ticker": ticker, "company": h["issuer"],
                               "note": f"Increased shares by +{chg:,.0f}%.",
                               "_size": abs(chg) * h["value"]})
            elif chg < -1:
                trades.append({"quarter": q, "action": "Decrease",
                               "ticker": ticker, "company": h["issuer"],
                               "note": f"Reduced shares by {chg:,.0f}%.",
                               "_size": abs(chg) * h["value"]})
    for cusip, p in prev.items():
        if cusip not in latest:
            ticker = (normalize_ticker(CUSIP_MAP.get(cusip, ""))
                      or override_ticker(p["issuer"]))
            trades.append({"quarter": q, "action": "Exit",
                           "ticker": ticker, "company": p["issuer"],
                           "note": "Position fully closed.",
                           "_size": p["value"]})
    trades.sort(key=lambda t: t["_size"], reverse=True)
    for t in trades:
        del t["_size"]
    trades = trades[:MAX_TRADES_PER_INVESTOR]

    # Stats
    sector_w = {}
    for h in holdings:
        if h["sector"] != "—":
            sector_w[h["sector"]] = sector_w.get(h["sector"], 0) + h["weight"]
    top_sectors = [s for s, _ in sorted(sector_w.items(),
                                        key=lambda kv: kv[1], reverse=True)[:3]]

    return {
        "id": meta["id"],
        "name": meta["name"],
        "style": meta["style"],
        "bio": meta["bio"],
        "stats": {
            "numHoldings": len(latest),
            "topSectors": top_sectors or ["—"],
            "avgDuration": "n/a",
            "last13F": filings[0]["filed"],
        },
        "holdings": holdings,
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if "--check" in sys.argv:
        print("Verifying CIKs against EDGAR:\n")
        for meta in INVESTORS:
            try:
                entity, filings = latest_13f_filings(meta["cik"])
                latest = filings[0]["period"] if filings else "NO 13F FOUND"
                print(f"  {meta['name']:<28} CIK {meta['cik']} -> {entity} "
                      f"(latest 13F period: {latest})")
            except Exception as e:
                print(f"  {meta['name']:<28} CIK {meta['cik']} -> ERROR: {e}")
        print("\nIf an entity name doesn't match the investor, fix the CIK in "
              "INVESTORS and rerun.")
        return

    out = {"generated": datetime.now(timezone.utc).isoformat(), "investors": []}
    failures = []
    for meta in INVESTORS:
        print(f"* {meta['name']} (CIK {meta['cik']})")
        try:
            out["investors"].append(build_investor(meta))
        except Exception as e:
            print(f"  ! FAILED: {e}")
            failures.append(meta["name"])

    save_cache("sector_map.json", SECTOR_MAP)
    fetch_prices(out["investors"])

    if not out["investors"]:
        print("No investors fetched successfully - data.json NOT written.")
        sys.exit(1)

    with open("data.json", "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nWrote data.json with {len(out['investors'])} investors.")
    if failures:
        print(f"Skipped (fix CIK or retry): {', '.join(failures)}")


if __name__ == "__main__":
    main()
