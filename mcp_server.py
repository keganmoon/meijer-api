#!/usr/bin/env python3
"""
MCP server exposing the Meijer client (search + cart) as tools.

Run (stdio):
    python3 mcp_server.py

Register with an MCP host, e.g. Hermes / Claude Desktop:
    {"mcpServers": {"meijer": {"command": "/path/to/.venv/bin/python",
                               "args": ["/path/to/mcp_server.py"]}}}

Checkout is deliberately not exposed: the agent fills the cart, the human places the order.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meijer  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

mcp = _Server(
    "meijer",
    instructions=(
        "Unofficial Meijer.com search + cart tools. Never place or submit an order — fill the "
        "cart and tell the user to finish checkout at https://www.meijer.com/shopping/cart.html. "
        "Quantity semantics: produce like apples is sold per PIECE even though the site shows "
        "a $/lb rate ('Approx. 0.75 lb' is the weight of ONE piece); deli/bulk items sold_by=LB "
        "take pounds (fractional ok). Always show the user the plan before adding."
    ),
)


def _session():
    return meijer.session()


def _store_id(s):
    st = meijer.userstate(s)
    if not st["isUserSignedIn"]:
        raise meijer.AuthError("Not signed in. Run `meijer.py login` in a terminal, sign in by hand in the Brave window, then retry.")
    return st["cart"]["pointOfService"]["name"]


@mcp.tool()
def meijer_status() -> dict:
    """Login state, selected store, fulfillment mode, and cart item count."""
    st = meijer.userstate(_session())
    pos = st["cart"]["pointOfService"]
    return {"signed_in": st["isUserSignedIn"], "store": pos.get("displayName"), "store_id": pos.get("name"),
            "mode": st["cart"].get("deliveryMode", {}).get("code"), "items": st["cart"].get("totalItems")}


@mcp.tool()
def meijer_search(term: str, limit: int = 10, include_out_of_stock: bool = False) -> dict:
    """Search products at the user's store. Returns upc, name, price_text (displayed price),
    sold_by (EA/LB/BAG/PKG...), avg_lb_each (for per-piece produce priced by weight),
    on_sale, stock, sponsored, bought_before (in the account's Buy Again history)."""
    s = _session()
    sid = _store_id(s)
    meijer.buy_again_upcs(s)
    rows = meijer.search(s, term, sid, first=limit, in_stock_only=not include_out_of_stock)
    keep = ("upc", "name", "price_text", "price", "on_sale", "sold_by", "unit", "avg_lb_each",
            "stock", "sponsored", "bought_before", "meijer_brand")
    return {"results": [{k: r.get(k) for k in keep} for r in rows]}


@mcp.tool()
def meijer_cart() -> dict:
    """Current cart: entries (entry number, qty, upc, name, unit, total, stock) and totals."""
    s = _session()
    c = meijer.get_cart(s)
    return {"entries": meijer.cart_entries(c), "subtotal": (c.get("subTotal") or {}).get("formattedValue"),
            "pickup_fee": (c.get("deliveryCost") or {}).get("formattedValue"),
            "total": c.get("totalPriceWithTax", {}).get("formattedValue"), "items": c.get("totalItems"),
            "summary": meijer.cart_summary(c)}


@mcp.tool()
def meijer_add(upc: str, quantity: float = 1) -> dict:
    """Add a product by UPC. quantity is a count for EA/BAG/PKG items, or POUNDS for
    sold_by=LB items (deli/bulk; 0.25 steps)."""
    d = meijer.add_to_cart(_session(), upc, quantity)
    e = d["entry"]
    return {"name": e["product"]["name"], "quantity": (e.get("quantityValue") if e.get("unit") == "LB" else None) or e["quantity"],
            "entry": e["entryNumber"], "line_total": e.get("totalPrice", {}).get("formattedValue")}


@mcp.tool()
def meijer_update(entry: int, quantity: float) -> dict:
    """Change the quantity of a cart entry (entry number from meijer_cart)."""
    meijer.update_entry(_session(), entry, quantity)
    return {"ok": True}


@mcp.tool()
def meijer_remove(entry: int) -> dict:
    """Remove a cart entry (entry number from meijer_cart)."""
    return meijer.delete_entry(_session(), entry)


@mcp.tool()
def meijer_plan(shopping_list: str, prefer: str = "relevance") -> dict:
    """Resolve a free-text shopping list (one item per line: '4 apples', '0.5 lb deli turkey',
    'eggs x2') to concrete products WITHOUT changing the cart. Returns per line: the chosen
    product, api_quantity to pass to meijer_add, and a human 'meaning' string explaining the
    quantity (e.g. '4 ea (≈3.0 lb)'). Buy Again items are preferred. prefer: relevance|cheapest|meijer.
    Show this to the user for approval before calling meijer_add."""
    s = _session()
    sid = _store_id(s)
    bought = meijer.buy_again_upcs(s)
    out = []
    for qty, unit, term in meijer.parse_list(shopping_list):
        res = meijer.search(s, term, sid, first=15)
        best = meijer.pick(res, term, prefer, bought, unit)
        if not best:
            out.append({"line": term, "requested": qty, "unit": unit, "match": None})
            continue
        api_qty, desc = meijer.describe_qty(best, qty, unit)
        out.append({"line": term, "requested": qty, "unit": unit,
                    "match": {"upc": best["upc"], "name": best["name"], "price_text": meijer.fmt_price(best),
                              "bought_before": best["bought_before"], "sold_by": best.get("sold_by")},
                    "api_quantity": api_qty, "meaning": desc,
                    "alternatives": [{"upc": r["upc"], "name": r["name"], "price_text": meijer.fmt_price(r),
                                      "bought_before": r["bought_before"]} for r in res[:4] if r["upc"] != best["upc"]]})
    return {"lines": out}


@mcp.tool()
def meijer_add_plan(plan_json: str) -> dict:
    """Add every resolved line from a meijer_plan result (pass the JSON back verbatim, after the
    user approved it). Skips lines with no match. Paced to avoid bot-detection 403s."""
    import time
    s = _session()
    plan = json.loads(plan_json)
    if isinstance(plan, dict):
        plan = plan.get("lines", [])
    added, skipped = [], []
    for row in plan:
        m = row.get("match")
        if not m:
            skipped.append(row.get("line"))
            continue
        d = meijer.add_to_cart(s, m["upc"], row["api_quantity"])
        e = d["entry"]
        added.append({"name": e["product"]["name"], "quantity": (e.get("quantityValue") if e.get("unit") == "LB" else None) or e["quantity"], "entry": e["entryNumber"]})
        time.sleep(meijer.ADD_PACE)
    c = meijer.get_cart(s)
    return {"added": added, "skipped": skipped, "cart_items": c.get("totalItems"),
            "cart_total": c.get("totalPriceWithTax", {}).get("formattedValue"), "cart_summary": meijer.cart_summary(c),
            "checkout_url": "https://www.meijer.com/shopping/cart.html"}


if __name__ == "__main__":
    mcp.run()
