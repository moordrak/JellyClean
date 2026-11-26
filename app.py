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

APP_VERSION = "1.6.0"
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
        self.plugin_libs = []
        self.plugin_skipped = 0

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

    def plugin_libraries(self, info=None):
        """Biblioteke koje stvaraju pluginovi (npr. Local Recommendations, JellyNext): mape su unutar
        Jellyfinove podatkovne mape (ProgramDataPath) ili se zovu kao poznate virtualne mape.
        Vraća [{"name", "plugin", "locations": [jellyfin putanje]}]."""
        try:
            info = info or self.req("GET", "/System/Info") or {}
        except Exception:
            info = {}
        roots = [p.rstrip("/") for p in (info.get("ProgramDataPath"), info.get("CachePath"),
                                          info.get("InternalMetadataPath")) if p]
        try:
            libs = self.req("GET", "/Library/VirtualFolders") or []
        except Exception:
            libs = []
        out = []
        for lib in libs:
            locs = [l.rstrip("/") for l in (lib.get("Locations") or []) if l]
            hits = []
            plugin = None
            for l in locs:
                ll = l.lower()
                if "virtual-libraries" in ll or "localrecs" in ll or "localrecommendation" in ll:
                    plugin = "Local Recommendations"
                elif "jellynext" in ll:
                    plugin = plugin or "JellyNext"
                elif any(l == r or l.startswith(r + "/") for r in roots):
                    plugin = plugin or "plugin (mapa unutar Jellyfin podataka)"
                else:
                    continue
                hits.append(l)
            if hits:
                out.append({"name": lib.get("Name") or "?", "plugin": plugin, "locations": hits})
        return out

    def sync(self):
        if not self.enabled():
            return
        with self.lock:
            if self.running:
                return
            self.running = True
        try:
            info = None
            try:
                info = self.req("GET", "/System/Info")
                self.server = {"name": info.get("ServerName"), "version": info.get("Version")}
            except Exception:
                self.server = None
            plibs = self.plugin_libraries(info)
            ploc = [l for pl in plibs for l in pl["locations"]]

            def in_plugin(path):
                return bool(path) and any(path == l or path.startswith(l + "/") for l in ploc)
            skipped = set()
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
                        if in_plugin(it.get("Path") or ""):
                            skipped.add(iid)
                            continue
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
                self.plugin_libs = [dict(pl, skipped=0) for pl in plibs]
                self.plugin_skipped = len(skipped)
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


HELPER_VERSION = 6

# NVENC patch: keylase/nvidia-patch, zakucana provjerena verzija. Skripta se ne sprema u ovaj
# repozitorij (nema licence za redistribuciju) - preuzima se s GitHuba i provjerava sha256 ovdje
# i ponovno u pomoćnom programu na Jellyfin računalu.
NVPATCH_COMMIT = "7f30a61d159fa4c9644a7bf9d71a679c07062f5f"
NVPATCH_SHA256 = ["317c1af2a403a641a05aceadfeac17c4a3b53e82cf0ddf33b2e9528437456ee0"]
NVPATCH_URLS = [f"https://raw.githubusercontent.com/keylase/nvidia-patch/{NVPATCH_COMMIT}/patch.sh",
                f"https://github.com/keylase/nvidia-patch/raw/{NVPATCH_COMMIT}/patch.sh"]
NVPATCH_VERIFY_N = 13

