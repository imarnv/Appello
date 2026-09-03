"""
Support Knowledge Base Crawler
Scrapes Q&A articles from a JavaScript-rendered support portal into a CSV file,
then ingests them into Qdrant via KBEngine for the endpoint-security agent.

Usage:
    pip install playwright
    playwright install chromium
    python crawl_support_kb.py
"""

import asyncio
import csv
import os
import sys
import time

from playwright.async_api import async_playwright


BASE_URL = os.getenv("SUPPORT_KB_BASE_URL", "")
# Listing page that holds every article. Portal-specific, so it is
# configuration rather than a constant.
ALL_ARTICLES_URL = os.getenv("SUPPORT_KB_ARTICLES_URL", "")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "support_kb.csv")
MAX_ARTICLES = 75


async def get_article_links(page) -> list[dict]:
    """Extract article links from the all-articles page, clicking Load More as needed."""
    await page.goto(ALL_ARTICLES_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)  # Extra wait for Salesforce JS rendering

    # Click "Load More" button until we have enough articles or no more button
    load_more_clicks = 0
    while True:
        # Count current articles
        links = await page.query_selector_all('a[href*="/s/article/"]')
        print(f"  Currently visible: {len(links)} articles")
        
        if len(links) >= MAX_ARTICLES:
            break

        # Try to find and click Load More
        load_more = await page.query_selector('button:has-text("Load More"), button:has-text("load more"), a:has-text("Load More")')
        if not load_more:
            # Also try common Salesforce community selectors
            load_more = await page.query_selector('.load-more-button, .cTopicArticleList button, [data-action="loadMore"]')
        
        if load_more and await load_more.is_visible():
            await load_more.click()
            load_more_clicks += 1
            print(f"  Clicked Load More (#{load_more_clicks})")
            await page.wait_for_timeout(2000)  # Wait for new articles to load
        else:
            print("  No more Load More button found")
            break

    # Extract all article links
    links = await page.query_selector_all('a[href*="/s/article/"]')
    articles = []
    seen_hrefs = set()

    for link in links:
        href = await link.get_attribute("href")
        title = (await link.inner_text()).strip()
        
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        full_url = href if href.startswith("http") else BASE_URL + href
        articles.append({"url": full_url, "title": title})

        if len(articles) >= MAX_ARTICLES:
            break

    return articles


async def scrape_article(page, url: str, title: str) -> dict:
    """Navigate to an individual article and extract the question + resolution."""
    try:
        await page.goto(url, wait_until="networkidle", timeout=20000)
        await page.wait_for_timeout(2000)  # Wait for content render

        # Extract question/issue section
        question = title  # Fallback to the link title
        
        # Try to get the main heading
        h1 = await page.query_selector("h1")
        if h1:
            question = (await h1.inner_text()).strip()

        # Extract the full article body text
        # Salesforce KB articles typically have the content in the main article area
        resolution = ""
        
        # Try common Salesforce KB content selectors
        content_selectors = [
            ".article-content",
            ".slds-rich-text-area__content",
            ".cArticleContent",
            '[class*="articleContent"]',
            '[class*="article-body"]',
            ".force-article-content",
            "article",
            ".slds-card__body",
        ]
        
        for selector in content_selectors:
            el = await page.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 50:  # Must have meaningful content
                    resolution = text
                    break

        # Fallback: grab main content area
        if not resolution:
            main = await page.query_selector("main, .main-content, #main-content, .content-body")
            if main:
                resolution = (await main.inner_text()).strip()

        # Last resort fallback: get the body text minus nav/footer
        if not resolution or len(resolution) < 50:
            body_text = await page.evaluate("""
                () => {
                    const main = document.querySelector('main') || document.body;
                    // Remove nav, header, footer elements
                    const clone = main.cloneNode(true);
                    clone.querySelectorAll('nav, header, footer, script, style').forEach(el => el.remove());
                    return clone.innerText;
                }
            """)
            if body_text and len(body_text) > len(resolution or ""):
                resolution = body_text.strip()

        # Clean up resolution text
        if resolution:
            # Remove the title from the beginning if duplicated
            if resolution.startswith(question):
                resolution = resolution[len(question):].strip()
            # Limit length to avoid huge entries
            if len(resolution) > 5000:
                resolution = resolution[:5000] + "..."

        return {
            "question": question,
            "resolution": resolution,
            "source_url": url,
        }

    except Exception as e:
        print(f"  ⚠ Error scraping {url}: {e}")
        return {
            "question": title,
            "resolution": "",
            "source_url": url,
        }


