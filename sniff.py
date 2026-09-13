import asyncio, json, sys, re
from cdp import *

SKIP = (".js", ".css", ".png", ".svg", ".woff", ".jpg", ".gif", "/content/dam", "/etc.clientlibs", "demdex", "criteo", "doubleclick", "google", "bigid", "adobe", "omtrdc", "medallia", "bing", "facebook", "tiktok", "pinterest", "quantserve", "launchdarkly", "snapchat", "spapi.io", "go-mpulse", "data:", "blob:", "cookielaw", "onetrust", "bazaarvoice", "qualtrics", "clarity.ms", "akstat", "salesforce", "evgnet", "attn.tv", "flashtalking", "yahoo", "reddit", "outbrain", "rlcdn", "liadm", "id5", "pubmatic", "nextdoor")

def interesting(u):
    return not any(x in u for x in SKIP)

async def m():
    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/capture.json"
    async with await main_tab() as t:
        await t.send("Network.enable")
        await t.send("Page.enable")
        await t.goto(url, wait=10)
        await t.drain(1)
        reqs = {}
        for e in t.events:
            if e.get("method") == "Network.requestWillBeSent":
                r = e["params"]["request"]
                if interesting(r["url"]):
                    reqs[e["params"]["requestId"]] = {"method": r["method"], "url": r["url"], "headers": r.get("headers"), "post": r.get("postData")}
            elif e.get("method") == "Network.responseReceived":
                rid = e["params"]["requestId"]
                if rid in reqs:
                    reqs[rid]["status"] = e["params"]["response"]["status"]
                    reqs[rid]["mime"] = e["params"]["response"]["mimeType"]
        for rid, r in reqs.items():
            if r.get("mime", "").startswith("application/json") or "graphql" in r["url"]:
                try:
                    b = await t.send("Network.getResponseBody", requestId=rid)
                    r["body"] = b["body"][:20000]
                except Exception as ex:
                    r["body_err"] = str(ex)
        json.dump(list(reqs.values()), open(out, "w"), indent=1)
        for r in reqs.values():
            print(r.get("status"), r["method"], r["url"][:160], len(r.get("body", "")))
            if r.get("post"): print("   POST", r["post"][:300])
        print("TITLE:", await t.js("document.title"))
        await t.screenshot("/tmp/cdp_shot.png")
asyncio.run(m())
