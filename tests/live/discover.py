"""Runtime discovery helpers for live regression (#31). Plain module (not a
conftest) so both fixtures and tests can import it without double plugin
registration. Never logs real paths — callers use hash IDs."""
import hashlib
import json
import os
import time
import urllib.request


def hid(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:8]


def census(root: str) -> tuple[int, int]:
    n = 0
    total = 0
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.startswith("."):
                continue
            try:
                total += os.path.getsize(os.path.join(dp, fn))
                n += 1
            except OSError:
                pass
    return n, total


def get_tree(base: str, deadline_s: float = 600.0):
    """Poll /api/folders until non-empty: a cold server serves stale-empty
    while the first full scan rebuilds in the background."""
    deadline = time.time() + deadline_s
    while True:
        with urllib.request.urlopen(base + "/api/folders", timeout=300) as r:
            assert r.status == 200
            tree = json.load(r)
        if tree:
            return tree
        assert time.time() < deadline, "folders stayed empty"
        time.sleep(5)


def walk_leaves(nodes, prefix=""):
    out = []
    for node in nodes or []:
        path = f"{prefix}/{node['name']}" if prefix else node["name"]
        children = node.get("children") or []
        if node.get("type") == "folder" and children and all(
            c.get("type") == "image" for c in children
        ):
            out.append({"path": path, "images": [c["name"] for c in children]})
        elif node.get("type") == "folder":
            out.extend(walk_leaves(children, path))
    return out
