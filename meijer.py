"""
meijer.py — unofficial Meijer.com client (search + cart) built from captured web traffic.

Auth model: Meijer uses Okta (id.meijer.com) + Akamai Bot Manager. We do NOT try to
script the login. Instead a real Brave window (separate profile, CDP on :9444) is used to
log in once; cookies are harvested from it and replayed with curl_cffi (Chrome TLS
impersonation, required — plain requests/curl get Akamai 403).

Endpoints (all relative to https://www.meijer.com unless noted):
  GET    /bin/meijer/cart/userstate                    -> {isUserSignedIn, cart{...}}
  GET    /bin/meijer/getcart?calculateForLC=false      -> cart
  POST   /bin/meijer/cart/add?upc=<upc>&quantity=<n>   -> {entry, actualQuantityAdded, ...}
  PUT    /bin/meijer/cart/update?entryNumber=<n>&quantity=<q>
  DELETE /bin/meijer/cart/delete?entryNumber=<n>       -> {"Status":"Success"}
  POST   https://digital.meijer.com/graphql/  (header x-meijer-gql-query: search) -> product search
"""
import argparse, json, os, sys, time, re, math, asyncio, subprocess, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
COOKIE_FILE = HERE / ".cookies.json"
QUERY_FILE = HERE / "search_query.graphql"
BASE = "https://www.meijer.com"
GQL = "https://digital.meijer.com/graphql/"
CDP = "http://127.0.0.1:9444"
PROFILE_DIR = Path.home() / ".local/share/meijer-automation-profile"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


class AuthError(RuntimeError):
    pass


# ---------------------------------------------------------------- session / login
def _browser_running():
    try:
        urllib.request.urlopen(f"{CDP}/json/version", timeout=2)
        return True
    except Exception:
        return False


def launch_browser(url=f"{BASE}/"):
    if _browser_running():
        return
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        ["brave-browser", "--remote-debugging-port=9444", f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if _browser_running():
            return
        time.sleep(1)
    raise RuntimeError("Brave did not come up on :9444")


def harvest_cookies():
    """Pull cookies out of the running Brave (:9444) and persist them."""
    sys.path.insert(0, str(HERE))
    from cdp import Tab, pages

    async def go():
        async with Tab(pages()[0]["webSocketDebuggerUrl"]) as t:
            r = await t.send("Network.getCookies", urls=[BASE, "https://digital.meijer.com"])
            return r["cookies"]
    cookies = asyncio.run(go())
    COOKIE_FILE.write_text(json.dumps(cookies))
    COOKIE_FILE.chmod(0o600)
    return cookies


# ---------------------------------------------------------------- transport
# Akamai Bot Manager scores the session; replayed cookies get 403'd after a few POSTs.
# Doing the fetch() *inside* the logged-in browser tab keeps the sensor cookie fresh, so
# that's the primary transport when Brave (:9444) is up. curl_cffi replay is the fallback.
class BrowserSession:
    def __init__(self):
        sys.path.insert(0, str(HERE))
        from cdp import Tab, pages
        self._Tab, self._pages = Tab, pages
        self.cookies = {c["name"]: c["value"] for c in json.loads(COOKIE_FILE.read_text())} if COOKIE_FILE.exists() else {}

    def _page(self):
        ps = [p for p in self._pages() if "meijer.com" in p["url"]]
        if not ps:
            raise AuthError("no meijer.com tab in Brave; run `meijer.py login`")
        return ps[0]["webSocketDebuggerUrl"]

    def _fetch(self, method, url, params=None, headers=None, json_body=None):
        import urllib.parse
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        same_origin = url.startswith(BASE) or url.startswith("/")
        opts = {"method": method, "headers": dict(headers or {}), "credentials": "include" if same_origin else "omit"}
        if json_body is not None:
            opts["body"] = json.dumps(json_body)
            opts["headers"]["content-type"] = "application/json"
        expr = f"""fetch({json.dumps(url)}, {json.dumps(opts)}).then(async r => ({{status: r.status, text: await r.text()}}))"""

        async def go():
            async with self._Tab(self._page()) as t:
                return await t.js(expr)
        res = asyncio.run(go())
        return _Resp(res["status"], res["text"])

    def get(self, url, params=None, headers=None, **_):
        return self._fetch("GET", url, params, headers)

    def post(self, url, params=None, headers=None, json=None, **_):
        return self._fetch("POST", url, params, headers, json)

    def put(self, url, params=None, headers=None, **_):
        return self._fetch("PUT", url, params, headers)

    def delete(self, url, params=None, headers=None, **_):
        return self._fetch("DELETE", url, params, headers)

    def nudge(self, reload=False):
        """Reload the tab (re-runs the Akamai sensor) and move the mouse a bit."""
        async def go():
            async with self._Tab(self._page()) as t:
                if reload:
                    await t.send("Page.reload")
                    await asyncio.sleep(8)
                for i in range(8):
                    await t.send("Input.dispatchMouseEvent", type="mouseMoved", x=300 + i * 37, y=280 + i * 13)
                    await asyncio.sleep(0.15)
                await t.send("Input.dispatchMouseEvent", type="mouseWheel", x=500, y=400, deltaX=0, deltaY=120)
                await asyncio.sleep(1.5)
        asyncio.run(go())


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:200]}")


