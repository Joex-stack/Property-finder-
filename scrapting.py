"""
PropertyFinder Egypt Scraper — API + Intercept Edition
-------------------------------------------------------
Strategy: Launch a real visible browser, intercept the XHR/fetch calls
that the page makes to load listings, and capture the JSON directly.
This bypasses all HTML parsing and bot-detection issues.

Install:
    pip install playwright pandas
    python -m playwright install chromium
"""

import asyncio
import json
import random
import re
from datetime import datetime
from dataclasses import dataclass, asdict

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ─── Config ────────────────────────────────────────────────────────────────────

SEARCH_URL  = "https://www.propertyfinder.eg/en/search?c=1&fu=0&ob=mr"   # or /rent/
MAX_PAGES   = None          # pages to scrape
DELAY_MIN   = 3.0
DELAY_MAX   = 6.0
OUTPUT_FILE = f"propertyfinder_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# ─── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Property:
    link:          str = ""
    photo:         str = ""
    property_type: str = ""
    price:         str = ""
    title:         str = ""
    location:      str = ""
    bedrooms:      str = ""
    bathrooms:     str = ""
    area:          str = ""
    price_per_sqm: str = ""
    tag:           str = ""
    time:          str = ""

# ─── JSON response parser ──────────────────────────────────────────────────────

def parse_api_response(data: dict) -> list[Property]:
    """Parse the JSON API response and extract Property objects."""
    props = []
    
    # Try common API response shapes
    listings = (
        data.get("listings")
        or data.get("data", {}).get("listings")
        or data.get("results")
        or data.get("data", {}).get("results")
        or data.get("hits")
        or data.get("data")
        or []
    )

    if not isinstance(listings, list):
        return []

    for item in listings:
        p = Property()

        # Link
        slug = item.get("slug") or item.get("url") or item.get("link") or ""
        if slug:
            p.link = slug if slug.startswith("http") else f"https://www.propertyfinder.eg{slug}"

        # Photo
        photos = item.get("photos") or item.get("images") or item.get("coverPhoto") or []
        if isinstance(photos, list) and photos:
            first = photos[0]
            p.photo = first.get("url") or first.get("src") or str(first)
        elif isinstance(photos, str):
            p.photo = photos

        # Property type
        p.property_type = (
            item.get("propertyType")
            or item.get("property_type")
            or item.get("type", {}).get("name", "")
            or ""
        )

        # Price
        price_val = item.get("price") or item.get("priceFormatted") or ""
        p.price = str(price_val)

        # Title
        p.title = item.get("title") or item.get("name") or ""

        # Location
        loc = item.get("location") or item.get("address") or {}
        if isinstance(loc, dict):
            parts = [
                loc.get("community") or "",
                loc.get("city")      or "",
                loc.get("area")      or "",
            ]
            p.location = ", ".join(x for x in parts if x)
        else:
            p.location = str(loc)

        # Bedrooms / bathrooms
        p.bedrooms  = str(item.get("bedrooms")  or item.get("beds")  or "")
        p.bathrooms = str(item.get("bathrooms") or item.get("baths") or "")

        # Area
        area = item.get("area") or item.get("size") or item.get("builtUpArea") or ""
        p.area = str(area)

        # Tag
        p.tag = item.get("tag") or item.get("badge") or item.get("label") or ""

        # Listed time
        p.time = item.get("createdAt") or item.get("publishedAt") or item.get("listedAt") or ""

        if p.link or p.title:
            props.append(p)

    return props


# ─── HTML fallback parser ──────────────────────────────────────────────────────

