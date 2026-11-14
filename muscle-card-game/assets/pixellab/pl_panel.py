import json, sys, time, urllib.request, base64
from PIL import Image
API = "https://api.pixellab.ai/v2"
H = {"Content-Type": "application/json", "Authorization": "Bearer dummy"}
def call(method, path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, headers=H, method=method)
    try: return json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e: print("HTTP", e.code, e.read()[:1000].decode()); sys.exit(1)
cfg = json.load(open(sys.argv[1])); out = sys.argv[2]
W, Hh = cfg["w"], cfg["h"]; k = 512 / max(W, Hh)
pieces = [dict(id=p["id"], kind="rounded_rect", label=p["label"], x=p["x"]*k, y=p["y"]*k, w=p["w"]*k, h=p["h"]*k, radius=p.get("r", 0)*k) for p in cfg["pieces"]]
body = {"description": cfg["desc"], "image_size": {"width": W, "height": Hh}, "pieces": pieces, "no_background": True, "name": cfg.get("name")}
if cfg.get("palette"): body["target_palette"] = {"colors": cfg["palette"]}
r = call("POST", "/create-ui-asset", body); print({k2: v for k2, v in r.items()})
aid = r["ui_asset_id"]
for i in range(80):
    time.sleep(6)
    a = call("GET", f"/ui-assets/{aid}")
    if a.get("status") in ("completed", "failed"): break
print("status", a.get("status"))
url = a.get("image_url")
if not url: print(json.dumps(a)[:1000]); sys.exit(1)
if url.startswith("data:"):
    open(out, "wb").write(base64.b64decode(url.split(",", 1)[1]))
else:
    urllib.request.urlretrieve(url, out)
print("saved", out, Image.open(out).size)
