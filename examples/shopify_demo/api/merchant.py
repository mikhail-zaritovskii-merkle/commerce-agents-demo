# SPDX-License-Identifier: Apache-2.0

"""The Develop Learning merchant router: MerchantBackend over the live Shopify catalog
(via ShopifyBackend), with performance/inventory/campaign/order-issue analytics read
from the JSON fixtures in ``data/`` (merchant_metrics.json, merchant_inventory.json,
merchant_campaigns.json, merchant_messages.json, orders.json).

Modeled closely on examples/retail/api/mock_merchant.py + merchant.py, the reference
MerchantBackend implementation in commerce-agents-demo. The main difference: that
example owns its whole catalog (MockRetail loads it from one catalog.json), so
``self.storefront.products`` is fully populated at boot. ShopifyBackend only caches
what a search or a detail lookup has touched, so every method that needs the *whole*
catalog (browsing, inventory alerts, the SQL view) calls ``_ensure_catalog()`` first,
which fetches the full product list once via ``ShopifyBackend.list_catalog()``.

IMPORTANT — staged writes are demo-only. ``apply_change`` mutates this process's own
in-memory state (the same pattern mock_merchant.py uses) so the portal's preview/apply
flow works end-to-end, but nothing is written back to Shopify: that needs the Admin API
(a different token/scope than the Storefront API token this demo has). Until an Admin
API client is wired in here, every "applied" price or inventory change is local to this
server and resets on restart. get_merchant_context() states this as a DataLimitation so
the assistant says so instead of implying the storefront changed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from demo_common.merchant_fixtures import (
    alert_counts,
    apply_campaign_item,
    family_pricing_context,
    filter_listings,
    is_browse,
    load_campaigns,
    load_issues,
    margin_pct,
    metric_window,
    named_ids,
    promotion_targets,
    rebase_daily,
    refuse_outside_range,
    share_content,
    snapshot_of,
    stage_campaign as _stage_campaign,
)
from demo_common.storefront_fixtures import load_json, refresh_family
from fastapi import APIRouter

from commerce_common.memory import MemoryStore
from demo_common import REPO_ROOT, MerchantIdentity, build_merchant_router, host_approval_default
from merchant_agent import (
    ActorKind,
    AnalysisTable,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeLedger,
    DataLimitation,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantAgentConfig,
    MerchantBackend,
    MerchantSessionContext,
    MetricPoint,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
    check_analysis_sql,
)
from merchant_agent_runtime import MerchantAgent
from shopping_agent import ProductDetails, SearchFilters, ShoppingSessionContext

from .shopify_backend import ShopifyBackend

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

IDENTITY = MerchantIdentity(merchant_id="develop-learning", operator="Mikhail")


def build_merchant_config(store_name: str) -> MerchantAgentConfig:
    return MerchantAgentConfig(
        brand_name=store_name,
        require_host_approval=host_approval_default(),
        approval_surface="the Approve button on the change preview card",
        enable_analysis=True,
    )


class ShopifyMerchantBackend(MerchantBackend):
    def __init__(
        self,
        storefront: ShopifyBackend,
        config: MerchantAgentConfig | None = None,
        data_dir: Path = DATA_DIR,
        merchant_id: str = IDENTITY.merchant_id,
    ) -> None:
        self.storefront = storefront
        self.config = config or MerchantAgentConfig(brand_name="Develop Learning")
        self.ledger = ChangeLedger(self.config)
        self.merchant_id = merchant_id

        metrics = load_json(data_dir, "merchant_metrics.json")
        self._daily = rebase_daily(metrics["daily"])
        self._currency: str = metrics.get("currency", "EUR")

        inventory = load_json(data_dir, "merchant_inventory.json")
        self._default_stock: int = inventory.get("default_stock", 40)
        self._default_threshold: int = inventory.get("default_threshold", 8)
        self._inventory: dict[str, dict[str, Any]] = {
            row["product_id"]: dict(row) for row in inventory["inventory"]
        }

        self._campaigns = load_campaigns(data_dir)
        self._issues = load_issues(data_dir)

        orders_raw = load_json(data_dir, "orders.json")
        self._orders_by_id: dict[str, dict[str, Any]] = {
            row["order_id"]: row for row in orders_raw["orders"]
        }

        self._catalog_loaded = False

    # ------------------------------------------------------------------
    # Catalog prefetch — see module docstring
    # ------------------------------------------------------------------

    async def _ensure_catalog(self) -> None:
        if self._catalog_loaded:
            return
        await self.storefront.list_catalog()
        self._catalog_loaded = True

    def _product(self, listing_id: str) -> ProductDetails | None:
        return self.storefront.product(listing_id)

    def _state_row(self, product_id: str) -> dict[str, Any]:
        """The inventory row for a plain listing or a variant, defaulted on first touch
        from the live Shopify price (not from any local catalog, since we have none)."""
        product = self._product(product_id)
        key = product.product_id if product else product_id
        if key not in self._inventory:
            plain = product is not None and not product.has_options
            self._inventory[key] = {
                "product_id": key,
                "stock": self._default_stock if plain else 0,
                "threshold": self._default_threshold,
                "sales_last_30d": None,
                "unit_cost": round(product.price * 0.55, 2) if plain and product else None,
            }
        return self._inventory[key]

    def _sales_30d(self, product: ProductDetails) -> int | None:
        rows = [self._state_row(v.product_id) for v in product.variants] or [
            self._state_row(product.product_id)
        ]
        sales = [row["sales_last_30d"] for row in rows if row.get("sales_last_30d") is not None]
        return sum(sales) if sales else None

    def _listing(self, product_id: str) -> Listing | None:
        product = self._product(product_id)
        if product is None:
            return None
        row = self._state_row(product.product_id)
        if product.has_options:
            variants = [
                v for variant in product.variants if (v := self._listing(variant.product_id))
            ]
            stock = sum(v.stock for v in variants)
            active = any(v.status == "active" for v in variants)
            status = row.get("status") or ("active" if active else "out_of_stock")
            price = min((v.price for v in variants), default=product.price)
        else:
            stock = int(row.get("stock", self._default_stock))
            status = row.get("status") or ("out_of_stock" if stock == 0 else "active")
            price = product.price
        return Listing(
            listing_id=product.product_id,
            title=product.title,
            status=status,
            price=price,
            currency=product.currency,
            stock=stock,
            category=product.category,
            content_quality=row.get("content_quality", "good"),
            attributes=product.attributes,
            image_url=product.image_url,
            short_description=product.short_description,
            options=product.options,
            option_values=product.option_values,
            variant_of=product.variant_of,
        )

    async def all_listings(self) -> list[Listing]:
        await self._ensure_catalog()
        listings = [
            listing
            for product_id in self.storefront.products
            if (listing := self._listing(product_id)) is not None
        ]
        listings.sort(key=lambda listing: (listing.category or "", listing.listing_id))
        return listings

    # ------------------------------------------------------------------
    # Performance
    # ------------------------------------------------------------------

    def _alert_counts(self, alerts: list[InventoryAlert]):
        return alert_counts(alerts, self._issues, self.ledger)

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        del session
        alerts = await self._compute_alerts()
        return snapshot_of(
            self._daily, period, currency=self._currency, alerts=self._alert_counts(alerts)
        )

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        del session
        current, _, label = metric_window(self._daily, period or "last_30_days")
        cleaned = metric.strip().lower().replace(" ", "_")
        segment_cleaned = (segment or "").strip().lower().replace(" ", "-") or None

        # No category breakdown in merchant_metrics.json (unlike the retail example's
        # kids_room_sales column) — a segment request gets the store-wide series back
        # with a note, rather than a silently wrong per-category number.
        note = (
            "no per-category breakdown in this data set; showing store-wide figures"
            if segment_cleaned
            else None
        )

        def value_for(rows: list[dict[str, Any]]) -> float:
            sales = sum(r["sales"] for r in rows)
            orders = sum(r["orders"] for r in rows)
            traffic = sum(r["traffic"] for r in rows)
            if cleaned in {"sales", "revenue"}:
                return round(sales, 2)
            if cleaned == "orders":
                return float(orders)
            if cleaned == "traffic":
                return float(traffic)
            if cleaned in {"conversion", "conversion_rate"}:
                return round(orders / traffic * 100, 2) if traffic else 0.0
            if cleaned in {"average_order_value", "aov"}:
                return round(sales / orders, 2) if orders else 0.0
            return round(sales, 2)

        if granularity == "week":
            points = [
                MetricPoint(
                    date=current[start]["date"], value=value_for(current[start : start + 7])
                )
                for start in range(0, len(current), 7)
            ]
        else:
            points = [MetricPoint(date=row["date"], value=value_for([row])) for row in current]
        unit = (
            self._currency
            if cleaned in {"sales", "revenue", "average_order_value", "aov"}
            else None
        )
        return MetricSeries(
            metric=cleaned,
            unit=unit,
            granularity="week" if granularity == "week" else "day",
            period=label,
            segment=segment_cleaned,
            points=points,
            note=note,
        )

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        del session
        campaigns = list(self._campaigns.values())
        if campaign_id:
            campaigns = [c for c in campaigns if c.campaign_id == campaign_id]
        return campaigns

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        await self._ensure_catalog()
        if ids := named_ids(query, [*self.storefront.products, *self.storefront.variants]):
            listings = [listing for pid in ids if (listing := self._listing(pid))]
        elif is_browse(query):
            listings = [
                listing for pid in self.storefront.products if (listing := self._listing(pid))
            ]
        else:
            shopper = ShoppingSessionContext(
                session_id=session.session_id, user_id="merchant-portal"
            )
            products = await self.storefront.search_products(
                shopper, query, SearchFilters(), limit=max(limit, 8)
            )
            listings = [listing for p in products if (listing := self._listing(p.product_id))]
        return filter_listings(
            listings,
            filters,
            limit,
            sales_of=lambda listing_id: self._state_row(listing_id).get("sales_last_30d") or 0,
        )

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        del session
        await self._ensure_catalog()
        product = self._product(listing_id)
        listing = self._listing(listing_id) if product else None
        if product is None or listing is None:
            return None
        row = self._state_row(product.product_id)
        variants = [v for variant in product.variants if (v := self._listing(variant.product_id))]
        return ListingDetails(
            **listing.model_dump(),
            long_description=product.long_description,
            review_snippets=product.review_highlights,
            sales_last_30d=self._sales_30d(product),
            return_rate_pct=row.get("return_rate_pct"),
            missing_attributes=row.get("missing_attributes") or [],
            variants=variants,
        )

    # ------------------------------------------------------------------
    # Inventory and order health
    # ------------------------------------------------------------------

    async def _compute_alerts(self) -> list[InventoryAlert]:
        await self._ensure_catalog()
        alerts: list[InventoryAlert] = []
        for product_id, row in self._inventory.items():
            product = self._product(product_id)
            if product is None or product.has_options:
                continue
            stock = int(row.get("stock", self._default_stock))
            threshold = int(row.get("threshold", self._default_threshold))
            sales_30d = row.get("sales_last_30d")
            daily_pace = (sales_30d or 0) / 30
            listing = self._listing(product_id)
            visible = listing is not None and listing.status == "active" and stock > 0
            if stock <= threshold:
                alerts.append(
                    InventoryAlert(
                        listing_id=product_id,
                        title=product.title,
                        kind="low_stock",
                        option_values=product.option_values,
                        variant_of=product.variant_of,
                        stock=stock,
                        threshold=threshold,
                        days_of_cover=round(stock / daily_pace, 1) if daily_pace else None,
                        sales_last_30d=sales_30d,
                        storefront_visible=visible,
                    )
                )
        alerts.sort(key=lambda alert: (alert.kind != "low_stock", -(alert.sales_last_30d or 0)))
        return alerts

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        del session
        return await self._compute_alerts()

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        del session
        return list(self._issues)

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        del session
        await self._ensure_catalog()
        product = self._product(listing_id)
        if product is None:
            return None
        if product.has_options:
            return family_pricing_context(
                product,
                [self._pricing_context(variant) for variant in product.variants],
                self.config,
            )
        return self._pricing_context(product)

    def _pricing_context(self, product: ProductDetails) -> PricingContext:
        row = self._state_row(product.product_id)
        unit_cost = row.get("unit_cost")
        sales_30d = row.get("sales_last_30d") or 0
        demand = "rising" if sales_30d >= 35 else "falling" if sales_30d <= 5 else "steady"
        margin = round((product.price - unit_cost) / product.price * 100, 1) if unit_cost else None
        return PricingContext(
            listing_id=product.product_id,
            current_price=product.price,
            currency=product.currency,
            unit_cost=unit_cost,
            margin_pct=margin,
            min_price=round(unit_cost * 1.15, 2) if unit_cost else None,
            min_price_basis="cost" if unit_cost else None,
            max_price=round(product.price * 1.35, 2),
            max_price_delta_pct=self.config.max_price_delta_pct,
            max_promotion_discount_pct=self.config.max_promotion_discount_pct,
            demand_signal=demand,
            last_changed=row.get("last_price_change"),
            option_values=product.option_values,
        )

    # ------------------------------------------------------------------
    # Staged writes — DEMO ONLY, see module docstring. These mutate this
    # process's in-memory state; nothing is written to Shopify.
    # ------------------------------------------------------------------

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        listing = await self.get_listing(session, listing_id)
        if listing is None:
            raise ValueError(f"no listing {listing_id}")
        items = [
            ChangeItem(
                target=listing.listing_id,
                field=name,
                before=getattr(listing, name, listing.attributes.get(name)),
                after=value,
            )
            for name, value in fields.items()
        ]
        return self.ledger.stage(
            kind=ChangeKind.LISTING_UPDATE,
            summary=note or f"Update listing content on {listing.listing_id}",
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        await self._ensure_catalog()
        change_items = []
        margin_impact = 0.0
        costed = True
        margins: list[tuple[float, float]] = []
        margin_notes: list[str] = []
        currency: str | None = None
        for item in items:
            product = self._product(item.listing_id)
            if product is None:
                raise ValueError(f"no listing {item.listing_id}")
            if product.has_options:
                raise ValueError(f"{product.product_id} is priced per variant")
            resolved = product.product_id
            refuse_outside_range(resolved, item.new_price, self._pricing_context(product))
            before = product.price
            if currency is None:
                currency = product.currency
            row = self._state_row(resolved)
            unit_cost = row.get("unit_cost")
            pace = (row.get("sales_last_30d") or 0) / 30
            if unit_cost is None:
                costed = False
            else:
                margin_impact += (item.new_price - before) * pace * 7
                margin_before = margin_pct(before, unit_cost)
                margin_after = margin_pct(item.new_price, unit_cost)
                margins.append((margin_before, margin_after))
                margin_notes.append(
                    f"{resolved} margin: {margin_before}% -> {margin_after}% "
                    f"({margin_after - margin_before:+.1f} pts)"
                )
            change_items.append(
                ChangeItem(target=resolved, field="price", before=before, after=item.new_price)
            )
        return self.ledger.stage(
            kind=ChangeKind.PRICE_UPDATE,
            summary=note or f"Price update for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency=currency,
            margin_impact=round(margin_impact, 2) if costed else None,
            margin_before_pct=margins[0][0] if len(margins) == 1 else None,
            margin_after_pct=margins[0][1] if len(margins) == 1 else None,
            guardrail_notes=margin_notes if len(margins) > 1 else None,
        )

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        await self._ensure_catalog()
        change_items = []
        for item in items:
            product = self._product(item.listing_id)
            if product is None:
                raise ValueError(f"no listing {item.listing_id}")
            resolved = product.product_id
            row = self._state_row(resolved)
            current: Any = int(row.get("stock", self._default_stock))
            if item.action == "restock":
                if product.has_options:
                    raise ValueError(f"{resolved} is restocked per variant")
                after: Any = current + (item.quantity or 0)
                field = "stock"
            else:
                after = "paused" if item.action == "pause" else "active"
                field = "status"
                listing = self._listing(resolved)
                current = listing.status if listing else None
            change_items.append(
                ChangeItem(target=resolved, field=field, before=current, after=after)
            )
        return self.ledger.stage(
            kind=ChangeKind.INVENTORY_ACTION,
            summary=note or f"Inventory action for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        await self._ensure_catalog()
        items = []
        margin_impact = 0.0
        margins: list[tuple[float, float]] = []
        margin_notes: list[str] = []
        currency: str | None = None
        requested = {
            requested_id: self._product(requested_id) for requested_id in promotion.listing_ids
        }
        if missing := [requested_id for requested_id, found in requested.items() if found is None]:
            raise ValueError(f"no listing {missing[0]}")
        targets = promotion_targets(requested.values())
        for product in targets:
            listing_id = product.product_id
            if currency is None:
                currency = product.currency
            row = self._state_row(listing_id)
            pace = (row.get("sales_last_30d") or 0) / 30
            discount_value = product.price * promotion.discount_pct / 100
            margin_impact -= discount_value * pace * 7
            promo_price = round(product.price * (1 - promotion.discount_pct / 100), 2)
            unit_cost = row.get("unit_cost") or 0.0
            if unit_cost and promo_price > 0:
                margin_before = margin_pct(product.price, unit_cost)
                margin_after = margin_pct(promo_price, unit_cost)
                margins.append((margin_before, margin_after))
                margin_notes.append(
                    f"{listing_id} margin: {margin_before}% -> {margin_after}% "
                    f"({margin_after - margin_before:+.1f} pts) for the window"
                )
            items.append(
                ChangeItem(
                    target=listing_id,
                    field="promotion_price",
                    before=product.price,
                    after=promo_price,
                )
            )
        return self.ledger.stage(
            kind=ChangeKind.PROMOTION,
            summary=f"{promotion.name} ({promotion.discount_pct:.0f}% off, "
            f"{promotion.starts} to {promotion.ends})",
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency=currency,
            margin_impact=round(margin_impact, 2),
            margin_before_pct=margins[0][0] if len(margins) == 1 else None,
            margin_after_pct=margins[0][1] if len(margins) == 1 else None,
            guardrail_notes=margin_notes if len(margins) > 1 else None,
        )

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        return _stage_campaign(
            self.ledger, self._campaigns, campaign, actor=session.operator, currency=self._currency
        )

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        del session
        return self.ledger.pending()

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        applied = self.ledger.apply(change_id, actor=session.operator)
        self._apply_to_local_state(applied)
        return applied

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        return self.ledger.discard(change_id, actor=session.operator, actor_kind=actor_kind)

    def _apply_to_local_state(self, change: StagedChange) -> None:
        """Write an applied change into this process's own caches only — see the
        DEMO ONLY note in the module docstring. ``product.price``/``product.in_stock``
        on the cached ProductDetails are mutated so the portal reflects the change
        immediately, but a restart (or the next ``list_catalog()`` refresh) reverts to
        whatever Shopify itself still says, since nothing was pushed to the Admin API."""
        for item in change.items:
            product = self._product(item.target)
            if product is not None and change.kind is ChangeKind.PROMOTION:
                product.attributes["promotion"] = f"{change.summary}: {float(item.after):.2f}"
            if product is not None and change.kind in {
                ChangeKind.PRICE_UPDATE,
                ChangeKind.INVENTORY_ACTION,
                ChangeKind.LISTING_UPDATE,
            }:
                row = self._state_row(item.target)
                family = self._product(product.variant_of) if product.variant_of else None
                if change.kind is ChangeKind.PRICE_UPDATE:
                    product.price = float(item.after)
                    row["last_price_change"] = datetime.now(UTC).date().isoformat()
                elif change.kind is ChangeKind.INVENTORY_ACTION:
                    if item.field == "stock":
                        row["stock"] = int(row.get("stock", self._default_stock)) + (
                            int(item.after) - int(item.before or 0)
                        )
                        product.in_stock = row["stock"] > 0 and row.get("status") != "paused"
                    elif item.field == "status":
                        row["status"] = "paused" if item.after == "paused" else "active"
                        for variant in product.variants or [product]:
                            variant_row = self._state_row(variant.product_id)
                            if product.has_options:
                                if item.after == "paused":
                                    variant_row["status"] = "paused"
                                else:
                                    variant_row.pop("status", None)
                            variant.in_stock = (
                                item.after != "paused" and int(variant_row.get("stock", 1)) > 0
                            )
                if family or product.has_options:
                    refresh_family(family or product)
                if change.kind is ChangeKind.LISTING_UPDATE:
                    if item.field in {"title", "short_description", "long_description", "category"}:
                        share_content(product, item.field, item.after)
                    elif item.field == "content_quality":
                        row["content_quality"] = item.after
                    else:
                        product.attributes[item.field] = str(item.after)
                        row.setdefault("missing_attributes", [])
                        if item.field in row["missing_attributes"]:
                            row["missing_attributes"].remove(item.field)
            elif change.kind is ChangeKind.CAMPAIGN:
                apply_campaign_item(self._campaigns, item)

    # ------------------------------------------------------------------
    # Analysis queries
    # ------------------------------------------------------------------

    ANALYSIS_SCHEMA_NOTES = (
        "daily_metrics(date, sales, orders, traffic) - one row per day, 90 days ending "
        "yesterday, no per-category breakdown. "
        "listings(listing_id, title, category, status, price, stock, content_quality, "
        "sales_last_30d, unit_cost). "
        "campaigns(campaign_id, name, status, channel, budget, spend, revenue, starts, ends). "
        "SQLite dialect; single SELECT statements only."
    )

    async def get_analysis_schema(self, session: MerchantSessionContext) -> str | None:
        if session.merchant_id != self.merchant_id:
            return None
        return self.ANALYSIS_SCHEMA_NOTES

    async def _analysis_rows(self) -> tuple[list[tuple], list[tuple], list[tuple]]:
        """The plain row tuples for each analysis table, gathered on the event loop
        (this is the async part: it needs the full catalog). Building the actual sqlite3
        connection has to happen synchronously, in whichever thread runs the query —
        sqlite3 connections are thread-affine, so a connection opened here would fail
        the moment ``asyncio.to_thread`` ran a query against it on a worker thread."""
        await self._ensure_catalog()
        listings = await self.all_listings()
        listing_rows = [
            (
                listing.listing_id,
                listing.title,
                listing.category,
                listing.status,
                listing.price,
                listing.stock,
                listing.content_quality,
                self._state_row(listing.listing_id).get("sales_last_30d"),
                self._state_row(listing.listing_id).get("unit_cost"),
            )
            for listing in listings
        ]
        daily_rows = [(r["date"], r["sales"], r["orders"], r["traffic"]) for r in self._daily]
        campaign_rows = [
            (c.campaign_id, c.name, c.status, c.channel, c.budget, c.spend, c.revenue,
             c.starts, c.ends)
            for c in self._campaigns.values()
        ]
        return listing_rows, daily_rows, campaign_rows

    @staticmethod
    def _build_analysis_connection(
        listing_rows: list[tuple], daily_rows: list[tuple], campaign_rows: list[tuple]
    ) -> sqlite3.Connection:
        """Synchronous on purpose — must run in the same thread that queries it."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE daily_metrics (date TEXT, sales REAL, orders INTEGER, traffic INTEGER)"
        )
        conn.executemany("INSERT INTO daily_metrics VALUES (?, ?, ?, ?)", daily_rows)
        conn.execute(
            "CREATE TABLE listings (listing_id TEXT, title TEXT, category TEXT, status TEXT, "
            "price REAL, stock INTEGER, content_quality TEXT, sales_last_30d INTEGER, "
            "unit_cost REAL)"
        )
        conn.executemany("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", listing_rows)
        conn.execute(
            "CREATE TABLE campaigns (campaign_id TEXT, name TEXT, status TEXT, channel TEXT, "
            "budget REAL, spend REAL, revenue REAL, starts TEXT, ends TEXT)"
        )
        conn.executemany("INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", campaign_rows)
        conn.commit()

        read_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        recursive = getattr(sqlite3, "SQLITE_RECURSIVE", None)
        if recursive is not None:
            read_actions.add(recursive)

        def read_only(action: int, *args: object) -> int:
            return sqlite3.SQLITE_OK if action in read_actions else sqlite3.SQLITE_DENY

        conn.set_authorizer(read_only)
        conn.set_progress_handler(lambda: 1, 5_000_000)
        return conn

    async def execute_analysis_query(
        self, session: MerchantSessionContext, sql: str
    ) -> AnalysisTable | None:
        if session.merchant_id != self.merchant_id:
            raise PermissionError("analysis queries are scoped to this store's own sessions")
        if reason := check_analysis_sql(sql):
            raise ValueError(f"query refused: {reason}")

        listing_rows, daily_rows, campaign_rows = await self._analysis_rows()

        def _run() -> tuple[list[str], list[Any]]:
            # Connection is opened AND queried here, in the same worker thread.
            conn = self._build_analysis_connection(listing_rows, daily_rows, campaign_rows)
            try:
                cursor = conn.execute(sql)
                columns = [d[0] for d in cursor.description or []]
                fetched = cursor.fetchmany(self.config.max_analysis_rows + 1)
                return columns, fetched
            finally:
                conn.close()

        try:
            columns, fetched = await asyncio.to_thread(_run)
        except sqlite3.Error as error:
            raise ValueError(str(error)) from error
        truncated = len(fetched) > self.config.max_analysis_rows
        rows = [list(row) for row in fetched[: self.config.max_analysis_rows]]
        return AnalysisTable(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            note="row-capped" if truncated else None,
        )

    # ------------------------------------------------------------------
    # Merchant context
    # ------------------------------------------------------------------

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        alerts = await self._compute_alerts()
        counts = self._alert_counts(alerts)
        latest = self._daily[-1]["date"]
        week_start = (date.fromisoformat(latest) - timedelta(days=6)).isoformat()
        await self._ensure_catalog()
        return {
            "store": self.storefront.store_name,
            "operator": session.operator,
            "current_period": f"{week_start}/{latest}",
            "catalog_size": len(self.storefront.products),
            "limitations": [
                DataLimitation(
                    source="writes",
                    note="price/inventory/listing changes apply to this demo session only "
                    "and are not pushed to Shopify (needs the Admin API, not yet configured)",
                ).model_dump(),
                DataLimitation(
                    source="orders",
                    note="order history comes from a fixed demo data set, not live Shopify orders",
                ).model_dump(),
                DataLimitation(
                    source="metrics",
                    note="daily sales/orders/traffic are demo data with no per-category breakdown",
                ).model_dump(),
            ],
            "alerts": {
                "low_stock": counts.low_stock,
                "slow_movers": counts.slow_movers,
                "order_issues": counts.order_issues,
                "pending_changes": counts.pending_changes,
            },
        }


def create_merchant_router(storefront: ShopifyBackend, memory_store: MemoryStore) -> APIRouter:
    config = build_merchant_config(storefront.store_name or "Develop Learning")
    merchant = ShopifyMerchantBackend(storefront, config, merchant_id=IDENTITY.merchant_id)
    agent = MerchantAgent(
        backend=merchant,
        skills_dir=REPO_ROOT / "merchant-agent" / "skills",
        config=config,
        memory_store=memory_store,
    )
    return build_merchant_router(
        storefront=storefront,
        backend=merchant,
        agent=agent,
        identity=IDENTITY,
        example_dir="shopify_demo",
    )