def session(force_http=False):
    if not force_http and _browser_running():
        return BrowserSession()
    from curl_cffi import requests
    if not COOKIE_FILE.exists():
        raise AuthError("no cookies; run `meijer.py login`")
    ck = {c["name"]: c["value"] for c in json.loads(COOKIE_FILE.read_text())
          if c["domain"] in ("www.meijer.com", ".meijer.com", ".www.meijer.com")}
    s = requests.Session(impersonate="chrome131")
    s.cookies.update(ck)
    s.headers.update({"referer": f"{BASE}/shopping/cart.html", "x-requested-with": "XMLHttpRequest",
                      "user-agent": UA})
    return s


def _check(r):
    if r.status_code == 403:
        raise AuthError("403 from Akamai — cookies stale/blocked; run `meijer.py login`")
    if r.status_code == 401:
        raise AuthError("401 — signed out; run `meijer.py login`")
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- cart
def userstate(s):
    return _check(s.get(f"{BASE}/bin/meijer/cart/userstate", params={"timestamp": int(time.time() * 1000)}))


def get_cart(s):
    return _check(s.get(f"{BASE}/bin/meijer/getcart", params={"calculateForLC": "false", "timestamp": int(time.time() * 1000)}))


def cart_entries(cart):
    out = []
    for g in cart.get("cartGroups", []):
        for e in g["entries"]:
            p = e["product"]
            out.append({"entry": e["entryNumber"], "qty": e.get("quantityValue") or e["quantity"], "upc": p["code"], "name": p["name"],
                        "unit": e.get("unit"), "avg_lb_each": float(e.get("avgPoundsEach") or 0) or None,
                        "unit_price": p.get("price", {}).get("formattedValue"),
                        "total": e.get("totalPrice", {}).get("formattedValue"),
                        "stock": p.get("stock", {}).get("stockLevelStatus"),
                        "group": g.get("deliveryMode", {}).get("code")})
    return out


ADD_PACE = 3.0  # seconds between cart writes; Akamai 403s bursts of POSTs


def add_to_cart(s, upc, qty=1):
    qty = int(qty) if float(qty).is_integer() else qty
    for attempt in range(4):
        r = s.post(f"{BASE}/bin/meijer/cart/add", params={"upc": upc, "quantity": qty},
                   headers={"content-type": "application/x-www-form-urlencoded"})
        if r.status_code == 403 and hasattr(s, "nudge") and attempt < 3:
            print(f"   (403 on add {upc}; reloading tab + backoff {6 * (attempt + 1)}s)")
            s.nudge(reload=True); time.sleep(6 * (attempt + 1))
            continue
        return _check(r)


def update_entry(s, entry_number, qty):
    return _check(s.put(f"{BASE}/bin/meijer/cart/update", params={"entryNumber": entry_number, "quantity": qty}))


def delete_entry(s, entry_number):
    return _check(s.delete(f"{BASE}/bin/meijer/cart/delete", params={"entryNumber": entry_number}))


