import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from apify import Actor
from playwright.async_api import async_playwright


def pick_first_not_none(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def to_sale_id(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    return s or None


def to_float(val):
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        nested = pick_first_not_none(val, "amount", "value", "salePrice", "price")
        return to_float(nested)
    if isinstance(val, str):
        cleaned = re.sub(r"[^\d.]", "", val.replace(",", ""))
        if not cleaned or cleaned.count(".") > 1:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def to_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("text", "name", "label", "title", "url", "value"):
            if k in val and val[k] is not None:
                return to_str(val[k])
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, list):
        return ", ".join(filter(None, (to_str(x) for x in val)))
    return str(val).strip()


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
    sale_id = to_sale_id(pick_first_not_none(raw, "saleId", "id"))
    name = to_str(pick_first_not_none(raw, "name", "title"))

    raw_price = pick_first_not_none(raw, "salePrice", "price", "priceAmount", "salePriceAmount")
    price_val = to_float(raw_price)

    raw_orig = pick_first_not_none(raw, "originalPrice")
    orig_price = to_float(raw_orig)

    status = to_str(pick_first_not_none(raw, "status", "saleStatus")).upper()
    is_sold_out = bool(raw.get("isSoldOut") or raw.get("soldOut") or "SOLD_OUT" in status or "SOLDOUT" in status)

    img = to_str(pick_first_not_none(raw, "thumbnailUrl", "imageUrl", "image", "thumbnailImageUrl"))
    if isinstance(raw.get("images"), list) and raw["images"] and not img:
        img = to_str(raw["images"][0])

    product_url = f"https://shop.weverse.io/en/shop/{currency}/artists/{artist_id}/sales/{sale_id}"
    if base_url and "/artists/" in base_url:
        prefix = base_url.split("/sales/")[0].split("/categories/")[0].split("?")[0]
        product_url = f"{prefix}/sales/{sale_id}"

    cat_str = to_str(category_name) or to_str(raw.get("categoryName"))
    label_str = to_str(pick_first_not_none(raw, "label", "badge"))

    return {
        "saleId": sale_id,
        "name": name,
        "price": price_val,
        "originalPrice": orig_price,
        "currency": to_str(currency),
        "isSoldOut": is_sold_out,
        "stockStatus": "SOLD_OUT" if is_sold_out else (status or "IN_STOCK"),
        "category": cat_str,
        "label": label_str,
        "imageUrl": img,
        "productUrl": product_url,
        "artistId": to_str(artist_id),
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
            "name": to_str(name_line),
            "price": to_float(price_line),
            "originalPrice": None,
            "currency": to_str(currency),
            "isSoldOut": is_sold_out,
            "stockStatus": "SOLD_OUT" if is_sold_out else "IN_STOCK",
            "category": to_str(category_name),
            "label": "",
            "imageUrl": to_str(img_url),
            "productUrl": urljoin("https://shop.weverse.io", href),
            "artistId": to_str(artist_id),
        })
    return items


async def safe_goto(page, url: str, timeout: int = 60000):
    """일시적인 네트워크 변경/끊김 시 최대 3번 자동 재시도"""
    last_err = None
    for attempt in range(1, 4):
        try:
            return await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            last_err = e
            Actor.log.warning(f"⚠️ 페이지 이동 재시도 ({attempt}/3): {e}")
            await page.wait_for_timeout(1500)
    raise last_err


async def collect_from_page(page, url: str, artist_id: str, currency: str, captured_api_jsons: list, products_map: dict, category_name: str = ""):
    captured_api_jsons.clear()
    await safe_goto(page, url, timeout=60000)
    await page.wait_for_timeout(2500)

    for _ in range(3):
        await page.evaluate(
            "() => { const h = document.documentElement?.scrollHeight || document.body?.scrollHeight || 0; if (h > 0) window.scrollTo(0, h); }"
        )
        await page.wait_for_timeout(900)

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


async def push_results_safely(results: list) -> int:
    if not results:
        return 0
    try:
        await Actor.push_data(results)
        return len(results)
    except Exception as batch_err:
        Actor.log.warning(f"⚠️ 일괄 저장 실패 ({batch_err}) → 개별 저장으로 전환합니다.")
        pushed = 0
        for item in results:
            try:
                await Actor.push_data(item)
                pushed += 1
            except Exception as item_err:
                Actor.log.warning(f"⚠️ 항목 저장 건너뜀 (saleId={item.get('saleId')}): {item_err}")
        return pushed


