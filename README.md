# Weverse Shop Scraper (K-Pop Merch, Prices & Stock API)

Extract official K-Pop merchandise, albums, prices (USD / KRW / JPY), sold-out stock status, labels, images, and direct product links from **Weverse Shop** (`shop.weverse.io`).

## What data does this Actor extract?

For every product found in an artist's store and category tabs, you get clean, structured JSON / CSV / Excel data:

- `saleId` — Unique Weverse Shop product ID
- `name` — Official product title
- `price` & `originalPrice` — Current and original prices in your selected currency (`USD`, `KRW`, or `JPY`)
- `currency` — Shop currency (`USD`, `KRW`, `JPY`)
- `isSoldOut` & `stockStatus` — Boolean sold-out flag and stock status (`SALE` / `SOLD_OUT`)
- `category` — Category / tab name (Merch, Album, Membership, etc.)
- `imageUrl` — Direct thumbnail image URL
- `productUrl` — Direct link to the Weverse Shop product page
- `artistId` — Weverse Shop artist ID

## Supported Artists (Examples)

Select directly from the dropdown or pass **any** `customArtistUrl`:
- **BTS** (`2`)
- **TOMORROW X TOGETHER** (`3`)
- **SEVENTEEN** (`7`)
- **ENHYPEN** (`10`)
- **LE SSERAFIM** (`50`)
- **NewJeans** (`82`)
- **ILLIT** (`148`)
- **KATSEYE** (`152`)

## Use Cases
- **K-Pop Resellers & Group Order Managers (GOM):** Monitor new drops, restocks, and sold-out status automatically on a schedule.
- **Cross-Border E-Commerce:** Compare `KRW` vs `USD` vs `JPY` pricing across Weverse Shop regions.
- **Fandom Apps & AI Agents:** Build merch alert Discord bots or K-Pop catalog APIs.

## Output Example

```json
{
  "saleId": 68696,
  "name": "S/S Jersey (NA Ver.)",
  "price": 85,
  "originalPrice": null,
  "currency": "USD",
  "isSoldOut": false,
  "stockStatus": "SALE",
  "category": "MERCH",
  "imageUrl": "https://cdn-contents.weverse.io/...",
  "productUrl": "https://shop.weverse.io/en/shop/USD/artists/2/sales/68696",
  "artistId": "2"
}