HELPER_BODY = r'''
set -u
export LC_ALL=C
sz() { if [ -e "$1" ]; then du -sb "$1" 2>/dev/null | tail -1 | cut -f1; else echo -1; fi; }
cnt() { if [ -e "$1" ]; then find "$1" -type f 2>/dev/null | wc -l; else echo 0; fi; }
okid() { echo "$1" | grep -Eq '^[0-9a-f]{2}/[0-9a-f]{32}$' && [ "${1%%/*}" = "$(echo "${1#*/}" | cut -c1-2)" ]; }

op_status() {
  echo "HVER __HVER__"
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
  for f in "$DATA"/*.db "$DATA"/*.db-wal "$DATA"/*.db-shm "$DATA"/data/*.db "$DATA"/data/*.db-wal "$DATA"/data/*.db-shm; do
    [ -f "$f" ] && echo "DB $(sz "$f") $(basename "$f")"
  done
  if [ -d "$DATA" ]; then
    for d in "$DATA"/*/; do [ -d "$d" ] && echo "SUB data $(sz "$d") $(basename "$d")"; done
    t=$(find "$DATA" -maxdepth 1 -type f ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'); echo "DATAFILES $t"
  fi
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

# ---- baza: ispravak putanja (Jellyfin 10.11+ / 12: data/jellyfin.db, tablica BaseItems) ----
jf_find_ctl() {
  JCTL=""; JNAME=""
  if command -v docker >/dev/null 2>&1; then
    for i in $(docker ps -a --format '{{.ID}}' 2>/dev/null); do
      src=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$i" 2>/dev/null)
      if [ -n "$src" ] && [ "${src%/}" = "${C%/}" ]; then JCTL="docker:$i"; JNAME=$(docker inspect -f '{{.Name}}' "$i" | sed 's#^/##'); break; fi
    done
  fi
  if [ -z "$JCTL" ] && command -v systemctl >/dev/null 2>&1 && systemctl cat jellyfin.service >/dev/null 2>&1; then JCTL="systemd:jellyfin"; JNAME=jellyfin; fi
}
jf_ctl() {
  case "$JCTL" in
    docker:*) docker "$1" "${JCTL#docker:}" >/dev/null 2>&1 ;;
    systemd:*) systemctl "$1" jellyfin >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}
jf_db() { DB="$DATA/data/jellyfin.db"; [ -f "$DB" ] || DB="$DATA/jellyfin.db"; }
jf_dbpy() {
python3 - "$@" <<'PYEOF'
import sqlite3, sys, uuid
mode, db, lst = sys.argv[1:4]
def norm(v):
    if isinstance(v, (bytes, bytearray)) and len(v) == 16:
        return uuid.UUID(bytes_le=bytes(v)).hex
    return str(v).replace("-", "").lower()
try:
    con = sqlite3.connect(db, timeout=30)
    cols = [r[1] for r in con.execute("PRAGMA table_info(BaseItems)")]
except Exception as e:
    print("ERR baza se ne može otvoriti: %s" % e); sys.exit(0)
if "Id" not in cols or "Path" not in cols:
    print("ERR baza nema očekivanu tablicu BaseItems(Id, Path) - nepoznata verzija sheme, ništa nije mijenjano"); sys.exit(0)
print("SCHEMA ok")
ok = miss = upd = 0
cur = con.cursor()
for line in open(lst, encoding="utf-8"):
    r = line.rstrip("\n").split("\t")
    if len(r) != 3 or not r[1].startswith("/") or not r[2].startswith("/"):
        continue
    iid, old, new = r
    rid = None
    for (cid, _p) in cur.execute("SELECT Id, Path FROM BaseItems WHERE Path = ?", (old,)).fetchall():
        if norm(cid) == iid:
            rid = cid
            break
    if rid is None:
        print("NOMATCH", iid); miss += 1; continue
    ok += 1
    if mode == "fix":
        cur.execute("UPDATE BaseItems SET Path = ? WHERE Id = ? AND Path = ?", (new, rid, old))
        upd += cur.rowcount
        print("FIXED", iid)
if mode == "fix":
    con.commit()
con.close()
print("RES", ok, miss, upd)
PYEOF
}
op_dbcheck() {
  tmp=$(mktemp); cat > "$tmp"
  jf_db
  if [ ! -f "$DB" ]; then echo "ERR Jellyfin baza nije pronađena ($DATA/data/jellyfin.db)"; rm -f "$tmp"; return; fi
  command -v python3 >/dev/null 2>&1 || { echo "ERR na Jellyfin računalu nema python3 (potreban za izmjenu baze)"; rm -f "$tmp"; return; }
  jf_find_ctl
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  echo "DB $(sz "$DB") $DB"
  jf_dbpy check "$DB" "$tmp"
  rm -f "$tmp"
}
op_dbfix() {
  tmp=$(mktemp); cat > "$tmp"
  jf_db
  if [ ! -f "$DB" ]; then echo "ERR Jellyfin baza nije pronađena"; rm -f "$tmp"; return; fi
  command -v python3 >/dev/null 2>&1 || { echo "ERR na Jellyfin računalu nema python3"; rm -f "$tmp"; return; }
  jf_find_ctl
  [ -n "$JCTL" ] || { echo "ERR ne znam kako zaustaviti Jellyfin (nije pronađen Docker kontejner ni systemd servis)"; rm -f "$tmp"; return; }
  jf_ctl stop || { echo "ERR zaustavljanje Jellyfina ($JNAME) nije uspjelo"; rm -f "$tmp"; return; }
  echo "STOPPED $JNAME"
  sleep 3
  ts=$(date +%Y%m%d-%H%M%S)
  for f in "$DB" "$DB-wal" "$DB-shm"; do [ -f "$f" ] && cp -a "$f" "$f.jellyclean-$ts.bak"; done
  echo "BACKUP $DB.jellyclean-$ts.bak"
  jf_dbpy fix "$DB" "$tmp"
  rm -f "$tmp"
  if jf_ctl start; then echo "STARTED $JNAME"; else echo "WARN Jellyfin ($JNAME) se nije ponovno pokrenuo - pokreni ga ručno"; fi
}

# ---- dijagnoza reprodukcije: vidi li Jellyfin datoteku, ffprobe, logovi ----
op_fileinfo() {
  p=${1:-}; term=${2:-}; iid=${3:-}
  case "$p" in /*) ;; *) echo "ERR neispravna putanja"; return ;; esac
  case "$p" in *..*|*"
"*) echo "ERR neispravna putanja"; return ;; esac
  jf_find_ctl
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  case "$JCTL" in
    docker:*)
      cid=${JCTL#docker:}
      if docker exec "$cid" test -e "$p" 2>/dev/null; then echo "CEXISTS 1"; else echo "CEXISTS 0"; fi
      if docker exec "$cid" test -r "$p" 2>/dev/null; then echo "CREAD 1"; else echo "CREAD 0"; fi
      docker exec "$cid" sh -c 'ls -lnL -- "$1" 2>&1 | head -1' _ "$p" | sed 's/^/CLS /'
      docker exec "$cid" sh -c 'ls -1 -- "$(dirname "$1")" 2>&1 | head -40' _ "$p" | sed 's/^/CDIR /'
      fp=""
      for f in /usr/lib/jellyfin-ffmpeg/ffprobe /usr/bin/ffprobe; do docker exec "$cid" test -x "$f" 2>/dev/null && { fp=$f; break; }; done
      if [ -n "$fp" ]; then
        out=$(timeout 60 docker exec "$cid" "$fp" -v error -show_entries format=format_name,duration:stream=index,codec_type,codec_name -of compact "$p" 2>&1); rc=$?
        printf '%s\n' "$out" | head -30 | sed 's/^/PROBE /'; echo "PROBERC $rc"
      else echo "PROBERC -"; fi ;;
    *)
      if [ -e "$p" ]; then echo "CEXISTS 1"; else echo "CEXISTS 0"; fi
      if [ -r "$p" ]; then echo "CREAD 1"; else echo "CREAD 0"; fi
      ls -lnL -- "$p" 2>&1 | head -1 | sed 's/^/CLS /'
      ls -1 -- "$(dirname "$p")" 2>&1 | head -40 | sed 's/^/CDIR /'
      fp=""
      for f in /usr/lib/jellyfin-ffmpeg/ffprobe /usr/bin/ffprobe; do [ -x "$f" ] && { fp=$f; break; }; done
      if [ -n "$fp" ]; then
        out=$(timeout 60 "$fp" -v error -show_entries format=format_name,duration:stream=index,codec_type,codec_name -of compact "$p" 2>&1); rc=$?
        printf '%s\n' "$out" | head -30 | sed 's/^/PROBE /'; echo "PROBERC $rc"
      else echo "PROBERC -"; fi ;;
  esac
  # Jellyfin log: retci sa putanjom, ID-em ili nazivom stavke koji izgledaju kao greška
  for f in $(ls -t "$LOG"/log_*.log "$LOG"/jellyfin*.log 2>/dev/null | head -2); do
    if [ -n "$term" ]; then m=$(grep -F -e "$p" -e "$iid" -e "$term" "$f" 2>/dev/null)
    else m=$(grep -F -e "$p" -e "$iid" "$f" 2>/dev/null); fi
    printf '%s\n' "$m" | grep -iE 'err|warn|exception|fail|not found|could not|denied|unable' | tail -25 | sed 's/^/LOG /'
  done
  # najnoviji FFmpeg log koji spominje ovu datoteku
  m=""
  for f in $(ls -t "$LOG"/FFmpeg.*.log 2>/dev/null | head -40); do grep -qF -- "$p" "$f" 2>/dev/null && { m=$f; break; }; done
  if [ -n "$m" ]; then echo "FFLOG $(basename "$m")"; tail -30 "$m" | sed 's/^/FF /'; fi
}

# ---- test paralelnog enkodiranja (NVIDIA NVENC) u Jellyfin okruženju ----
jf_exec_prefix() {
  EX=""
  case "$JCTL" in docker:*) EX="docker exec ${JCTL#docker:}" ;; esac
}
op_enctest() {
  n=${1:-1}; dur=${2:-30}; mode=${3:-max}; h=${4:-1080}; p=${5:-}
  case "$n$dur$h" in *[!0-9]*|"") echo "ERR neispravni parametri"; return ;; esac
  [ "$n" -ge 1 ] && [ "$n" -le 32 ] || { echo "ERR broj tokova 1-32"; return; }
  [ "$dur" -ge 5 ] && [ "$dur" -le 300 ] || { echo "ERR trajanje 5-300 s"; return; }
  case "$h" in 1080) br=8M ;; 720) br=4M ;; 2160) br=20M ;; *) echo "ERR cilj"; return ;; esac
  case "$p" in /*) ;; *) echo "ERR neispravna putanja"; return ;; esac
  case "$p" in *..*) echo "ERR neispravna putanja"; return ;; esac
  re=""; [ "$mode" = realtime ] && re="-re"
  jf_find_ctl; jf_exec_prefix
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  $EX nvidia-smi -L >/dev/null 2>&1 || { echo "ERR nvidia-smi nije dostupan u Jellyfin okruženju - NVIDIA kartica nije proslijeđena Jellyfinu (ili nema drivera)"; return; }
  echo "GPU $($EX nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)"
  $EX test -f "$p" || { echo "ERR Jellyfin ne vidi testnu datoteku: $p"; return; }
  $EX sh -c '
    FF=/usr/lib/jellyfin-ffmpeg/ffmpeg; [ -x "$FF" ] || FF=$(command -v ffmpeg)
    [ -n "$FF" ] || { echo "ERR u Jellyfin okruženju nema ffmpega"; exit 0; }
    n=$1; dur=$2; re=$3; h=$4; br=$5; p=$6
    d=$(mktemp -d); pids=""
    i=1
    while [ $i -le $n ]; do
      "$FF" -hide_banner -nostdin -y -hwaccel cuda -hwaccel_output_format cuda -t "$dur" $re -i "$p" \
        -vf "scale_cuda=-2:$h:format=yuv420p" -c:v h264_nvenc -preset p4 -b:v "$br" -c:a aac -b:a 192k \
        -metadata title=jellyclean-enctest -f null - 2>"$d/$i.log" &
      pids="$pids $!"; i=$((i+1))
    done
    ( while :; do nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,utilization.decoder,memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | sed "s/^/UTIL /"; sleep 1; done ) &
    smp=$!
    for x in $pids; do wait $x; done
    kill $smp 2>/dev/null
    i=1
    while [ $i -le $n ]; do
      f="$d/$i.log"
      sp=$(grep -o "speed= *[0-9.]*x" "$f" | tail -1 | tr -dc "0-9.")
      fps=$(grep -o "fps= *[0-9.]*" "$f" | tail -1 | tr -dc "0-9.")
      er=$(grep -m1 -E "OpenEncodeSessionEx|incompatible client key|out of memory|No capable devices|Cannot load|Error|Invalid" "$f" | cut -c1-200)
      if [ -z "$sp" ]; then echo "FAIL $i $(tail -3 "$f" | tr "\n" " " | cut -c1-300)"; else echo "RES $i $sp ${fps:-0} $er"; fi
      i=$((i+1))
    done
    rm -rf "$d"
  ' _ "$n" "$dur" "$re" "$h" "$br" "$p"
}
op_enckill() {
  jf_find_ctl; jf_exec_prefix
  $EX pkill -f jellyclean-enctest >/dev/null 2>&1; echo "KILLED"
}

# ---- NVENC patch (keylase/nvidia-patch): skripta stiže na stdin (base64), pokreće se samo ako
# ---- se njen sha256 nalazi na popisu odobrenih verzija zapečenom u ovom pomoćnom programu
NVP_SHA="__NVPSHA__"
NVP_BAK=/opt/nvidia/libnvidia-encode-backup
nv_lib() {
  NVVER=""; NVOBJ=""; NVLIB=""
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  NVVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' \r')
  [ -n "$NVVER" ] || return 1
  maj=${NVVER%%.*}
  NVOBJ=libnvidia-encode.so
  case "$maj" in ''|*[!0-9]*) ;; *) [ "$maj" -ge 415 ] && [ "$maj" -le 435 ] && NVOBJ=libnvcuvid.so ;; esac
  for nvdir in /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu/nvidia/current /usr/lib/x86_64-linux-gnu/nvidia/tesla "/usr/lib/x86_64-linux-gnu/nvidia/tesla-$maj" /usr/lib64 /usr/lib "/usr/lib/nvidia-$maj"; do
    [ -e "$nvdir/$NVOBJ.$NVVER" ] && { NVLIB="$nvdir/$NVOBJ.$NVVER"; break; }
  done
  return 0
}
jf_running() {
  case "$JCTL" in
    docker:*) [ "$(docker inspect -f '{{.State.Running}}' "${JCTL#docker:}" 2>/dev/null)" = true ] ;;
    systemd:*) systemctl is-active --quiet jellyfin ;;
    *) return 1 ;;
  esac
}
op_nvstatus() {
  jf_find_ctl; echo "CTL ${JCTL:-none} ${JNAME:--}"
  if ! nv_lib; then echo "NVNONE"; return; fi
  echo "NVGPU $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
  echo "NVDRV $NVVER"
  echo "NVLIB ${NVLIB:--}"
  b="$NVP_BAK/$NVOBJ.$NVVER"
  if [ -f "$b" ] && [ -n "$NVLIB" ]; then
    echo "NVBAK 1"
    if cmp -s "$b" "$NVLIB"; then echo "NVPATCHED 0"; else echo "NVPATCHED 1"; fi
  else echo "NVBAK 0"; echo "NVPATCHED 0"; fi
  for f in "$NVP_BAK"/*; do [ -f "$f" ] && echo "NVOLD $(basename "$f")"; done
  if command -v bash >/dev/null 2>&1; then echo "NVBASH 1"; else echo "NVBASH 0"; fi
}
nvp_cleanup() {
  # briše samo privremenu mapu koju je napravio mktemp (ime jc-nvp.*), nikad ništa drugo
  case "${NVTMP:-}" in */jc-nvp.*) [ -d "$NVTMP" ] && rm -rf -- "$NVTMP" ;; esac
  NVTMP=""
}
op_nvpatch() {
  case "${1:-}" in patch) fl="" ;; rollback) fl="-r" ;; *) echo "ERR nepoznata radnja"; return ;; esac
  command -v bash >/dev/null 2>&1 || { echo "ERR na Jellyfin računalu nema bash-a"; return; }
  command -v sha256sum >/dev/null 2>&1 || { echo "ERR na Jellyfin računalu nema sha256sum"; return; }
  NVTMP=$(mktemp -d "${TMPDIR:-/tmp}/jc-nvp.XXXXXX") || { echo "ERR mktemp nije uspio"; return; }
  chmod 700 "$NVTMP"
  if ! base64 -d > "$NVTMP/patch.sh" 2>/dev/null; then echo "ERR skripta nije ispravno prenesena"; nvp_cleanup; return; fi
  h=$(sha256sum "$NVTMP/patch.sh" | cut -d' ' -f1)
  ok=0; for x in $NVP_SHA; do [ "$h" = "$x" ] && ok=1; done
  if [ "$ok" != 1 ]; then echo "ERR skripta za patch nije odobrena verzija (sha256 $h) - nadogradi pomoćni program"; nvp_cleanup; return; fi
  if ! nv_lib; then echo "ERR nvidia-smi ne radi na $(hostname) - NVIDIA driver nije instaliran na ovom računalu"; nvp_cleanup; return; fi
  jf_find_ctl
  st=0
  if [ -n "$JCTL" ] && jf_running; then
    if jf_ctl stop; then st=1; echo "STOPPED $JNAME"; sleep 2; else echo "WARN Jellyfin ($JNAME) nije zaustavljen - nastavljam"; fi
  fi
  out=$(cd / && bash "$NVTMP/patch.sh" $fl 2>&1); rc=$?
  printf '%s\n' "$out" | sed 's/^/OUT /'
  echo "RC $rc"
  nvp_cleanup
  if [ "$st" = 1 ]; then
    if jf_ctl start; then echo "STARTED $JNAME"; else echo "WARN Jellyfin ($JNAME) se nije ponovno pokrenuo - pokreni ga ručno"; fi
  fi
}
op_nvverify() {
  n=${1:-13}
  case "$n" in ''|*[!0-9]*) echo "ERR neispravan broj sesija"; return ;; esac
  [ "$n" -ge 1 ] && [ "$n" -le 24 ] || { echo "ERR broj sesija 1-24"; return; }
  jf_find_ctl; jf_exec_prefix
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  $EX nvidia-smi -L >/dev/null 2>&1 || { echo "ERR nvidia-smi nije dostupan u Jellyfin okruženju - NVIDIA kartica nije proslijeđena Jellyfinu"; return; }
  $EX sh -c '
    FF=/usr/lib/jellyfin-ffmpeg/ffmpeg; [ -x "$FF" ] || FF=$(command -v ffmpeg)
    [ -n "$FF" ] || { echo "ERR u Jellyfin okruženju nema ffmpega"; exit 0; }
    n=$1; d=$(mktemp -d); pids=""; i=1
    while [ $i -le $n ]; do
      "$FF" -hide_banner -nostdin -v error -re -f lavfi -i testsrc2=size=640x360:rate=25 -t 8 \
        -c:v h264_nvenc -preset p1 -b:v 1M -metadata title=jellyclean-enctest -f null - 2>"$d/$i.log" &
      pids="$pids $!"; i=$((i+1))
    done
    i=1
    for x in $pids; do
      if wait $x; then echo "VOK $i"; else echo "VFAIL $i $(grep -v "^\s*$" "$d/$i.log" | head -2 | tr "\n" " " | cut -c1-240)"; fi
      i=$((i+1))
    done
    rm -rf "$d"
  ' _ "$n"
}

main() {
  op=${1:-}; [ $# -gt 0 ] && shift
  case "$op" in
    status) op_status ;;
    version) echo "HVER __HVER__" ;;
    clean) op_clean "$@" ;;
    listmeta) op_listmeta ;;
    dumeta) op_dumeta ;;
    delmeta) op_delmeta ;;
    dbcheck) op_dbcheck ;;
    dbfix) op_dbfix ;;
    fileinfo) op_fileinfo "$@" ;;
    enctest) op_enctest "$@" ;;
    enckill) op_enckill ;;
    nvstatus) op_nvstatus ;;
    nvpatch) op_nvpatch "$@" ;;
    nvverify) op_nvverify "$@" ;;
    *) echo "ERR nepoznata operacija '$op'"; exit 2 ;;
  esac
}
'''


