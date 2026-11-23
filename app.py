#!/usr/bin/env python3
"""
JellyClean - pregled i čišćenje prostora na Jellyfin poslužitelju.

Bez vanjskih ovisnosti (samo Python 3.9+ standardna biblioteka).
Sva konfiguracija ide preko varijabli okruženja (vidi README.md).
"""
import base64
import hashlib
import heapq
import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "1.0.4"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# --------------------------------------------------------------------------
# Konfiguracija
# --------------------------------------------------------------------------


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _parse_labeled(value):
    """'Filmovi=/media/movies, Serije=/media/tv'  ->  [(label, path)]"""
    out = []
    for part in re.split(r"[,;\n]", value):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            label, path = part.split("=", 1)
        else:
            path = part
            label = os.path.basename(part.rstrip("/")) or part
        out.append((label.strip(), os.path.realpath(path.strip())))
    return out


def _parse_map(value):
    """'/media=/data/media; /mnt/tv=/data/tv' -> [(jellyfin_prefix, local_prefix)]"""
    out = []
    for part in re.split(r"[;\n]", value):
        part = part.strip()
        if "=" in part:
            a, b = part.split("=", 1)
            out.append((a.strip().rstrip("/"), b.strip().rstrip("/")))
    out.sort(key=lambda x: -len(x[0]))
    return out


CFG = {
    "port": int(_env("PORT", "8095")),
    "bind": _env("BIND", "0.0.0.0"),
    "media": _parse_labeled(_env("MEDIA_PATHS", "/media")),
    "jf_dirs": _parse_labeled(_env("JELLYFIN_DIRS", "")),
    "jf_url": _env("JELLYFIN_URL").rstrip("/"),
    "jf_key": _env("JELLYFIN_API_KEY"),
    "jf_users": [u.strip().lower() for u in _env("JELLYFIN_USERS").split(",") if u.strip()],
    "path_map": _parse_map(_env("PATH_MAP")),
    "allow_delete": _env("ALLOW_DELETE", "true").lower() in ("1", "true", "yes", "da"),
    "trash": _env("TRASH_MODE", "true").lower() in ("1", "true", "yes", "da"),
    "trash_days": int(_env("TRASH_DAYS", "7")),
    "trash_name": ".jellyclean-trash",
    "scan_hours": float(_env("SCAN_INTERVAL_HOURS", "6")),
    "auth_user": _env("AUTH_USER"),
    "auth_pass": _env("AUTH_PASS"),
    "data_dir": _env("DATA_DIR", os.path.join(BASE_DIR, "data")),
    "verify_tls": _env("JELLYFIN_VERIFY_TLS", "true").lower() in ("1", "true", "yes", "da"),
    # Jellyfin config mapa na drugom računalu (VM), pristup preko SSH-a
    "ssh_host": _env("JF_SSH_HOST"),
    "ssh_user": _env("JF_SSH_USER", "root"),
    "ssh_port": _env("JF_SSH_PORT", "22"),
    "ssh_key": _env("JF_SSH_KEY"),
    "ssh_sudo": _env("JF_SSH_SUDO", "false").lower() in ("1", "true", "yes", "da"),
    "ssh_helper": _env("JF_SSH_HELPER"),
    "jf_config": _env("JF_CONFIG_PATH").rstrip("/"),
    "jf_cache": _env("JF_CACHE_PATH").rstrip("/"),
    "jf_log": _env("JF_LOG_PATH").rstrip("/"),
    "jf_data": _env("JF_DATA_PATH").rstrip("/"),
    "jf_metadata": _env("JF_METADATA_PATH").rstrip("/"),
}
if CFG["jf_config"]:
    CFG["jf_cache"] = CFG["jf_cache"] or CFG["jf_config"] + "/cache"
    CFG["jf_log"] = CFG["jf_log"] or CFG["jf_config"] + "/log"
    CFG["jf_data"] = CFG["jf_data"] or CFG["jf_config"] + "/data"
if CFG["jf_data"]:
    CFG["jf_metadata"] = CFG["jf_metadata"] or CFG["jf_data"] + "/metadata"
if not CFG["ssh_key"]:
    CFG["ssh_key"] = os.path.join(CFG["data_dir"], "ssh", "id_ed25519")

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg",
             ".mpeg", ".webm", ".flv", ".iso", ".vob", ".divx", ".3gp", ".ogm"}
PARTIAL_EXT = {".part", ".!qb", ".!ut", ".tmp", ".crdownload", ".partial", ".download", ".bc!"}
JUNK_EXT = {".exe", ".url", ".lnk", ".bat", ".scr", ".com", ".cmd", ".vbs", ".lnk"}
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", "rarbg.txt", "rarbg_do_not_mirror.exe",
              "www.yts.mx.jpg", "www.yts.am.jpg", "yts.txt"}
SAMPLE_RE = re.compile(r"(^|[\W_])sample([\W_]|$)", re.I)
EPISODE_RE = re.compile(r"[Ss](\d{1,3})[ ._-]*[Ee](\d{1,4})")
MULTIPART_RE = re.compile(r"(cd|part|disc|disk|pt)[ ._-]?\d", re.I)
RAR_RE = re.compile(r"\.(rar|r\d\d|zip|7z)$", re.I)

# --------------------------------------------------------------------------
# Pomoćne funkcije
# --------------------------------------------------------------------------


