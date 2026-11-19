#!/usr/bin/env python3
"""rs-vs-py-perf driver — serial per env.

Usage: perf_compare.py <base-url> [container-to-restart]

Discovers a large image folder and an archive from /api/v1/tree itself, then
measures cold first-hits, warm API p50s, sequential /image reads, and a
16-parallel wall-clock probe. Live folder/archive names are never printed —
only counts, sizes, and timings (tests/live rule).
"""
import concurrent.futures
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
RESTART = sys.argv[2] if len(sys.argv) > 2 else None
ARCHIVE_EXT = ("cbz", "zip", "epub", "cbr", "rar")


def get(path, timeout=180):
    t = time.perf_counter()
    try:
        r = urllib.request.urlopen(BASE + path, timeout=timeout)
        body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code  # 304 counts as a valid answer
    return (time.perf_counter() - t) * 1000, len(body), status


def jget(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=180).read())


def run(name, path, n=3):
    ts, size, st = [], 0, 0
    for _ in range(n):
        ms, size, st = get(path)
        ts.append(ms)
    med = statistics.median(ts)
    print(f"  {name:30s} p50={med:8.1f}ms min={min(ts):8.1f} max={max(ts):8.1f} n={n} bytes={size} [{st}]")
    return med


def wait_tree_200():
    t0 = time.time()
    while True:
        ms, size, st = get("/api/v1/tree")
        if st == 200:
            return time.time() - t0
        if st != 202:
            print(f"  tree status {st} — aborting")
            sys.exit(1)
        time.sleep(3)


def discover():
    """Walk the v1 tree: biggest direct-image folder + first archive path."""
    tree = jget("/api/v1/tree")
    best, arch = ("", 0), ""
    def walk(n, prefix):
        nonlocal best, arch
        p = (prefix + "/" + n["name"]).strip("/")
        nm = n["name"].lower()
        if nm.rsplit(".", 1)[-1] in ARCHIVE_EXT and not arch:
            arch = p
        imgs = sum(1 for c in (n.get("children") or []) if c.get("type") == "image")
        if imgs > best[1]:
            best = (p, imgs)
        for c in n.get("children") or []:
            walk(c, p)
    for node in tree["nodes"]:
        walk(node, "")
    return tree, best[0], best[1], arch


def healthy():
    try:
        urllib.request.urlopen(BASE + "/api/v1/health", timeout=5)
        return True
    except Exception:
        return False


if RESTART:
    subprocess.run(["docker", "restart", RESTART], check=True, capture_output=True)
    for _ in range(180):
        if healthy():
            break
        time.sleep(1)
    else:
        sys.exit("env never became healthy")

print("== index wait (202 → 200) ==")
elapsed = wait_tree_200()
print(f"  index build                    {elapsed:8.1f}s")

tree, img_folder, n_imgs, archive = discover()
gen = tree.get("generation")
print(f"  targets: dir({n_imgs} imgs) archive={'yes' if archive else 'none'} gen={gen}")

q = urllib.parse.quote
out = {}
print("== warm API ==")
out["tree"] = run("v1/tree", "/api/v1/tree")
out["folders_d1"] = run("v0/folders?depth=1", "/api/folders?depth=1")
out["since"] = run("v1/tree?since=<gen>", f"/api/v1/tree?since={gen}", n=5)
out["imglist_dir"] = run("v1/images?folder=<bigdir>", "/api/v1/images?folder=" + q(img_folder))
if archive:
    out["imglist_arch"] = run("v1/images?folder=<arch>", "/api/v1/images?folder=" + q(archive))

print("== viewer reads (sequential) ==")
imgs = jget("/api/v1/images?folder=" + q(img_folder))["images"][:15]
ts = [get("/image/" + q(img_folder) + "/" + q(i))[0] for i in imgs]
print(f"  /image loose {len(imgs)} seq          p50={statistics.median(ts):8.1f}ms first={ts[0]:8.1f}")
out["img_seq"] = statistics.median(ts)

if archive:
    ents = jget("/api/v1/images?folder=" + q(archive))["images"][:10]
    ts = [get("/image/" + q(archive) + "/" + q(e))[0] for e in ents]
    print(f"  /image arch {len(ents)} seq           p50={statistics.median(ts):8.1f}ms first={ts[0]:8.1f}")
    out["arch_seq"] = statistics.median(ts)

print("== 16-parallel /image ==")
names = jget("/api/v1/images?folder=" + q(img_folder))["images"][:16]
def one(n):
    t = time.perf_counter()
    get("/image/" + q(img_folder) + "/" + q(n))
    return (time.perf_counter() - t) * 1000
for w in names:
    get("/image/" + q(img_folder) + "/" + q(w))  # warm each path
t = time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(16) as ex:
    pts = list(ex.map(one, names))
wall = (time.perf_counter() - t) * 1000
print(f"  wall={wall:8.1f}ms max-req={max(pts):8.1f}ms")
out["par16_wall"] = wall

print(json.dumps({"env": BASE, **out}))
