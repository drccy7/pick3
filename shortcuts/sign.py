import json, sys, urllib.request
for src, title in (("pick3", "Pick3"),):
    xml = open(f"{src}_patched.plist", encoding="utf-8").read()
    req = urllib.request.Request("https://hubsign.routinehub.services/sign",
        data=json.dumps({"shortcutName": title, "shortcut": xml}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "cherri/2.3.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            print(title, r.status, r.headers.get("Content-Type"), len(body), body[:4])
            if body[:4] == b"AEA1":
                open(f"{title}.shortcut", "wb").write(body)
    except urllib.error.HTTPError as e:
        print(title, e.code, e.read()[:300])
