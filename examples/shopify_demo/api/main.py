"""Shopify demo API: a real Shopify store behind the shared storefront routes, plus the
merchant portal (MerchantBackend over the same Shopify catalog) under /api/merchant.

    uvicorn shopify_demo.api.main:app --app-dir examples --reload --port 8000
    (cd examples/shopify_demo/merchant-web && npm run dev)   # :3100, once that app exists

Set SHOPIFY_STORE_DOMAIN and SHOPIFY_STOREFRONT_TOKEN in .env before running.
"""

from __future__ import annotations

from pathlib import Path

from commerce_common.memory import InMemoryMemoryStore
from demo_common import (
    REPO_ROOT,
    CartAddRequest,
    MemorySeeder,
    build_storefront_host,
    load_demo_env,
)
from shopping_agent import ShoppingAgentConfig
from shopping_agent_runtime import ShoppingAgent

from .merchant import create_merchant_router
from .shopify_backend import ShopifyBackend

EXAMPLE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = EXAMPLE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_demo_env(EXAMPLE_DIR)

backend = ShopifyBackend()

config = ShoppingAgentConfig(
    brand_name="Develop Learning",
    assistant_name="Shopping Assistant",
    brand_voice=(
        "Professional, warm, and helpful. "
        "This is a general fashion and lifestyle store selling clothing, accessories, "
        "and apparel for men and women. "
        "All prices are in EUR."
    ),
)

agent = ShoppingAgent(
    backend=backend,
    skills_dir=REPO_ROOT / "shopping-agent" / "skills",
    config=config,
    memory_store=InMemoryMemoryStore(),
)

host = build_storefront_host(
    title="Shopify Commerce Agent Demo",
    example_root=EXAMPLE_DIR,
    backend=backend,
    agent=agent,
    memory_seeder=MemorySeeder(
        DATA_DIR / "memory-seed.json", marker=DATA_DIR / ".memory-seeded.json"
    ),
)
app = host.app
app.include_router(create_merchant_router(backend, InMemoryMemoryStore()), prefix="/api/merchant")


@app.on_event("startup")
async def _warm_merchant_catalog() -> None:
    # ShopifyMerchantBackend.all_listings() is synchronous (the shared merchant router
    # calls it without `await`, matching examples/retail/api/mock_merchant.py's own
    # sync all_listings()), so the catalog has to already be populated by the time any
    # request can reach it — fetch it once here, before uvicorn starts accepting traffic.
    await backend.list_catalog()


@app.post("/api/cart/add")
async def cart_add(request: CartAddRequest, record: host.CurrentSession) -> dict:
    return await host.direct_add(
        record,
        request,
        note="[Already completed] The customer used the add-to-cart button to add {title} ({product_id}), quantity {quantity}. The item is already in the cart — do NOT add it again.",
    )