async def crawl():
    """Main crawler: fetch article list, scrape each article, save to CSV."""
    print(f"🕷  Support KB crawler (max {MAX_ARTICLES} articles)")
    print(f"   Target: {ALL_ARTICLES_URL}")
    print(f"   Output: {OUTPUT_CSV}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Step 1: Get article links
        print("📋 Step 1: Collecting article links...")
        articles = await get_article_links(page)
        print(f"   Found {len(articles)} unique article links\n")

        if not articles:
            print("❌ No articles found! The page structure may have changed.")
            await browser.close()
            return

        # Step 2: Scrape each article
        print("📖 Step 2: Scraping individual articles...")
        results = []
        for i, article in enumerate(articles):
            print(f"  [{i+1}/{len(articles)}] {article['title'][:60]}...")
            data = await scrape_article(page, article["url"], article["title"])
            if data["resolution"]:  # Only keep articles with actual content
                results.append(data)
            else:
                print(f"    ⚠ Skipped (no resolution content found)")
            
            # Small delay to be polite to the server
            await page.wait_for_timeout(500)

        await browser.close()

        # Step 3: Save to CSV
        print(f"\n💾 Step 3: Saving {len(results)} articles to CSV...")
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["question", "resolution", "source_url"])
            writer.writeheader()
            writer.writerows(results)

        print(f"✅ Done! Saved {len(results)} Q&A articles to {OUTPUT_CSV}")
        print(f"\n📊 Summary:")
        print(f"   Total articles found: {len(articles)}")
        print(f"   Successfully scraped: {len(results)}")
        print(f"   Skipped (no content): {len(articles) - len(results)}")

        return results


async def ingest_to_qdrant():
    """Ingest the scraped CSV into Qdrant via the existing KBEngine."""
    if not os.path.exists(OUTPUT_CSV):
        print("❌ CSV file not found. Run the crawler first.")
        return

    print(f"\n🔄 Ingesting {OUTPUT_CSV} into Qdrant (collection: kb_fsecure_support)...")
    
    # Import KBEngine from the bridge
    sys.path.insert(0, os.path.dirname(__file__))
    from kb_engine import KBEngine

    kb = KBEngine()
    await kb.initialize()

    with open(OUTPUT_CSV, "rb") as f:
        csv_data = f.read()

    result = await kb.ingest_csv(csv_data, "support_kb.csv", "fsecure_support")
    print(f"   Chunks parsed: {result['chunks']}")
    print(f"   Rows processed: {result['rows']}")

    # Wait for the background embedding task to finish
    # ingest_csv fires _store_chunks via asyncio.create_task
    print("   ⏳ Embedding and storing vectors (this may take a moment)...")
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    
    print(f"✅ Ingestion complete!")
    await kb.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Support KB crawler and Qdrant ingestor")
    parser.add_argument("--ingest-only", action="store_true", help="Skip crawling, only ingest existing CSV into Qdrant")
    parser.add_argument("--crawl-only", action="store_true", help="Only crawl and save CSV, don't ingest")
    args = parser.parse_args()

    if args.ingest_only:
        asyncio.run(ingest_to_qdrant())
    elif args.crawl_only:
        asyncio.run(crawl())
    else:
        # Full pipeline: crawl then ingest
        asyncio.run(crawl())
        asyncio.run(ingest_to_qdrant())