def helper_script(standalone=True):
    """Shell skripta s operacijama nad Jellyfin config mapom.
    standalone=True: datoteka za /usr/local/sbin/jellyclean-helper (putanje zapečene, poziva main "$@")."""
    q = shlex.quote
    head = ("#!/bin/sh\n# JellyClean helper - generirano. Dopušta SAMO mjerenje i čišćenje Jellyfin\n"
            "# cachea, logova i siročadi u metadata/library unutar ovih putanja, test\n"
            "# enkodiranja i NVENC patch (samo odobrene verzije skripte, provjera sha256):\n"
            f"C={q(CFG['jf_config'])}\nCACHE={q(CFG['jf_cache'])}\nLOG={q(CFG['jf_log'])}\n"
            f"DATA={q(CFG['jf_data'])}\nMETA={q(CFG['jf_metadata'])}\n")
    body = HELPER_BODY.replace("__HVER__", str(HELPER_VERSION)).replace("__NVPSHA__", " ".join(NVPATCH_SHA256))
    return head + body + ('main "$@"\n' if standalone else "")


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
              "mode": "helper" if CFG["ssh_helper"] else ("sudo" if CFG["ssh_sudo"] else "direct"),
              "helper_version": None, "helper_expected": HELPER_VERSION, "helper_outdated": False,
              "data_files": 0}
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
                elif k == "HVER":
                    st["helper_version"] = int(parts[1])
                elif k == "DATAFILES":
                    st["data_files"] = int(parts[1])
                elif k == "WRITE":
                    st["writable"] = parts[1] == "1"
            except (ValueError, IndexError):
                pass
        for v in st["subs"].values():
            v.sort(key=lambda x: -x["size"])
        if st["mode"] == "helper" and st["helper_version"] != HELPER_VERSION:
            st["helper_outdated"] = True
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
# Provjera putanja: Jellyfin baza ↔ stvarne datoteke
# --------------------------------------------------------------------------

import unicodedata


def unmap_to_jf(local):
    """Lokalna putanja (JellyClean) -> putanja kako je vidi Jellyfin (obrnuti PATH_MAP)."""
    if not local:
        return local
    for jf, loc in sorted(CFG["path_map"], key=lambda x: -len(x[1])):
        if local == loc or local.startswith(loc + "/"):
            return jf + local[len(loc):]
    return local


def _nfc(s):
    return unicodedata.normalize("NFC", s)


class PathCheckCancelled(Exception):
    pass


