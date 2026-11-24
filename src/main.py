import asyncio
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from apify import Actor
from playwright.async_api import async_playwright


def extract_next_data(html_text: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            pass
    return {}


def find_products_in_json(obj, found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        has_id = "saleId" in obj or ("id" in obj and isinstance(obj.get("id"), (int, str)))
        has_name = ("name" in obj and isinstance(obj.get("name"), str)) or ("title" in obj and isinstance(obj.get("title"), str))
        has_price = any(k in obj for k in ("price", "salePrice", "originalPrice", "priceAmount", "salePriceAmount"))
        if has_id and has_name and has_price:
            found.append(obj)
        for v in obj.values():
            find_products_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_products_in_json(item, found)
    return found


def normalize_product(raw: dict, base_url: str, artist_id: str, currency: str, category_name: str = "") -> dict:
    sale_id = raw.get("saleId") or raw.get("id")
    name = (raw.get("name") or raw.get("title") or "").strip()

    price_val = raw.get("salePrice") or raw.get("price") or raw.get("priceAmount") or raw.get("salePriceAmount")
    if isinstance(price_val, dict):
        price_val = price_val.get("amount") or price_val.get("value") or price_val.get("salePrice")

    orig_price = raw.get("originalPrice")
    if isinstance(orig_price, dict):
        orig_price = orig_price.get("amount") or orig_price.get("value")

    status = str(raw.get("status") or raw.get("saleStatus") or "").upper()
    is_sold_out = bool(raw.get("isSoldOut") or raw.get("soldOut") or "SOLD_OUT" in status or "SOLDOUT" in status)

    img = raw.get("thumbnailUrl") or raw.get("imageUrl") or raw.get("image") or raw.get("thumbnailImageUrl") or ""
    if isinstance(raw.get("images"), list) and raw["images"] and not img:
        first = raw["images"][0]
        img = first.get("url") if isinstance(first, dict) else str(first)

    product_url = f"https://shop.weverse.io/en/shop/{currency}/artists/{artist_id}/sales/{sale_id}"
    if base_url and "/artists/" in base_url:
        prefix = base_url.split("/sales/")[0].split("/categories/")[0].split("?")[0]
        product_url = f"{prefix}/sales/{sale_id}"

    return {
        "saleId": sale_id,
        "name": name,
        "price": price_val,
        "originalPrice": orig_price,
        "currency": currency,
        "isSoldOut": is_sold_out,
        "stockStatus": "SOLD_OUT" if is_sold_out else (status or "IN_STOCK"),
        "category": category_name or raw.get("categoryName") or "",
        "label": raw.get("label") or raw.get("badge") or "",
        "imageUrl": img,
        "productUrl": product_url,
        "artistId": artist_id,
    }


async def scrape_dom_fallback(page, artist_id: str, currency: str, category_name: str = "") -> list:
    items = []
    links = await page.query_selector_all('a[href*="/sales/"]')
    seen_ids = set()
    for link in links:
        href = await link.get_attribute("href") or ""
        m = re.search(r"/sales/(\d+)", href)
        if not m:
            continue
        sale_id = int(m.group(1))
        if sale_id in seen_ids:
            continue
        seen_ids.add(sale_id)

        full_text = (await link.inner_text() or "").strip()
        lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
        img_el = await link.query_selector("img")
        img_url = await img_el.get_attribute("src") if img_el else ""
        is_sold_out = "sold out" in full_text.lower() or "품절" in full_text
        price_line = next((ln for ln in lines if any(sym in ln for sym in ("$", "₩", "¥", "USD", "KRW", "JPY"))), "")
        name_line = next((ln for ln in lines if ln != price_line and "sold out" not in ln.lower() and "품절" not in ln), "Unknown Product")

        items.append({
            "saleId": sale_id,
            "name": name_line,
            "price": price_line,
            "originalPrice": None,
            "currency": currency,
            "isSoldOut": is_sold_out,
            "stockStatus": "SOLD_OUT" if is_sold_out else "IN_STOCK",
            "category": category_name,
            "label": "",
            "imageUrl": img_url or "",
            "productUrl": urljoin("https://shop.weverse.io", href),
            "artistId": artist_id,
        })
    return items


async def collect_from_page(page, url: str, artist_id: str, currency: str, captured_api_jsons: list, products_map: dict, category_name: str = ""):
    captured_api_jsons.clear()
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    for _ in range(4):
        await page.evaluate(
            "() => { const h = document.documentElement?.scrollHeight || document.body?.scrollHeight || 0; if (h > 0) window.scrollTo(0, h); }"
        )
        await page.wait_for_timeout(1000)

    html_text = await page.content()
    final_url = page.url

    before_count = len(products_map)
    for _, json_data in captured_api_jsons:
        for raw in find_products_in_json(json_data):
            norm = normalize_product(raw, final_url, artist_id, currency, category_name)
            if norm["saleId"] and norm["name"]:
                products_map[str(norm["saleId"])] = norm

    if len(products_map) == before_count:
        next_data = extract_next_data(html_text)
        for raw in find_products_in_json(next_data):
            norm = normalize_product(raw, final_url, artist_id, currency, category_name)
            if norm["saleId"] and norm["name"]:
                products_map[str(norm["saleId"])] = norm

    if len(products_map) == before_count:
        for item in await scrape_dom_fallback(page, artist_id, currency, category_name):
            products_map[str(item["saleId"])] = item


async def run_browser_scrape(target_url: str, artist_id: str, currency: str, max_items: int, include_details: bool, proxy_url: str | None):
    captured_api_jsons = []
    products_map = {}

    async with async_playwright() as p:
        launch_opts = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}

        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        async def handle_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "application/json" in ct and response.status == 200:
                    url_l = response.url.lower()
                    if any(k in url_l for k in ("sale", "product", "artist", "display", "shop", "catalog")):
                        data = await response.json()
                        captured_api_jsons.append((response.url, data))
            except Exception:
                pass

        page.on("response", handle_response)

        # 1. 메인 아티스트 페이지 수집
        await collect_from_page(page, target_url, artist_id, currency, captured_api_jsons, products_map, "Featured")
        Actor.log.info(f"🏠 메인 페이지 상품 수집: {len(products_map)}개")

        # 2. 카테고리 링크들(/categories/...) 자동 발견 후 추가 수집
        if (max_items == 0 or len(products_map) < max_items) and "/categories/" not in target_url:
            cat_links = await page.query_selector_all('a[href*="/categories/"]')
            cat_urls = []
            seen_cat = set()
            for el in cat_links:
                href = await el.get_attribute("href") or ""
                full_cat = urljoin("https://shop.weverse.io", href).split("?")[0]
                if f"/artists/{artist_id}/categories/" in full_cat and full_cat not in seen_cat:
                    seen_cat.add(full_cat)
                    cat_name = (await el.inner_text() or "").strip()
                    cat_urls.append((full_cat, cat_name))

            Actor.log.info(f"📂 발견된 카테고리 탭 {len(cat_urls)}개 탐색 시작...")
            for cat_url, cat_name in cat_urls[:12]:
                if max_items > 0 and len(products_map) >= max_items:
                    break
                await collect_from_page(page, cat_url, artist_id, currency, captured_api_jsons, products_map, cat_name)
                Actor.log.info(f"   └ [{cat_name or 'Category'}] 누적 상품: {len(products_map)}개")

        results = list(products_map.values())
        if max_items > 0:
            results = results[:max_items]

        # 3. 상세 페이지 보강 (선택 사항)
        if include_details and results:
            for idx, item in enumerate(results):
                try:
                    await page.goto(item["productUrl"], wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(600)
                    d_html = await page.content()
                    d_json = extract_next_data(d_html)
                    for dr in find_products_in_json(d_json):
                        if str(dr.get("saleId") or dr.get("id")) == str(item["saleId"]):
                            enriched = normalize_product(dr, item["productUrl"], artist_id, currency, item.get("category", ""))
                            item.update({k: v for k, v in enriched.items() if v not in (None, "")})
                            break
                except Exception as e:
                    Actor.log.warning(f"상세 건너뜀 ({item['saleId']}): {e}")

        await browser.close()
        return results


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        artist_id = str(actor_input.get("artistId", "2")).strip()
        currency = str(actor_input.get("currency", "USD")).strip()
        language = str(actor_input.get("language", "en")).strip()
        max_items = int(actor_input.get("maxItems", 50) or 50)
        custom_url = (actor_input.get("customArtistUrl") or "").strip()
        include_details = bool(actor_input.get("includeProductDetails", False))

        target_url = custom_url if custom_url else f"https://shop.weverse.io/{language}/shop/{currency}/artists/{artist_id}"
        m_art = re.search(r"/artists/(\d+)", target_url)
        if m_art:
            artist_id = m_art.group(1)

        Actor.log.info(f"🚀 Scraping Weverse Shop: {target_url}")
        results = await run_browser_scrape(target_url, artist_id, currency, max_items, include_details, None)

        if not results:
            Actor.log.info("🔄 직접 접속 결과 0건 → Apify 프록시로 재시도합니다...")
            proxy_config = await Actor.create_proxy_configuration()
            proxy_url = await proxy_config.new_url() if proxy_config else None
            if proxy_url:
                results = await run_browser_scrape(target_url, artist_id, currency, max_items, include_details, proxy_url)

        if results:
            await Actor.push_data(results)
            # Apify Pay-Per-Event 과금 이벤트 발생 (Publishing 탭에서 product-scraped 가격 설정 시 자동 정산!)
            try:
                await Actor.charge(event_name="product-scraped", count=len(results))
            except Exception as charge_err:
                Actor.log.info(f"ℹ️ PPE charge info (무료 테스트 중이면 정상): {charge_err}")
            Actor.log.info(f"🎉 완료! 총 {len(results)}개 상품 추출 성공.")
        else:
            Actor.log.warning("❌ 상품을 찾지 못했습니다.")