async def parse_html_cards(page) -> list[Property]:
    """
    Last-resort: try to extract data from the rendered HTML
    by looking for __NEXT_DATA__ or window.__STATE__ JSON blobs.
    """
    props = []
    try:
        content = await page.content()

        # Next.js injects all page data into a <script id="__NEXT_DATA__"> tag
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            # Walk the props tree to find listings
            page_props = (
                data.get("props", {})
                    .get("pageProps", {})
            )
            # Try several known keys
            for key in ["listings", "results", "data", "initialData"]:
                candidate = page_props.get(key)
                if isinstance(candidate, list) and candidate:
                    props = parse_api_response({key: candidate})
                    if props:
                        print(f"    [html] found {len(props)} listings in __NEXT_DATA__.{key}")
                        return props
                elif isinstance(candidate, dict):
                    props = parse_api_response(candidate)
                    if props:
                        print(f"    [html] found {len(props)} listings in __NEXT_DATA__.{key}")
                        return props

        # Try window.__STATE__ or similar
        for var in ["__STATE__", "__INITIAL_STATE__", "__PRELOADED_STATE__"]:
            match = re.search(rf'window\.{var}\s*=\s*(\{{.*?\}})\s*;', content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                props = parse_api_response(data)
                if props:
                    print(f"    [html] found {len(props)} listings in window.{var}")
                    return props

    except Exception as e:
        print(f"    [html] parse error: {e}")

    return props


# ─── Main scraper ──────────────────────────────────────────────────────────────

async def scrape(
    base_url:  str  = SEARCH_URL,
    max_pages: int  = MAX_PAGES,
    output:    str  = OUTPUT_FILE,
    headless:  bool = False,   # False = visible browser (harder to block)
) -> pd.DataFrame:

    all_props   = []
    intercepted = []   # JSON responses captured from network

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()

        # ── Intercept API responses ──────────────────────────────────────────
        async def handle_response(response):
            url = response.url
            # Capture JSON from API-like endpoints
            if any(kw in url for kw in [
                "/api/", "/graphql", "listings", "properties",
                "search", "plp", "v1", "v2", "v3",
            ]):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        parsed = parse_api_response(body)
                        if parsed:
                            print(f"    [API] {url[:80]} -> {len(parsed)} listings")
                            intercepted.extend(parsed)
                except Exception:
                    pass

        page.on("response", handle_response)

        # ── Page 1 ──────────────────────────────────────────────────────────
        for page_num in range(1, (max_pages or 999) + 1):
            intercepted.clear()

            url   = base_url if page_num == 1 else f"{base_url}?page={page_num}"
            print(f"\n[*] Page {page_num}: {url}")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                # Wait for network to settle (up to 10s)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    pass
                await asyncio.sleep(2)   # let late XHR calls finish

            except PWTimeout:
                print(f"  [!] Page load timeout, trying HTML fallback ...")

            # ── Use intercepted API data if available ────────────────────────
            if intercepted:
                print(f"    -> {len(intercepted)} listings from API intercept")
                all_props.extend(intercepted)
            else:
                # ── Fall back to __NEXT_DATA__ extraction ────────────────────
                print("    [!] No API intercept, trying __NEXT_DATA__ ...")
                html_props = await parse_html_cards(page)
                if html_props:
                    all_props.extend(html_props)
                else:
                    print("    [!] No listings found on this page — stopping.")
                    break

            # ── Check if there's a next page ─────────────────────────────────
            next_btn = page.locator("a[aria-label='Next page'], a[rel='next'], [class*='pagination-next']").first
            try:
                visible = await next_btn.is_visible()
                if not visible:
                    print("[*] No more pages.")
                    break
            except Exception:
                pass

            if max_pages and page_num >= max_pages:
                break

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            print(f"[*] Waiting {delay:.1f}s ...")
            await asyncio.sleep(delay)

        await browser.close()

    # ── Save ─────────────────────────────────────────────────────────────────
    df = pd.DataFrame([asdict(p) for p in all_props])
    if not df.empty:
        df.drop_duplicates(subset=["link"], inplace=True)
    df.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"\n[OK] Saved {len(df)} unique listings -> {output}")
    return df


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Runs with a VISIBLE browser window by default (harder for sites to block)
    df = asyncio.run(scrape(
        base_url  = "https://www.propertyfinder.eg/en/search?c=1&fu=0&ob=mr",
        max_pages = 20,
        headless  = False,   # <-- set True to run invisibly after it works
    ))

    # Rent listings:
    # df = asyncio.run(scrape(
    #     base_url  = "https://www.propertyfinder.eg/en/plp/rent/",
    #     max_pages = 20,
    #     headless  = False,
    # ))

    print(df.head())
    print(f"Shape: {df.shape}")