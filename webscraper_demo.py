import csv
import argparse
import re
import time
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.liquidation.com"
SEARCH_URL = f"{BASE_URL}/auction/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": BASE_URL,
}


class ScraperRequestError(RuntimeError):
    """Raised when Liquidation.com blocks or rejects the scraper request."""


def build_search_url(keyword, page=1, per_page=28, sort="relevance"):
    params = {
        "flag": "new",
        "searchparam_words": keyword,
        "_per_page": per_page,
        "sort": sort,
        "page": page,
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_text_after_label(lines, label):
    """
    Finds text like 'Current Bid: $140.00 (6 Bids)' from a list of bullet strings.
    """
    for line in lines:
        if line.lower().startswith(label.lower()):
            return clean_text(line.split(":", 1)[1]) if ":" in line else clean_text(line)
    return ""


def parse_bid_info(text):
    """
    Example input: '$140.00 (6 Bids)'
    Returns: ('$140.00', '6')
    """
    if not text:
        return "", ""
    bid_match = re.search(r"^\$?[\d,]+(?:\.\d{2})?", text)
    count_match = re.search(r"\((\d+)\s+Bids?\)", text, re.IGNORECASE)
    current_bid = bid_match.group(0) if bid_match else ""
    bid_count = count_match.group(1) if count_match else ""
    return current_bid, bid_count


def parse_qty_condition(text):
    """
    Example input: '40 | Returns'
    Returns: ('40', 'Returns')
    """
    if "|" in text:
        left, right = text.split("|", 1)
        return clean_text(left), clean_text(right)
    return clean_text(text), ""


def fetch_search_page_html_requests(session, keyword, page=1, per_page=28, sort="relevance"):
    search_url = build_search_url(keyword, page=page, per_page=per_page, sort=sort)
    try:
        resp = session.get(search_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        final_url = exc.response.url if exc.response is not None else search_url
        if status_code == 403:
            raise ScraperRequestError(
                "Liquidation.com returned HTTP 403 Forbidden. The site is "
                "reachable, but it is blocking this non-browser request. "
                f"URL: {final_url}"
            ) from exc
        raise ScraperRequestError(
            f"Liquidation.com returned HTTP {status_code}. URL: {final_url}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ScraperRequestError(
            f"Could not connect to Liquidation.com: {exc}"
        ) from exc

    return resp.text


def fetch_search_page_html_browser(page_obj, keyword, page=1, per_page=28, sort="relevance"):
    search_url = build_search_url(keyword, page=page, per_page=per_page, sort=sort)

    response = page_obj.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
    if response is None:
        raise ScraperRequestError(f"Browser navigation failed. URL: {search_url}")

    status_code = response.status
    if status_code == 403:
        raise ScraperRequestError(
            "Liquidation.com returned HTTP 403 Forbidden even in the browser. "
            "Try running with --headed so the browser is visible, then complete "
            f"any manual challenge if one appears. URL: {response.url}"
        )
    if status_code >= 400:
        raise ScraperRequestError(
            f"Liquidation.com returned HTTP {status_code}. URL: {response.url}"
        )

    try:
        page_obj.wait_for_selector("h4, H4, text=Search Results", timeout=20_000)
    except Exception:
        # Some pages legitimately have no results. Parse whatever rendered.
        pass

    return page_obj.content()


def parse_search_html(html):
    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen_urls = set()

    # Current Liquidation.com search pages render each result in a thumbnail card.
    # The page can include both list and grid markup for the same auction, so
    # de-duplicate by URL.
    for card in soup.select(".thumbnail[data-id]"):
        title_link = card.select_one("h4 a.desc[href], h4 a[href]")
        if not title_link:
            continue

        title = clean_text(title_link.get_text(" ", strip=True))
        href = title_link["href"]

        if not title or "Bid Now" in title or "WATCHLIST_LINK_TEXT" in title:
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url in seen_urls:
            continue

        detail_lines = [
            clean_text(li.get_text(" ", strip=True))
            for li in card.select("ul.auction-details li")
        ]
        current_bid_raw = extract_text_after_label(detail_lines, "Current Bid")
        qty_condition_raw = extract_text_after_label(detail_lines, "Qty")
        num_packages = extract_text_after_label(detail_lines, "Number of Packages")
        location = extract_text_after_label(detail_lines, "Location")
        closing = extract_text_after_label(detail_lines, "CLOSING")

        current_bid, bid_count = parse_bid_info(current_bid_raw)
        qty, condition = parse_qty_condition(qty_condition_raw)

        seller_node = card.select_one(".sellername a")
        seller = clean_text(seller_node.get_text(" ", strip=True)) if seller_node else ""

        if not any([current_bid, qty, location, closing]):
            continue

        results.append(
            {
                "title": title,
                "current_bid": current_bid,
                "bid_count": bid_count,
                "qty": qty,
                "condition": condition,
                "number_of_packages": num_packages,
                "location": location,
                "closing": closing,
                "seller": seller,
                "url": full_url,
            }
        )
        seen_urls.add(full_url)

    if results:
        return results

    # The search-result titles appear as headings with links.
    # We look for h4 headings that contain auction links.
    for h4 in soup.find_all(["h4", "H4"]):
        a = h4.find("a", href=True)
        if not a:
            continue

        title = clean_text(a.get_text(" ", strip=True))
        href = a["href"]

        # Skip obvious non-auction links
        if not title or "Bid Now" in title or "WATCHLIST_LINK_TEXT" in title:
            continue

        full_url = urljoin(BASE_URL, href)

        # Walk forward through siblings to collect nearby bullet lines
        lines = []
        seller = ""
        node = h4.parent

        # Look at a limited number of following elements near this result card
        steps = 0
        while node and steps < 15:
            node = node.find_next_sibling()
            steps += 1
            if node is None:
                break

            text = clean_text(node.get_text(" ", strip=True))
            if not text:
                continue

            # Stop if we likely reached another result heading
            if node.name in {"h4", "H4"}:
                break

            # Capture seller from linked text if present
            for link in node.find_all("a", href=True):
                link_text = clean_text(link.get_text(" ", strip=True))
                if link_text and link_text not in {"Bid Now", "WATCHLIST_LINK_TEXT", "Compare"}:
                    # crude heuristic: seller names are often short and near the card
                    if len(link_text) < 40:
                        seller = seller or link_text

            lines.append(text)

            # Once we have enough info, no need to keep scanning too far
            if any("Location:" in x for x in lines) and any("CLOSING:" in x for x in lines):
                break

        current_bid_raw = extract_text_after_label(lines, "Current Bid")
        qty_condition_raw = extract_text_after_label(lines, "Qty")
        num_packages = extract_text_after_label(lines, "Number of Packages")
        location = extract_text_after_label(lines, "Location")
        closing = extract_text_after_label(lines, "CLOSING")

        current_bid, bid_count = parse_bid_info(current_bid_raw)
        qty, condition = parse_qty_condition(qty_condition_raw)

        # Basic sanity check: only keep rows that look like auction cards
        if not any([current_bid, qty, location, closing]):
            continue

        results.append(
            {
                "title": title,
                "current_bid": current_bid,
                "bid_count": bid_count,
                "qty": qty,
                "condition": condition,
                "number_of_packages": num_packages,
                "location": location,
                "closing": closing,
                "seller": seller,
                "url": full_url,
            }
        )

    return results


def scrape_search_page_requests(session, keyword, page=1, per_page=28, sort="relevance"):
    """
    Scrape one Liquidation.com search results page with requests.
    """
    html = fetch_search_page_html_requests(
        session=session,
        keyword=keyword,
        page=page,
        per_page=per_page,
        sort=sort,
    )
    return parse_search_html(html)


def scrape_keyword_requests(keyword, max_pages=1, per_page=28, delay_seconds=2):
    """
    Scrape multiple pages for one keyword with requests.
    """
    all_results = []

    with requests.Session() as session:
        for page in range(1, max_pages + 1):
            print(f"Scraping page {page} for keyword: {keyword!r}")
            page_results = scrape_search_page_requests(
                session=session,
                keyword=keyword,
                page=page,
                per_page=per_page,
            )

            if not page_results:
                print("No more results found.")
                break

            all_results.extend(page_results)
            time.sleep(delay_seconds)

    return all_results


def scrape_keyword_browser(
    keyword,
    max_pages=1,
    per_page=28,
    delay_seconds=2,
    headless=True,
    sort="relevance",
):
    """
    Scrape multiple pages for one keyword with Playwright.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScraperRequestError(
            "Playwright is not installed. Install it with:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    all_results = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1365, "height": 900},
            extra_http_headers={
                "Accept-Language": HEADERS["Accept-Language"],
            },
        )
        page_obj = context.new_page()

        try:
            for page_num in range(1, max_pages + 1):
                print(f"Scraping page {page_num} for keyword: {keyword!r}")
                html = fetch_search_page_html_browser(
                    page_obj=page_obj,
                    keyword=keyword,
                    page=page_num,
                    per_page=per_page,
                    sort=sort,
                )
                page_results = parse_search_html(html)

                if not page_results:
                    print("No more results found.")
                    break

                all_results.extend(page_results)
                time.sleep(delay_seconds)
        finally:
            context.close()
            browser.close()

    return all_results


def save_to_csv(rows, filename="liquidation_results.csv"):
    if not rows:
        print("No rows to save.")
        return

    fieldnames = list(rows[0].keys())
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to {filename}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape Liquidation.com auction search results."
    )
    parser.add_argument("--keyword", default="unclaimed packages")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--per-page", type=int, default=28)
    parser.add_argument("--delay-seconds", type=float, default=2)
    parser.add_argument("--sort", default="relevance")
    parser.add_argument(
        "--mode",
        choices=("browser", "requests"),
        default="browser",
        help="Use Playwright browser automation or plain requests.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window. Useful if the site presents a manual challenge.",
    )
    parser.add_argument("--output", default="liquidation_results.csv")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        if args.mode == "browser":
            results = scrape_keyword_browser(
                keyword=args.keyword,
                max_pages=args.max_pages,
                per_page=args.per_page,
                delay_seconds=args.delay_seconds,
                headless=not args.headed,
                sort=args.sort,
            )
        else:
            results = scrape_keyword_requests(
                keyword=args.keyword,
                max_pages=args.max_pages,
                per_page=args.per_page,
                delay_seconds=args.delay_seconds,
            )
    except ScraperRequestError as exc:
        print(f"Scraper request failed: {exc}")
        raise SystemExit(1) from exc

    for row in results[:5]:
        print(row)

    save_to_csv(results, filename=args.output)