# ---------------------------------------------------------------- search
def search(s, term, store_id, first=20, in_stock_only=True):
    ck = s.cookies if isinstance(s.cookies, dict) else {k: v for k, v in s.cookies.items()}
    cid = ck.get("ConstructorioID_client_id") or "00000000-0000-4000-8000-000000000000"
    payload = {
        "query": QUERY_FILE.read_text(),
        "variables": {
            "args": {"searchType": "SearchTerm", "searchValue": term, "includeThirdParty": False},
            "storeId": int(store_id),
            "context": {"storeId": int(store_id), "constructorSessionId": 1, "userSegments": ["web"]},
            "includeThirdParty": False,
            "userIdentity": {"constructorClientId": cid},
            "sort": {"by": "relevance", "order": "descending"},
            "filters": [], "first": first,
        },
    }
    hs = {"x-meijer-gql-query": "search"}
    if isinstance(s, BrowserSession):
        # cross-origin from the page: browser sets origin/referer itself; digital.meijer.com allows it
        r = s.post(GQL, json=payload, headers=hs)
    else:
        r = s.post(GQL, json=payload, headers={**hs, "origin": BASE, "referer": f"{BASE}/"})
    d = _check(r)
    ps = d["data"]["productSearch"]
    if ps.get("__typename") != "ProductsResult":
        return []
    out = []
    for it in ps["itemConnection"]["items"]:
        if it.get("__typename") != "ProductExtended":
            continue
        ssd = it.get("storeSpecificProductDetails") or {}
        st = ssd.get("productStore") or {}
        inv = ssd.get("productStoreInventory") or {}
        row = {
            "upc": it["upc"], "name": it["productName"], "price": st.get("customerPrice"),
            "base_price": st.get("basePrice"), "on_sale": st.get("isOnSale"), "price_text": st.get("priceText"),
            "unit": it.get("soldByUnitDescription"), "by_weight": it.get("isPriceByWeight"),
            "sold_by": it.get("soldByUnit"), "pricing_unit": st.get("pricingUnit"),
            "avg_lb_each": st.get("avgPricingUnitsPerSoldByUnit") if st.get("pricingUnit") == "LB" else None,
            "price_per_unit": st.get("customerPricePerSoldByUnit"), "qty_step": it.get("unitOfMeasureQuantity") or 1,
            "stock": inv.get("stockStatus"), "aisle": inv.get("ilcPrimary"),
            "sponsored": it.get("isSponsored"), "pickup": it.get("isCurbsideEligible"),
            "meijer_brand": bool(re.match(r"^(Meijer|Fresh from Meijer|True Goodness)", it["productName"], re.I)),
            "bought_before": it["upc"] in (_BUY_AGAIN or ()),
        }
        if in_stock_only and row["stock"] and row["stock"].lower() not in ("instock", "in_stock", "in stock"):
            continue
        out.append(row)
    return out


_BUY_AGAIN = None


def buy_again_upcs(s):
    """UPCs the account has purchased before (Meijer's 'Buy Again' list).
    GET /bin/meijer/personalizedproducts/upclist?listType=1 -> {"upclist":[...]}"""
    global _BUY_AGAIN
    if _BUY_AGAIN is None:
        try:
            _BUY_AGAIN = set(_check(s.get(f"{BASE}/bin/meijer/personalizedproducts/upclist", params={"listType": 1})).get("upclist", []))
        except Exception:
            _BUY_AGAIN = set()
    return _BUY_AGAIN


def pick(results, term, prefer="relevance", bought=frozenset(), unit=None):
    """Best match: name must contain every query word (loose), non-sponsored, in Meijer's
    relevance order. Anything in the Buy Again list outranks everything else.
    prefer='cheapest'/'meijer' re-rank only within those matches."""
    words = [w for w in re.findall(r"[a-z0-9]+", term.lower()) if w not in ("of", "the", "a", "and", "pack")]

    def matches(r):
        n = r["name"].lower()
        return all(w in n or w.rstrip("s") in n or w + "s" in n for w in words)

    cands = [r for r in results if matches(r)] or results[:1]
    ns = [r for r in cands if not r["sponsored"]]
    cands = ns or cands
    if not cands:
        return None
    if unit == "lb":
        # asked by weight: prefer true by-the-pound items (deli/bulk), then per-each-priced-by-lb produce
        wt = ([r for r in cands if r.get("sold_by") == "LB"]
              or [r for r in results if r.get("sold_by") == "LB" and not r["sponsored"]]  # e.g. "deli turkey" -> counter item w/o "deli" in name
              or [r for r in cands if r.get("avg_lb_each")])
        cands = wt or cands
    ba = [r for r in cands if r["upc"] in bought]
    if ba:
        cands = ba
    if prefer == "meijer":
        mb = [r for r in cands if r["meijer_brand"]]
        cands = mb or cands
    if prefer in ("cheapest", "meijer"):
        priced = [r for r in cands if r["price"] is not None]
        if priced:
            return min(priced, key=lambda r: r["price"])
    return cands[0]