class PathCheck:
    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.running = False
        self.error = None
        self.cancelled = False
        self.cancel_ev = threading.Event()
        self.progress = {"phase": "", "done": 0, "total": 0}
        self.items = {}      # id -> zapis (za akcije)

    def cancel(self):
        if self.running:
            self.cancel_ev.set()
            self.progress["phase"] = "Prekidam…"
        return {"cancelling": self.running}

    def _check_cancel(self):
        if self.cancel_ev.is_set():
            raise PathCheckCancelled()

    def _fetch(self):
        items, start = [], 0
        while True:
            self._check_cancel()
            res = JF.req("GET", "/Items", {
                "Recursive": "true", "IncludeItemTypes": "Movie,Episode,Series",
                "Fields": "Path,MediaSources", "EnableImages": "false", "EnableUserData": "false",
                "EnableTotalRecordCount": "true", "StartIndex": start, "Limit": 2000}, timeout=300) or {}
            batch = res.get("Items", [])
            items.extend(batch)
            start += len(batch)
            self.progress.update(phase="Dohvaćam stavke iz Jellyfina", done=start,
                                 total=res.get("TotalRecordCount", 0))
            if not batch or start >= res.get("TotalRecordCount", 0):
                break
        return items

    def run(self):
        if not JF.enabled():
            raise SSHError("Provjera putanja treba Jellyfin API (JELLYFIN_URL, JELLYFIN_API_KEY)")
        with self.lock:
            if self.running:
                return
            self.running = True
            self.error = None
            self.cancelled = False
            self.cancel_ev.clear()
            self.progress = {"phase": "Pokrećem…", "done": 0, "total": 0}
        try:
            self._run()
        except PathCheckCancelled:
            self.cancelled = True
            log("Provjera putanja: prekinuto")
        except Exception as e:
            self.error = str(e)
            log("Provjera putanja:", e)
        finally:
            self.running = False
            self.progress["phase"] = ""

    def _run(self):
        t0 = time.time()
        self.progress.update(phase="Tražim plugin biblioteke", done=0, total=0)
        plibs = JF.plugin_libraries()
        ploc = sorted(((l, pl) for pl in plibs for l in pl["locations"]), key=lambda x: -len(x[0]))

        def plugin_of(path):
            for l, pl in ploc:
                if path == l or path.startswith(l + "/"):
                    return pl
            return None
        raw = self._fetch()
        series = {it["Id"]: it for it in raw if it.get("Type") == "Series"}
        # indeksi iz zadnjeg skeniranja diska
        with SCAN.lock:
            files = [(f[0], f[1]) for f in SCAN.files if f[4] in VIDEO_EXT]
        by_name, by_lower, by_nfc, by_size = {}, {}, {}, {}
        for path, size in files:
            b = os.path.basename(path)
            by_name.setdefault(b, []).append(path)
            by_lower.setdefault(b.lower(), []).append(path)
            by_nfc.setdefault(_nfc(b).lower(), []).append(path)
            if size > 50 * 1024 ** 2:
                by_size.setdefault(size, []).append(path)
        roots = [r for _, r in CFG["media"]]

        def under_roots(p):
            return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)

        def best(cands, dbpath):
            if not cands:
                return None
            # najdulji zajednički završetak putanje
            dbp = dbpath.lower().split("/")

            def score(c):
                cp = c.lower().split("/")
                k = 0
                while k < min(len(cp), len(dbp)) and cp[-1 - k] == dbp[-1 - k]:
                    k += 1
                return k
            return sorted(cands, key=lambda c: (-score(c), c))[0]

        out, counts = [], {"ok": 0, "moved": 0, "missing": 0, "virtual": 0, "virtual_found": 0, "outside": 0,
                           "plugin": 0}
        plugin_count = {}
        recs = {}
        todo = sum(1 for it in raw if it.get("Type") in ("Movie", "Episode"))
        self.progress.update(phase="Uspoređujem s diskom", done=0, total=todo)
        for it in raw:
            t = it.get("Type")
            if t in ("Movie", "Episode"):
                self.progress["done"] += 1
                if self.progress["done"] % 100 == 0:
                    self._check_cancel()
            if t not in ("Movie", "Episode"):
                continue
            iid = it["Id"].replace("-", "").lower()
            jf_path = it.get("Path") or ""
            loc_type = it.get("LocationType") or ""
            ms = it.get("MediaSources") or []
            jsize = (ms[0].get("Size") if ms else None) or 0
            name = it.get("Name") or ""
            label = name
            if t == "Episode":
                se = f"S{(it.get('ParentIndexNumber') or 0):02d}E{(it.get('IndexNumber') or 0):02d}"
                label = f"{it.get('SeriesName') or '?'} {se} · {name}"
            elif it.get("ProductionYear"):
                label = f"{name} ({it['ProductionYear']})"
            rec = {"id": iid, "type": t, "label": label, "jf_path": jf_path, "local": None,
                   "status": None, "reason": "", "cand_local": None, "cand_jf": None,
                   "series_id": (it.get("SeriesId") or "").replace("-", "").lower() or None}
            pl = plugin_of(jf_path) if jf_path else None
            if pl:
                # stavka iz biblioteke koju stvara plugin (npr. symlinkovi Local Recommendations):
                # nije greška u bazi i NE smije se "ispravljati" na izvornu datoteku
                rec.update(status="plugin", plugin=pl["plugin"], library=pl["name"],
                           reason=f"„{pl['name']}” · {pl['plugin']} - stavke stvara plugin, ne provjeravaju se")
                counts["plugin"] += 1
                plugin_count[pl["name"]] = plugin_count.get(pl["name"], 0) + 1
                recs[iid] = rec
                out.append(rec)
                continue
            local = map_jf_path(jf_path) if jf_path else ""
            rec["local"] = local
            if not jf_path or loc_type == "Virtual":
                rec["status"] = "virtual"
            elif os.path.exists(local):
                counts["ok"] += 1
                continue
            elif not under_roots(local):
                rec["status"] = "outside"
                rec["reason"] = "putanja nije unutar medijskih mapa koje JellyClean vidi - provjeri PATH_MAP ili MEDIA_PATHS"
            else:
                rec["status"] = "missing"
            # traženje kandidata
            cand, reason = None, ""
            base = os.path.basename(local) if local else ""
            if base and rec["status"] != "virtual":
                if by_name.get(base):
                    cand = best(by_name[base], local)
                    reason = "ista datoteka pod drugom putanjom (premještena ili promijenjen mount)"
                elif by_lower.get(base.lower()):
                    cand = best(by_lower[base.lower()], local)
                    reason = "razlikuju se samo velika i mala slova u imenu"
                elif by_nfc.get(_nfc(base).lower()):
                    cand = best(by_nfc[_nfc(base).lower()], local)
                    reason = "drugačiji zapis posebnih znakova (Unicode, npr. č/ć/ž)"
                elif jsize and len(by_size.get(jsize, [])) == 1:
                    cand = by_size[jsize][0]
                    reason = "datoteka iste veličine pod drugim imenom"
            if not cand and t == "Episode":
                sr = series.get(it.get("SeriesId") or "")
                sdir = map_jf_path(sr.get("Path")) if sr and sr.get("Path") else None
                sn, en = it.get("ParentIndexNumber"), it.get("IndexNumber")
                if sdir and sn is not None and en is not None:
                    pre = sdir.rstrip("/") + "/"
                    hits = []
                    for n_f, (path, _) in enumerate(files):
                        if n_f % 20000 == 0:
                            self._check_cancel()
                        if path.startswith(pre):
                            m = EPISODE_RE.search(os.path.basename(path))
                            if m and int(m.group(1)) == sn and int(m.group(2)) == en:
                                hits.append(path)
                    if hits:
                        cand = best(hits, local or sdir)
                        reason = ("Jellyfin ima epizodu bez datoteke, a datoteka postoji u mapi serije"
                                  if rec["status"] == "virtual" else "epizoda pronađena pod drugim imenom u mapi serije")
            if rec["status"] == "virtual":
                if not cand:
                    counts["virtual"] += 1     # normalno: epizoda koja nedostaje
                    continue
                rec["status"] = "virtual_found"
            elif cand:
                rec["status"] = "moved"
            if cand:
                rec["cand_local"] = cand
                rec["cand_jf"] = unmap_to_jf(cand)
                rec["reason"] = reason
            counts[rec["status"]] += 1
            recs[iid] = rec
            out.append(rec)
        # analiza prefiksa: ista promjena putanje kod mnogo stavki
        pairs = {}
        for r in out:
            if r["status"] == "moved" and r["jf_path"] and r["cand_jf"]:
                a, b = r["jf_path"].split("/"), r["cand_jf"].split("/")
                k = 0
                while k < min(len(a), len(b)) - 1 and a[-1 - k] == b[-1 - k]:
                    k += 1
                key = ("/".join(a[:len(a) - k]) or "/", "/".join(b[:len(b) - k]) or "/")
                if key[0] != key[1]:
                    pairs[key] = pairs.get(key, 0) + 1
        prefixes = [{"from": k[0], "to": k[1], "count": v} for k, v in sorted(pairs.items(), key=lambda x: -x[1]) if v >= 3]
        out.sort(key=lambda r: ({"moved": 0, "virtual_found": 1, "missing": 2, "outside": 3, "plugin": 9}[r["status"]],
                                r["label"].lower()))
        with self.lock:
            self.items = recs
            self.result = {"time": now_iso(), "duration": round(time.time() - t0, 1), "total": sum(counts.values()),
                           "counts": counts, "items": out[:5000], "prefixes": prefixes[:10],
                           "plugin_libs": [dict(pl, items=plugin_count.get(pl["name"], 0)) for pl in plibs],
                           "scan_time": SCAN.finished, "ssh": JFC.enabled()}
        log(f"Provjera putanja: {counts}")

    # ---------------------------------------------------------- akcije ---
    def _selected(self, ids, need_cand=True):
        with self.lock:
            recs = [self.items[i] for i in ids if i in self.items]
        recs = [r for r in recs if r["status"] != "plugin"]
        if need_cand:
            recs = [r for r in recs if r["cand_jf"]]
        return recs

    def notify(self, ids):
        """Siguran ispravak: javi Jellyfinu da je stara putanja nestala, a nova se pojavila."""
        recs = self._selected(ids)
        if not recs:
            raise SSHError("Nema odabranih stavki s pronađenom datotekom")
        updates = []
        for r in recs:
            if r["jf_path"] and r["status"] == "moved":
                updates.append({"Path": r["jf_path"], "UpdateType": "Deleted"})
            updates.append({"Path": r["cand_jf"], "UpdateType": "Created"})
        JF.req("POST", "/Library/Media/Updated", body={"Updates": updates})
        series = {r["series_id"] for r in recs if r["series_id"]}
        for sid in series:
            try:
                JF.req("POST", f"/Items/{sid}/Refresh", {"Recursive": "true", "MetadataRefreshMode": "Default",
                                                         "ImageRefreshMode": "Default"})
            except Exception:
                pass
        log(f"Provjera putanja: Jellyfinu javljeno {len(updates)} promjena")
        return {"sent": len(updates), "series_refreshed": len(series)}

    def _lines(self, recs):
        out = []
        for r in recs:
            if r["status"] != "moved" or not r["jf_path"] or "\t" in r["jf_path"] + r["cand_jf"] \
                    or "\n" in r["jf_path"] + r["cand_jf"]:
                continue
            out.append(f"{r['id']}\t{r['jf_path']}\t{r['cand_jf']}")
        return out

    def dbfix(self, ids, dry_run=True):
        if not JFC.enabled():
            raise SSHError("Ispravak u bazi treba SSH pristup Jellyfin računalu (kartica Jellyfin config)")
        lines = self._lines(self._selected(ids))
        if not lines:
            raise SSHError("Među odabranima nema stavki koje se mogu ispraviti u bazi "
                           "(samo 'pronađeno drugdje' s postojećim zapisom putanje)")
        out = JFC.run("dbcheck" if dry_run else "dbfix", lines=lines, timeout=900)
        res = {"dry_run": dry_run, "requested": len(lines), "ctl": None, "name": None, "db": None,
               "db_size": None, "matched": 0, "nomatch": 0, "updated": 0, "backup": None,
               "stopped": False, "started": False, "warnings": []}
        for line in out.splitlines():
            p = line.split(" ", 2)
            if p[0] == "CTL":
                res["ctl"], res["name"] = p[1], (p[2] if len(p) > 2 else None)
            elif p[0] == "DB":
                res["db_size"], res["db"] = int(p[1]), (p[2] if len(p) > 2 else None)
            elif p[0] == "RES":
                q = line.split()
                res["matched"], res["nomatch"], res["updated"] = int(q[1]), int(q[2]), int(q[3])
            elif p[0] == "BACKUP":
                res["backup"] = line[7:]
            elif p[0] == "STOPPED":
                res["stopped"] = True
            elif p[0] == "STARTED":
                res["started"] = True
            elif p[0] == "WARN":
                res["warnings"].append(line[5:])
        if not dry_run:
            log(f"Provjera putanja: ispravljeno u bazi {res['updated']} zapisa, kopija {res['backup']}")
            threading.Thread(target=lambda: (time.sleep(30), self.run()), daemon=True).start()
        return res


