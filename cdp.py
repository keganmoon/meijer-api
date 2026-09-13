"""Minimal CDP client for driving the Brave instance on :9444 (no automation flags)."""
import asyncio, json, urllib.request, itertools, sys
import websockets

import os
CDP = f"http://127.0.0.1:{os.environ.get('MEIJER_CDP_PORT', '9444')}"

def pages():
    return [t for t in json.load(urllib.request.urlopen(f"{CDP}/json/list")) if t["type"] == "page"]

class Tab:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ids = itertools.count(1)
        self.events = []

    async def __aenter__(self):
        self.ws = await websockets.connect(self.ws_url, max_size=None)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    async def send(self, method, **params):
        mid = next(self.ids)
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self.events.append(msg)

    async def drain(self, timeout=0.5):
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
                self.events.append(msg)
        except asyncio.TimeoutError:
            pass

    async def js(self, expr, await_promise=True):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
        return r["result"].get("value")

    async def goto(self, url, wait=4):
        await self.send("Page.navigate", url=url)
        await asyncio.sleep(wait)

    async def screenshot(self, path="/tmp/cdp_shot.png"):
        import base64
        r = await self.send("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))
        return path


def run(coro):
    return asyncio.run(coro)


async def main_tab():
    return Tab(pages()[0]["webSocketDebuggerUrl"])