# ---------------------------------------------------------------- CLI
def parse_list(text):
    """Lines like '2 x bananas', 'bananas x2', '3 milk', '1.5 lb ground beef', '4 apples', 'eggs'.
    Returns [(qty, unit, term)] where unit is None | 'lb' | 'ea'."""
    items = []
    num = r"(\d+(?:\.\d+)?)"
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line or line.startswith("#"):
            continue
        unit = None
        m = re.match(rf"^{num}\s*(lbs?|pounds?|ea|each)?\s*[xX×]?\s+(.+)$", line, re.I) or re.match(rf"^(.+?)\s*[xX×]\s*{num}\s*(lbs?|pounds?|ea|each)?$", line, re.I)
        if m:
            g = m.groups()
            if re.match(num, g[0]):
                qty, u, term = float(g[0]), g[1], g[2]
            else:
                term, qty, u = g[0], float(g[1]), g[2]
            if u:
                unit = "lb" if u.lower().startswith(("lb", "pound")) else "ea"
        else:
            qty, term = 1, line
        items.append((qty, unit, term.strip()))
    return items


def describe_qty(r, qty, unit=None):
    """Turn a list quantity into (api_qty, human description) using the product's unit model.
    Models seen at Meijer:
      EA priced by LB  (apples, squash, bananas-each): qty = count; est weight = qty*avg_lb_each
      LB sold by LB    (deli meat/cheese): qty is POUNDS, step 0.25
      BAG/PKG/BUNCH    : qty = count of packages
    unit: None | 'lb' | 'ea' from the list line."""
    sold, lb_each = r.get("sold_by"), r.get("avg_lb_each")
    step = float(r.get("qty_step") or 1)
    if sold == "LB":
        pounds = float(qty)
        api = round(pounds / step) * step if step else pounds
        api = max(api, step)
        return api, f"{api:g} lb @ ${r.get('price') or 0:.2f}/lb ≈ ${api * (r.get('price') or 0):.2f}"
    if unit == "lb" and lb_each:
        count = max(1, round(float(qty) / lb_each))
        return count, f"{count} ea (≈{count * lb_each:.1f} lb, you asked {qty:g} lb) ≈ ${count * (r.get('price_per_unit') or 0):.2f}"
    if lb_each and sold == "EA":
        return int(qty), f"{int(qty)} ea (≈{int(qty) * lb_each:.1f} lb, ~${(r.get('price_per_unit') or 0):.2f} each)"
    n = max(1, math.ceil(float(qty)))
    if unit == "lb":
        return n, f"{n} x {r.get('unit') or 'unit'}  ⚠ asked for lb but item is sold per {sold or 'unit'} — check size"
    return n, f"{n} x {r.get('unit') or sold or 'ea'}"


