"""Shopify StorefrontBackend: maps the Shopify Storefront API (GraphQL) to the
commerce-agents StorefrontBackend interface.

Requires: httpx (pip install httpx)
Env vars: SHOPIFY_STORE_DOMAIN, SHOPIFY_STOREFRONT_TOKEN
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)


class ShopifyBackend(StorefrontBackend):
    """A StorefrontBackend that talks to a real Shopify store via the Storefront API."""

    def __init__(
        self,
        store_domain: str | None = None,
        storefront_token: str | None = None,
    ) -> None:
        self._domain = store_domain or os.environ["SHOPIFY_STORE_DOMAIN"]
        self._token = storefront_token or os.environ["SHOPIFY_STOREFRONT_TOKEN"]
        self._api_url = f"https://{self._domain}/api/2024-10/graphql.json"
        self._client = httpx.AsyncClient(timeout=15.0)
        self._session_carts: dict[str, str] = {}
        self.products: dict[str, ProductDetails] = {}
        self.variants: dict[str, ProductDetails] = {}
        self.store_name: str = store_domain or "the store"

    # ------------------------------------------------------------------
    # GraphQL helper
    # ------------------------------------------------------------------

    async def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        resp = await self._client.post(
            self._api_url,
            json={"query": query, "variables": variables or {}},
            headers={
                "X-Shopify-Storefront-Access-Token": self._token,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"Shopify GraphQL error: {body['errors']}")
        return body.get("data", {})

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_gid(gid: str) -> str:
        return gid.rsplit("/", 1)[-1] if gid else gid

    @staticmethod
    def _money(price_obj: dict | None) -> float:
        if not price_obj:
            return 0.0
        return float(price_obj.get("amount", 0))

    @staticmethod
    def _currency(price_obj: dict | None) -> str:
        if not price_obj:
            return "EUR"
        return price_obj.get("currencyCode", "EUR")

    def _map_product_summary(self, node: dict) -> Product:
        price_range = node.get("priceRange", {})
        min_price = price_range.get("minVariantPrice", {})
        first_image = None
        images = node.get("images", {}).get("nodes", [])
        if images:
            first_image = images[0].get("url")

        attributes: dict[str, str] = {}
        if node.get("productType"):
            attributes["type"] = node["productType"]
        for tag in node.get("tags", []):
            attributes[tag.lower()] = tag

        in_stock = node.get("availableForSale", True)

        return Product(
            product_id=self._parse_gid(node["id"]),
            title=node.get("title", ""),
            brand=node.get("vendor"),
            price=self._money(min_price),
            currency=self._currency(min_price),
            image_url=first_image,
            category=node.get("productType") or None,
            attributes=attributes,
            in_stock=in_stock,
            short_description=(node.get("description") or "")[:200] or None,
        )

    def _map_product_details(self, node: dict) -> ProductDetails:
        summary = self._map_product_summary(node)
        description_html = node.get("descriptionHtml", "")
        long_desc = re.sub(r"<[^>]+>", "", description_html).strip() if description_html else None

        variants: list[Product] = []
        variant_nodes = node.get("variants", {}).get("nodes", [])
        has_real_options = len(variant_nodes) > 1

        for v in variant_nodes:
            v_price = v.get("price", {})
            option_values: dict[str, str] = {}
            for opt in v.get("selectedOptions", []):
                option_values[opt["name"]] = opt["value"]

            v_image = None
            if v.get("image"):
                v_image = v["image"].get("url")

            variants.append(
                Product(
                    product_id=self._parse_gid(v["id"]),
                    title=v.get("title", ""),
                    price=self._money(v_price),
                    currency=self._currency(v_price),
                    in_stock=v.get("availableForSale", True),
                    image_url=v_image,
                    option_values=option_values,
                    variant_of=summary.product_id if has_real_options else None,
                )
            )

        options: dict[str, list[str]] = {}
        if has_real_options:
            for v in variant_nodes:
                for opt in v.get("selectedOptions", []):
                    options.setdefault(opt["name"], [])
                    if opt["value"] not in options[opt["name"]]:
                        options[opt["name"]].append(opt["value"])

        details = ProductDetails(
            product_id=summary.product_id,
            title=summary.title,
            brand=summary.brand,
            price=summary.price,
            currency=summary.currency,
            image_url=summary.image_url,
            category=summary.category,
            attributes=summary.attributes,
            in_stock=summary.in_stock,
            short_description=summary.short_description,
            long_description=long_desc,
            options=options,
            variants=variants if has_real_options else [],
        )
        self.products[details.product_id] = details
        return details

    # ------------------------------------------------------------------
    # Catalog — @inContext(country: PT) forces EUR pricing everywhere
    # ------------------------------------------------------------------

    _SEARCH_QUERY = """
    query SearchProducts($query: String!, $first: Int!) @inContext(country: PT) {
      search(query: $query, first: $first, types: PRODUCT) {
        nodes {
          ... on Product {
            id
            title
            vendor
            productType
            tags
            description
            descriptionHtml
            availableForSale
            priceRange {
              minVariantPrice { amount currencyCode }
            }
            images(first: 5) {
              nodes { url }
            }
            variants(first: 50) {
              nodes {
                id
                title
                price { amount currencyCode }
                availableForSale
                selectedOptions { name value }
                image { url }
              }
            }
          }
        }
      }
    }
    """

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        del session
        data = await self._gql(self._SEARCH_QUERY, {"query": query, "first": min(limit, 20)})
        nodes = data.get("search", {}).get("nodes", [])

        products: list[Product] = []
        for n in nodes:
            if not n.get("id"):
                continue
            details = self._map_product_details(n)
            products.append(
                Product(
                    product_id=details.product_id,
                    title=details.title,
                    brand=details.brand,
                    price=details.price,
                    currency=details.currency,
                    image_url=details.image_url,
                    category=details.category,
                    attributes=details.attributes,
                    in_stock=details.in_stock,
                    short_description=details.short_description,
                    options=details.options,
                )
            )

        if filters:
            if filters.min_price is not None:
                products = [p for p in products if p.price >= filters.min_price]
            if filters.max_price is not None:
                products = [p for p in products if p.price <= filters.max_price]
            if filters.category:
                cat = filters.category.lower()
                products = [p for p in products if (p.category or "").lower() == cat]

        return products[:limit]

    _PRODUCT_QUERY = """
    query GetProduct($id: ID!) @inContext(country: PT) {
      product(id: $id) {
        id
        title
        vendor
        productType
        tags
        description
        descriptionHtml
        availableForSale
        priceRange {
          minVariantPrice { amount currencyCode }
        }
        images(first: 5) {
          nodes { url }
        }
        variants(first: 50) {
          nodes {
            id
            title
            price { amount currencyCode }
            availableForSale
            selectedOptions { name value }
            image { url }
          }
        }
      }
    }
    """

    def product(self, product_id: str) -> ProductDetails | None:
        return self.products.get(product_id)

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        del session
        if product_id in self.products:
            return self.products[product_id]
        gid = f"gid://shopify/Product/{product_id}"
        data = await self._gql(self._PRODUCT_QUERY, {"id": gid})
        node = data.get("product")
        if not node:
            return None
        return self._map_product_details(node)

    # ------------------------------------------------------------------
    # Cart — all mutations use @inContext(country: PT) for EUR
    # ------------------------------------------------------------------

    _CART_FIELDS = """
        id checkoutUrl
        lines(first: 50) { nodes {
          id quantity
          merchandise { ... on ProductVariant {
            id title price { amount currencyCode }
            product { id title }
            image { url }
            selectedOptions { name value }
          }}
        }}
        cost { subtotalAmount { amount currencyCode } }
    """

    _CART_CREATE = f"""
    mutation CartCreate($lines: [CartLineInput!]!) @inContext(country: PT) {{
      cartCreate(input: {{ lines: $lines }}) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """

    _CART_LINES_ADD = f"""
    mutation CartLinesAdd($cartId: ID!, $lines: [CartLineInput!]!) @inContext(country: PT) {{
      cartLinesAdd(cartId: $cartId, lines: $lines) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """

    _CART_LINES_UPDATE = f"""
    mutation CartLinesUpdate($cartId: ID!, $lines: [CartLineUpdateInput!]!) @inContext(country: PT) {{
      cartLinesUpdate(cartId: $cartId, lines: $lines) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """

    _CART_LINES_REMOVE = f"""
    mutation CartLinesRemove($cartId: ID!, $lineIds: [ID!]!) @inContext(country: PT) {{
      cartLinesRemove(cartId: $cartId, lineIds: $lineIds) {{
        cart {{ {_CART_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """

    _CART_QUERY = f"""
    query GetCart($id: ID!) @inContext(country: PT) {{
      cart(id: $id) {{ {_CART_FIELDS} }}
    }}
    """

    def _map_cart(self, cart_data: dict | None) -> Cart:
        if not cart_data:
            return Cart()
        items: list[CartItem] = []
        for line in cart_data.get("lines", {}).get("nodes", []):
            merch = line.get("merchandise", {})
            product = merch.get("product", {})
            option_values = {
                o["name"]: o["value"] for o in merch.get("selectedOptions", [])
            }
            items.append(
                CartItem(
                    product_id=self._parse_gid(merch.get("id", "")),
                    title=product.get("title", merch.get("title", "")),
                    price=self._money(merch.get("price")),
                    quantity=line.get("quantity", 1),
                    image_url=(merch.get("image") or {}).get("url"),
                    option_values=option_values,
                    variant_of=self._parse_gid(product.get("id", "")),
                )
            )
        currency = "EUR"
        cost = cart_data.get("cost", {})
        subtotal = cost.get("subtotalAmount", {})
        if subtotal:
            currency = subtotal.get("currencyCode", "EUR")
        return Cart(items=items, currency=currency)

    def _find_line_id(self, cart_data: dict, variant_id: str) -> str | None:
        for line in cart_data.get("lines", {}).get("nodes", []):
            merch = line.get("merchandise", {})
            if self._parse_gid(merch.get("id", "")) == variant_id:
                return line["id"]
        return None

    async def _get_or_create_cart_id(self, session_id: str) -> str | None:
        return self._session_carts.get(session_id)

    async def _get_raw_cart(self, cart_id: str) -> dict | None:
        data = await self._gql(self._CART_QUERY, {"id": cart_id})
        return data.get("cart")

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        cart_id = await self._get_or_create_cart_id(session.session_id)
        if not cart_id:
            return Cart()
        raw = await self._get_raw_cart(cart_id)
        return self._map_cart(raw)

    async def _resolve_first_variant_id(self, product_id: str) -> str:
        cached = self.products.get(product_id)
        if cached and cached.variants:
            for v in cached.variants:
                if v.in_stock:
                    return f"gid://shopify/ProductVariant/{v.product_id}"
        gid = f"gid://shopify/Product/{product_id}"
        data = await self._gql(
            """query ($id: ID!) @inContext(country: PT) { product(id: $id) {
                variants(first: 1) { nodes { id availableForSale } }
            }}""",
            {"id": gid},
        )
        product = data.get("product")
        if not product:
            raise KeyError(f"Product {product_id} not found")
        variants = product.get("variants", {}).get("nodes", [])
        if not variants:
            raise KeyError(f"No variants for product {product_id}")
        if not variants[0].get("availableForSale", False):
            raise Unavailable(f"Product {product_id} is out of stock")
        return variants[0]["id"]

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        variant_gid = await self._resolve_first_variant_id(product_id)
        cart_id = await self._get_or_create_cart_id(session.session_id)
        line = {"merchandiseId": variant_gid, "quantity": quantity}

        if cart_id:
            data = await self._gql(self._CART_LINES_ADD, {"cartId": cart_id, "lines": [line]})
            cart_data = data.get("cartLinesAdd", {}).get("cart")
        else:
            data = await self._gql(self._CART_CREATE, {"lines": [line]})
            cart_data = data.get("cartCreate", {}).get("cart")
            if cart_data:
                self._session_carts[session.session_id] = cart_data["id"]

        return self._map_cart(cart_data)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        cart_id = await self._get_or_create_cart_id(session.session_id)
        if not cart_id:
            return Cart()
        raw = await self._get_raw_cart(cart_id)
        if not raw:
            return Cart()
        line_id = self._find_line_id(raw, product_id)
        if not line_id:
            return self._map_cart(raw)
        data = await self._gql(
            self._CART_LINES_UPDATE,
            {"cartId": cart_id, "lines": [{"id": line_id, "quantity": quantity}]},
        )
        return self._map_cart(data.get("cartLinesUpdate", {}).get("cart"))

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        cart_id = await self._get_or_create_cart_id(session.session_id)
        if not cart_id:
            return Cart()
        raw = await self._get_raw_cart(cart_id)
        if not raw:
            return Cart()
        line_id = self._find_line_id(raw, product_id)
        if not line_id:
            return self._map_cart(raw)
        data = await self._gql(
            self._CART_LINES_REMOVE, {"cartId": cart_id, "lineIds": [line_id]}
        )
        return self._map_cart(data.get("cartLinesRemove", {}).get("cart"))

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        cart_id = await self._get_or_create_cart_id(session.session_id)
        if not cart_id:
            return []
        raw = await self._get_raw_cart(cart_id)
        if not raw or not raw.get("checkoutUrl"):
            return []
        return [CheckoutHandoff(url=raw["checkoutUrl"], label="Complete purchase on Shopify")]

    # ------------------------------------------------------------------
    # Customer context (stubbed for demo)
    # ------------------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        return UserPreferences(
            user_id=session.user_id,
            display_name="Shopper",
        )

    # ------------------------------------------------------------------
    # Orders and policies (stubbed for demo)
    # ------------------------------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        return []

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        return None

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        return [
            Policy(
                policy_id="shipping",
                title="Shipping Policy",
                category="shipping",
                content="Standard shipping 3-5 business days. Free shipping on orders over €49.",
            ),
            Policy(
                policy_id="returns",
                title="Return Policy",
                category="returns",
                content="30-day return policy. Items must be unused and in original packaging.",
            ),
        ]

    # ------------------------------------------------------------------
    # Fulfillment (simplified)
    # ------------------------------------------------------------------

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        return [
            FulfillmentOption(method="delivery", eta="3-5 business days", fee=5.99),
            FulfillmentOption(method="delivery", eta="1-2 business days (express)", fee=12.99),
        ]

    # ------------------------------------------------------------------
    # Full catalog — search_products only fetches what a query matches, but the
    # merchant portal (inventory alerts, browse, SQL analysis) needs the whole
    # store. This walks products(first, after) rather than search(), and fills
    # self.products the same way search/get_product_details do.
    # ------------------------------------------------------------------

    _CATALOG_PAGE_QUERY = """
    query CatalogPage($first: Int!, $after: String) @inContext(country: PT) {
      products(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          title
          vendor
          productType
          tags
          description
          descriptionHtml
          availableForSale
          priceRange {
            minVariantPrice { amount currencyCode }
          }
          images(first: 5) {
            nodes { url }
          }
          variants(first: 50) {
            nodes {
              id
              title
              price { amount currencyCode }
              availableForSale
              selectedOptions { name value }
              image { url }
            }
          }
        }
      }
    }
    """

    async def list_catalog(
        self, *, page_size: int = 50, max_products: int = 250
    ) -> list[ProductDetails]:
        """Fetch every product (paginated), caching each into ``self.products`` exactly
        as ``search_products``/``get_product_details`` do. Call once at merchant-backend
        startup and again whenever the portal needs a fresher view; there is no
        change-notification from Shopify here, so staleness is bounded by how often the
        caller re-fetches, not tracked automatically."""
        collected: list[ProductDetails] = []
        cursor: str | None = None
        while len(collected) < max_products:
            data = await self._gql(
                self._CATALOG_PAGE_QUERY,
                {"first": min(page_size, max_products - len(collected)), "after": cursor},
            )
            page = data.get("products", {})
            for node in page.get("nodes", []):
                if not node.get("id"):
                    continue
                collected.append(self._map_product_details(node))
            page_info = page.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return collected

    # ------------------------------------------------------------------
    # DemoStorefront protocol extras (demo_common.host) — required to mount the
    # shared merchant portal router alongside the storefront routes.
    # ------------------------------------------------------------------

    def reset_session(self, session_id: str) -> None:
        self._session_carts.pop(session_id, None)

    def recent_orders(self, limit: int = 6) -> list[Order]:
        # Order history lives in Shopify's Admin API, which this demo backend does not
        # call (only the Storefront API). The merchant portal's "recent orders" panel
        # reads the merchant backend's own orders.json instead — see api/merchant.py.
        # Sync on purpose: the shared merchant router calls this without ``await``
        # (matching examples/retail/api/mock_retail.py's own recent_orders, which is
        # sync too) — the DemoStorefront Protocol's "async def" in host.py doesn't match
        # how it's actually called, so the real contract is: keep this synchronous.
        return []
