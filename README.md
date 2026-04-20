# dropshipping_clanker
messing around with web scraping on bulk sales sites like liquidation.com

## Setup

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```powershell
python webscraper_demo.py --mode browser --headed
```

`--headed` opens a real browser window. Use it if Liquidation.com shows a
manual challenge or blocks headless browser traffic.
