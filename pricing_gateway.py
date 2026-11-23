"""
Thin adapter between the pricing service and the Dodo Payments SDK.

The pricing service only needs two operations, so tests can swap in a fake with the same two methods:
  read_product(product_id)  -> {"price_minor": int, "currency": str}
  ensure_product(sync_key, spec) -> product_id        (find-or-create, then verified)

Dodo has no idempotency key on product creation, so de-duplication is done through product metadata:
every product we create carries `sync_key`; a retry first looks for a product with that key.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Gateway call failed or the result could not be verified. Message is safe to show an admin."""


def _interval_params(interval: str) -> dict:
    # Fallback shapes, used only when there is no existing product of this plan to clone from.
    if interval == "yearly":
        return dict(payment_frequency_count=1, payment_frequency_interval="Year",
                    subscription_period_count=10, subscription_period_interval="Year")
    return dict(payment_frequency_count=1, payment_frequency_interval="Month",
                subscription_period_count=10, subscription_period_interval="Year")


class DodoGateway:
    def __init__(self, client):
        self.client = client

    # -- reads ---------------------------------------------------------------------------------------
    def read_product(self, product_id: str) -> dict:
        try:
            p = self.client.products.retrieve(product_id)
            return {"price_minor": int(p.price.price), "currency": str(p.price.currency)}
        except Exception as e:
            raise GatewayError(f"Could not read Dodo product {product_id}: {e}")

    def _find_by_sync_key(self, sync_key: str) -> Optional[str]:
        try:
            for item in self.client.products.list(page_size=100):
                if (getattr(item, "metadata", None) or {}).get("sync_key") == sync_key:
                    return item.product_id
        except Exception as e:
            raise GatewayError(f"Could not look up existing Dodo products: {e}")
        return None

    # -- find-or-create + verify ------------------------------------------------------------------------
    def ensure_product(self, sync_key: str, spec: dict) -> str:
        product_id = self._find_by_sync_key(sync_key)
        if not product_id:
            product_id = self._create(sync_key, spec)
        got = self.read_product(product_id)
        if got["price_minor"] != spec["price_minor"] or got["currency"].upper() != spec["currency"].upper():
            raise GatewayError(
                f"Dodo product {product_id} has price {got['price_minor']} {got['currency']}, "
                f"expected {spec['price_minor']} {spec['currency']}."
            )
        return product_id

    def _create(self, sync_key: str, spec: dict) -> str:
        template = None
        tmpl_id = spec.get("template_product_id")
        if tmpl_id:
            try:
                template = self.client.products.retrieve(tmpl_id)
            except Exception as e:
                raise GatewayError(f"Could not read the current Dodo product {tmpl_id} to copy its settings: {e}")

        if template is not None:
            price = template.price.model_dump(exclude_none=True)      # keep intervals, tax mode, etc.
            tax_category = str(template.tax_category)
        else:
            price = {"purchasing_power_parity": False, "discount": 0}
            if spec["interval"] == "one_time":
                price["type"] = "one_time_price"
            else:
                price["type"] = "recurring_price"
                price.update(_interval_params(spec["interval"]))
            tax_category = spec.get("tax_category") or "saas"
        price["price"] = int(spec["price_minor"])
        price["currency"] = spec["currency"].upper()

        metadata = {str(k): str(v) for k, v in (spec.get("metadata") or {}).items()}
        metadata["sync_key"] = sync_key
        try:
            created = self.client.products.create(
                name=spec["name"],
                description=spec.get("description") or None,
                price=price,
                tax_category=tax_category,
                metadata=metadata,
            )
        except Exception as e:
            raise GatewayError(f"Dodo rejected the new product: {e}")
        logger.info("Created Dodo product %s for %s", created.product_id, sync_key)
        return created.product_id


def get_gateway() -> DodoGateway:
    """Real gateway built on the payments router's Dodo client (imported lazily to avoid an import cycle)."""
    from routers import payments_router
    return DodoGateway(payments_router.client)