PC = PathCheck()


# --------------------------------------------------------------------------
# Dijagnoza reprodukcije: "Zašto se ne pušta?"
# --------------------------------------------------------------------------

PLAYBACK_ERR = {
    "NotAllowed": "korisnik nema dopuštenje za reprodukciju (postavke korisnika u Jellyfinu)",
    "NoCompatibleStream": "nema kompatibilnog toka, a ni transkodiranje ga ne može napraviti",
    "RateLimitExceeded": "prekoračeno ograničenje istovremenih reprodukcija",
}


def _admin_user():
    users = JF.req("GET", "/Users") or []
    return next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0] if users else None)


def diag_search(q):
    res = JF.req("GET", "/Items", {"searchTerm": q, "Recursive": "true", "IncludeItemTypes": "Movie,Episode,Series",
                                   "Limit": 30, "Fields": "Path", "EnableImages": "false"}) or {}
    out = []
    for it in res.get("Items", []):
        lab = it.get("Name") or "?"
        if it.get("Type") == "Episode":
            lab = f"{it.get('SeriesName') or '?'} S{(it.get('ParentIndexNumber') or 0):02d}E{(it.get('IndexNumber') or 0):02d} · {lab}"
        elif it.get("ProductionYear"):
            lab += f" ({it['ProductionYear']})"
        out.append({"id": it["Id"].replace("-", "").lower(), "type": it.get("Type"), "label": lab})
    return out


def diag_episodes(series_id):
    admin = _admin_user()
    params = {"Fields": "Path", "EnableImages": "false"}
    if admin:
        params["UserId"] = admin["Id"]
    res = JF.req("GET", f"/Shows/{series_id}/Episodes", params) or {}
    out = []
    for it in res.get("Items", []):
        out.append({"id": it["Id"].replace("-", "").lower(), "type": "Episode",
                    "label": f"S{(it.get('ParentIndexNumber') or 0):02d}E{(it.get('IndexNumber') or 0):02d} · {it.get('Name') or '?'}",
                    "virtual": (it.get("LocationType") == "Virtual" or not it.get("Path"))})
    return out


def _find_candidate(path, it):
    """Gdje je datoteka zapravo (iz zadnjeg skeniranja diska)."""
    with SCAN.lock:
        files = [f[0] for f in SCAN.files if f[4] in VIDEO_EXT]
    base = os.path.basename(path) if path else ""
    if base:
        for cmp in (lambda b: b, lambda b: b.lower(), lambda b: _nfc(b).lower()):
            hits = [f for f in files if cmp(os.path.basename(f)) == cmp(base)]
            if hits:
                return hits[0]
    if it.get("Type") == "Episode" and it.get("SeriesId"):
        try:
            sr = (JF.req("GET", "/Items", {"Ids": it["SeriesId"], "Recursive": "true", "Fields": "Path"}) or {}).get("Items", [])
        except Exception:
            sr = []
        sdir = map_jf_path(sr[0].get("Path")) if sr and sr[0].get("Path") else None
        sn, en = it.get("ParentIndexNumber"), it.get("IndexNumber")
        if sdir and sn is not None and en is not None:
            for f in files:
                if f.startswith(sdir.rstrip("/") + "/"):
                    m = EPISODE_RE.search(os.path.basename(f))
                    if m and int(m.group(1)) == sn and int(m.group(2)) == en:
                        return f
    return None


