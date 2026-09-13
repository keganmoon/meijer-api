# meijer-api

An unofficial Python client + MCP server for Meijer.com — product search and cart
manipulation — reconstructed from captured web traffic. Give an agent a shopping list;
it fills your pickup cart; you press *Begin Checkout*.

> **Unofficial and unaffiliated.** Not endorsed by Meijer. The endpoints are internal
> and undocumented; they can change or break without notice. For personal use.

## Install

```bash
git clone https://github.com/keganmoon/meijer-api && cd meijer-api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # only if you have no Chrome/Brave/Chromium
.venv/bin/python meijer.py login                   # a window opens — sign in by hand once
```

That's the whole setup. `login` opens a browser on a private profile
(`~/.local/share/meijer-automation-profile`), you sign in like a human, and the tool
keeps using that window. If you already have Chrome, Brave, Chromium or Edge on `PATH`
it uses that; otherwise it downloads Playwright's Chromium (~150 MB, one time). Override
with `MEIJER_BROWSER=/path/to/chrome`.

## The interesting problem

Meijer fronts its site with **Okta** (`id.meijer.com`) for auth and **Akamai Bot
Manager** for bot detection. Plain `requests`/`curl` get a 403 before reaching the app:
Akamai fingerprints the TLS handshake *and* mints its `_abck` cookie from a JavaScript
sensor (mouse, timing, canvas) that only a real browser can run. Scripting the Okta login
doesn't help — you'd still have no sensor cookie. That's where the previous public
attempt ([dapperfu/python_Meijer](https://github.com/dapperfu/python_Meijer)) died.

So this client doesn't fight it:

1. **Log in once, as a human,** in a real headed Chromium with CDP exposed on `:9444`.
   No automation flags; the profile persists, so it's usually one login per few weeks.
2. **Make the requests from inside that tab** (`fetch()` over the DevTools Protocol).
   The sensor keeps refreshing, cookies stay valid, Akamai sees a browser because it is one.
3. **Fallback:** cookies harvested from the tab, replayed with `curl_cffi` Chrome TLS
   impersonation. Fine for reads and search; cart writes get 403'd after a burst, so the
   in-tab path is primary whenever the browser is up.

Bursty writes still trip Akamai occasionally; adds are paced (3 s) and a 403 triggers a
tab reload + backoff, which recovers in practice.

## CLI

```bash
meijer.py status                     # signed in? which store? mode? item count
meijer.py search "oat milk" -n 8     # store-specific price, stock, ★ = in your Buy Again history
meijer.py cart
meijer.py add <upc> [qty]            # qty = count, or POUNDS for deli/bulk (0.5 ok)
meijer.py update <entry> <qty>
meijer.py remove <entry>
meijer.py plan list.txt              # resolve a list → products, no changes
meijer.py order list.txt [--yes]     # resolve + add; --prefer relevance|cheapest|meijer
```

`list.txt` is one item per line: `2 bananas`, `eggs x2`, `3 lb honeycrisp apples`,
`0.5 lb deli turkey`, `whole milk gallon`. `plan` shows what each line resolved to and
what the quantity *means*:

```
4 honeycrisp apples            -> Honeycrisp Apple  $1.12  (3283)
                                4 ea (≈3.0 lb, ~$1.12 each)
0.5 lb deli turkey             -> Dietz & Watson Oven Roasted Turkey Breast  $10.49  (21842800000)
                                0.5 lb @ $10.49/lb ≈ $5.25
```

### Quantity gotcha

Meijer produce like apples shows **"$1.49 / lb · Approx. 0.75 lb"** but is sold **per
piece** — the quantity is a count, and 0.75 lb is the weight of *one* apple. Deli and bulk
items (`sold_by: LB`) are the opposite: quantity is pounds. The client reads the product's
unit model and reports both; read the second line of `plan` before trusting a number.

### Matching

Search results are filtered to names containing every word of your line, sponsored
results are dropped, and anything in your account's **Buy Again** history wins. Then
Meijer's relevance order (or `--prefer cheapest` / `--prefer meijer` brand).

## MCP server

`mcp_server.py` exposes the same operations as tools (stdio transport, works with
`mcp` 1.x and 2.x):

| Tool | Purpose |
|---|---|
| `meijer_status` | login state, store, item count |
| `meijer_search(term, limit)` | products with price / stock / `sold_by` / `bought_before` |
| `meijer_cart()` | entries + subtotal, pickup fee, total |
| `meijer_add(upc, quantity)` / `meijer_update` / `meijer_remove` | cart writes |
| `meijer_plan(shopping_list)` | resolve a list, no changes — show this to the user |
| `meijer_add_plan(plan_json)` | add an approved plan |

Checkout is **not** exposed, on purpose. The server's instructions tell the model to
stop at a filled cart and hand the user `https://www.meijer.com/shopping/cart.html`.

Register it (Claude Desktop / Hermes / any MCP host):

```json
{"mcpServers": {"meijer": {"command": "/path/to/meijer-api/.venv/bin/python",
                           "args": ["/path/to/meijer-api/mcp_server.py"]}}}
```

## Endpoints

All relative to `https://www.meijer.com`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/bin/meijer/cart/userstate` | `{isUserSignedIn, cart{pointOfService...}}` |
| GET | `/bin/meijer/getcart?calculateForLC=true` | Full cart with recomputed totals |
| POST | `/bin/meijer/cart/add?upc=<upc>&quantity=<n>` | Add (n may be fractional for LB items) |
| PUT | `/bin/meijer/cart/update?entryNumber=<n>&quantity=<q>` | Change quantity |
| DELETE | `/bin/meijer/cart/delete?entryNumber=<n>` | Remove |
| GET | `/bin/meijer/personalizedproducts/upclist?listType=1` | Buy Again UPCs |
| POST | `https://digital.meijer.com/graphql/` | Search (`x-meijer-gql-query: search`; `userIdentity.constructorClientId` required) |

## Files

| File | Role |
|---|---|
| `meijer.py` | Client + CLI: browser launch, cookie harvest, search, cart, list resolution |
| `mcp_server.py` | MCP server over the client |
| `cdp.py` | Minimal Chrome DevTools Protocol client (websockets) |
| `sniff.py` | Traffic capture used to reverse the endpoints |
| `search_query.graphql` | The GraphQL search document |

## Notes and limits

- Cookies are account credentials in practice. `.cookies.json` and the browser profile
  are gitignored / outside the repo; keep them that way.
- The browser window must stay open for writes. If it's closed, reads still work via the
  cookie fallback until Akamai rotates; then `meijer.py login` again.
- Store is whatever the account has selected; the pickup fee ($4.95 under $35) shows up
  in totals even on an empty cart.
- Rate-limit yourself. This talks to production infrastructure that belongs to someone else.

## License

MIT — see [LICENSE](LICENSE).
