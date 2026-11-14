import json, sys, time, urllib.request, base64, io, zipfile, threading
from PIL import Image
API = "https://api.pixellab.ai/v2"
H = {"Content-Type": "application/json", "Authorization": "Bearer " + __import__("os").environ.get("PIXELLAB_API_KEY", "")}
def call(method, path, body=None, raw=False):
    for t in range(3):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body else None, headers=H, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=180).read()
            return r if raw else json.loads(r)
        except urllib.error.HTTPError as e:
            msg = e.read()[:600].decode()
            if e.code >= 500 and t < 2: time.sleep(5); continue
            raise RuntimeError(f"HTTP {e.code} {msg}")
PAL = ["#1A1613","#2B2622","#3A332D","#5A5048","#FFF8EF","#E2D5C3","#A3968A","#D8CABB"]
ACC = {"training":["#8A2D18","#C9492F","#E8603F"],"mental":["#1F4166","#2F5D8A","#4F86C0"],"method":["#4A3470","#6A4C93","#8A6BB8"]}
COL = {"training":"red","mental":"blue","method":"purple"}
def frame_cfg(kind):
    p = [dict(id="card",label=f"card frame with {COL[kind]} metallic border",x=0,y=0,w=200,h=280,r=10)]
    if kind == "training":
        p += [dict(id="art",label="empty illustration window",x=10,y=30,w=180,h=90,r=2),
              dict(id="text",label="empty cream text box",x=10,y=132,w=180,h=60,r=4),
              dict(id="weight",label="empty dark inset panel",x=10,y=226,w=180,h=44,r=4)]
        for i in range(6):
            p.append(dict(id=f"cell{i}",label="empty small square slot",x=11+i*30,y=199,w=28,h=20,r=3))
        for i in range(10):
            p.append(dict(id=f"seg{i}",label="empty gauge segment",x=13+i*17.6,y=256,w=15,h=6,r=1))
        extra = "A row of six small empty square slots, and a gauge of ten small empty segments inside the bottom panel."
    else:
        p += [dict(id="art",label="empty illustration window",x=10,y=30,w=180,h=110,r=2),
              dict(id="text",label="empty cream text box",x=10,y=152,w=180,h=116,r=4)]
        extra = ""
    desc = (f"pixel art trading card frame for a gym bodybuilding card game. Dark charcoal body, {COL[kind]} metallic beveled border "
            f"with small metal rivets at the corners. Each piece is an empty recessed area: illustration window is plain dark, "
            f"text box is plain cream paper. {extra} No text, no icons, flat empty areas.")
    return dict(desc=desc, pieces=p, palette=PAL+ACC[kind])
def frame(kind):
    c = frame_cfg(kind); k = 512/280
    pieces = [dict(id=q["id"],kind="rounded_rect",label=q["label"],x=q["x"]*k,y=q["y"]*k,w=q["w"]*k,h=q["h"]*k,radius=q["r"]*k) for q in c["pieces"]]
    r = call("POST","/create-ui-asset",{"description":c["desc"],"image_size":{"width":200,"height":280},"pieces":pieces,"no_background":True,"name":f"card_frame_{kind}","target_palette":{"colors":c["palette"]}})
    aid = r["ui_asset_id"]
    for i in range(100):
        time.sleep(6)
        a = call("GET",f"/ui-assets/{aid}")
        if a.get("status") in ("completed","failed"): break
    if a.get("status") != "completed": raise RuntimeError(str(a)[:500])
    z = zipfile.ZipFile(io.BytesIO(call("GET",f"/ui-assets/{aid}/zip",raw=True)))
    png = [n for n in z.namelist() if n.endswith(".png")][0]
    open(f"gen/frame_{kind}.png","wb").write(z.read(png))
    json.dump(c, open(f"gen/frame_{kind}.json","w"), ensure_ascii=False, indent=1)
def sprite(name, desc, w, h, nobg=True):
    body = {"description":desc,"image_size":{"width":w,"height":h},"no_background":nobg,"outline":"single color black outline","shading":"basic shading","detail":"medium detail"}
    r = call("POST","/create-image-pixflux",body)
    b = r["image"]["base64"]
    if "," in b[:40]: b = b.split(",",1)[1]
    open(f"gen/{name}.png","wb").write(base64.b64decode(b))
jobs = [lambda k=k: frame(k) for k in ["training","mental","method"]]
S = [("cost_orb","round golden coin token, plain empty center, no symbols, game UI icon",32,32),
     ("icon_barbell","black barbell with weight plates, simple game icon, side view",32,32),
     ("icon_dumbbell","single black dumbbell, simple game icon",32,32),
     ("icon_cable","gym cable pulley machine handle with cable, simple game icon",32,32),
     ("icon_machine","gym weight stack machine, simple game icon",32,32),
     ("icon_bodyweight","person doing a push-up silhouette, simple game icon",32,32),
     ("gem_common","small grey diamond shaped gem, game icon",32,32),
     ("gem_uncommon","small blue diamond shaped gem, game icon",32,32),
     ("gem_rare","small gold diamond shaped gem, game icon",32,32),
     ("art_bench_press","muscular bodybuilder lying on a bench doing barbell bench press in a gym, side view",176,88),
     ("art_deep_breath","calm muscular man in gym clothes taking a deep breath with eyes closed, meditating in a gym",176,108),
     ("art_whey_protein","protein shaker bottle and a big tub of whey protein powder on a gym table",176,108)]
jobs += [lambda s=s: sprite(*s) for s in S]
names = ["frame_training","frame_mental","frame_method"] + [s[0] for s in S]
res = {}
def run(n, f):
    try: f(); res[n] = "ok"
    except Exception as e: res[n] = f"ERR {e}"
ts = []
for n, f in zip(names, jobs):
    t = threading.Thread(target=run, args=(n, f)); t.start(); ts.append(t); time.sleep(1)
for t in ts: t.join()
for n in names: print(n, res.get(n))
print(call("GET","/balance"))