def fmt_price(r):
    """priceText is what the site displays (e.g. '$13.29' for a by-weight family pack whose
    customerPrice is the per-lb $2.69); prefer it."""
    if r.get("price_text"):
        return r["price_text"]
    return f"${r['price']:.2f}" if r.get("price") is not None else "?"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open Brave for manual login, then harvest cookies")
    sub.add_parser("harvest", help="re-harvest cookies from already-running Brave")
    sub.add_parser("status")
    sub.add_parser("cart")
    p = sub.add_parser("search"); p.add_argument("term"); p.add_argument("-n", type=int, default=10); p.add_argument("--all", action="store_true")
    p = sub.add_parser("add"); p.add_argument("upc"); p.add_argument("qty", type=float, nargs="?", default=1, help="count, or pounds for LB-sold items")
    p = sub.add_parser("update"); p.add_argument("entry", type=int); p.add_argument("qty", type=int)
    p = sub.add_parser("remove"); p.add_argument("entry", type=int)
    p = sub.add_parser("plan", help="resolve a shopping list file to candidates (no changes)")
    p.add_argument("file"); p.add_argument("--prefer", choices=["relevance", "cheapest", "meijer"], default="relevance")
    p = sub.add_parser("order", help="resolve a list and add best matches to the cart")
    p.add_argument("file"); p.add_argument("--prefer", choices=["relevance", "cheapest", "meijer"], default="relevance")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    a = ap.parse_args()

    if a.cmd == "login":
        launch_browser(f"{BASE}/login.html?state=https://www.meijer.com/")
        print("Brave opened on :9444. Log in to Meijer in that window, then press Enter here...")
        input()
        harvest_cookies()
        st = userstate(session())
        print("signed in:", st["isUserSignedIn"], "| store:", st["cart"]["pointOfService"]["displayName"], st["cart"]["pointOfService"]["name"])
        return
    if a.cmd == "harvest":
        harvest_cookies(); print("cookies saved"); return

    s = session()
    if a.cmd == "status":
        st = userstate(s); pos = st["cart"]["pointOfService"]
        print(json.dumps({"signedIn": st["isUserSignedIn"], "store": pos["displayName"], "storeId": pos["name"],
                          "mode": st["cart"]["deliveryMode"]["code"], "items": st["cart"]["totalItems"]}, indent=1))
        return
    if a.cmd == "cart":
        c = get_cart(s)
        for e in cart_entries(c):
            flag = "" if e["stock"] == "inStock" else f"  [{e['stock']}]"
            if e["unit"] == "LB":
                q = f"{e['qty']:g} lb"
            elif e["avg_lb_each"]:
                q = f"{e['qty']:g} ea ≈{e['qty'] * e['avg_lb_each']:.1f} lb"
            else:
                q = f"{e['qty']:g} x"
            print(f"#{e['entry']:<3} {q:>14}  {e['name']}  ({e['upc']})  {e['total']}{flag}")
        print("total:", c.get("totalPriceWithTax", {}).get("formattedValue"), "| items:", c.get("totalItems"))
        return
    st = userstate(s)
    if not st["isUserSignedIn"]:
        raise AuthError("not signed in; run `meijer.py login`")
    store_id = st["cart"]["pointOfService"]["name"]
    bought = buy_again_upcs(s)
    if a.cmd == "search":
        for r in search(s, a.term, store_id, first=a.n, in_stock_only=not a.all):
            tag = (" [sponsored]" if r["sponsored"] else "") + (" ★bought before" if r["bought_before"] else "")
            sale = " SALE" if r["on_sale"] else ""
            print(f"{r['upc']:>14}  {fmt_price(r):>7}{sale}  {r['name']}  ({r['unit'] or ''}; {r['stock']}){tag}")
        return
    if a.cmd == "add":
        d = add_to_cart(s, a.upc, a.qty)
        print("added:", d["entry"]["product"]["name"], "qty", d["entry"]["quantity"], "entry", d["entry"]["entryNumber"])
        return
    if a.cmd == "update":
        update_entry(s, a.entry, a.qty); print("ok"); return
    if a.cmd == "remove":
        print(delete_entry(s, a.entry)); return
    if a.cmd in ("plan", "order"):
        items = parse_list(Path(a.file).read_text())
        plan = []
        for qty, unit, term in items:
            res = search(s, term, store_id, first=15)
            best = pick(res, term, a.prefer, bought, unit)
            api_qty, desc = describe_qty(best, qty, unit) if best else (None, "")
            plan.append((api_qty, term, best))
            label = f"{qty:g}{(' ' + unit) if unit else ''} {term}"
            if best:
                tag = "  ★bought before" if best["bought_before"] else ""
                print(f"{label:<30} -> {best['name']}  {fmt_price(best)}{tag}  ({best['upc']})\n{'':32}{desc}")
            else:
                print(f"{label:<30} -> NO MATCH")
        if a.cmd == "plan":
            return
        if not a.yes:
            if input("\nAdd these to cart? [y/N] ").strip().lower() != "y":
                return
        for qty, term, best in plan:
            if not best:
                continue
            d = add_to_cart(s, best["upc"], qty)
            print("added:", d["entry"]["product"]["name"], "qty", d["entry"]["quantity"])
            time.sleep(ADD_PACE)
        c = get_cart(s)
        print("cart now:", c.get("totalItems"), "items,", c.get("totalPriceWithTax", {}).get("formattedValue"))


if __name__ == "__main__":
    main()