def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_within(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def media_root_for(path):
    """Vrati (label, root) medijske mape u kojoj je path ili None."""
    rp = os.path.realpath(path)
    best = None
    for label, root in CFG["media"]:
        if is_within(rp, root) and (best is None or len(root) > len(best[1])):
            best = (label, root)
    return best


def map_jf_path(p):
    if not p:
        return p
    for jf, local in CFG["path_map"]:
        if p == jf or p.startswith(jf + "/"):
            return local + p[len(jf):]
    return p


def file_size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def tree_size(path):
    total = 0
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------
# Skener diska
# --------------------------------------------------------------------------


class Node:
    __slots__ = ("name", "size", "files", "children", "mtime")

    def __init__(self, name):
        self.name = name
        self.size = 0
        self.files = 0
        self.children = None  # dict ime -> Node (samo za mape)
        self.mtime = 0


class ScanState:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.progress = {"files": 0, "current": ""}
        self.started = None
        self.finished = None
        self.duration = 0
        self.error = None
        self.roots = {}        # root path -> dict(label, kind, node, ...)
        self.files = []        # [path, size, mtime, nlink, ext, root]
        self.junk = []
        self.empty_dirs = []
        self.duplicates = []
        self.ext_stats = {}
        self.disks = []


SCAN = ScanState()


def _scan_dir(path, node, ctx, depth=0):
    try:
        it = os.scandir(path)
    except OSError as e:
        ctx["errors"].append(f"{path}: {e.strerror}")
        return
    with it:
        entries = list(it)
    node.children = {}
    video_files = []
    for e in entries:
        full = e.path
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                if depth == 0 and e.name == CFG["trash_name"]:
                    continue
                child = Node(e.name)
                _scan_dir(full, child, ctx, depth + 1)
                node.children[e.name] = child
                node.size += child.size
                node.files += child.files
                node.mtime = max(node.mtime, child.mtime)
                if child.files == 0 and not child.children and ctx["kind"] == "media":
                    ctx["empty"].append(full)
                continue
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError as ex:
            ctx["errors"].append(f"{full}: {ex.strerror}")
            continue
        size = st.st_size
        ext = os.path.splitext(e.name)[1].lower()
        node.size += size
        node.files += 1
        node.mtime = max(node.mtime, int(st.st_mtime))
        # Za ukupnu zauzetost diska hardlinkove brojimo samo jednom
        ino_key = (st.st_dev, st.st_ino)
        if st.st_nlink > 1:
            if ino_key in ctx["inodes"]:
                ctx["dup_inode_bytes"] += size
            else:
                ctx["inodes"].add(ino_key)
            ctx["hardlinked_bytes"] += size
        SCAN.progress["files"] += 1
        if SCAN.progress["files"] % 2000 == 0:
            SCAN.progress["current"] = path
        if ctx["kind"] != "media":
            continue
        rec = [full, size, int(st.st_mtime), st.st_nlink, ext, ctx["root"]]
        ctx["files"].append(rec)
        es = ctx["ext"].setdefault(ext or "(bez)", [0, 0])
        es[0] += 1
        es[1] += size
        lname = e.name.lower()
        reason = None
        if ext in PARTIAL_EXT:
            reason = "Nedovršeno preuzimanje"
        elif ext in JUNK_EXT or lname in JUNK_NAMES:
            reason = "Smeće iz releasea"
        elif ext in VIDEO_EXT and SAMPLE_RE.search(os.path.splitext(e.name)[0]) and size < 500 * 1024 ** 2:
            reason = "Sample video"
        elif RAR_RE.search(lname):
            reason = "Arhiva (rar/zip) u medijskoj mapi"
        elif size == 0:
            reason = "Prazna datoteka"
        if reason:
            ctx["junk"].append({"path": full, "size": size, "reason": reason, "mtime": int(st.st_mtime)})
        if ext in VIDEO_EXT and size > 50 * 1024 ** 2 and not SAMPLE_RE.search(e.name):
            video_files.append((e.name, full, size))
    # Duplikati u istoj mapi
    if len(video_files) >= 2:
        eps = {}
        plain = []
        for name, full, size in video_files:
            m = EPISODE_RE.search(name)
            if m:
                eps.setdefault((int(m.group(1)), int(m.group(2))), []).append((full, size))
            elif not MULTIPART_RE.search(name):
                plain.append((full, size))
        for key, lst in eps.items():
            if len(lst) > 1:
                ctx["dups"].append({"dir": path, "kind": f"Epizoda S{key[0]:02d}E{key[1]:02d} više puta",
                                    "files": [{"path": f, "size": s} for f, s in sorted(lst, key=lambda x: -x[1])]})
        if len(plain) > 1 and not eps and depth >= 1:
            big = [x for x in plain if x[1] > 500 * 1024 ** 2]
            if len(big) > 1:
                ctx["dups"].append({"dir": path, "kind": "Više verzija filma u istoj mapi",
                                    "files": [{"path": f, "size": s} for f, s in sorted(big, key=lambda x: -x[1])]})


def run_scan():
    with SCAN.lock:
        if SCAN.running:
            return False
        SCAN.running = True
        SCAN.error = None
        SCAN.started = now_iso()
        SCAN.progress = {"files": 0, "current": ""}
    t0 = time.time()
    try:
        roots = {}
        files, junk, empty, dups, ext = [], [], [], [], {}
        disks = {}
        targets = [(l, p, "media") for l, p in CFG["media"]] + [(l, p, "jellyfin") for l, p in CFG["jf_dirs"]]
        for label, root, kind in targets:
            SCAN.progress["current"] = root
            info = {"label": label, "path": root, "kind": kind, "exists": os.path.isdir(root),
                    "errors": [], "trash_size": 0, "hardlinked_bytes": 0, "dup_inode_bytes": 0}
            node = Node(label)
            if info["exists"]:
                ctx = {"kind": kind, "root": root, "files": files, "junk": junk, "empty": empty,
                       "dups": dups, "ext": ext, "errors": info["errors"], "inodes": set(),
                       "hardlinked_bytes": 0, "dup_inode_bytes": 0}
                _scan_dir(root, node, ctx)
                info["hardlinked_bytes"] = ctx["hardlinked_bytes"]
                info["dup_inode_bytes"] = ctx["dup_inode_bytes"]
                tdir = os.path.join(root, CFG["trash_name"])
                if kind == "media" and os.path.isdir(tdir):
                    info["trash_size"] = tree_size(tdir)
                try:
                    st = os.stat(root)
                    du = shutil.disk_usage(root)
                    d = disks.setdefault(st.st_dev, {"total": du.total, "used": du.used, "free": du.free,
                                                     "roots": []})
                    d["roots"].append(label)
                except OSError:
                    pass
                info["errors"] = info["errors"][:200]
            info["node"] = node
            roots[root] = info
        with SCAN.lock:
            SCAN.roots = roots
            SCAN.files = files
            SCAN.junk = sorted(junk, key=lambda x: -x["size"])
            SCAN.empty_dirs = sorted(empty)
            SCAN.duplicates = dups
            SCAN.ext_stats = ext
            SCAN.disks = list(disks.values())
            SCAN.finished = now_iso()
            SCAN.duration = round(time.time() - t0, 1)
        log(f"Skeniranje gotovo: {len(files)} datoteka, {SCAN.duration}s")
    except Exception as e:  # pragma: no cover
        SCAN.error = str(e)
        traceback.print_exc()
    finally:
        SCAN.running = False
    return True


def start_scan_bg():
    if SCAN.running:
        return False
    threading.Thread(target=run_scan, daemon=True).start()
    return True


def find_node(path):
    """Vrati (root_info, node, relativni dijelovi) za path ili None."""
    rp = os.path.realpath(path)
    for root, info in SCAN.roots.items():
        if is_within(rp, root):
            rel = os.path.relpath(rp, root)
            parts = [] if rel == "." else rel.split(os.sep)
            node = info["node"]
            chain = [node]
            for p in parts:
                if not node.children or p not in node.children:
                    return info, None, chain
                node = node.children[p]
                chain.append(node)
            return info, node, chain
    return None, None, []


def forget_path(path, size, nfiles):
    """Ukloni obrisanu putanju iz memorijskog stabla bez ponovnog skeniranja."""
    with SCAN.lock:
        info, node, chain = find_node(path)
        if info is None:
            return
        rel = os.path.relpath(os.path.realpath(path), info["path"])
        parts = rel.split(os.sep)
        if node is not None and len(chain) >= 2:
            parent = chain[-2]
            if parent.children and parts[-1] in parent.children:
                del parent.children[parts[-1]]
            for n in chain[:-1]:
                n.size = max(0, n.size - size)
                n.files = max(0, n.files - nfiles)
        else:
            # Datoteka (nije u stablu kao čvor) - oduzmi od mape roditelja
            info2, pnode, pchain = find_node(os.path.dirname(path))
            for n in pchain:
                n.size = max(0, n.size - size)
                n.files = max(0, n.files - nfiles)
        prefix = os.path.realpath(path)
        pre_slash = prefix.rstrip("/") + "/"
        SCAN.files = [f for f in SCAN.files if f[0] != prefix and not f[0].startswith(pre_slash)]
        SCAN.junk = [j for j in SCAN.junk if j["path"] != prefix and not j["path"].startswith(pre_slash)]
        SCAN.empty_dirs = [d for d in SCAN.empty_dirs if d != prefix and not d.startswith(pre_slash)]
        newd = []
        for d in SCAN.duplicates:
            fl = [f for f in d["files"] if f["path"] != prefix and not f["path"].startswith(pre_slash)]
            if len(fl) > 1:
                newd.append(dict(d, files=fl))
        SCAN.duplicates = newd


# --------------------------------------------------------------------------
# Brisanje i kanta
# --------------------------------------------------------------------------


class DeleteError(Exception):
    pass


def validate_delete_path(path):
    if not CFG["allow_delete"]:
        raise DeleteError("Brisanje je isključeno (ALLOW_DELETE=false)")
    rp = os.path.realpath(path)
    mr = media_root_for(rp)
    if not mr:
        raise DeleteError(f"Putanja nije unutar medijskih mapa: {path}")
    label, root = mr
    if rp == os.path.realpath(root):
        raise DeleteError("Ne mogu obrisati korijen medijske mape")
    trash = os.path.join(root, CFG["trash_name"])
    if is_within(rp, trash):
        raise DeleteError("Za kantu koristi karticu Kanta")
    if not os.path.lexists(rp):
        raise DeleteError(f"Ne postoji: {path}")
    return rp, root


def count_files(path):
    if os.path.isfile(path):
        return 1
    n = 0
    for _, _, fn in os.walk(path):
        n += len(fn)
    return n


def hardlink_bytes(path):
    """Koliko bajtova u putanji ima još neki hardlink (npr. torrent kopija)."""
    total = 0
    paths = [path] if os.path.isfile(path) else (
        os.path.join(dp, f) for dp, _, fn in os.walk(path) for f in fn)
    for p in paths:
        try:
            st = os.lstat(p)
            if st.st_nlink > 1:
                total += st.st_size
        except OSError:
            pass
    return total


def delete_paths(paths, permanent=False, dry_run=False):
    results = []
    use_trash = CFG["trash"] and not permanent
    batch = time.strftime("%Y%m%d-%H%M%S")
    # Ukloni duplikate i podputanje već odabranih mapa
    norm = sorted({os.path.realpath(p) for p in paths}, key=len)
    final = []
    for p in norm:
        if not any(is_within(p, q) for q in final):
            final.append(p)
    for p in final:
        r = {"path": p, "ok": False}
        try:
            rp, root = validate_delete_path(p)
            size = tree_size(rp)
            nfiles = count_files(rp)
            r.update(size=size, files=nfiles, hardlinked=hardlink_bytes(rp))
            if dry_run:
                r["ok"] = True
                results.append(r)
                continue
            if use_trash:
                rel = os.path.relpath(rp, root)
                dest = os.path.join(root, CFG["trash_name"], batch, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(rp, dest)
                r["trash"] = dest
            else:
                if os.path.isdir(rp) and not os.path.islink(rp):
                    shutil.rmtree(rp)
                else:
                    os.remove(rp)
            r["ok"] = True
            forget_path(rp, size, nfiles)
            log(("U kantu: " if use_trash else "Obrisano: ") + rp)
        except DeleteError as e:
            r["error"] = str(e)
        except OSError as e:
            r["error"] = f"{e.strerror or e}"
        results.append(r)
    if not dry_run and any(r["ok"] for r in results):
        with SCAN.lock:
            for info in SCAN.roots.values():
                if info["kind"] == "media":
                    tdir = os.path.join(info["path"], CFG["trash_name"])
                    info["trash_size"] = tree_size(tdir) if os.path.isdir(tdir) else 0
        refresh_disks()
        if JF.enabled():
            threading.Thread(target=JF.library_refresh, daemon=True).start()
    return results


def refresh_disks():
    disks = {}
    for label, root in CFG["media"] + CFG["jf_dirs"]:
        try:
            st = os.stat(root)
            du = shutil.disk_usage(root)
            d = disks.setdefault(st.st_dev, {"total": du.total, "used": du.used, "free": du.free, "roots": []})
            d["roots"].append(label)
        except OSError:
            pass
    with SCAN.lock:
        SCAN.disks = list(disks.values())


def trash_list():
    out = []
    for label, root in CFG["media"]:
        tdir = os.path.join(root, CFG["trash_name"])
        if not os.path.isdir(tdir):
            continue
        for batch in sorted(os.listdir(tdir), reverse=True):
            bdir = os.path.join(tdir, batch)
            if not os.path.isdir(bdir):
                continue
            items = []
            for top in _trash_items(bdir):
                rel = os.path.relpath(top, bdir)
                items.append({"path": top, "original": os.path.join(root, rel), "size": tree_size(top),
                              "exists_original": os.path.lexists(os.path.join(root, rel))})
            try:
                ts = datetime.strptime(batch, "%Y%m%d-%H%M%S")
                age_days = (datetime.now() - ts).total_seconds() / 86400
            except ValueError:
                age_days = None
            out.append({"root": label, "batch": batch, "path": bdir, "items": items,
                        "size": sum(i["size"] for i in items), "age_days": age_days})
    return out


def _trash_items(bdir):
    """Kanta čuva relativnu strukturu; nađi stvarno premještene stavke.
    Mapa koja sadrži točno jednu podmapu i ništa drugo smatra se 'putanjskom' mapom."""
    items = []
    stack = [bdir]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        if d != bdir and (len(entries) != 1 or not entries[0].is_dir(follow_symlinks=False)):
            items.append(d)
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            else:
                items.append(e.path)
    return items


def trash_restore(path):
    for label, root in CFG["media"]:
        tdir = os.path.join(root, CFG["trash_name"])
        rp = os.path.realpath(path)
        if is_within(rp, tdir) and rp != tdir:
            rel = os.path.relpath(rp, tdir).split(os.sep, 1)
            if len(rel) < 2:
                raise DeleteError("Odaberi stavku, ne cijelu seriju brisanja")
            dest = os.path.join(root, rel[1])
            if os.path.lexists(dest):
                raise DeleteError(f"Na originalnoj lokaciji već postoji: {dest}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(rp, dest)
            _cleanup_empty_parents(os.path.dirname(rp), tdir)
            start_scan_bg()
            if JF.enabled():
                threading.Thread(target=JF.library_refresh, daemon=True).start()
            return dest
    raise DeleteError("Putanja nije u kanti")


def _cleanup_empty_parents(d, stop):
    while is_within(d, stop) and d != stop:
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def trash_empty(path=None, older_than_days=None):
    freed = 0
    for label, root in CFG["media"]:
        tdir = os.path.join(root, CFG["trash_name"])
        if not os.path.isdir(tdir):
            continue
        for batch in os.listdir(tdir):
            bdir = os.path.join(tdir, batch)
            if path and os.path.realpath(path) != os.path.realpath(bdir):
                continue
            if older_than_days is not None:
                try:
                    ts = datetime.strptime(batch, "%Y%m%d-%H%M%S")
                    if (datetime.now() - ts).total_seconds() < older_than_days * 86400:
                        continue
                except ValueError:
                    continue
            freed += tree_size(bdir)
            shutil.rmtree(bdir, ignore_errors=True)
            log("Kanta ispražnjena: " + bdir)
    with SCAN.lock:
        for info in SCAN.roots.values():
            if info["kind"] == "media":
                tdir = os.path.join(info["path"], CFG["trash_name"])
                info["trash_size"] = tree_size(tdir) if os.path.isdir(tdir) else 0
    refresh_disks()
    return freed


# --------------------------------------------------------------------------
# Jellyfin API
# --------------------------------------------------------------------------


class Jellyfin:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}       # id -> item
        self.series = {}      # id -> series agregat
        self.users = []
        self.synced = None
        self.error = None
        self.running = False
        self.server = None

    def enabled(self):
        return bool(CFG["jf_url"] and CFG["jf_key"])

    def req(self, method, path, params=None, body=None, timeout=60):
        url = CFG["jf_url"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f'MediaBrowser Client="JellyClean", Device="JellyClean", '
                                      f'DeviceId="jellyclean", Version="{APP_VERSION}", Token="{CFG["jf_key"]}"')
        r.add_header("Accept", "application/json")
        if data is not None:
            r.add_header("Content-Type", "application/json")
        ctx = None
        if url.startswith("https") and not CFG["verify_tls"]:
            import ssl
            ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(r, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                return None

    def library_refresh(self):
        try:
            self.req("POST", "/Library/Refresh")
            log("Jellyfin: pokrenut refresh biblioteke")
        except Exception as e:
            log("Jellyfin refresh nije uspio:", e)

    def sync(self):
        if not self.enabled():
            return
        with self.lock:
            if self.running:
                return
            self.running = True
        try:
            try:
                info = self.req("GET", "/System/Info")
                self.server = {"name": info.get("ServerName"), "version": info.get("Version")}
            except Exception:
                self.server = None
            users = self.req("GET", "/Users") or []
            if CFG["jf_users"]:
                users = [u for u in users if u.get("Name", "").lower() in CFG["jf_users"]]
            users = [u for u in users if not (u.get("Policy") or {}).get("IsDisabled")]
            items = {}
            series_paths = {}
            fields = "Path,MediaSources,DateCreated,ProductionYear"
            for idx, u in enumerate(users):
                start = 0
                while True:
                    res = self.req("GET", f"/Users/{u['Id']}/Items", {
                        "Recursive": "true", "IncludeItemTypes": "Movie,Episode,Series",
                        "Fields": fields, "EnableUserData": "true", "EnableImages": "false",
                        "StartIndex": start, "Limit": 2000}, timeout=180) or {}
                    batch = res.get("Items", [])
                    for it in batch:
                        iid = it["Id"]
                        t = it.get("Type")
                        if t == "Series":
                            if it.get("Path"):
                                series_paths[iid] = (it.get("Name"), map_jf_path(it["Path"]),
                                                     it.get("ProductionYear"), it.get("DateCreated"))
                            continue
                        ud = it.get("UserData") or {}
                        rec = items.get(iid)
                        if rec is None:
                            path = map_jf_path(it.get("Path") or "")
                            ms = it.get("MediaSources") or []
                            jsize = sum((m.get("Size") or 0) for m in ms[:1])
                            rec = {
                                "id": iid, "type": t, "name": it.get("Name"),
                                "year": it.get("ProductionYear"),
                                "series": it.get("SeriesName"), "series_id": it.get("SeriesId"),
                                "season": it.get("ParentIndexNumber"), "episode": it.get("IndexNumber"),
                                "path": path, "size": jsize, "added": it.get("DateCreated"),
                                "played_by": [], "last_played": None, "play_count": 0,
                                "in_progress_by": [],
                            }
                            items[iid] = rec
                        if ud.get("Played"):
                            rec["played_by"].append(u["Name"])
                        elif (ud.get("PlaybackPositionTicks") or 0) > 0:
                            rec["in_progress_by"].append(u["Name"])
                        rec["play_count"] += ud.get("PlayCount") or 0
                        lp = ud.get("LastPlayedDate")
                        if lp and (rec["last_played"] is None or lp > rec["last_played"]):
                            rec["last_played"] = lp
                    start += len(batch)
                    if not batch or start >= res.get("TotalRecordCount", 0):
                        break
            # Stvarna veličina s diska ako je putanja dostupna
            for rec in items.values():
                p = rec["path"]
                rec["on_disk"] = bool(p) and os.path.exists(p)
                if rec["on_disk"]:
                    rec["size"] = file_size(p)
            # Agregat po serijama
            series = {}
            for rec in items.values():
                if rec["type"] != "Episode":
                    continue
                sid = rec["series_id"] or rec["series"]
                s = series.get(sid)
                if s is None:
                    sp = series_paths.get(sid)
                    s = {"id": sid, "name": rec["series"], "path": sp[1] if sp else None,
                         "year": sp[2] if sp else None, "added": sp[3] if sp else None,
                         "episodes": 0, "size": 0, "watched_all": 0, "watched_any": 0,
                         "watched_all_size": 0, "last_played": None, "seasons": {}, "users": {}}
                    series[sid] = s
                s["episodes"] += 1
                s["size"] += rec["size"] or 0
                if rec["played_by"]:
                    s["watched_any"] += 1
                if len(set(rec["played_by"])) >= len(users) and users:
                    s["watched_all"] += 1
                    s["watched_all_size"] += rec["size"] or 0
                for un in rec["played_by"]:
                    s["users"][un] = s["users"].get(un, 0) + 1
                if rec["last_played"] and (s["last_played"] is None or rec["last_played"] > s["last_played"]):
                    s["last_played"] = rec["last_played"]
                ss = s["seasons"].setdefault(str(rec["season"]), {"episodes": 0, "size": 0, "watched_all": 0})
                ss["episodes"] += 1
                ss["size"] += rec["size"] or 0
                if len(set(rec["played_by"])) >= len(users) and users:
                    ss["watched_all"] += 1
            for s in series.values():
                if not s["path"]:
                    eps = [r["path"] for r in items.values()
                           if r["type"] == "Episode" and (r["series_id"] or r["series"]) == s["id"] and r["path"]]
                    if eps:
                        cp = os.path.commonpath(eps)
                        s["path"] = cp if not os.path.isfile(cp) else os.path.dirname(cp)
            with self.lock:
                self.items = items
                self.series = series
                self.users = [u["Name"] for u in users]
                self.synced = now_iso()
                self.error = None
            log(f"Jellyfin sinkroniziran: {len(items)} stavki, {len(series)} serija, {len(users)} korisnika")
        except urllib.error.HTTPError as e:
            self.error = f"HTTP {e.code}: {e.reason}"
            log("Jellyfin greška:", self.error)
        except Exception as e:
            self.error = str(e)
            log("Jellyfin greška:", e)
        finally:
            self.running = False

    def delete_targets(self, ids, scope):
        """Iz ID-eva napravi popis putanja za brisanje.
        scope: 'item' (film/epizoda), 'series' (cijela serija), 'series_watched' (odgledane epizode)."""
        targets = []
        with self.lock:
            n_users = len(self.users)
            for iid in ids:
                if scope == "series":
                    s = self.series.get(iid)
                    if s and s["path"]:
                        targets.append(s["path"])
                    continue
                if scope == "series_watched":
                    for r in self.items.values():
                        if (r["type"] == "Episode" and (r["series_id"] or r["series"]) == iid
                                and len(set(r["played_by"])) >= n_users and r["path"]):
                            targets.extend(item_file_targets(r["path"], whole_dir=False))
                    continue
                r = self.items.get(iid)
                if not r or not r["path"]:
                    continue
                targets.extend(item_file_targets(r["path"], whole_dir=(r["type"] == "Movie")))
        return targets

    def scheduled_tasks(self):
        tasks = self.req("GET", "/ScheduledTasks", {"isHidden": "false"}) or []
        out = []
        for t in tasks:
            last = t.get("LastExecutionResult") or {}
            out.append({"id": t.get("Id"), "name": t.get("Name"), "key": t.get("Key"),
                        "category": t.get("Category"), "state": t.get("State"),
                        "description": t.get("Description"),
                        "progress": t.get("CurrentProgressPercentage"),
                        "last_end": last.get("EndTimeUtc"), "last_status": last.get("Status")})
        return out


JF = Jellyfin()


def item_file_targets(path, whole_dir):
    """Za film: ako je sam u svojoj mapi, briše se cijela mapa.
    Inače briše se video + prateće datoteke istog imena (.nfo, .srt, -thumb.jpg...)."""
    if not path or not os.path.exists(path):
        return []
    d = os.path.dirname(path)
    mr = media_root_for(d)
    stem = os.path.splitext(os.path.basename(path))[0]
    if whole_dir and mr and os.path.realpath(d) != os.path.realpath(mr[1]):
        try:
            other_videos = [f for f in os.listdir(d)
                            if os.path.splitext(f)[1].lower() in VIDEO_EXT and f != os.path.basename(path)
                            and not SAMPLE_RE.search(f) and not f.startswith(stem)]
        except OSError:
            other_videos = []
        if not other_videos:
            return [d]
    out = []
    try:
        for f in os.listdir(d):
            full = os.path.join(d, f)
            if f == os.path.basename(path) or (f.startswith(stem) and os.path.isfile(full)
                                               and os.path.splitext(f)[1].lower() not in VIDEO_EXT
                                               and (len(f) == len(stem) or f[len(stem)] in ".-_ [")):
                out.append(full)
    except OSError:
        out.append(path)
    # trickplay mapa Jellyfina 10.9+
    tp = os.path.join(d, stem + ".trickplay")
    if os.path.isdir(tp):
        out.append(tp)
    return out


# --------------------------------------------------------------------------
# Jellyfin config mapa preko SSH-a (cache, slike, logovi, metadata)
# --------------------------------------------------------------------------

import shlex
import subprocess

HEX32 = re.compile(r"^[0-9a-f]{32}$")


HELPER_BODY = r'''
set -u
export LC_ALL=C
sz() { if [ -e "$1" ]; then du -sb "$1" 2>/dev/null | tail -1 | cut -f1; else echo -1; fi; }
cnt() { if [ -e "$1" ]; then find "$1" -type f 2>/dev/null | wc -l; else echo 0; fi; }
okid() { echo "$1" | grep -Eq '^[0-9a-f]{2}/[0-9a-f]{32}$' && [ "${1%%/*}" = "$(echo "${1#*/}" | cut -c1-2)" ]; }

op_status() {
  [ -d "$C" ] || { echo "ERR Mapa $C ne postoji na $(hostname)"; return; }
  [ -r "$C" ] || { echo "ERR Nema prava čitanja $C"; return; }
  echo "HOST $(hostname)"
  echo "LOC config $(sz "$C") 0"
  echo "LOC cache $(sz "$CACHE") $(cnt "$CACHE")"
  echo "LOC images $(sz "$CACHE/images") $(cnt "$CACHE/images")"
  echo "LOC transcodes $(sz "$CACHE/transcodes") $(cnt "$CACHE/transcodes")"
  echo "LOC log $(sz "$LOG") $(cnt "$LOG")"
  echo "LOC metadata $(sz "$META") 0"
  echo "LOC metalib $(sz "$META/library") 0"
  echo "LOC people $(sz "$META/People") 0"
  echo "LOC data $(sz "$DATA") 0"
  for d in "$CACHE"/*/ "$META"/*/; do
    [ -d "$d" ] || continue
    case "$d" in "$CACHE"/*) k=cache;; *) k=metadata;; esac
    echo "SUB $k $(sz "$d") $(basename "$d")"
  done
  for f in "$DATA"/*.db "$DATA"/data/*.db; do [ -f "$f" ] && echo "DB $(sz "$f") $(basename "$f")"; done
  if [ -d "$LOG" ]; then
    for n in 7 14 30; do
      s=$(find "$LOG" -type f -mtime +$n -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
      echo "LOGAGE $n $s"
    done
    o=$(find "$LOG" -type f -printf '%T@\n' 2>/dev/null | sort -n | head -1 | cut -d. -f1); echo "LOGOLD ${o:-0}"
  fi
  df -B1 -P "$C" 2>/dev/null | tail -1 | awk '{print "DF", $2, $3, $4}'
  w=1; for d in "$CACHE" "$LOG" "$META/library"; do [ -d "$d" ] && [ ! -w "$d" ] && w=0; done
  echo "WRITE $w"
}

op_clean() {
  a=${1:-}; d=${2:-0}; dry=${3:-1}
  case "$d" in ''|*[!0-9]*) echo "ERR neispravan broj dana"; return;; esac
  age=""; [ "$d" -gt 0 ] && age="-mtime +$((d - 1))"
  case "$a" in
    images)      R="$CACHE/images";     set -- "$R" -mindepth 1 -type f ;;
    transcodes)  R="$CACHE/transcodes"; set -- "$R" -mindepth 1 -type f -mmin +60; age="" ;;
    cache_other) R="$CACHE";            set -- "$R" -mindepth 1 -type f -not -path "$CACHE/images/*" -not -path "$CACHE/transcodes/*" ;;
    logs)        R="$LOG";              [ -n "$age" ] || age="-mtime +0"; set -- "$R" -mindepth 1 -type f ;;
    *) echo "ERR nepoznata akcija"; return ;;
  esac
  case "$R" in ""|/) echo "ERR prazna putanja"; return;; esac
  [ -d "$R" ] || { echo "RES 0 0"; return; }
  # shellcheck disable=SC2086
  find "$@" $age -printf '%s\n' 2>/dev/null | awk '{s+=$1;n++} END{print "RES", n+0, s+0}'
  if [ "$dry" = 0 ]; then
    find "$@" $age -delete 2>/dev/null
    find "$R" -mindepth 1 -type d -empty -delete 2>/dev/null
    echo DONE
  fi
}

op_listmeta() {
  [ -d "$META/library" ] || { echo "ERR Nema mape $META/library"; return; }
  find "$META/library" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | sed "s#^$META/library/##"
}

op_dumeta() { while read -r i; do okid "$i" && [ -d "$META/library/$i" ] && du -sb "$META/library/$i" 2>/dev/null; done; }

op_delmeta() {
  n=0
  while read -r i; do
    okid "$i" || continue
    if [ -d "$META/library/$i" ]; then rm -rf -- "$META/library/$i" && n=$((n + 1)); fi
  done
  rmdir "$META"/library/?? 2>/dev/null
  echo "DEL $n"
}

main() {
  op=${1:-}; [ $# -gt 0 ] && shift
  case "$op" in
    status) op_status ;;
    clean) op_clean "$@" ;;
    listmeta) op_listmeta ;;
    dumeta) op_dumeta ;;
    delmeta) op_delmeta ;;
    *) echo "ERR nepoznata operacija '$op'"; exit 2 ;;
  esac
}
'''


def helper_script(standalone=True):
    """Shell skripta s operacijama nad Jellyfin config mapom.
    standalone=True: datoteka za /usr/local/sbin/jellyclean-helper (putanje zapečene, poziva main "$@")."""
    q = shlex.quote
    head = ("#!/bin/sh\n# JellyClean helper - generirano. Dopušta SAMO mjerenje i čišćenje Jellyfin\n"
            "# cachea, logova i siročadi u metadata/library unutar ovih putanja:\n"
            f"C={q(CFG['jf_config'])}\nCACHE={q(CFG['jf_cache'])}\nLOG={q(CFG['jf_log'])}\n"
            f"DATA={q(CFG['jf_data'])}\nMETA={q(CFG['jf_metadata'])}\n")
    return head + HELPER_BODY + ('main "$@"\n' if standalone else "")


class SSHError(Exception):
    pass


class JFConfig:
    """Mjerenje i čišćenje Jellyfin config mape na drugom računalu preko SSH-a.

    Načini rada:
      • JF_SSH_HELPER postavljen: na udaljenom računalu postoji jellyclean-helper koji se
        pokreće sa 'sudo -n' (ograničeno sudoers pravilo, bez lozinke)
      • inače: helper skripta se šalje na stdin 'sh -s' (ili 'sudo -n sh -s' uz JF_SSH_SUDO)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = None
        self.orphans = None

    def enabled(self):
        return bool(CFG["ssh_host"] and CFG["jf_config"])

    def paths(self):
        return {"config": CFG["jf_config"], "cache": CFG["jf_cache"], "log": CFG["jf_log"],
                "data": CFG["jf_data"], "metadata": CFG["jf_metadata"]}

    def run(self, op, args=(), lines=None, timeout=600):
        if not self.enabled():
            raise SSHError("SSH pristup Jellyfinu nije podešen (JF_SSH_HOST, JF_CONFIG_PATH)")
        os.makedirs(os.path.join(CFG["data_dir"], "ssh"), exist_ok=True)
        kh = os.path.join(CFG["data_dir"], "ssh", "known_hosts")
        cmd = ["ssh", "-p", str(CFG["ssh_port"]), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={kh}",
               "-o", "LogLevel=ERROR"]
        if CFG["ssh_key"] and os.path.exists(CFG["ssh_key"]):
            cmd += ["-i", CFG["ssh_key"], "-o", "IdentitiesOnly=yes"]
        cmd.append(f"{CFG['ssh_user']}@{CFG['ssh_host']}")
        argstr = " ".join(shlex.quote(str(a)) for a in (op, *args))
        body = "\n".join(lines or []) + ("\n" if lines else "")
        if CFG["ssh_helper"]:
            cmd.append(f"sudo -n {shlex.quote(CFG['ssh_helper'])} {argstr}")
            stdin = body
        else:
            cmd.append(("sudo -n " if CFG["ssh_sudo"] else "") + "sh -s")
            stdin = helper_script(standalone=False) + f"\nmain {argstr} <<'JC_EOF'\n{body}JC_EOF\n"
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise SSHError("Naredba 'ssh' nije instalirana (apt install openssh-client)")
        except subprocess.TimeoutExpired:
            raise SSHError("SSH naredba je istekla")
        err = p.stderr.strip()
        if p.returncode == 255:
            raise SSHError("SSH spajanje nije uspjelo: " + (err or "nepoznata greška"))
        if "sudo:" in err and ("password" in err or "lozink" in err or "not allowed" in err):
            raise SSHError("sudo traži lozinku ili nije dopušten - pokreni LXC skriptu ponovno ili postavi "
                           "jellyclean-helper (vidi README): " + err[:200])
        if p.returncode != 0 and not p.stdout:
            raise SSHError((err or f"izlazni kod {p.returncode}")[:500])
        for line in p.stdout.splitlines():
            if line.startswith("ERR "):
                raise SSHError(line[4:])
        return p.stdout

    # ---------------------------------------------------------- pregled ---
    def refresh_status(self):
        st = {"host": None, "locations": {}, "subs": {"cache": [], "metadata": []}, "dbs": [],
              "log_age": {}, "log_oldest": 0, "disk": None, "writable": False, "error": None,
              "time": now_iso(), "paths": self.paths(),
              "mode": "helper" if CFG["ssh_helper"] else ("sudo" if CFG["ssh_sudo"] else "direct")}
        try:
            out = self.run("status")
        except SSHError as e:
            if str(e).startswith("Mapa ") or str(e).startswith("Nema prava"):
                st["error"] = str(e)
                with self.lock:
                    self.status = st
                return st
            raise
        for line in out.splitlines():
            parts = line.split(" ", 3)
            k = parts[0]
            try:
                if k == "HOST":
                    st["host"] = parts[1]
                elif k == "LOC":
                    st["locations"][parts[1]] = {"size": int(parts[2]), "files": int(parts[3])}
                elif k == "SUB":
                    st["subs"].setdefault(parts[1], []).append({"size": int(parts[2]), "name": parts[3]})
                elif k == "DB":
                    st["dbs"].append({"size": int(parts[1]), "name": line.split(" ", 2)[2]})
                elif k == "LOGAGE":
                    st["log_age"][parts[1]] = int(parts[2])
                elif k == "LOGOLD":
                    st["log_oldest"] = int(parts[1])
                elif k == "DF":
                    st["disk"] = {"total": int(parts[1]), "used": int(parts[2]), "free": int(parts[3])}
                elif k == "WRITE":
                    st["writable"] = parts[1] == "1"
            except (ValueError, IndexError):
                pass
        for v in st["subs"].values():
            v.sort(key=lambda x: -x["size"])
        with self.lock:
            self.status = st
        return st

    # ---------------------------------------------------------- čišćenje --
    ACTIONS = {
        "images": "Slike u cacheu (cache/images)",
        "transcodes": "Ostaci transkodiranja (cache/transcodes, starije od 1 h)",
        "cache_other": "Ostatak cachea (bez slika i transkodiranja)",
        "logs": "Logovi",
    }

    def clean(self, action, days=0, dry_run=True):
        if action not in self.ACTIONS:
            raise SSHError("Nepoznata akcija")
        days = max(0, int(days or 0))
        if action == "logs":
            days = max(days, 1)
        out = self.run("clean", (action, days, 1 if dry_run else 0))
        res = {"action": action, "label": self.ACTIONS[action], "days": days, "count": 0, "size": 0,
               "done": False, "dry_run": dry_run}
        for line in out.splitlines():
            p = line.split()
            if p and p[0] == "RES":
                res["count"], res["size"] = int(p[1]), int(p[2])
            elif p and p[0] == "DONE":
                res["done"] = True
        if not dry_run:
            log(f"Jellyfin config: {action} (>{days} d) obrisano {res['count']} datoteka, {res['size']} B")
            threading.Thread(target=self._safe_refresh, daemon=True).start()
        return res

    def _safe_refresh(self):
        try:
            self.refresh_status()
        except Exception as e:
            log("Jellyfin config refresh:", e)

    # ---------------------------------------------------------- siročad ---
    def _jf_all_ids(self):
        ids = set()
        start = 0
        while True:
            res = JF.req("GET", "/Items", {"Recursive": "true", "EnableImages": "false",
                                           "EnableUserData": "false", "EnableTotalRecordCount": "true",
                                           "StartIndex": start, "Limit": 5000}, timeout=300) or {}
            items = res.get("Items", [])
            for it in items:
                ids.add(it["Id"].replace("-", "").lower())
            start += len(items)
            if not items or start >= res.get("TotalRecordCount", 0):
                break
        return ids

    def _item_exists_fn(self, known_id):
        """Vrati funkciju id -> True/False/None koja pojedinačno provjerava stavku u Jellyfinu."""
        def probe(path):
            try:
                JF.req("GET", path, timeout=30)
                return True
            except urllib.error.HTTPError as e:
                if e.code in (404, 400):
                    return False
                return None
        if known_id and probe(f"/Items/{known_id}"):
            return lambda i: probe(f"/Items/{i}")
        users = JF.req("GET", "/Users") or []
        admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)
        if not admin:
            raise SSHError("Ne mogu provjeriti stavke: nema Jellyfin korisnika")
        if known_id and not probe(f"/Users/{admin['Id']}/Items/{known_id}"):
            raise SSHError("Jellyfin ne vraća ni poznatu stavku - provjera siročadi nije sigurna")
        return lambda i: probe(f"/Users/{admin['Id']}/Items/{i}")

    def find_orphans(self):
        """Mape u metadata/library čiji ID više ne postoji u Jellyfinu."""
        if not JF.enabled():
            raise SSHError("Za provjeru siročadi treba Jellyfin API (JELLYFIN_URL, JELLYFIN_API_KEY)")
        dirs = []
        for line in self.run("listmeta").splitlines():
            parts = line.strip().split("/")
            if len(parts) == 2 and HEX32.match(parts[1]) and parts[1][:2] == parts[0]:
                dirs.append(parts[1])
        known = self._jf_all_ids()
        cands = [d for d in dirs if d not in known]
        exists = self._item_exists_fn(next(iter(known), None))
        orphans, unsure = [], 0
        for d in cands:
            r = exists(d)
            if r is False:
                orphans.append(d)
            elif r is None:
                unsure += 1
        sizes = {}
        if orphans:
            for line in self.run("dumeta", lines=[f"{d[:2]}/{d}" for d in orphans]).splitlines():
                p = line.split("\t")
                if len(p) == 2:
                    sizes[os.path.basename(p[1].rstrip("/"))] = int(p[0])
        res = {"time": now_iso(), "total_dirs": len(dirs), "jellyfin_items": len(known),
               "orphans": [{"id": d, "size": sizes.get(d, 0)} for d in orphans],
               "orphan_size": sum(sizes.values()), "unsure": unsure,
               "suspicious": bool(dirs) and len(orphans) > 0.4 * len(dirs)}
        res["orphans"].sort(key=lambda x: -x["size"])
        with self.lock:
            self.orphans = res
        return res

    def delete_orphans(self, ids):
        with self.lock:
            allowed = {o["id"] for o in (self.orphans or {}).get("orphans", [])}
        ids = [i for i in ids if HEX32.match(i) and i in allowed]
        if not ids:
            raise SSHError("Nema potvrđenih siročadi za brisanje (pokreni analizu ponovno)")
        n = 0
        for line in self.run("delmeta", lines=[f"{i[:2]}/{i}" for i in ids]).splitlines():
            if line.startswith("DEL "):
                n = int(line.split()[1])
        with self.lock:
            freed = 0
            if self.orphans:
                gone = set(ids)
                freed = sum(o["size"] for o in self.orphans["orphans"] if o["id"] in gone)
                self.orphans["orphans"] = [o for o in self.orphans["orphans"] if o["id"] not in gone]
                self.orphans["orphan_size"] = sum(o["size"] for o in self.orphans["orphans"])
        log(f"Jellyfin metadata: obrisano {n} mapa siročadi")
        threading.Thread(target=self._safe_refresh, daemon=True).start()
        return {"deleted": n, "freed": freed}


JFC = JFConfig()


# --------------------------------------------------------------------------
# Pozadinski poslovi
# --------------------------------------------------------------------------


def scheduler():
    last_scan = time.time()
    last_purge = 0
    while True:
        time.sleep(60)
        try:
            if CFG["scan_hours"] > 0 and time.time() - last_scan > CFG["scan_hours"] * 3600:
                last_scan = time.time()
                run_scan()
                JF.sync()
            if CFG["trash_days"] > 0 and time.time() - last_purge > 3600:
                last_purge = time.time()
                freed = trash_empty(older_than_days=CFG["trash_days"])
                if freed:
                    log(f"Automatski ispražnjena kanta, oslobođeno {freed} B")
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def node_json(node, path, depth=1):
    out = {"name": node.name, "path": path, "size": node.size, "files": node.files,
           "mtime": node.mtime, "is_dir": node.children is not None}
    if depth > 0 and node.children is not None:
        kids = sorted(node.children.values(), key=lambda n: -n.size)
        out["children"] = [node_json(k, os.path.join(path, k.name), depth - 1) for k in kids]
    return out


def dir_listing(path):
    """Mapa iz stabla + datoteke izravno u njoj (čitane uživo)."""
    with SCAN.lock:
        info, node, chain = find_node(path)
        if info is None or node is None:
            return None
        data = node_json(node, os.path.realpath(path), depth=1)
        data["root"] = {"label": info["label"], "path": info["path"], "kind": info["kind"]}
    files = []
    try:
        for e in os.scandir(path):
            if e.is_file(follow_symlinks=False):
                st = e.stat(follow_symlinks=False)
                files.append({"name": e.name, "path": e.path, "size": st.st_size, "mtime": int(st.st_mtime),
                              "nlink": st.st_nlink, "is_dir": False})
    except OSError:
        pass
    files.sort(key=lambda f: -f["size"])
    data["file_entries"] = files
    data["files_size"] = sum(f["size"] for f in files)
    rel = os.path.relpath(os.path.realpath(path), info["path"])
    crumbs = [{"name": info["label"], "path": info["path"]}]
    acc = info["path"]
    if rel != ".":
        for p in rel.split(os.sep):
            acc = os.path.join(acc, p)
            crumbs.append({"name": p, "path": acc})
    data["crumbs"] = crumbs
    data["deletable"] = info["kind"] == "media" and CFG["allow_delete"]
    return data


def overview():
    with SCAN.lock:
        roots = []
        for root, info in SCAN.roots.items():
            node = info["node"]
            top = []
            if node.children:
                for k in sorted(node.children.values(), key=lambda n: -n.size)[:12]:
                    top.append({"name": k.name, "path": os.path.join(root, k.name), "size": k.size,
                                "files": k.files})
            roots.append({"label": info["label"], "path": root, "kind": info["kind"], "exists": info["exists"],
                          "size": node.size, "files": node.files, "trash_size": info["trash_size"],
                          "hardlinked_bytes": info["hardlinked_bytes"], "errors": info["errors"][:20],
                          "error_count": len(info["errors"]), "top": top})
        ext = sorted(([k, v[0], v[1]] for k, v in SCAN.ext_stats.items()), key=lambda x: -x[2])[:20]
        cats = {"video": 0, "titlovi": 0, "slike": 0, "nfo/metapodaci": 0, "audio": 0, "smeće/ostalo": 0}
        for k, (n, s) in SCAN.ext_stats.items():
            if k in VIDEO_EXT:
                cats["video"] += s
            elif k in (".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"):
                cats["titlovi"] += s
            elif k in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tbn"):
                cats["slike"] += s
            elif k in (".nfo", ".xml", ".json", ".bif"):
                cats["nfo/metapodaci"] += s
            elif k in (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".mka", ".dts", ".ac3"):
                cats["audio"] += s
            else:
                cats["smeće/ostalo"] += s
        return {
            "version": APP_VERSION,
            "scan": {"running": SCAN.running, "started": SCAN.started, "finished": SCAN.finished,
                     "duration": SCAN.duration, "error": SCAN.error, "progress": SCAN.progress},
            "roots": roots, "disks": SCAN.disks, "ext": ext, "categories": cats,
            "junk": {"count": len(SCAN.junk), "size": sum(j["size"] for j in SCAN.junk)},
            "empty_dirs": len(SCAN.empty_dirs),
            "duplicates": {"count": len(SCAN.duplicates),
                           "size": sum(sum(f["size"] for f in d["files"][1:]) for d in SCAN.duplicates)},
            "jellyfin": {"enabled": JF.enabled(), "synced": JF.synced, "error": JF.error,
                         "running": JF.running, "server": JF.server, "users": JF.users,
                         "items": len(JF.items), "series": len(JF.series)},
            "jfconfig": {"enabled": JFC.enabled(), "host": CFG["ssh_host"], "path": CFG["jf_config"]},
            "config": {"allow_delete": CFG["allow_delete"], "trash": CFG["trash"],
                       "trash_days": CFG["trash_days"], "scan_hours": CFG["scan_hours"]},
        }


def largest_files(min_mb=0, older_days=0, ext=None, q=None, limit=300, root=None):
    min_b = float(min_mb) * 1024 ** 2
    cutoff = time.time() - float(older_days) * 86400 if older_days else None
    ql = q.lower() if q else None
    exts = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in ext.split(",")} if ext else None
    with SCAN.lock:
        def ok(f):
            if f[1] < min_b:
                return False
            if cutoff and f[2] > cutoff:
                return False
            if exts and f[4] not in exts:
                return False
            if ql and ql not in f[0].lower():
                return False
            if root and f[5] != root:
                return False
            return True
        top = heapq.nlargest(limit, (f for f in SCAN.files if ok(f)), key=lambda f: f[1])
        total = sum(f[1] for f in SCAN.files if ok(f))
    return {"total": total, "files": [{"path": f[0], "size": f[1], "mtime": f[2], "nlink": f[3],
                                       "ext": f[4]} for f in top]}


def jf_list():
    with JF.lock:
        n_users = len(JF.users)
        movies = [dict(r, watched_all=(n_users > 0 and len(set(r["played_by"])) >= n_users))
                  for r in JF.items.values() if r["type"] == "Movie"]
        series = []
        for s in JF.series.values():
            sc = dict(s)
            sc["seasons"] = [dict(v, season=k) for k, v in sorted(s["seasons"].items(),
                                                                     key=lambda x: int(x[0]) if x[0].isdigit() else 999)]
            sc["on_disk"] = bool(s["path"]) and os.path.isdir(s["path"])
            series.append(sc)
        # Datoteke na disku koje Jellyfin ne poznaje
        orphans = []
        if JF.items:
            known = {r["path"] for r in JF.items.values() if r["path"]}
            with SCAN.lock:
                for f in SCAN.files:
                    if f[4] in VIDEO_EXT and f[0] not in known and f[1] > 20 * 1024 ** 2 \
                            and not SAMPLE_RE.search(os.path.basename(f[0])):
                        orphans.append({"path": f[0], "size": f[1], "mtime": f[2]})
            orphans.sort(key=lambda x: -x["size"])
        missing = [{"id": r["id"], "name": r["name"], "series": r["series"], "path": r["path"]}
                   for r in JF.items.values() if r["path"] and not r.get("on_disk")]
    return {"users": JF.users, "synced": JF.synced, "error": JF.error, "running": JF.running,
            "movies": movies, "series": series, "orphans": orphans[:1000],
            "orphans_size": sum(o["size"] for o in orphans), "missing_on_disk": missing[:500],
            "missing_count": len(missing), "path_map": CFG["path_map"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "JellyClean/" + APP_VERSION

    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        if not CFG["auth_user"]:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                ok_u = hashlib.sha256(u.encode()).digest() == hashlib.sha256(CFG["auth_user"].encode()).digest()
                ok_p = hashlib.sha256(p.encode()).digest() == hashlib.sha256(CFG["auth_pass"].encode()).digest()
                return ok_u and ok_p
            except Exception:
                return False
        return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="JellyClean"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 5 * 1024 * 1024:
            raise ValueError("Prevelik zahtjev")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._auth_ok():
            return self._deny()
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/health":
                return self._send(200, {"ok": True, "version": APP_VERSION})
            if u.path == "/api/overview":
                return self._send(200, overview())
            if u.path == "/api/dir":
                path = q.get("path") or (CFG["media"][0][1] if CFG["media"] else "/")
                d = dir_listing(path)
                if d is None:
                    return self._send(404, {"error": "Mapa nije pronađena u zadnjem skeniranju"})
                return self._send(200, d)
            if u.path == "/api/files":
                return self._send(200, largest_files(q.get("min_mb", 0), q.get("older_days", 0),
                                                     q.get("ext"), q.get("q"), int(q.get("limit", 300)),
                                                     q.get("root")))
            if u.path == "/api/junk":
                with SCAN.lock:
                    return self._send(200, {"junk": SCAN.junk[:3000], "empty_dirs": SCAN.empty_dirs[:3000],
                                            "duplicates": SCAN.duplicates[:1000]})
            if u.path == "/api/trash":
                return self._send(200, {"batches": trash_list(), "trash_days": CFG["trash_days"]})
            if u.path == "/api/jellyfin":
                return self._send(200, jf_list())
            if u.path == "/api/jfconfig":
                if not JFC.enabled():
                    return self._send(200, {"enabled": False})
                st = JFC.status
                if q.get("refresh") or st is None:
                    st = JFC.refresh_status()
                return self._send(200, {"enabled": True, "status": st, "orphans": JFC.orphans,
                                        "ssh": f"{CFG['ssh_user']}@{CFG['ssh_host']}:{CFG['ssh_port']}",
                                        "sudo": CFG["ssh_sudo"], "helper": CFG["ssh_helper"],
                                        "allow_delete": CFG["allow_delete"],
                                        "jellyfin": JF.enabled()})
            if u.path == "/api/jellyfin/tasks":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin nije podešen"})
                return self._send(200, {"tasks": JF.scheduled_tasks()})
            return self._send(404, {"error": "Nije pronađeno"})
        except SSHError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._auth_ok():
            return self._deny()
        # Jednostavna CSRF zaštita: traži custom header koji forma s drugog sitea ne može poslati
        if self.headers.get("X-Requested-With") != "JellyClean":
            return self._send(403, {"error": "Nedostaje X-Requested-With"})
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/scan":
                started = start_scan_bg()
                if body.get("jellyfin") and JF.enabled():
                    threading.Thread(target=JF.sync, daemon=True).start()
                return self._send(200, {"started": started})
            if u.path == "/api/jellyfin/sync":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin nije podešen"})
                threading.Thread(target=JF.sync, daemon=True).start()
                return self._send(200, {"started": True})
            if u.path == "/api/delete":
                paths = body.get("paths") or []
                if not isinstance(paths, list) or not paths:
                    return self._send(400, {"error": "Nema putanja"})
                res = delete_paths(paths, bool(body.get("permanent")), bool(body.get("dry_run")))
                return self._send(200, {"results": res, "trash": CFG["trash"] and not body.get("permanent")})
            if u.path == "/api/jellyfin/delete":
                ids = body.get("ids") or []
                scope = body.get("scope", "item")
                targets = JF.delete_targets(ids, scope)
                if not targets:
                    return self._send(400, {"error": "Za odabrane stavke nema datoteka na disku "
                                                     "(provjeri PATH_MAP)"})
                res = delete_paths(targets, bool(body.get("permanent")), bool(body.get("dry_run")))
                if not body.get("dry_run") and any(r["ok"] for r in res):
                    threading.Thread(target=lambda: (time.sleep(20), JF.sync()), daemon=True).start()
                return self._send(200, {"results": res, "trash": CFG["trash"] and not body.get("permanent")})
            if u.path.startswith("/api/jfconfig/"):
                act = u.path.rsplit("/", 1)[1]
                if act == "orphans":
                    return self._send(200, JFC.find_orphans())
                if not CFG["allow_delete"] and not (act == "clean" and body.get("dry_run")):
                    return self._send(403, {"error": "Brisanje je isključeno (ALLOW_DELETE=false)"})
                if act == "clean":
                    return self._send(200, JFC.clean(body.get("action", ""), body.get("days", 0),
                                                     bool(body.get("dry_run", True))))
                if act == "delete-orphans":
                    return self._send(200, JFC.delete_orphans(body.get("ids") or []))
                return self._send(404, {"error": "Nije pronađeno"})
            if u.path == "/api/trash/restore":
                dest = trash_restore(body.get("path", ""))
                return self._send(200, {"restored": dest})
            if u.path == "/api/trash/empty":
                freed = trash_empty(body.get("path"))
                return self._send(200, {"freed": freed})
            if u.path == "/api/jellyfin/task":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin nije podešen"})
                tid = body.get("id", "")
                if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", tid):
                    return self._send(400, {"error": "Neispravan ID"})
                JF.req("POST", f"/ScheduledTasks/Running/{tid}")
                return self._send(200, {"started": True})
            return self._send(404, {"error": "Nije pronađeno"})
        except (DeleteError, SSHError) as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(502, {"error": f"Jellyfin HTTP {e.code}"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def main():
    os.makedirs(CFG["data_dir"], exist_ok=True)
    log(f"JellyClean {APP_VERSION} na http://{CFG['bind']}:{CFG['port']}")
    for l, p in CFG["media"]:
        log(f"  medij: {l} = {p}" + ("" if os.path.isdir(p) else "  (NE POSTOJI!)"))
    for l, p in CFG["jf_dirs"]:
        log(f"  jellyfin mapa: {l} = {p}" + ("" if os.path.isdir(p) else "  (NE POSTOJI!)"))
    log("  Jellyfin API: " + (CFG["jf_url"] if JF.enabled() else "nije podešen"))
    log("  Jellyfin config (SSH): " + (f"{CFG['ssh_user']}@{CFG['ssh_host']}:{CFG['jf_config']}"
                                       if JFC.enabled() else "nije podešen"))
    log(f"  brisanje: {'da' if CFG['allow_delete'] else 'NE'}, kanta: {'da' if CFG['trash'] else 'ne'}"
        f" ({CFG['trash_days']} dana)")
    if not CFG["auth_user"]:
        log("  UPOZORENJE: AUTH_USER nije postavljen - sučelje je bez lozinke")
    start_scan_bg()
    if JF.enabled():
        threading.Thread(target=JF.sync, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    if "--helper-script" in sys.argv:
        if not CFG["jf_config"]:
            sys.exit("JF_CONFIG_PATH nije postavljen")
        sys.stdout.write(helper_script(standalone=True))
    else:
        main()