async def run_browser_scrape(target_url: str, artist_id: str, currency: str, max_items: int, include_details: bool, proxy_url: str | None) -> list:
    captured_api_jsons = []
    products_map = {}

    async with async_playwright() as p:
        launch_opts = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
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

        await collect_from_page(page, target_url, artist_id, currency, captured_api_jsons, products_map, "Featured")
        Actor.log.info(f"🏠 메인 페이지 상품 수집: {len(products_map)}개")

        if (max_items == 0 or len(products_map) < max_items) and "/categories/" not in target_url:
            cat_links = await page.query_selector_all('a[href*="/categories/"]')
            cat_urls = []
            seen_cat = set()
            for el in cat_links:
                href = await el.get_attribute("href") or ""
                full_cat = urljoin("https://shop.weverse.io", href).split("?")[0]
                if f"/artists/{artist_id}/categories/" in full_cat and full_cat not in seen_cat:
                    seen_cat.add(full_cat)
                    raw_text = (await el.inner_text() or "").strip()
                    first_line = raw_text.splitlines()[0].strip() if raw_text else "Category"
                    clean_cat = re.sub(r"\s+", " ", first_line)[:50]
                    cat_urls.append((full_cat, clean_cat))

            Actor.log.info(f"📂 카테고리 탭 {len(cat_urls)}개 발견, 탐색 시작...")
            for cat_url, cat_name in cat_urls[:12]:
                if max_items > 0 and len(products_map) >= max_items:
                    break
                try:
                    await collect_from_page(page, cat_url, artist_id, currency, captured_api_jsons, products_map, cat_name)
                    Actor.log.info(f"   └ [{cat_name}] 누적 상품: {len(products_map)}개")
                except Exception as cat_err:
                    Actor.log.warning(f"카테고리 건너뜀 ({cat_name}): {cat_err}")

        results = list(products_map.values())
        if max_items > 0:
            results = results[:max_items]

        if include_details and results:
            Actor.log.info(f"🔎 상세 페이지 보강 시작 ({len(results)}개)...")
            for idx, item in enumerate(results):
                try:
                    await page.goto(item["productUrl"], wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(400)
                    d_html = await page.content()
                    d_json = extract_next_data(d_html)
                    for dr in find_products_in_json(d_json):
                        if str(dr.get("saleId") or dr.get("id")) == str(item["saleId"]):
                            enriched = normalize_product(dr, item["productUrl"], artist_id, currency, item.get("category", ""))
                            item.update({k: v for k, v in enriched.items() if v not in (None, "")})
                            break
                except Exception as e:
                    Actor.log.warning(f"상세 건너뜀 ({item['saleId']}): {e}")

                if (idx + 1) % 10 == 0:
                    Actor.log.info(f"   ✅ 상세 보강 진행률: {idx + 1}/{len(results)}개")

        await browser.close()
        return results


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        artist_id = str(actor_input.get("artistId", "2")).strip()
        currency = str(actor_input.get("currency", "USD")).strip()
        language = str(actor_input.get("language", "en")).strip()
        max_items_raw = actor_input.get("maxItems", 50)
        max_items = 50 if max_items_raw is None else int(max_items_raw)
        custom_url = (actor_input.get("customArtistUrl") or "").strip()
        include_details = bool(actor_input.get("includeProductDetails", False))

        target_url = custom_url if custom_url else f"https://shop.weverse.io/{language}/shop/{currency}/artists/{artist_id}"
        m_art = re.search(r"/artists/(\d+)", target_url)
        if m_art:
            artist_id = m_art.group(1)

        Actor.log.info(f"🚀 Scraping Weverse Shop: {target_url}")
        results = []

        try:
            results = await run_browser_scrape(target_url, artist_id, currency, max_items, include_details, None)
        except Exception as direct_err:
            Actor.log.warning(f"⚠️ 직접 접속 오류 ({direct_err}) → 프록시로 자동 전환합니다.")

        if not results:
            Actor.log.info("🔄 Apify 프록시로 재시도합니다...")
            proxy_config = await Actor.create_proxy_configuration()
            proxy_url = await proxy_config.new_url() if proxy_config else None
            if proxy_url:
                results = await run_browser_scrape(target_url, artist_id, currency, max_items, include_details, proxy_url)

        pushed_count = await push_results_safely(results)

        if pushed_count > 0:
            try:
                await Actor.charge(event_name="product-scraped", count=pushed_count)
            except Exception as charge_err:
                Actor.log.info(f"ℹ️ PPE charge info (무료 테스트 중이면 정상): {charge_err}")
            Actor.log.info(f"🎉 완료! 총 {pushed_count}개 상품 저장 성공.")
        else:
            Actor.log.warning("❌ 상품을 찾지 못했습니다.")