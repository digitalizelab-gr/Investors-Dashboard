# Super Investors Radar

Personal dashboard tracking the portfolios and quarterly moves of 10 super
investors, fed directly from SEC EDGAR 13F filings.

- `index.html` - the dashboard. Loads `data.json` when present, falls back to
  embedded sample data otherwise (badge in the header shows which mode you're in).
- `fetch_13f.py` - pulls the two latest 13F-HR filings per investor from EDGAR,
  diffs them into trades, writes `data.json`. Stdlib only.
- `.github/workflows/update-data.yml` - runs the fetcher every Monday and
  commits the refreshed `data.json`.
- `cusip_map.json` / `sector_map.json` - lookup caches, created on first run
  and committed so later runs are fast.

## Setup (once)

1. Create a GitHub repo, push these files.
2. Settings -> Secrets and variables -> Actions -> add secret `SEC_USER_AGENT`
   with value `YourName youremail@example.com` (SEC requires identification).
   Optional secrets: `OPENFIGI_API_KEY` (faster ticker mapping),
   `FINNHUB_API_KEY` (adds sectors).
3. Verify the CIK list locally: `python fetch_13f.py --check` - each line must
   show the right fund name. Fix any mismatch in `INVESTORS` inside the script.
4. Actions tab -> "Update 13F data" -> Run workflow. Wait for the green check;
   `data.json` appears in the repo.
5. Settings -> Pages -> Source: Deploy from branch -> `main` / root. Your
   dashboard is live at `https://<user>.github.io/<repo>/`.

## Live prices (optional)

Open `index.html`, find `const CONFIG = { finnhubKey: "" }` and paste a free
Finnhub key. The "Last price" column fills in per selected investor.
Note: this key is visible in the page source - fine for a personal/private
page, don't use a paid key here.

## Notes

- 13F data is quarterly with a 45-day lag; "live" means current-as-filed.
- Entry dates ("holding since") aren't in 13Fs; the column shows "—" unless
  you fill them by hand in `data.json` post-processing.
- Not financial advice. The Radar Picks ranking is a simple consensus
  heuristic - a research queue, nothing more.
