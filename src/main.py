import asyncio
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from apify import Actor
from playwright.async_api import async_playwright


def extract_next_data(html_text: str) -> dict:
    """Next.js SSR 페이지 내부의 __NEXT_DATA__ JSON 추출"""
    soup = BeautifulSoup(html_text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            pass
    return {}


def find_products_in_json(obj, found=None):
    """중첩된 Next.js JSON 트리에서 상품(saleId/name/price) 객체 재귀 탐색"""
    if found is None:
        found = []

    if isinstance(obj, dict):
        # 위버스샵 상품 객체 특징: saleId 또는 id + name + price 관련 필드
        has_id = "saleId" in obj or ("id" in obj and isinstance(obj.get("id"), int))
        has_name = "name" in obj and isinstance(obj.get("name"), str)
        has_price = any(k in obj for k in ("price", "salePrice", "originalPrice", "priceAmount"))
        if has_id and has_name and has_price:
            found.append(obj)
        for v in obj.values():
            find_products_in_json(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_products_in_json(item, found)

    return found


def normalize_product(raw: dict, base_url: str, artist_id: str, currency: str) -> dict:
    sale_id = raw.get("saleId") or raw.get("id")
    # 가격 추출 (다양한 구조 대응)
    price_val = raw.get("price") or raw.get("salePrice") or raw.get("priceAmount")
    if isinstance(price_val, dict):
        price_val = price_val.get("amount") or price_val.get("value") or price_val.get("salePrice")

    orig_price = raw.get("originalPrice")
    if isinstance(orig_price, dict):
        orig_price = orig_price.get("amount") or orig_price.get("value")

    # 품절 / 재고 상태 판단
    status = str(raw.get("status") or raw.get("saleStatus") or "").upper()
    is_sold_out = bool(raw.get("isSoldOut") or raw.get("soldOut") or "SOLD_OUT" in status or "SOLDOUT" in status)

    # 이미지 추출
    img = raw.get("thumbnailUrl") or raw.get("imageUrl") or raw.get("image") or ""
    if isinstance(raw.get("images"), list) and raw["images"] and not img:
        first = raw["images"][0]
        img = first.get("url") if isinstance(first, dict) else str(first)

    product_url = f"https://shop.weverse.io/en/shop/{currency}/artists/{artist_id}/sales/{sale_id}"
    if base_url and "/artists/" in base_url:
        prefix = base_url.split("/sales/")[0].split("/categories/")[0]
        product_url = f"{prefix}/sales/{sale_id}"

    return {
        "saleId": sale_id,
        "name": raw.get("name", "").strip(),
        "price": price_val,
        "originalPrice": orig_price,
        "currency": currency,
        "isSoldOut": is_sold_out,
        "stockStatus": "SOLD_OUT" if is_sold_out else (status or "IN_STOCK"),
        "label": raw.get("label") or raw.get("badge") or "",
        "imageUrl": img,
        "productUrl": product_url,
        "artistId": artist_id,
    }


async def scrape_dom_fallback(page, base_url: str, artist_id: str, currency: str) -> list:
    """JSON 탐색 실패 시 실제 렌더링된 DOM에서 상품 카드 직접 추출"""
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
            "label": "",
            "imageUrl": img_url or "",
            "productUrl": urljoin("https://shop.weverse.io", href),
            "artistId": artist_id,
        })
    return items


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        artist_id = str(actor_input.get("artistId", "2")).strip()
        currency = str(actor_input.get("currency", "USD")).strip()
        language = str(actor_input.get("language", "en")).strip()
        max_items = int(actor_input.get("maxItems", 50) or 50)
        custom_url = (actor_input.get("customArtistUrl") or "").strip()
        include_details = bool(actor_input.get("includeProductDetails", True))

        target_url = custom_url if custom_url else f"https://shop.weverse.io/{language}/shop/{currency}/artists/{artist_id}"
        m_art = re.search(r"/artists/(\d+)", target_url)
        if m_art:
            artist_id = m_art.group(1)

        Actor.log.info(f"🚀 Scraping Weverse Shop URL: {target_url}")

        # Apify 프록시 설정 (실행 시 차단 방지)
        proxy_config = await Actor.create_proxy_configuration()
        proxy_url = await proxy_config.new_url() if proxy_config else None

        async with async_playwright() as p:
            launch_opts = {"headless": True}
            if proxy_url:
                launch_opts["proxy"] = {"server": proxy_url}

            browser = await p.chromium.launch(**launch_opts)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = await context.new_page()

            await page.goto(target_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(2000)

            # 스크롤하여 동적 상품 로딩
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)

            html_text = await page.content()
            next_data = extract_next_data(html_text)
            raw_products = find_products_in_json(next_data)

            products_map = {}
            for raw in raw_products:
                norm = normalize_product(raw, target_url, artist_id, currency)
                if norm["saleId"] and norm["name"]:
                    products_map[norm["saleId"]] = norm

            if not products_map:
                Actor.log.info("ℹ️ __NEXT_DATA__에서 상품을 찾지 못해 DOM 폴백 추출을 실행합니다.")
                dom_items = await scrape_dom_fallback(page, target_url, artist_id, currency)
                for item in dom_items:
                    products_map[item["saleId"]] = item

            results = list(products_map.values())
            if max_items > 0:
                results = results[:max_items]

            Actor.log.info(f"📦 총 {len(results)}개 상품 발견! 세부 정보 보강 여부: {include_details}")

            if include_details and results:
                for idx, item in enumerate(results):
                    try:
                        await page.goto(item["productUrl"], wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(800)
                        detail_html = await page.content()
                        detail_json = extract_next_data(detail_html)
                        detail_raws = find_products_in_json(detail_json)
                        for dr in detail_raws:
                            if str(dr.get("saleId") or dr.get("id")) == str(item["saleId"]):
                                enriched = normalize_product(dr, item["productUrl"], artist_id, currency)
                                item.update({k: v for k, v in enriched.items() if v not in (None, "")})
                                break
                    except Exception as e:
                        Actor.log.warning(f"상세 페이지 건너뜀 ({item['saleId']}): {e}")

                    await Actor.push_data(item)
                    if (idx + 1) % 10 == 0:
                        Actor.log.info(f"✅ 진행률: {idx + 1}/{len(results)}개 저장 완료")
            else:
                await Actor.push_data(results)

            await browser.close()
            Actor.log.info(f"🎉 완료! 총 {len(results)}개 상품 추출 성공.")