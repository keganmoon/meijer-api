# meijer-api

An unofficial Python client for Meijer.com — product search and cart manipulation —
reconstructed from captured web traffic.

> **Unofficial and unaffiliated.** Not endorsed by Meijer. The endpoints are internal
> and undocumented; they can change or break without notice. For personal use.

## The interesting problem

Meijer fronts its site with **Okta** (`id.meijer.com`) for auth and **Akamai Bot
Manager** for bot detection. Plain `requests` or `curl` gets a 403 before it ever
reaches the application — Akamai fingerprints the TLS handshake, not just headers, so
no amount of User-Agent spoofing helps.

This client sidesteps the whole fight rather than trying to win it:

1. **Log in once, as a human**, in a real Brave window running on a dedicated profile
   with CDP exposed on `:9444`. No automation flags, so nothing looks scripted.
2. **Harvest the cookies** out of that live browser over the DevTools Protocol.
3. **Replay them with `curl_cffi`**, which impersonates Chrome's actual TLS
   fingerprint. Akamai sees a browser because, at the transport layer, it is one.

Login is never scripted. That is deliberate: it is the part that is both hardest to
automate and most likely to trip fraud detection.

## Endpoints

All relative to `https://www.meijer.com`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/bin/meijer/cart/userstate` | `{isUserSignedIn, cart{...}}` |
| GET | `/bin/meijer/getcart?calculateForLC=false` | Full cart |
| POST | `/bin/meijer/cart/add?upc=<upc>&quantity=<n>` | Add item |
| PUT | `/bin/meijer/cart/update?entryNumber=<n>&quantity=<q>` | Change quantity |
| DELETE | `/bin/meijer/cart/delete?entryNumber=<n>` | Remove item |
| POST | `https://digital.meijer.com/graphql/` | Product search (header `x-meijer-gql-query: search`) |

## Files

| File | Role |
|---|---|
| `meijer.py` | The client: search, cart ops, cookie harvesting |
| `cdp.py` | Minimal Chrome DevTools Protocol client over websockets |
| `sniff.py` | Traffic capture used to reverse the endpoints |
| `search_query.graphql` | The GraphQL search document |

## Requirements

```bash
pip install curl_cffi websockets
```

Plus Brave (or any Chromium) on `PATH`. `curl_cffi` is **not optional** — it provides
the Chrome TLS impersonation the whole approach depends on.

## Usage

```bash
# One-time: opens Brave on a dedicated profile, log in by hand
python3 meijer.py login

# Then
python3 meijer.py search "oat milk"
python3 meijer.py cart
python3 meijer.py add <upc> --quantity 2
python3 meijer.py remove <entryNumber>
```

Cookies are cached in `.cookies.json` beside the script and refreshed from the
browser when they expire. The browser profile lives at
`~/.local/share/meijer-automation-profile`, separate from your daily browser.

## Notes and limits

- **Session expiry** means re-running `login`. There is no way around it without
  scripting Okta, which is out of scope by design.
- Cookies are account credentials in practice. `.cookies.json` is gitignored; keep it
  that way.
- Rate-limit yourself. This talks to production infrastructure that belongs to
  someone else.
- Checkout is intentionally **not** implemented. Search and cart only.

## License

MIT — see [LICENSE](LICENSE).