def diagnose(iid):
    if not JF.enabled():
        raise SSHError("Dijagnoza treba Jellyfin API (JELLYFIN_URL, JELLYFIN_API_KEY)")
    if not HEX32.match(iid):
        raise SSHError("Neispravan ID stavke")
    res = JF.req("GET", "/Items", {"Ids": iid, "Recursive": "true", "Fields": "Path,MediaSources,MediaStreams",
                                   "EnableImages": "false"}) or {}
    items = res.get("Items") or []
    if not items:
        raise SSHError("Stavka nije pronađena u Jellyfinu")
    it = items[0]
    t = it.get("Type")
    label = it.get("Name") or "?"
    if t == "Episode":
        label = f"{it.get('SeriesName') or '?'} S{(it.get('ParentIndexNumber') or 0):02d}E{(it.get('IndexNumber') or 0):02d} · {label}"
    path = it.get("Path") or ""
    loc_type = it.get("LocationType") or ""
    ms = it.get("MediaSources") or []
    checks, concl, fixes = [], [], []
    d = {"id": iid, "label": label, "type": t, "path": path, "checks": checks, "conclusions": concl,
         "fixes": fixes, "logs": [], "ffmpeg": [], "fflog": None, "dir": [], "probe": [], "candidate": None,
         "candidate_jf": None, "ssh": JFC.enabled(), "time": now_iso()}

    def chk(name, ok, detail=""):
        checks.append({"name": name, "ok": ok, "detail": detail})

    # 1) zapis u bazi
    virtual = (not path) or loc_type == "Virtual"
    chk("Zapis u Jellyfinu", not virtual,
        f"{'film' if t == 'Movie' else 'epizoda'} · {loc_type or '?'}" + ("" if path else " · bez putanje datoteke"))
    if path:
        chk("Putanja u bazi", True, path)
    chk("Izvori u bazi (MediaSources)", bool(ms), f"{len(ms)} izvor(a)" if ms else "nema nijednog izvora")

    # 2) PlaybackInfo
    pi_err, pi_sources = None, None
    try:
        admin = _admin_user()
        q = {"UserId": admin["Id"]} if admin else None
        pi = JF.req("POST", f"/Items/{iid}/PlaybackInfo", q, body={}) or {}
        pi_err = pi.get("ErrorCode")
        pi_sources = len(pi.get("MediaSources") or [])
        chk("PlaybackInfo (što Jellyfin nudi klijentu)", bool(pi_sources) and not pi_err,
            (f"greška: {pi_err}" if pi_err else "") + (f"{' · ' if pi_err else ''}{pi_sources} izvor(a) za reprodukciju"))
    except urllib.error.HTTPError as e:
        chk("PlaybackInfo (što Jellyfin nudi klijentu)", False, f"HTTP {e.code}")
        pi_sources = 0
    except Exception as e:
        chk("PlaybackInfo (što Jellyfin nudi klijentu)", False, str(e)[:120])

    # 3) plugin biblioteka?
    plibs = []
    try:
        plibs = JF.plugin_libraries()
    except Exception:
        pass
    plugin = next((pl for pl in plibs for l in pl["locations"] if path == l or path.startswith(l + "/")), None) if path else None

    # 4) datoteka na NAS-u (kako je vidi JellyClean)
    local = map_jf_path(path) if path else ""
    local_exists = bool(local) and os.path.exists(local)
    if plugin:
        chk("Datoteka na NAS-u (JellyClean)", None, f"stavka iz plugin biblioteke „{plugin['name']}” ({plugin['plugin']}) - provjerava se samo na Jellyfin računalu")
    elif path:
        chk("Datoteka na NAS-u (JellyClean)", local_exists, local)
    cand = None
    if not local_exists and not plugin:
        cand = _find_candidate(local, it)
        if cand:
            d["candidate"], d["candidate_jf"] = cand, unmap_to_jf(cand)
            chk("Datoteka pronađena drugdje", True, cand)

    # 5) Jellyfin računalo: kontejner, ffprobe, logovi
    r = {"cexists": None, "cread": None, "cls": "", "probe_rc": None}
    if JFC.enabled() and path:
        try:
            out = JFC.run("fileinfo", (path, it.get("Name") or "", iid), timeout=180)
            for line in out.splitlines():
                k, _, v = line.partition(" ")
                if k == "CEXISTS":
                    r["cexists"] = v.strip() == "1"
                elif k == "CREAD":
                    r["cread"] = v.strip() == "1"
                elif k == "CLS":
                    r["cls"] = v
                elif k == "CDIR":
                    d["dir"].append(v)
                elif k == "PROBE":
                    d["probe"].append(v)
                elif k == "PROBERC":
                    r["probe_rc"] = None if v.strip() == "-" else int(v.strip() or 0)
                elif k == "LOG":
                    d["logs"].append(v)
                elif k == "FFLOG":
                    d["fflog"] = v
                elif k == "FF":
                    d["ffmpeg"].append(v)
            chk("Datoteka u Jellyfin kontejneru", r["cexists"], path)
            if r["cexists"]:
                chk("Jellyfin je može čitati", r["cread"], r["cls"])
                if r["probe_rc"] is not None:
                    bad = r["probe_rc"] != 0 or any(re.search(r"error|invalid|moov atom|could not|failed", x, re.I) for x in d["probe"])
                    chk("ffprobe (čitanje sadržaja)", not bad, " · ".join(d["probe"][:4])[:300] or "u redu")
                    r["probe_bad"] = bad
        except SSHError as e:
            chk("Jellyfin računalo (SSH)", False, str(e)[:200])
    elif path:
        chk("Jellyfin računalo (SSH)", None, "nije podešeno - bez provjere unutar kontejnera i logova")

    # 6) zaključak
    def fix(kind, text):
        fixes.append({"kind": kind, "text": text})
    if virtual:
        if cand:
            concl.append(("bad", "Jellyfin ovu epizodu ima samo kao zapis bez datoteke, a datoteka postoji: "
                                 f"{cand}. Zato javlja „Unable to find a valid media source”."))
            fix("notify", "Javi Jellyfinu da datoteka postoji (pa osvježi seriju)")
        else:
            concl.append(("bad", "Jellyfin ovu epizodu ima samo kao zapis bez datoteke, a datoteka ne postoji ni na NAS-u. "
                                 "To je epizoda koja nedostaje (prikazuje se jer je uključen prikaz epizoda koje nedostaju)."))
    elif r["cexists"] is False or (r["cexists"] is None and not local_exists and not plugin):
        if local_exists and r["cexists"] is False:
            concl.append(("bad", "JellyClean vidi datoteku na NAS-u, ali je Jellyfin kontejner na toj putanji NE vidi. "
                                 "Vjerojatno je problem u mountu (Docker volumen) ili mapiranju putanje biblioteke."))
        else:
            concl.append(("bad", "Datoteka ne postoji na putanji koju Jellyfin ima zapisanu u bazi."))
        if cand:
            concl.append(("info", f"Datoteka je pronađena ovdje: {cand} (za Jellyfin: {unmap_to_jf(cand)})."))
            fix("pathcheck", "Ispravi putanju u kartici Provjera putanja (čuva gledanost)")
            fix("notify", "Ili javi Jellyfinu novu datoteku (može dobiti novi ID)")
        if d["dir"]:
            concl.append(("info", "Sadržaj mape u kontejneru prikazan je ispod: provjeri razlikuje li se ime (velika i mala slova, posebni znakovi)."))
    elif r["cread"] is False:
        concl.append(("bad", f"Jellyfin vidi datoteku, ali je ne može čitati (dozvole): {r['cls']}. "
                             "Provjeri vlasnika i dozvole (PUID/PGID kontejnera)."))
    elif r.get("probe_bad"):
        concl.append(("bad", "ffprobe ne može ispravno pročitati datoteku: vjerojatno je oštećena, nepotpuna "
                             "(npr. prekinuto preuzimanje) ili je format neispravan. Pogledaj ispis ffprobe ispod."))
    elif pi_err:
        concl.append(("bad", f"Jellyfin odbija reprodukciju: {pi_err} - {PLAYBACK_ERR.get(pi_err, 'nepoznata greška')}."))
    elif not ms or pi_sources == 0:
        concl.append(("bad", "Datoteka je u redu, ali Jellyfin za nju nema izvor u bazi. Najčešće pomaže osvježavanje stavke."))
        fix("refresh", "Osvježi stavku (ponovno očitaj datoteku)")
    elif any(re.search(r"error|failed|invalid|exited with code", x, re.I) for x in d["ffmpeg"]):
        concl.append(("bad", f"Zadnje transkodiranje ove datoteke nije uspjelo (FFmpeg log {d['fflog']}). "
                             "Uzrok je u ispisu ispod; najčešće je to hardversko kodiranje ili kodek koji ffmpeg ne podržava."))
    elif d["logs"]:
        concl.append(("warn", "Na poslužitelju nije pronađen očit problem, ali Jellyfin log ima upozorenja za ovu stavku (ispod)."))
    else:
        concl.append(("ok", "Na poslužitelju nije pronađen problem: datoteka postoji, čitljiva je i Jellyfin nudi izvor. "
                            "Greška je vjerojatno do klijenta (format ili kodek koji uređaj ne podržava) ili do mreže. "
                            "Pokušaj pustiti ponovno pa odmah pokreni dijagnozu - tada će se greška vidjeti u logu."))
    if plugin and not virtual:
        concl.append(("info", f"Stavka je iz plugin biblioteke „{plugin['name']}” ({plugin['plugin']}), pa je datoteka zapravo symlink na izvornu datoteku."))
    if not d["ssh"] and not virtual:
        concl.append(("info", "Za provjeru unutar Jellyfin kontejnera, ffprobe i logove podesi SSH pristup (kartica Jellyfin config)."))
    if not any(f["kind"] == "refresh" for f in fixes) and not virtual:
        fix("refresh", "Osvježi stavku u Jellyfinu")
    d["conclusions"] = [{"level": a, "text": b} for a, b in concl]
    return d


def diag_fix(iid, kind, cand_jf=None):
    if not HEX32.match(iid or ""):
        raise SSHError("Neispravan ID")
    if kind == "refresh":
        JF.req("POST", f"/Items/{iid}/Refresh", {"MetadataRefreshMode": "FullRefresh", "ImageRefreshMode": "Default",
                                                 "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"})
        return {"ok": True, "msg": "Jellyfin osvježava stavku - pričekaj malo pa pokušaj pustiti ponovno."}
    if kind == "notify":
        if not cand_jf or not cand_jf.startswith("/"):
            raise SSHError("Nema pronađene datoteke")
        JF.req("POST", "/Library/Media/Updated", body={"Updates": [{"Path": cand_jf, "UpdateType": "Created"}]})
        try:
            res = (JF.req("GET", "/Items", {"Ids": iid, "Recursive": "true"}) or {}).get("Items") or []
            sid = res[0].get("SeriesId") if res else None
            if sid:
                JF.req("POST", f"/Items/{sid}/Refresh", {"Recursive": "true", "MetadataRefreshMode": "Default",
                                                         "ImageRefreshMode": "Default"})
        except Exception:
            pass
        return {"ok": True, "msg": "Jellyfinu je javljena datoteka i serija se osvježava. Za minutu-dvije pokušaj ponovno."}
    raise SSHError("Nepoznata akcija")


# --------------------------------------------------------------------------
# Test paralelnog enkodiranja (NVIDIA NVENC u Jellyfin okruženju)
# --------------------------------------------------------------------------

class EncTestCancelled(Exception):
    pass


class EncTest:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.cancel_ev = threading.Event()
        self.state = None
        self.path = os.path.join(CFG["data_dir"], "enctest.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:
            self.state = None

    def _save(self):
        try:
            os.makedirs(CFG["data_dir"], exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def item_info(iid):
        res = JF.req("GET", "/Items", {"Ids": iid, "Recursive": "true", "Fields": "Path,MediaStreams,MediaSources",
                                       "EnableImages": "false"}) or {}
        it = (res.get("Items") or [None])[0]
        if not it:
            raise SSHError("Stavka nije pronađena")
        ms = (it.get("MediaSources") or [{}])[0]
        streams = it.get("MediaStreams") or ms.get("MediaStreams") or []
        v = next((x for x in streams if x.get("Type") == "Video"), {})
        a = next((x for x in streams if x.get("Type") == "Audio"), {})
        lab = it.get("Name") or "?"
        if it.get("Type") == "Episode":
            lab = f"{it.get('SeriesName') or '?'} S{(it.get('ParentIndexNumber') or 0):02d}E{(it.get('IndexNumber') or 0):02d} · {lab}"
        return {"id": iid, "label": lab, "path": it.get("Path"),
                "video": " ".join(str(x) for x in [v.get("Codec", "").upper(), f"{v.get('Width')}x{v.get('Height')}" if v.get("Width") else "",
                                                    f"{v.get('BitDepth')}-bit" if v.get("BitDepth") else "", v.get("VideoRange") or ""] if x),
                "audio": " ".join(str(x) for x in [a.get("Codec", "").upper(), a.get("ChannelLayout") or ""] if x),
                "bitrate": ms.get("Bitrate"), "container": ms.get("Container")}

    @staticmethod
    def active_transcodes():
        try:
            ses = JF.req("GET", "/Sessions") or []
        except Exception:
            return None
        out = []
        for s in ses:
            if s.get("NowPlayingItem") and s.get("TranscodingInfo"):
                out.append(f"{s.get('UserName') or '?'} · {s['NowPlayingItem'].get('Name') or '?'} ({s.get('DeviceName') or s.get('Client') or ''})")
        return out

    def start(self, iid, max_n, dur, mode, target):
        if not JFC.enabled():
            raise SSHError("Test treba SSH pristup Jellyfin računalu (kartica Jellyfin config)")
        if not JF.enabled():
            raise SSHError("Test treba Jellyfin API")
        max_n = max(1, min(int(max_n or 10), 16))
        dur = max(10, min(int(dur or 30), 120))
        mode = "realtime" if mode == "realtime" else "max"
        target = target if target in ("1080", "720") else "1080"
        info = self.item_info(iid)
        if not info["path"]:
            raise SSHError("Stavka nema datoteku")
        with self.lock:
            if self.running:
                raise SSHError("Test je već u tijeku")
            self.running = True
            self.cancel_ev.clear()
            self.state = {"status": "running", "started": now_iso(), "finished": None, "file": info,
                          "params": {"max": max_n, "dur": dur, "mode": mode, "target": target},
                          "gpu": None, "steps": [], "current": 0, "error": None, "conclusion": None}
        threading.Thread(target=self._run, daemon=True).start()
        return self.state

    def cancel(self):
        if self.running:
            self.cancel_ev.set()
            threading.Thread(target=lambda: JFC.run("enckill", timeout=60), daemon=True).start()
        return {"cancelling": self.running}

    def _run(self):
        st = self.state
        p = st["params"]
        try:
            for n in range(1, p["max"] + 1):
                if self.cancel_ev.is_set():
                    raise EncTestCancelled()
                st["current"] = n
                timeout = (p["dur"] * 3 if p["mode"] == "realtime" else p["dur"] * 2) + 120
                out = JFC.run("enctest", (n, p["dur"], p["mode"], p["target"], st["file"]["path"]), timeout=timeout)
                if self.cancel_ev.is_set():
                    raise EncTestCancelled()
                speeds, fails, errs, util = [], [], [], []
                for line in out.splitlines():
                    k, _, v = line.partition(" ")
                    if k == "GPU" and not st["gpu"]:
                        st["gpu"] = v.strip()
                    elif k == "RES":
                        q = v.split(" ", 3)
                        try:
                            speeds.append(float(q[1]))
                        except (ValueError, IndexError):
                            pass
                        if len(q) > 3 and q[3].strip():
                            errs.append(q[3].strip())
                    elif k == "FAIL":
                        fails.append(v.split(" ", 1)[1] if " " in v else v)
                    elif k == "UTIL":
                        try:
                            g, e, d, m = [float(x.strip()) for x in v.split(",")[:4]]
                            util.append((g, e, d, m))
                        except ValueError:
                            pass
                sess_err = any(re.search(r"OpenEncodeSessionEx|incompatible client key|out of memory", x, re.I) for x in errs + fails)
                step = {"n": n, "min": min(speeds) if speeds else 0.0, "avg": round(sum(speeds) / len(speeds), 2) if speeds else 0.0,
                        "ok_streams": len(speeds), "failed": max(0, n - len(speeds)),
                        "enc_max": max((u[1] for u in util), default=None), "dec_max": max((u[2] for u in util), default=None),
                        "gpu_max": max((u[0] for u in util), default=None), "mem_max": max((u[3] for u in util), default=None),
                        "enc_avg": round(sum(u[1] for u in util) / len(util)) if util else None,
                        "session_limit": sess_err, "errors": (errs + fails)[:3]}
                st["steps"].append(step)
                self._save()
                thr = 0.97 if p["mode"] == "realtime" else 1.0
                if sess_err or step["failed"] or step["min"] < thr:
                    break
            st["status"] = "done"
        except EncTestCancelled:
            st["status"] = "cancelled"
        except Exception as e:
            st["status"] = "error"
            st["error"] = str(e)
            log("Test enkodiranja:", e)
        finally:
            st["finished"] = now_iso()
            st["conclusion"] = self._conclude(st)
            self.running = False
            self._save()

    @staticmethod
    def _conclude(st):
        steps, p = st["steps"], st["params"]
        if not steps:
            return None
        rt = p["mode"] == "realtime"
        thr = 0.97 if rt else 1.0
        good = [s for s in steps if not s["session_limit"] and not s["failed"] and s["min"] >= thr]
        capable = max((s["n"] for s in good), default=0)
        if rt:
            rec = max((s["n"] for s in good if (s["enc_max"] or 0) <= 85 and (s["dec_max"] or 0) <= 85), default=0)
        else:
            rec = max((s["n"] for s in good if s["min"] >= 1.2), default=0)
        last = steps[-1]
        if last["session_limit"]:
            reason = f"ograničenje broja sesija u NVIDIA driveru (kod {last['n']} istovremenih tokova)"
        elif last["failed"]:
            reason = f"ffmpeg nije uspio pokrenuti sve tokove kod {last['n']} istovremenih"
        elif last["min"] < thr:
            reason = (f"snaga kartice: kod {last['n']} tokova najsporiji ide {last['min']}x"
                      + (f" (ENC {last['enc_max']:.0f}%, DEC {last['dec_max']:.0f}%)" if last['enc_max'] is not None else ""))
        elif st["status"] == "cancelled":
            reason = "test je prekinut"
        else:
            reason = f"dosegnut zadani najveći broj tokova ({p['max']}) - kartica može i više"
        return {"capable": capable, "recommended": rec, "reason": reason,
                "more_possible": st["status"] == "done" and capable == p["max"]}

    def get(self):
        return {"running": self.running, "state": self.state, "ssh": JFC.enabled(), "jellyfin": JF.enabled(),
                "active": self.active_transcodes() if JF.enabled() else None}


ENC = EncTest()


# --------------------------------------------------------------------------
# NVENC patch: uklanjanje ograničenja istovremenih NVENC sesija (keylase/nvidia-patch)
# --------------------------------------------------------------------------

class NvPatch:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = None
        self.path = os.path.join(CFG["data_dir"], "nvpatch.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:
            self.state = {}

    def _save(self):
        try:
            os.makedirs(CFG["data_dir"], exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except Exception:
            pass

    # -- skripta --
    def script(self):
        """Vrati sadržaj patch.sh (bytes) provjerene verzije: iz lokalne kopije ili s GitHuba."""
        local = os.path.join(CFG["data_dir"], f"nvidia-patch-{NVPATCH_COMMIT[:12]}.sh")
        manual = os.path.join(CFG["data_dir"], "nvidia-patch.sh")
        for f in (local, manual):
            try:
                with open(f, "rb") as fh:
                    b = fh.read()
                if hashlib.sha256(b).hexdigest() in NVPATCH_SHA256:
                    return b
            except Exception:
                pass
        if time.time() - getattr(self, "_dl_fail_t", 0) < 300:
            raise SSHError(self._dl_fail_msg)
        errs = []
        for url in NVPATCH_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "JellyClean/" + APP_VERSION})
                with urllib.request.urlopen(req, timeout=30) as r:
                    b = r.read(2_000_000)
                h = hashlib.sha256(b).hexdigest()
                if h not in NVPATCH_SHA256:
                    errs.append(f"{url}: neočekivan sha256 {h[:16]}…")
                    continue
                try:
                    os.makedirs(CFG["data_dir"], exist_ok=True)
                    with open(local, "wb") as fh:
                        fh.write(b)
                except Exception:
                    pass
                return b
            except Exception as e:
                errs.append(f"{url}: {e}")
        self._dl_fail_t = time.time()
        self._dl_fail_msg = ("Skripta za patch se ne može preuzeti s GitHuba (" + "; ".join(errs)[:400] +
                             f"). Možeš je ručno spremiti u {manual} (keylase/nvidia-patch, commit {NVPATCH_COMMIT[:12]}).")
        raise SSHError(self._dl_fail_msg)

    def supported(self):
        try:
            txt = self.script().decode("utf-8", "replace")
        except SSHError as e:
            return None, str(e)
        return set(re.findall(r'^\s*\["([0-9][0-9.]*)"\]=', txt, re.M)), None

    @staticmethod
    def _helper_err(e):
        m = str(e)
        if "nepoznata operacija" in m:
            return SSHError("Pomoćni program na Jellyfin računalu je stariji od verzije " + str(HELPER_VERSION) +
                            " i ne zna za NVENC patch. Na Proxmox hostu pokreni update-lxc.sh (jednom traži sudo lozinku).")
        return e

    # -- stanje --
    def status(self):
        if not JFC.enabled():
            return {"enabled": False, "reason": "Treba SSH pristup Jellyfin računalu (kartica Jellyfin config)."}
        try:
            out = JFC.run("nvstatus", timeout=60)
        except SSHError as e:
            return {"enabled": True, "error": str(self._helper_err(e)), "state": self.state}
        r = {"enabled": True, "gpu": None, "driver": None, "lib": None, "backup": False, "patched": False,
             "old_backups": [], "ctl": None, "bash": True, "state": self.state, "commit": NVPATCH_COMMIT,
             "verify_n": NVPATCH_VERIFY_N, "busy": self.busy}
        for line in out.splitlines():
            k, _, v = line.partition(" ")
            if k == "NVNONE":
                r["none"] = True
            elif k == "NVGPU":
                r["gpu"] = v.strip()
            elif k == "NVDRV":
                r["driver"] = v.strip()
            elif k == "NVLIB":
                r["lib"] = None if v.strip() == "-" else v.strip()
            elif k == "NVBAK":
                r["backup"] = v.strip() == "1"
            elif k == "NVPATCHED":
                r["patched"] = v.strip() == "1"
            elif k == "NVOLD":
                r["old_backups"].append(v.strip())
            elif k == "NVBASH":
                r["bash"] = v.strip() == "1"
            elif k == "CTL":
                r["ctl"] = v.strip()
        if r.get("none"):
            return r
        sup, serr = self.supported()
        r["supported"] = None if sup is None else (r["driver"] in sup)
        r["supported_error"] = serr
        if sup:
            maj = (r["driver"] or "").split(".")[0]
            r["supported_same_branch"] = sorted((v for v in sup if v.split(".")[0] == maj),
                                                key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])[-6:]
        drv = r["driver"]
        prev = self.state.get("patched_driver")
        others = [b.rsplit(".so.", 1)[-1] for b in r["old_backups"] if not b.endswith(".so." + (drv or ""))]
        # patch je nestao: prije je bio primijenjen (JellyClean pamti ili postoje kopije za stare drivere)
        r["lost"] = (not r["patched"]) and bool((prev and prev != drv) or
                                                (others and not r["backup"] and "rolled_back_at" not in self.state))
        r["lost_from"] = prev if (prev and prev != drv) else (others[-1] if others else None)
        if r["patched"] and prev != drv:
            self.state.pop("rolled_back_at", None)
            self.state["patched_driver"] = drv
            self._save()
        return r

    # -- radnje --
    def _guard(self, what):
        if ENC.running:
            raise SSHError("Test enkodiranja je u tijeku - pričekaj da završi")
        with self.lock:
            if self.busy:
                raise SSHError(f"Već je u tijeku: {self.busy}")
            self.busy = what

    def apply(self, action):
        if action not in ("patch", "rollback"):
            raise SSHError("Nepoznata radnja")
        if not JFC.enabled():
            raise SSHError("Treba SSH pristup Jellyfin računalu")
        script = self.script()
        if action == "patch":
            pre = self.status()
            if pre.get("error"):
                raise SSHError(pre["error"])
            if pre.get("none"):
                raise SSHError("Na Jellyfin računalu nije pronađen NVIDIA driver")
            if pre.get("supported") is False:
                raise SSHError(f"Za driver {pre.get('driver')} patch ne postoji - ništa nije mijenjano")
            if not pre.get("lib"):
                raise SSHError("Biblioteka libnvidia-encode nije pronađena - ništa nije mijenjano")
            if pre.get("patched"):
                return {"ok": True, "message": "Driver je već patchan - ništa nije mijenjano.", "log": "", "rc": 0,
                        "status": pre}
        self._guard("primjena patcha" if action == "patch" else "vraćanje originala")
        try:
            b64 = base64.b64encode(script).decode()
            lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
            try:
                out = JFC.run("nvpatch", (action,), lines=lines, timeout=240)
            except SSHError as e:
                raise self._helper_err(e)
            log_lines, rc, info = [], None, {}
            for line in out.splitlines():
                k, _, v = line.partition(" ")
                if k == "OUT":
                    log_lines.append(v)
                elif k == "RC":
                    rc = int(v) if v.strip().lstrip("-").isdigit() else -1
                elif k in ("STOPPED", "STARTED"):
                    info[k.lower()] = v.strip()
                elif k == "WARN":
                    info.setdefault("warnings", []).append(v.strip())
            ok = rc == 0
            text = "\n".join(log_lines)
            if ok and action == "patch" and "Skipping patch" in text:
                msg = "Driver je već patchan (datoteka se razlikuje od sigurnosne kopije) - ništa nije mijenjano."
            elif ok and action == "patch":
                msg = "Patch je primijenjen."
            elif ok:
                msg = "Vraćena je originalna biblioteka drivera."
            elif "not found" in text and "Patch for this" in text:
                msg = "Za ovu verziju drivera patch (još) ne postoji."
            elif "Backup not found" in text:
                msg = "Sigurnosna kopija nije pronađena - driver nije bio patchan ovim alatom."
            elif "Could not find bytes" in text:
                msg = "Biblioteka drivera ne sadrži očekivani kod - ništa nije mijenjano."
            elif "cannot detect driver directory" in text:
                msg = "Biblioteka drivera (libnvidia-encode) nije pronađena na standardnim mjestima."
            else:
                msg = "Patch skripta je javila grešku."
            st = self.status()
            if ok and action == "patch" and st.get("patched"):
                self.state["patched_driver"] = st.get("driver")
                self.state["patched_at"] = now_iso()
                self.state.pop("rolled_back_at", None)
            if ok and action == "rollback":
                self.state.pop("patched_driver", None)
                self.state["rolled_back_at"] = now_iso()
            self.state["last_action"] = {"action": action, "ok": ok, "time": now_iso(), "message": msg,
                                         "driver": st.get("driver")}
            self._save()
            log(f"NVENC patch ({action}):", msg)
            return {"ok": ok, "message": msg, "log": text[-4000:], "rc": rc, **info, "status": st}
        finally:
            self.busy = None

    def verify(self):
        if not JFC.enabled():
            raise SSHError("Treba SSH pristup Jellyfin računalu")
        self._guard("provjera ograničenja")
        try:
            try:
                out = JFC.run("nvverify", (NVPATCH_VERIFY_N,), timeout=120)
            except SSHError as e:
                raise self._helper_err(e)
            okn, fails = 0, []
            for line in out.splitlines():
                k, _, v = line.partition(" ")
                if k == "VOK":
                    okn += 1
                elif k == "VFAIL":
                    fails.append(v.split(" ", 1)[1] if " " in v else "")
            limit_err = any(re.search(r"OpenEncodeSessionEx|incompatible client key|session", f, re.I) for f in fails)
            drv = None
            try:
                drv = (re.search(r"^NVDRV (\S+)", JFC.run("nvstatus", timeout=60), re.M) or [None, None])[1]
            except Exception:
                pass
            res = {"n": NVPATCH_VERIFY_N, "ok": okn, "failed": len(fails), "errors": fails[:3],
                   "limit_hit": limit_err, "time": now_iso(), "driver": drv}
            if okn == NVPATCH_VERIFY_N:
                res["message"] = f"Svih {okn} istovremenih NVENC sesija je uspjelo - ograničenje drivera ne smeta."
            elif limit_err:
                res["message"] = (f"Uspjelo je {okn} od {NVPATCH_VERIFY_N} sesija; ostale je odbio driver "
                                  "(ograničenje broja sesija).")
            else:
                res["message"] = f"Uspjelo je {okn} od {NVPATCH_VERIFY_N} sesija (greška nije ograničenje drivera)."
            self.state["verify"] = res
            self._save()
            return res
        finally:
            self.busy = None


NVP = NvPatch()


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
    exts = None
    if ext:
        exts = set()
        for e in ext.split(","):
            e = e.strip().lower()
            if e in ("(bez)", "-"):
                exts.add("")          # datoteke bez ekstenzije
            elif e:
                exts.add(e if e.startswith(".") else "." + e)
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
            "plugin_libs": JF.plugin_libs, "plugin_skipped": JF.plugin_skipped,
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
            if u.path == "/api/paths":
                return self._send(200, {"enabled": JF.enabled(), "running": PC.running, "error": PC.error,
                                        "cancelled": PC.cancelled, "progress": PC.progress,
                                        "result": PC.result, "allow_delete": CFG["allow_delete"],
                                        "ssh": JFC.enabled()})
            if u.path == "/api/diag/search":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin API nije podešen"})
                return self._send(200, {"items": diag_search(q.get("q", "")[:100])})
            if u.path == "/api/diag/episodes":
                return self._send(200, {"items": diag_episodes(re.sub(r"[^0-9a-fA-F-]", "", q.get("series", "")))})
            if u.path == "/api/enctest":
                return self._send(200, ENC.get())
            if u.path == "/api/nvpatch":
                return self._send(200, NVP.status())
            if u.path == "/api/enctest/item":
                return self._send(200, EncTest.item_info(re.sub(r"[^0-9a-fA-F]", "", q.get("id", ""))))
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
            if u.path == "/api/paths/check":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin API nije podešen"})
                if SCAN.running:
                    return self._send(409, {"error": "Skeniranje diska je u tijeku - pričekaj da završi"})
                threading.Thread(target=PC.run, daemon=True).start()
                return self._send(200, {"started": True})
            if u.path == "/api/diag":
                return self._send(200, diagnose((body.get("id") or "").replace("-", "").lower()))
            if u.path == "/api/diag/fix":
                return self._send(200, diag_fix((body.get("id") or "").replace("-", "").lower(), body.get("kind"),
                                                body.get("candidate_jf")))
            if u.path == "/api/enctest/start":
                return self._send(200, ENC.start((body.get("id") or "").replace("-", "").lower(), body.get("max"),
                                                 body.get("dur"), body.get("mode"), str(body.get("target") or "1080")))
            if u.path == "/api/enctest/cancel":
                return self._send(200, ENC.cancel())
            if u.path == "/api/nvpatch/apply":
                if not CFG["allow_delete"]:
                    return self._send(403, {"error": "Izmjene su isključene (ALLOW_DELETE=false)"})
                return self._send(200, NVP.apply(body.get("action")))
            if u.path == "/api/nvpatch/verify":
                return self._send(200, NVP.verify())
            if u.path == "/api/paths/cancel":
                return self._send(200, PC.cancel())
            if u.path == "/api/paths/notify":
                return self._send(200, PC.notify(body.get("ids") or []))
            if u.path == "/api/paths/dbfix":
                dry = bool(body.get("dry_run", True))
                if not dry and not CFG["allow_delete"]:
                    return self._send(403, {"error": "Izmjene su isključene (ALLOW_DELETE=false)"})
                return self._send(200, PC.dbfix(body.get("ids") or [], dry))
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
