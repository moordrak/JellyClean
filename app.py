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

APP_VERSION = "2.3.1"
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


_GCFG = {
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
if not _GCFG["ssh_key"]:
    _GCFG["ssh_key"] = os.path.join(_GCFG["data_dir"], "ssh", "id_ed25519")

# ---- više Jellyfin servera ----
# Ključevi koji pripadaju pojedinom Jellyfin serveru. Sve ostalo (mediji, kanta, prijava...) je zajedničko.
SRV_KEYS = ("jf_url", "jf_key", "jf_users", "path_map", "verify_tls", "ssh_host", "ssh_user", "ssh_port",
            "ssh_sudo", "ssh_helper", "jf_config", "jf_cache", "jf_log", "jf_data", "jf_metadata")
_CUR = threading.local()


def srv_normalize(c):
    c["jf_url"] = (c.get("jf_url") or "").strip().rstrip("/")
    c["jf_key"] = (c.get("jf_key") or "").strip()
    for k in ("jf_config", "jf_cache", "jf_log", "jf_data", "jf_metadata"):
        c[k] = (c.get(k) or "").strip().rstrip("/")
    if c["jf_config"]:
        c["jf_cache"] = c["jf_cache"] or c["jf_config"] + "/cache"
        c["jf_log"] = c["jf_log"] or c["jf_config"] + "/log"
        c["jf_data"] = c["jf_data"] or c["jf_config"] + "/data"
    if c["jf_data"]:
        c["jf_metadata"] = c["jf_metadata"] or c["jf_data"] + "/metadata"
    c["ssh_port"] = str(c.get("ssh_port") or "22")
    c["ssh_user"] = (c.get("ssh_user") or "root").strip()
    c["ssh_host"] = (c.get("ssh_host") or "").strip()
    c["ssh_helper"] = (c.get("ssh_helper") or "").strip()
    c["ssh_sudo"] = bool(c.get("ssh_sudo"))
    c["verify_tls"] = bool(c.get("verify_tls", True))
    c["jf_users"] = [u.strip().lower() for u in (c.get("jf_users") or []) if str(u).strip()]
    c["path_map"] = [tuple(x) for x in (c.get("path_map") or [])]
    return c


ENV_SRV = srv_normalize({k: _GCFG.pop(k) for k in SRV_KEYS})


class _CfgView(dict):
    """CFG: ključevi servera čitaju se iz servera koji obrađuje trenutni zahtjev ili dretvu."""
    def __getitem__(self, k):
        if k in SRV_KEYS:
            return cur_server().cfg[k]
        return dict.__getitem__(self, k)

    def __setitem__(self, k, v):
        if k in SRV_KEYS:
            cur_server().cfg[k] = v
        else:
            dict.__setitem__(self, k, v)

    def get(self, k, d=None):
        try:
            return self[k]
        except KeyError:
            return d


CFG = _CfgView(_GCFG)


def cur_server():
    s = getattr(_CUR, "srv", None)
    return s if s is not None else SERVERS.default()


class use_server:
    def __init__(self, srv):
        self.srv = srv

    def __enter__(self):
        self.prev = getattr(_CUR, "srv", None)
        _CUR.srv = self.srv
        return self.srv

    def __exit__(self, *a):
        _CUR.srv = self.prev


class CtxThread(threading.Thread):
    """Dretva koja nasljeđuje server iz kojeg je pokrenuta."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._jc_srv = getattr(_CUR, "srv", None)

    def run(self):
        _CUR.srv = self._jc_srv
        super().run()


class _SrvProxy:
    def __init__(self, attr):
        object.__setattr__(self, "_attr", attr)

    def __getattr__(self, n):
        return getattr(getattr(cur_server(), self._attr), n)

    def __setattr__(self, n, v):
        setattr(getattr(cur_server(), self._attr), n, v)

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
    CtxThread(target=run_scan, daemon=True).start()
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
        refresh_all_libraries()
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
            refresh_all_libraries()
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


JF = _SrvProxy("jf")


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

import secrets
import shlex
import subprocess

HEX32 = re.compile(r"^[0-9a-f]{32}$")


HELPER_VERSION = 11

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
  n=${1:-1}; dur=${2:-30}; mode=${3:-max}; h=${4:-1080}; p=${5:-}; codec=${6:-h264}
  case "$n$dur$h" in *[!0-9]*|"") echo "ERR neispravni parametri"; return ;; esac
  [ "$n" -ge 1 ] && [ "$n" -le 32 ] || { echo "ERR broj tokova 1-32"; return; }
  [ "$dur" -ge 5 ] && [ "$dur" -le 300 ] || { echo "ERR trajanje 5-300 s"; return; }
  case "$codec" in
    h264) enc=h264_nvenc
      case "$h" in 2160) br=25M ;; 1440) br=14M ;; 1080) br=8M ;; 720) br=4M ;; 480) br=1500k ;; *) echo "ERR cilj"; return ;; esac ;;
    hevc) enc=hevc_nvenc
      case "$h" in 2160) br=15M ;; 1440) br=9M ;; 1080) br=5M ;; 720) br=2500k ;; 480) br=1M ;; *) echo "ERR cilj"; return ;; esac ;;
    *) echo "ERR kodek"; return ;;
  esac
  case "$p" in /*) ;; *) echo "ERR neispravna putanja"; return ;; esac
  case "$p" in *..*) echo "ERR neispravna putanja"; return ;; esac
  re=""; [ "$mode" = realtime ] && re="-re"
  jf_find_ctl; jf_exec_prefix
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  $EX nvidia-smi -L >/dev/null 2>&1 || { echo "ERR nvidia-smi nije dostupan u Jellyfin okruženju - NVIDIA kartica nije proslijeđena Jellyfinu (ili nema drivera)"; return; }
  echo "GPU $($EX nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)"
  echo "ENC $enc $br"
  $EX test -f "$p" || { echo "ERR Jellyfin ne vidi testnu datoteku: $p"; return; }
  $EX sh -c '
    FF=/usr/lib/jellyfin-ffmpeg/ffmpeg; [ -x "$FF" ] || FF=$(command -v ffmpeg)
    [ -n "$FF" ] || { echo "ERR u Jellyfin okruženju nema ffmpega"; exit 0; }
    n=$1; dur=$2; re=$3; h=$4; br=$5; p=$6; enc=$7
    d=$(mktemp -d); pids=""
    i=1
    while [ $i -le $n ]; do
      "$FF" -hide_banner -nostdin -y -hwaccel cuda -hwaccel_output_format cuda -t "$dur" $re -i "$p" \
        -vf "scale_cuda=-2:$h:format=yuv420p" -c:v "$enc" -preset p4 -b:v "$br" -c:a aac -b:a 192k \
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
  ' _ "$n" "$dur" "$re" "$h" "$br" "$p" "$enc"
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

# ---- nadzor kartice uživo (isti NVML podaci koje prikazuje nvtop) ----
op_gpumon() {
  secs=${1:-300}
  case "$secs" in ''|*[!0-9]*) echo "ERR neispravno trajanje"; return ;; esac
  [ "$secs" -ge 5 ] && [ "$secs" -le 1800 ] || { echo "ERR trajanje 5-1800 s"; return; }
  if command -v nvidia-smi >/dev/null 2>&1; then NS="nvidia-smi"; PSX=""
  else jf_find_ctl; jf_exec_prefix; NS="$EX nvidia-smi"; PSX="$EX"; fi
  $NS -L >/dev/null 2>&1 || { echo "ERR nvidia-smi nije dostupan na Jellyfin računalu"; return; }
  echo "NAME $($NS --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)"
  pm=1
  end=$(( $(date +%s) + secs ))
  while [ "$(date +%s)" -lt "$end" ]; do
    t0=$(date +%s)
    echo "G $t0,$($NS --query-gpu=utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed,clocks.gr,clocks.mem,pstate --format=csv,noheader,nounits 2>/dev/null | head -1)" || return
    pids=""
    if [ "$pm" = 1 ]; then
      pmo=$($NS pmon -c 1 -s um 2>/dev/null)
      if [ -n "$pmo" ]; then
        printf '%s\n' "$pmo" | awk '/^# *gpu/ {sub(/^# */,""); print "PH " $0; next} /^#/ {next} NF>3 && $2 ~ /^[0-9]+$/ {print "P " $0}'
        pids=$(printf '%s\n' "$pmo" | awk '!/^#/ && $2 ~ /^[0-9]+$/ {print $2}')
      else pm=0; fi
    fi
    apps=$($NS --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null)
    [ -n "$apps" ] && printf '%s\n' "$apps" | sed 's/^/M /'
    pids="$pids $(printf '%s\n' "$apps" | cut -d, -f1)"
    for x in $(printf '%s\n' $pids | sort -u); do
      case "$x" in ''|*[!0-9]*) continue ;; esac
      if [ -z "$PSX" ] && [ -r "/proc/$x/cmdline" ]; then
        echo "C $x $(tr '\000\n' '\037 ' < "/proc/$x/cmdline" | cut -c1-600)"
      else
        echo "C $x $($PSX ps -o args= -p "$x" 2>/dev/null | head -1 | cut -c1-400)"
      fi
    done
    echo "T"
    # najviše jedan uzorak u sekundi (pmon na nekim driverima vrati odmah)
    while [ "$(date +%s)" -le "$t0" ]; do sleep 0.2 2>/dev/null || sleep 1; done
  done
  echo "END"
}

# ---- sigurnosna kopija i vraćanje (puni backup: baza, postavke, pluginovi, statistika gledanja) ----
# Arhiva: config/ = mapa sa system.xml, data/ = Jellyfin podatkovna mapa (data/jellyfin.db,
# playback_reporting.db, plugins, root ...). Cache, logovi i transkodiranje se ne spremaju.
jf_confdir() {
  JCONF=""
  for d in "$C" "$C/config" "$DATA/config" /etc/jellyfin; do
    if [ -f "$d/system.xml" ]; then JCONF=$d; return 0; fi
  done
  return 0
}
jf_under() { case "$2" in "$1"/*) printf '%s' "${2#"$1"/}" ;; *) printf '' ;; esac; }
op_bkinfo() {
  jf_find_ctl; jf_confdir; jf_db
  echo "CTL ${JCTL:-none} ${JNAME:--}"
  echo "CONF ${JCONF:--}"
  echo "DATAP $DATA"
  echo "DBSZ $(sz "$DB")"
  r=$(jf_under "$JCONF" "$DATA")
  if [ -n "$JCONF" ]; then
    if [ -n "$r" ]; then echo "SZ config $(du -sb --exclude="$(basename "$r")" --exclude=log --exclude=cache "$JCONF" 2>/dev/null | tail -1 | cut -f1)"
    else echo "SZ config $(du -sb --exclude=log --exclude=cache "$JCONF" 2>/dev/null | tail -1 | cut -f1)"; fi
  fi
  echo "SZ data $(du -sb --exclude=metadata --exclude=cache --exclude=transcodes --exclude=log --exclude=config "$DATA" 2>/dev/null | tail -1 | cut -f1)"
  echo "SZ metadata $(sz "$META")"
  for f in "$DATA/data/playback_reporting.db" "$DATA/playback_reporting.db"; do
    if [ -f "$f" ]; then echo "STATS $(sz "$f") $f"; break; fi
  done
  df -B1 -P "$DATA" 2>/dev/null | tail -1 | awk '{print "DF", $2, $3, $4}'
  echo "OWNER $(stat -c %u:%g "$DATA" 2>/dev/null)"
  if command -v tar >/dev/null 2>&1 && command -v gzip >/dev/null 2>&1; then echo "TAR 1"; else echo "TAR 0"; fi
  n=0; for d in "$DATA"/.jellyclean-restore-*; do [ -d "$d" ] && n=$((n + 1)); done; echo "PREVS $n"
}
op_backup() {
  stop=${1:-1}; meta=${2:-0}
  jf_find_ctl; jf_confdir; jf_db
  [ -n "$JCONF" ] || { echo "ERR ne nalazim Jellyfin postavke (system.xml) u $C ni /etc/jellyfin" >&2; exit 3; }
  [ -f "$DB" ] || { echo "ERR ne nalazim Jellyfin bazu ($DATA/data/jellyfin.db)" >&2; exit 3; }
  command -v tar >/dev/null 2>&1 && command -v gzip >/dev/null 2>&1 || { echo "ERR na Jellyfin računalu nema tar/gzip" >&2; exit 3; }
  S=$(mktemp -d) || { echo "ERR mktemp" >&2; exit 3; }
  ln -s "$DATA" "$S/data"; parts=data
  if [ "$JCONF" != "$DATA" ]; then ln -s "$JCONF" "$S/config"; parts="config data"; fi
  set -- --exclude=config/log --exclude=config/cache --exclude=config/transcodes \
         --exclude=data/log --exclude=data/cache --exclude=data/transcodes \
         --exclude='*.jellyclean-*.bak' --exclude='.jellyclean-restore-*'
  r=$(jf_under "$JCONF" "$DATA"); [ -n "$r" ] && set -- "$@" "--exclude=config/$r"
  r=$(jf_under "$DATA" "$JCONF"); [ -n "$r" ] && set -- "$@" "--exclude=data/$r"
  for x in "$CACHE" "$LOG"; do
    r=$(jf_under "$JCONF" "$x"); [ -n "$r" ] && set -- "$@" "--exclude=config/$r"
    r=$(jf_under "$DATA" "$x"); [ -n "$r" ] && set -- "$@" "--exclude=data/$r"
  done
  if [ "$meta" != 1 ]; then
    set -- "$@" --exclude=data/metadata
    r=$(jf_under "$DATA" "$META"); [ -n "$r" ] && set -- "$@" "--exclude=data/$r"
  fi
  stopped=0
  if [ "$stop" = 1 ] && [ -n "$JCTL" ]; then
    if jf_ctl stop; then stopped=1; echo "INFO STOPPED $JNAME" >&2; sleep 3; else echo "WARN Jellyfin ($JNAME) se nije zaustavio - kopija se radi dok radi" >&2; fi
  fi
  echo "INFO TAR $JCONF $DATA" >&2
  # shellcheck disable=SC2086
  tar -C "$S" -czhf - "$@" $parts 2>"$S.err"; rc=$?
  if [ $stopped = 1 ]; then
    if jf_ctl start; then echo "INFO STARTED $JNAME" >&2; else echo "WARN Jellyfin ($JNAME) se nije ponovno pokrenuo - pokreni ga ručno" >&2; fi
  fi
  grep -v -e 'socket ignored' -e 'file changed as we read it' -e 'Removing leading' "$S.err" | head -5 | sed 's/^/WARN tar: /' >&2
  rm -rf "$S" "$S.err"
  [ $rc -le 1 ] || { echo "ERR tar je završio s kodom $rc" >&2; exit 4; }
  echo "INFO DONE" >&2
}
op_restore() {
  pairs=${1:-}; newid=${2:-0}
  jf_find_ctl; jf_confdir; jf_db
  [ -n "$JCTL" ] || { echo "ERR ne znam kako zaustaviti Jellyfin (nije pronađen Docker kontejner ni systemd servis)"; cat >/dev/null; return; }
  [ -n "$JCONF" ] || { echo "ERR ne nalazim Jellyfin postavke (system.xml) - pokreni Jellyfin barem jednom"; cat >/dev/null; return; }
  W="$DATA/.jellyclean-restore-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$W/new" || { echo "ERR ne mogu napraviti $W"; cat >/dev/null; return; }
  cat > "$W/in.tgz" || { echo "ERR prijenos arhive nije uspio (ima li mjesta na disku?)"; rm -rf "$W"; return; }
  echo "RECV $(sz "$W/in.tgz")"
  if ! tar -tzf "$W/in.tgz" > "$W/list" 2>/dev/null; then echo "ERR arhiva je oštećena"; rm -rf "$W"; return; fi
  if grep -Ev '^(\./)?(config|data)(/|$)' "$W/list" | grep -q .; then echo "ERR arhiva ima neočekivan sadržaj"; rm -rf "$W"; return; fi
  if grep -q -e '\.\./' -e '^/' "$W/list"; then echo "ERR arhiva ima nedopuštene putanje"; rm -rf "$W"; return; fi
  grep -Eq '^(\./)?data/(data/)?(jellyfin|library)\.db$' "$W/list" || { echo "ERR u arhivi nema Jellyfin baze"; rm -rf "$W"; return; }
  if ! tar -xzf "$W/in.tgz" -C "$W/new" 2>"$W/err"; then echo "ERR raspakiravanje nije uspjelo: $(head -c 300 "$W/err")"; rm -rf "$W"; return; fi
  rm -f "$W/in.tgz" "$W/list" "$W/err"
  jf_ctl stop || { echo "ERR zaustavljanje Jellyfina ($JNAME) nije uspjelo"; rm -rf "$W"; return; }
  echo "STOPPED $JNAME"
  sleep 3
  mkdir -p "$W/prev/config" "$W/prev/data"
  for pair in "config|$JCONF" "data|$DATA"; do
    k=${pair%%|*}; dst=${pair#*|}
    own=$(stat -c %u:%g "$dst")
    for e in "$W/new/$k"/* "$W/new/$k"/.[!.]*; do
      [ -e "$e" ] || continue
      b=$(basename "$e")
      case "$b" in .jellyclean-restore-*) continue ;; esac
      [ -e "$dst/$b" ] && mv "$dst/$b" "$W/prev/$k/$b"
      mv "$e" "$dst/$b" && chown -R "$own" "$dst/$b"
    done
  done
  n=0
  if [ -n "$pairs" ]; then
    printf '%s' "$pairs" | base64 -d 2>/dev/null > "$W/pairs"
    for f in "$JCONF"/*.xml "$DATA"/plugins/configurations/*.xml; do
      [ -f "$f" ] || continue
      while IFS='|' read -r from to; do
        case "$from$to" in *'#'*|'') continue ;; esac
        case "$from" in /*) ;; *) continue ;; esac
        sed -i "s#${from}\([/<\"]\)#${to}\1#g; s#${from}\$#${to}#" "$f"
      done < "$W/pairs"
      n=$((n + 1))
    done
    echo "REMAP $n"
  fi
  if [ "$newid" = 1 ]; then rm -f "$DATA/data/device.txt" "$DATA/device.txt"; echo "NEWID"; fi
  rm -rf "$W/new" "$W/pairs"
  echo "PREV $W/prev"
  if jf_ctl start; then echo "STARTED $JNAME"; else echo "WARN Jellyfin ($JNAME) se nije ponovno pokrenuo - pokreni ga ručno"; fi
  echo "DONE"
}
op_restorelist() { for d in "$DATA"/.jellyclean-restore-*; do [ -d "$d" ] && echo "PREVDIR $(sz "$d") $(basename "$d")"; done; echo "END"; }
op_restoreclean() {
  n=0
  for d in "$DATA"/.jellyclean-restore-*; do
    case "$d" in "$DATA"/.jellyclean-restore-2*) [ -d "$d" ] && rm -rf -- "$d" && n=$((n + 1)) ;; esac
  done
  echo "DEL $n"
}

# ---- putanje zapečene u ovom pomoćnom programu i otkrivanje Jellyfina ----
op_paths() {
  echo "P_CONFIG $C"; echo "P_CACHE $CACHE"; echo "P_LOG $LOG"; echo "P_DATA $DATA"; echo "P_META $META"
}
op_detect() {
  found=0
  if command -v docker >/dev/null 2>&1; then
    docker ps >/dev/null 2>&1 || echo "DOCKERPERM"
    for id in $(docker ps -a --format '{{.ID}} {{.Image}} {{.Names}}' 2>/dev/null | grep -i jellyfin | awk '{print $1}'); do
      name=$(docker inspect -f '{{.Name}}' "$id" | sed 's#^/##'); img=$(docker inspect -f '{{.Config.Image}}' "$id")
      cfg=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$id")
      cch=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/cache"}}{{.Source}}{{end}}{{end}}' "$id")
      [ -n "$cfg" ] || continue
      found=1
      echo "D_SRC docker:$name ($img)"
      echo "D_CONFIG $cfg"
      if [ -d "$cfg/data/metadata" ]; then echo "D_DATA $cfg/data"; elif [ -d "$cfg/metadata" ]; then echo "D_DATA $cfg"; else echo "D_DATA $cfg/data"; fi
      if [ -n "$cch" ]; then echo "D_CACHE $cch"; else echo "D_CACHE $cfg/cache"; fi
      echo "D_LOG $cfg/log"
      break
    done
  fi
  if [ $found = 0 ] && [ -d /var/lib/jellyfin ]; then
    echo "D_SRC paket (systemd)"; echo "D_CONFIG /var/lib/jellyfin"; echo "D_DATA /var/lib/jellyfin"
    echo "D_CACHE /var/cache/jellyfin"; echo "D_LOG /var/log/jellyfin"; found=1
  fi
  [ $found = 1 ] || echo "D_NONE"
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
    gpumon) op_gpumon "$@" ;;
    paths) op_paths ;;
    detect) op_detect ;;
    bkinfo) op_bkinfo ;;
    backup) op_backup "$@" ;;
    restore) op_restore "$@" ;;
    restorelist) op_restorelist ;;
    restoreclean) op_restoreclean ;;
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
            "# enkodiranja, NVENC patch (samo odobrene verzije skripte, provjera sha256)\n"
            "# te sigurnosnu kopiju i vraćanje Jellyfin podataka:\n"
            f"C={q(CFG['jf_config'])}\nCACHE={q(CFG['jf_cache'])}\nLOG={q(CFG['jf_log'])}\n"
            f"DATA={q(CFG['jf_data'])}\nMETA={q(CFG['jf_metadata'])}\n")
    body = HELPER_BODY.replace("__HVER__", str(HELPER_VERSION)).replace("__NVPSHA__", " ".join(NVPATCH_SHA256))
    return head + body + ('main "$@"\n' if standalone else "")


# --------------------------------------------------------------------------
# Postavljanje novog Jellyfin servera: jednokratna skripta koju korisnik pokrene kao root
# (ključ za SSH, otkrivanje Jellyfina, pomoćni program i sudoers pravilo)
# --------------------------------------------------------------------------

BOOT_TOKENS = {}   # token -> (server_id, istek)


def ssh_pubkey():
    key = _GCFG["ssh_key"]
    if not os.path.exists(key):
        os.makedirs(os.path.dirname(key), exist_ok=True)
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jellyclean", "-f", key],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SSHError("SSH ključ se ne može napraviti: " + (r.stderr or "").strip()[:200])
    try:
        with open(key + ".pub", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        r = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True)
        if r.returncode != 0:
            raise SSHError("Javni SSH ključ se ne može pročitati")
        return r.stdout.strip()


def boot_token(sid):
    now = time.time()
    for t, (_, exp) in list(BOOT_TOKENS.items()):
        if exp < now:
            BOOT_TOKENS.pop(t, None)
    t = secrets.token_urlsafe(18)
    BOOT_TOKENS[t] = (sid, now + 1800)
    return t


def bootstrap_script(sv):
    c = sv.cfg
    q = shlex.quote
    body = HELPER_BODY.replace("__HVER__", str(HELPER_VERSION)).replace("__NVPSHA__", " ".join(NVPATCH_SHA256))
    pub = ssh_pubkey()
    return f"""#!/bin/sh
# JellyClean {APP_VERSION} - postavljanje pristupa za Jellyfin server "{sv.name}"
# Pokreni kao root na računalu na kojem radi Jellyfin:  curl -fsSL '<adresa>' | sudo sh
set -u
U={q(c["ssh_user"])}
PUB={q(pub)}
IN_C={q(c["jf_config"])}; IN_CACHE={q(c["jf_cache"])}; IN_LOG={q(c["jf_log"])}; IN_DATA={q(c["jf_data"])}; IN_META={q(c["jf_metadata"])}
HP=/usr/local/sbin/jellyclean-helper
say() {{ printf '\\033[1;36m==>\\033[0m %s\\n' "$*"; }}
die() {{ printf '\\033[1;31mGREŠKA:\\033[0m %s\\n' "$*" >&2; exit 1; }}
[ "$(id -u)" = 0 ] || die "pokreni kao root (… | sudo sh)"
WANT={q(c["ssh_host"])}
if [ -f /etc/jellyclean.env ] || [ -d /opt/jellyclean ]; then
  die "ovo je računalo na kojem radi JellyClean ($(hostname)). Naredbu pokreni na računalu s Jellyfinom: $WANT"
fi
case "$WANT" in
  *[!0-9.]*|"") ;;
  *) if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1 && ! hostname -I | tr ' ' '\n' | grep -qxF "$WANT"; then
       printf '\033[1;33mUPOZORENJE:\033[0m ovo računalo (%s: %s) nema adresu %s koja je upisana u JellyCleanu.\n' "$(hostname)" "$(hostname -I | xargs)" "$WANT" >&2
       printf '             Ako Jellyfin radi na %s, prekini (Ctrl+C) i pokreni naredbu ondje. Nastavljam za 10 s…\n' "$WANT" >&2
       sleep 10
     fi ;;
esac
id "$U" >/dev/null 2>&1 || die "korisnik $U ne postoji na ovom računalu"

say "1/3 SSH ključ JellyCleana za korisnika $U"
H=$(getent passwd "$U" | cut -d: -f6); G=$(id -gn "$U")
[ -n "$H" ] || die "korisnik $U nema home mapu"
install -d -m 700 -o "$U" -g "$G" "$H/.ssh"
touch "$H/.ssh/authorized_keys"
grep -qxF "$PUB" "$H/.ssh/authorized_keys" || printf '%s\\n' "$PUB" >> "$H/.ssh/authorized_keys"
chown "$U:$G" "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"

say "2/3 Tražim Jellyfin"
T=$(mktemp -d) || die "mktemp"
trap 'rm -rf "$T"' EXIT
cat > "$T/body.sh" <<'JC_HELPER_BODY'
{body}
JC_HELPER_BODY
DET=$( (echo 'C=; CACHE=; LOG=; DATA=; META='; cat "$T/body.sh"; echo 'main detect') | sh 2>/dev/null )
gv() {{ printf '%s\\n' "$DET" | sed -n "s/^$1 //p" | head -1; }}
C=${{IN_C:-$(gv D_CONFIG)}}; DATA=${{IN_DATA:-$(gv D_DATA)}}; CACHE=${{IN_CACHE:-$(gv D_CACHE)}}; LOG=${{IN_LOG:-$(gv D_LOG)}}
META=${{IN_META:-${{DATA:+$DATA/metadata}}}}
[ -n "$C" ] || die "na računalu $(hostname) Jellyfin nije pronađen (ni Docker kontejner s 'jellyfin' u nazivu ili imageu, ni paket u /var/lib/jellyfin). Ako je ovo pravo računalo, upiši putanje u JellyCleanu (Uredi → Putanje) pa pokreni ponovno."
for x in "$C" "$CACHE" "$LOG" "$DATA" "$META"; do case "$x" in *"'"*|*..*|"") die "neispravna putanja: $x";; /*) ;; *) die "putanja nije apsolutna: $x";; esac; done
echo "    izvor:    $(gv D_SRC)"
echo "    config:   $C"; echo "    cache:    $CACHE"; echo "    logovi:   $LOG"; echo "    data:     $DATA"; echo "    metadata: $META"

if [ "$U" = root ]; then
  say "3/3 Korisnik je root - pomoćni program nije potreban"
  echo "JC_MODE direct"
else
  say "3/3 Pomoćni program $HP i sudoers pravilo"
  command -v sudo >/dev/null 2>&1 || die "sudo nije instaliran (apt install sudo)"
  command -v visudo >/dev/null 2>&1 || die "visudo nije pronađen"
  {{ printf '#!/bin/sh\\n# JellyClean helper - generirano. Dopušta SAMO mjerenje i čišćenje Jellyfin\\n'
     printf '# cachea, logova i siročadi u metadata/library unutar ovih putanja, test\\n'
     printf '# enkodiranja, nadzor kartice i NVENC patch (samo odobrene verzije skripte):\\n'
     printf "C='%s'\\nCACHE='%s'\\nLOG='%s'\\nDATA='%s'\\nMETA='%s'\\n" "$C" "$CACHE" "$LOG" "$DATA" "$META"
     cat "$T/body.sh"; printf 'main "$@"\\n'; }} > "$HP.new"
  chown root:root "$HP.new"; chmod 755 "$HP.new"; mv -f "$HP.new" "$HP"
  printf '%s\\n' "# JellyClean: $U smije bez lozinke pokretati SAMO jellyclean-helper" "$U ALL=(root) NOPASSWD: $HP" > /etc/sudoers.d/jellyclean.new
  chmod 440 /etc/sudoers.d/jellyclean.new
  if visudo -cf /etc/sudoers.d/jellyclean.new >/dev/null; then mv -f /etc/sudoers.d/jellyclean.new /etc/sudoers.d/jellyclean
  else rm -f /etc/sudoers.d/jellyclean.new; die "sudoers pravilo nije ispravno"; fi
  su -s /bin/sh "$U" -c "sudo -n $HP version" >/dev/null 2>&1 || die "provjera 'sudo -n $HP' kao $U nije uspjela"
  echo "JC_MODE helper"
fi
say "Gotovo. U JellyCleanu klikni 'Provjeri vezu' za ovaj server."
"""


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

    def _cmd(self, op, args=(), lines=None, force=False):
        if not (self.enabled() or (force and CFG["ssh_host"])):
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
        return cmd, stdin

    def stream(self, op, args, on_line, stop_ev):
        """Pokreće operaciju i predaje svaku liniju izlaza odmah (za nadzor uživo)."""
        cmd, stdin = self._cmd(op, args)
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1)
        except FileNotFoundError:
            raise SSHError("Naredba 'ssh' nije instalirana (apt install openssh-client)")
        try:
            try:
                p.stdin.write(stdin)
                p.stdin.close()
            except BrokenPipeError:
                pass
            for line in p.stdout:
                if stop_ev.is_set():
                    break
                line = line.rstrip("\n")
                if line.startswith("ERR "):
                    raise SSHError(line[4:])
                on_line(line)
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        err = (p.stderr.read() or "").strip() if p.stderr else ""
        if p.returncode == 255 and not stop_ev.is_set():
            raise SSHError("SSH spajanje nije uspjelo: " + (err or "nepoznata greška"))
        if "jellyclean-helper" in err and "not found" in err:
            raise SSHError(f"Pomoćni program nije postavljen na {CFG['ssh_host']} - na kartici Serveri klikni 'Postavi pristup'.")
        if "sudo:" in err and ("password" in err or "not allowed" in err):
            raise SSHError("sudo traži lozinku ili nije dopušten: " + err[:200])

    def run(self, op, args=(), lines=None, timeout=600, force=False):
        cmd, stdin = self._cmd(op, args, lines, force=force)
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise SSHError("Naredba 'ssh' nije instalirana (apt install openssh-client)")
        except subprocess.TimeoutExpired:
            raise SSHError("SSH naredba je istekla")
        err = p.stderr.strip()
        if p.returncode == 255:
            raise SSHError("SSH spajanje nije uspjelo: " + (err or "nepoznata greška"))
        if "jellyclean-helper" in err and ("command not found" in err or "No such file" in err or "not found" in err):
            raise SSHError(f"Pomoćni program nije postavljen na {CFG['ssh_host']} (korisnik {CFG['ssh_user']}). "
                           "Na kartici Serveri klikni 'Postavi pristup' i naredbu pokreni NA TOM računalu.")
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
            CtxThread(target=self._safe_refresh, daemon=True).start()
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
        CtxThread(target=self._safe_refresh, daemon=True).start()
        return {"deleted": n, "freed": freed}


JFC = _SrvProxy("jfc")


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
            CtxThread(target=lambda: (time.sleep(30), self.run()), daemon=True).start()
        return res


PC = _SrvProxy("pc")


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
        self.path = os.path.join(cur_server().data_dir, "enctest.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:
            self.state = None

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
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
                "bitrate": ms.get("Bitrate"), "container": ms.get("Container"),
                "width": v.get("Width"), "height": v.get("Height")}

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

    TARGETS = ("2160", "1440", "1080", "720", "480")

    def start(self, iid, max_n, dur, mode, target, codec="h264"):
        if not JFC.enabled():
            raise SSHError("Test treba SSH pristup Jellyfin računalu (kartica Jellyfin config)")
        if not JF.enabled():
            raise SSHError("Test treba Jellyfin API")
        max_n = max(1, min(int(max_n or 10), 16))
        dur = max(10, min(int(dur or 30), 120))
        mode = "realtime" if mode == "realtime" else "max"
        target = target if target in self.TARGETS else "1080"
        codec = codec if codec in ("h264", "hevc") else "h264"
        info = self.item_info(iid)
        if not info["path"]:
            raise SSHError("Stavka nema datoteku")
        sh = info.get("height") or 0
        if sh and int(target) > sh * 1.1:
            raise SSHError(f"Izvor je {sh}p, a cilj {target}p. Jellyfin ne povećava rezoluciju - za ovaj cilj "
                           f"odaberi datoteku od barem {target}p (npr. 4K film za cilj 2160p).")
        with self.lock:
            if self.running:
                raise SSHError("Test je već u tijeku")
            self.running = True
            self.cancel_ev.clear()
            self.state = {"status": "running", "started": now_iso(), "finished": None, "file": info,
                          "params": {"max": max_n, "dur": dur, "mode": mode, "target": target, "codec": codec},
                          "gpu": None, "steps": [], "current": 0, "error": None, "conclusion": None}
        CtxThread(target=self._run, daemon=True).start()
        return self.state

    def cancel(self):
        if self.running:
            self.cancel_ev.set()
            CtxThread(target=lambda: JFC.run("enckill", timeout=60), daemon=True).start()
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
                timeout += 120 if p["target"] == "2160" else 0
                try:
                    out = JFC.run("enctest", (n, p["dur"], p["mode"], p["target"], st["file"]["path"],
                                              p.get("codec", "h264")), timeout=timeout)
                except SSHError as e:
                    if str(e) in ("cilj", "kodek") or "nepoznata operacija" in str(e):
                        raise SSHError("Pomoćni program na Jellyfin računalu je stariji od verzije " + str(HELPER_VERSION) +
                                       " i ne zna za ovaj cilj. Na Proxmox hostu pokreni update-lxc.sh "
                                       "(jednom traži sudo lozinku).")
                    raise
                if p.get("codec", "h264") != "h264" and not re.search(r"^ENC ", out, re.M):
                    raise SSHError("Pomoćni program na Jellyfin računalu je stariji od verzije " + str(HELPER_VERSION) +
                                   " i ne zna za HEVC. Na Proxmox hostu pokreni update-lxc.sh (jednom traži sudo lozinku).")
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


ENC = _SrvProxy("enc")


# --------------------------------------------------------------------------
# NVENC patch: uklanjanje ograničenja istovremenih NVENC sesija (keylase/nvidia-patch)
# --------------------------------------------------------------------------

class NvPatch:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = None
        self.path = os.path.join(cur_server().data_dir, "nvpatch.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:
            self.state = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
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


NVP = _SrvProxy("nvp")


# --------------------------------------------------------------------------
# Nadzor kartice uživo (nvidia-smi / NVML - isti izvor kao nvtop)
# --------------------------------------------------------------------------

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class GpuMon:
    KEEP = 400          # najviše uzoraka u povijesti
    WINDOW = 310        # s povijesti (graf prikazuje 5 min)
    IDLE_STOP = 45      # s bez upita iz preglednika -> zaustavi
    CHUNK = 600         # trajanje jednog SSH poziva

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_ev = threading.Event()
        self.samples = []
        self.procs = []
        self.name = None
        self.error = None
        self.last_poll = 0
        self.seq = 0
        self._cur = None
        self.err_t = 0

    def poll(self, since=0):
        self.last_poll = time.time()
        if (JFC.enabled() and (self.thread is None or not self.thread.is_alive())
                and not (self.error and time.time() - self.err_t < 30)):
            self.stop_ev.clear()
            self.error = None
            self.thread = CtxThread(target=self._run, daemon=True)
            self.thread.start()
        with self.lock:
            return {"enabled": JFC.enabled(), "running": bool(self.thread and self.thread.is_alive()),
                    "name": self.name, "error": self.error, "seq": self.seq,
                    "samples": [x for x in self.samples if x["seq"] > since][-self.KEEP:],
                    "procs": self.procs}

    def stop(self):
        self.stop_ev.set()
        return {"ok": True}

    def _on_line(self, line):
        if time.time() - self.last_poll > self.IDLE_STOP:
            self.stop_ev.set()
            return
        k, _, v = line.partition(" ")
        cur = self._cur
        if k == "NAME":
            self.name = v.strip()
        elif k == "G":
            q = [x.strip() for x in v.split(",")]
            q += [""] * (13 - len(q))
            self._cur = {"t": int(_num(q[0]) or time.time()), "gpu": _num(q[1]), "enc": _num(q[2]), "dec": _num(q[3]),
                         "mem": _num(q[4]), "mem_total": _num(q[5]), "temp": _num(q[6]), "power": _num(q[7]),
                         "power_limit": _num(q[8]), "fan": _num(q[9]), "clk": _num(q[10]), "memclk": _num(q[11]),
                         "pstate": q[12] or None, "_ph": None, "_p": {}, "_m": {}, "_c": {}}
        elif cur is None:
            return
        elif k == "PH":
            cur["_ph"] = v.split()
        elif k == "P":
            f = v.split()
            hdr = cur["_ph"] or ["gpu", "pid", "type", "sm", "mem", "enc", "dec", "fb", "command"]
            row = dict(zip(hdr, f))
            if len(f) > len(hdr):
                row["command"] = " ".join(f[len(hdr) - 1:])
            if row.get("pid", "").isdigit():
                cur["_p"][row["pid"]] = row
        elif k == "M":
            a, _, b = v.partition(",")
            cur["_m"][a.strip()] = _num(b.strip())
        elif k == "C":
            pid, _, args = v.partition(" ")
            cur["_c"][pid] = args.strip()
        elif k == "T":
            procs = []
            for pid in sorted(set(cur["_p"]) | set(cur["_m"]), key=lambda x: int(x)):
                r = cur["_p"].get(pid, {})
                args = cur["_c"].get(pid, "")
                fb = _num(r.get("fb"))
                procs.append({"pid": int(pid), "type": r.get("type"), "sm": _num(r.get("sm")),
                              "mem": _num(r.get("mem")), "enc": _num(r.get("enc")), "dec": _num(r.get("dec")),
                              "vram": cur["_m"].get(pid, fb),
                              "name": (r.get("command") or (re.split(r"[\x1f ]", args)[0] if args else "?")).split("/")[-1],
                              **self._describe(args)})
            smp = {k2: v2 for k2, v2 in cur.items() if not k2.startswith("_")}
            with self.lock:
                self.procs = procs
                if self.samples and smp["t"] <= self.samples[-1]["t"]:
                    self._cur = None
                    return          # već imamo uzorak za tu sekundu
                self.seq += 1
                smp["seq"] = self.seq
                self.samples.append(smp)
                cut = smp["t"] - self.WINDOW
                while self.samples and (self.samples[0]["t"] < cut or len(self.samples) > self.KEEP):
                    self.samples.pop(0)
            self._cur = None

    @staticmethod
    def _describe(args):
        """Iz ffmpeg naredbe izvuci ulaznu datoteku, kodek i rezoluciju (za prikaz)."""
        d = {"what": None, "input": None, "codec": None, "test": "jellyclean-enctest" in args}
        if not args:
            return d
        if "\x1f" in args:
            a = [x for x in args.split("\x1f") if x != ""]
            args = " ".join(a)
        else:
            try:
                a = shlex.split(args)
            except ValueError:
                a = args.split()
        for i, x in enumerate(a[:-1]):
            nx = a[i + 1]
            if x == "-i" and not d["input"]:
                d["input"] = nx.replace("file:", "").strip('"').rsplit("/", 1)[-1]
            elif x in ("-c:v", "-codec:v", "-vcodec", "-c:v:0") and not d["codec"]:
                d["codec"] = nx
        m = re.search(r"scale(?:_cuda|_npp)?=(?:w=)?(-?\d+)[:x](?:h=)?(-?\d+)", args)
        if m:
            w, h = m.group(1), m.group(2)
            d["what"] = f"{h}p" if w.startswith("-") else f"{w}×{h}"
        if "lavfi" in args and "testsrc" in args:
            d["input"] = "testni signal"
        return d

    def _run(self):
        try:
            while not self.stop_ev.is_set():
                if time.time() - self.last_poll > self.IDLE_STOP:
                    break
                JFC.stream("gpumon", (self.CHUNK,), self._on_line, self.stop_ev)
        except SSHError as e:
            m = str(e)
            if "nepoznata operacija" in m:
                m = ("Pomoćni program na Jellyfin računalu je stariji od verzije " + str(HELPER_VERSION) +
                     " i ne zna za nadzor uživo. Na Proxmox hostu pokreni update-lxc.sh (jednom traži sudo lozinku).")
            self.error = m
            self.err_t = time.time()
        except Exception as e:
            self.error = str(e)
            self.err_t = time.time()


GMON = _SrvProxy("gmon")


# --------------------------------------------------------------------------
# Registar Jellyfin servera
# --------------------------------------------------------------------------

def path_map_str(pm):
    return "; ".join(f"{a}={b}" for a, b in pm)


class Server:
    def __init__(self, sid, name, cfg, data_dir):
        self.id = sid
        self.name = name or sid
        self.cfg = srv_normalize(dict(cfg))
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        with use_server(self):
            self.jf = Jellyfin()
            self.jfc = JFConfig()
            self.pc = PathCheck()
            self.enc = EncTest()
            self.nvp = NvPatch()
            self.gmon = GpuMon()

    def public(self):
        c = self.cfg
        mode = "helper" if c["ssh_helper"] else ("sudo" if c["ssh_sudo"] else "direct")
        return {"id": self.id, "name": self.name, "jf_url": c["jf_url"], "has_key": bool(c["jf_key"]),
                "jf_users": ", ".join(c["jf_users"]), "path_map": path_map_str(c["path_map"]),
                "verify_tls": c["verify_tls"], "ssh_host": c["ssh_host"], "ssh_user": c["ssh_user"],
                "ssh_port": c["ssh_port"], "ssh_mode": mode, "ssh_helper": c["ssh_helper"],
                "jf_config": c["jf_config"], "jf_cache": c["jf_cache"], "jf_log": c["jf_log"],
                "jf_data": c["jf_data"], "jf_metadata": c["jf_metadata"],
                "jf_enabled": bool(c["jf_url"] and c["jf_key"]), "ssh_enabled": bool(c["ssh_host"] and c["jf_config"]),
                "synced": self.jf.synced, "jf_error": self.jf.error,
                "server_name": (self.jf.server or {}).get("name"), "server_version": (self.jf.server or {}).get("version")}


class ServerRegistry:
    MAIN = "main"

    def __init__(self):
        self.lock = threading.RLock()
        self.servers = {}
        self.path = os.path.join(_GCFG["data_dir"], "servers.json")
        self.main_override = False

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log("servers.json se ne može pročitati:", e)
            return {}

    def load(self):
        data = self._read()
        entries = data.get("servers") or []
        with self.lock:
            self.servers = {}
            m = next((e for e in entries if e.get("id") == self.MAIN), None)
            mcfg = dict(ENV_SRV)
            mname = (m or {}).get("name") or data.get("main_name") or "Jellyfin"
            self.main_override = bool(m and m.get("override"))
            if self.main_override:
                mcfg.update({k: m[k] for k in SRV_KEYS if k in m})
            self.servers[self.MAIN] = Server(self.MAIN, mname, mcfg, _GCFG["data_dir"])
            for e in entries:
                sid = e.get("id")
                if not sid or sid == self.MAIN or not re.fullmatch(r"[a-z0-9-]{1,32}", sid):
                    continue
                self.servers[sid] = Server(sid, e.get("name"), {k: e[k] for k in SRV_KEYS if k in e},
                                           os.path.join(_GCFG["data_dir"], "servers", sid))

    def save(self):
        out = []
        with self.lock:
            for sid, sv in self.servers.items():
                e = {"id": sid, "name": sv.name}
                if sid != self.MAIN or self.main_override:
                    c = dict(sv.cfg)
                    c["path_map"] = [list(x) for x in c["path_map"]]
                    e.update(c)
                    if sid == self.MAIN:
                        e["override"] = True
                out.append(e)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump({"servers": out}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def default(self):
        with self.lock:
            if not self.servers:
                self.servers[self.MAIN] = Server(self.MAIN, "Jellyfin", dict(ENV_SRV), _GCFG["data_dir"])
            return self.servers.get(self.MAIN) or next(iter(self.servers.values()))

    def get(self, sid):
        with self.lock:
            return self.servers.get(sid) if sid else None

    def all(self):
        with self.lock:
            return list(self.servers.values())

    def upsert(self, body):
        """Spremi server iz sučelja. Prazan API ključ = zadrži postojeći."""
        sid = (body.get("id") or "").strip()
        name = (body.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("Upiši naziv servera")
        cfg = {
            "jf_url": (body.get("jf_url") or "").strip(),
            "jf_key": (body.get("jf_key") or "").strip(),
            "jf_users": [u for u in re.split(r"[,;]", body.get("jf_users") or "") if u.strip()],
            "path_map": _parse_map(body.get("path_map") or ""),
            "verify_tls": bool(body.get("verify_tls", True)),
            "ssh_host": (body.get("ssh_host") or "").strip(),
            "ssh_user": (body.get("ssh_user") or "").strip() or "root",
            "ssh_port": str(body.get("ssh_port") or "22").strip(),
            "ssh_sudo": body.get("ssh_mode") == "sudo",
            "ssh_helper": ("/usr/local/sbin/jellyclean-helper" if body.get("ssh_mode") == "helper" else ""),
            "jf_config": body.get("jf_config") or "", "jf_cache": body.get("jf_cache") or "",
            "jf_log": body.get("jf_log") or "", "jf_data": body.get("jf_data") or "",
            "jf_metadata": body.get("jf_metadata") or "",
        }
        if cfg["ssh_user"] == "root":
            cfg["ssh_helper"], cfg["ssh_sudo"] = "", False
        if cfg["jf_url"] and not re.match(r"https?://", cfg["jf_url"]):
            raise ValueError("Jellyfin URL mora počinjati s http:// ili https://")
        if not re.fullmatch(r"\d{1,5}", cfg["ssh_port"]):
            raise ValueError("Neispravan SSH port")
        if cfg["ssh_host"] and not re.fullmatch(r"[A-Za-z0-9._:-]+", cfg["ssh_host"]):
            raise ValueError("Neispravan SSH host")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", cfg["ssh_user"]):
            raise ValueError("Neispravno SSH korisničko ime")
        for k in ("jf_config", "jf_cache", "jf_log", "jf_data", "jf_metadata"):
            v = (cfg[k] or "").strip()
            if v and (not v.startswith("/") or ".." in v or not re.fullmatch(r"[^\x00-\x1f'\"`$\\]+", v)):
                raise ValueError(f"Neispravna putanja: {v}")
        with self.lock:
            old = self.servers.get(sid) if sid else None
            if old and not cfg["jf_key"]:
                cfg["jf_key"] = old.cfg["jf_key"]
            if not sid:
                base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "server"
                sid, i = base, 2
                while sid in self.servers or sid == self.MAIN:
                    sid, i = f"{base}-{i}", i + 1
            if sid == self.MAIN:
                self.main_override = True
            if old:
                old.name = name
                old.cfg = srv_normalize(cfg)
                old.jf.synced = None
                old.jfc.status = None
                sv = old
            else:
                sv = Server(sid, name, cfg, os.path.join(_GCFG["data_dir"], "servers", sid))
                self.servers[sid] = sv
            self.save()
        return sv

    def remove(self, sid):
        with self.lock:
            if sid == self.MAIN:
                raise ValueError("Glavni server (iz /etc/jellyclean.env) ne može se ukloniti")
            sv = self.servers.pop(sid, None)
            if not sv:
                raise ValueError("Server ne postoji")
            sv.gmon.stop()
            self.save()

    def reset_main(self):
        with self.lock:
            self.main_override = False
            m = self.servers[self.MAIN]
            m.cfg = srv_normalize(dict(ENV_SRV))
            m.jf.synced = None
            self.save()


SERVERS = ServerRegistry()


def server_test(sv):
    """Provjeri Jellyfin API i SSH za server; putanje i način rada preuzmi iz pomoćnog programa."""
    out = {"jf": None, "ssh": None}
    changed = False
    with use_server(sv):
        c = sv.cfg
        if c["jf_url"] and c["jf_key"]:
            try:
                info = JF.req("GET", "/System/Info", timeout=15) or {}
                out["jf"] = {"ok": True, "name": info.get("ServerName"), "version": info.get("Version")}
            except urllib.error.HTTPError as e:
                out["jf"] = {"ok": False, "error": f"HTTP {e.code}" + (" - API ključ nije ispravan" if e.code in (401, 403) else "")}
            except Exception as e:
                out["jf"] = {"ok": False, "error": str(e)[:200]}
        if c["ssh_host"]:
            r = {"ok": False}
            try:
                if c["ssh_user"] == "root" and (c["ssh_helper"] or c["ssh_sudo"]):
                    c["ssh_helper"], c["ssh_sudo"] = "", False
                    changed = True
                if not c["ssh_helper"] and c["ssh_user"] != "root" and not c["ssh_sudo"]:
                    # možda je pomoćni program već postavljen (bootstrap skripta) - probaj ga
                    c["ssh_helper"] = "/usr/local/sbin/jellyclean-helper"
                    try:
                        JFC.run("version", timeout=30, force=True)
                        changed = True
                    except SSHError:
                        c["ssh_helper"] = ""
                ver = JFC.run("version", timeout=30, force=True).strip().splitlines()
                hv = next((x.split()[-1] for x in ver if x.strip()), "")
                r["helper_version"] = int(hv) if hv.isdigit() else None
                r["mode"] = "helper" if c["ssh_helper"] else ("sudo" if c["ssh_sudo"] else "direct")
                if c["ssh_helper"]:
                    txt = JFC.run("paths", timeout=30, force=True)
                    m = dict(l.split(" ", 1) for l in txt.splitlines() if l.startswith("P_") and " " in l)
                    newp = {"jf_config": m.get("P_CONFIG", ""), "jf_cache": m.get("P_CACHE", ""), "jf_log": m.get("P_LOG", ""),
                            "jf_data": m.get("P_DATA", ""), "jf_metadata": m.get("P_META", "")}
                    if newp["jf_config"] and any(c[k] != v for k, v in newp.items()):
                        c.update(newp)
                        changed = True
                elif not c["jf_config"]:
                    txt = JFC.run("detect", timeout=60, force=True)
                    m = dict(l.split(" ", 1) for l in txt.splitlines() if l.startswith("D_") and " " in l)
                    if m.get("D_CONFIG"):
                        c.update({"jf_config": m["D_CONFIG"], "jf_cache": m.get("D_CACHE", ""), "jf_log": m.get("D_LOG", ""),
                                  "jf_data": m.get("D_DATA", ""), "jf_metadata": ""})
                        srv_normalize(c)
                        changed = True
                        r["detected"] = m.get("D_SRC")
                    elif "DOCKERPERM" in txt:
                        r["warn"] = f"{c['ssh_user']} nema pristup Dockeru - pokreni naredbu za postavljanje (pomoćni program)"
                r["ok"] = True
                r["paths"] = {k: c[k] for k in ("jf_config", "jf_cache", "jf_log", "jf_data", "jf_metadata")}
                if r["helper_version"] is not None and r["helper_version"] < HELPER_VERSION:
                    r["warn"] = (f"Pomoćni program je verzija {r['helper_version']}, a JellyClean očekuje {HELPER_VERSION}. "
                                 "Ponovno pokreni naredbu za postavljanje na tom računalu.")
            except SSHError as e:
                r["error"] = str(e)[:300]
            out["ssh"] = r
    if changed:
        if sv.id == SERVERS.MAIN:
            SERVERS.main_override = True
        SERVERS.save()
        sv.jfc.status = None
    return out


# --------------------------------------------------------------------------
# Pozadinski poslovi
# --------------------------------------------------------------------------


def sync_all_servers(background=False):
    for sv in SERVERS.all():
        with use_server(sv):
            if not JF.enabled():
                continue
            if background:
                CtxThread(target=JF.sync, daemon=True).start()
            else:
                try:
                    JF.sync()
                except Exception:
                    traceback.print_exc()


def refresh_all_libraries():
    """Mediji su zajednički - nakon brisanja ili vraćanja javi svim Jellyfin serverima."""
    for sv in SERVERS.all():
        with use_server(sv):
            if JF.enabled():
                CtxThread(target=JF.library_refresh, daemon=True).start()


# --------------------------------------------------------------------------
# Proxmox pristup: migracija Jellyfina iz Dockera u LXC (kartica Migracija)
#
# JellyClean na Proxmox host ne dobiva shell. Njegov SSH ključ u authorized_keys ima
#   from="<IP JellyCleana>",command="/usr/local/sbin/jellyclean-pve-agent <INST>:<KEYID>",restrict
# pa smije pokrenuti SAMO agenta (ping/start/status/answer/cancel/log/revoke) i samo s IP adrese
# JellyCleana. Kad se ta adresa promijeni, ključ prestaje raditi i traži se novo dopuštenje:
# napravi se novi ključ, naredba na Proxmoxu briše sve stare ključeve ovog JellyCleana, a kad novi
# proradi, JellyClean briše i svoju kopiju starog.
# --------------------------------------------------------------------------
import socket

PVE_SRC_DIR = os.path.join(BASE_DIR, "proxmox")
PVE_AGENT_SRC = os.path.join(PVE_SRC_DIR, "jellyclean-pve-agent.sh")
PVE_MIGRATE_SRC = os.path.join(PVE_SRC_DIR, "jellyfin-docker2lxc.sh")
PVE_TOKENS = {}   # token -> (keyid, istek)
_PVE_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _src_version(path, rx):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(rx, f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


class PveError(SSHError):
    def __init__(self, msg, kind="error"):
        super().__init__(msg)
        self.kind = kind


class PveAccess:
    TOKEN_MIN = 30

    def __init__(self):
        self.lock = threading.RLock()
        self.last = None

    # ------------------------------------------------------------ stanje ---
    def _file(self):
        return os.path.join(_GCFG["data_dir"], "pve.json")

    def _sshdir(self):
        d = os.path.join(_GCFG["data_dir"], "ssh")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    def load(self):
        try:
            with open(self._file(), encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, st):
        tmp = self._file() + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._file())

    def key_file(self, kid):
        if not re.fullmatch(r"[a-f0-9]{8,32}", kid or ""):
            raise PveError("neispravan ID ključa")
        return os.path.join(self._sshdir(), f"pve-{kid}")

    def _inst(self, st):
        if not re.fullmatch(r"[a-f0-9]{8}", st.get("inst") or ""):
            st["inst"] = secrets.token_hex(4)
        return st["inst"]

    def _drop_key(self, kid):
        for ext in ("", ".pub"):
            try:
                os.remove(self.key_file(kid) + ext)
            except (OSError, PveError):
                pass

    @staticmethod
    def local_ip(host, port=22):
        """Adresa s koje JellyClean izlazi prema Proxmoxu (ona ide u from=)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((socket.gethostbyname(host), int(port)))
                return s.getsockname()[0]
        except (OSError, ValueError):
            return None

    def key_info(self, kid):
        """Provjera ključa na strani JellyCleana: postoji, prava 600, javni dio odgovara privatnom."""
        key = self.key_file(kid)
        info = {"id": kid, "exists": os.path.exists(key), "fingerprint": None, "fixed_perms": False, "problem": None}
        if not info["exists"]:
            info["problem"] = "privatni ključ ne postoji"
            return info
        if os.stat(key).st_mode & 0o077:
            os.chmod(key, 0o600)
            info["fixed_perms"] = True
        r = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True)
        if r.returncode != 0:
            info["problem"] = "privatni ključ je oštećen"
            return info
        try:
            with open(key + ".pub", encoding="utf-8") as f:
                pub = f.read().split()
        except OSError:
            pub = []
        if pub[:2] != r.stdout.split()[:2]:
            info["problem"] = "javni ključ ne odgovara privatnom"
        f = subprocess.run(["ssh-keygen", "-lf", key], capture_output=True, text=True)
        if f.returncode == 0 and f.stdout.split():
            info["fingerprint"] = f.stdout.split()[1]
        return info

    # --------------------------------------------------------------- SSH ---
    def _call(self, st, kid, args, stdin=None, timeout=30):
        host, port = st.get("host"), str(st.get("port") or 22)
        if not host:
            raise PveError("Proxmox pristup nije postavljen", "none")
        key = self.key_file(kid)
        if not os.path.exists(key):
            raise PveError("ključ za Proxmox ne postoji", "key_missing")
        sd = self._sshdir()
        cmd = ["ssh", "-p", port, "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={os.path.join(sd, 'pve_known_hosts')}", "-o", "LogLevel=ERROR",
               "-o", "ControlMaster=auto", "-o", f"ControlPath={os.path.join(sd, 'cm-pve-' + kid)}",
               "-o", "ControlPersist=60", f"root@{host}", " ".join(str(a) for a in args)]
        try:
            p = subprocess.run(cmd, input=stdin or "", capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise PveError("Naredba 'ssh' nije instalirana (apt install openssh-client)")
        except subprocess.TimeoutExpired:
            raise PveError("Proxmox nije odgovorio na vrijeme", "unreachable")
        err = (p.stderr or "").strip()
        if p.returncode == 255:
            low = err.lower()
            if "permission denied" in low:
                raise PveError("Proxmox je odbio ključ", "auth")
            if "host key verification failed" in low or "identification has changed" in low:
                raise PveError("Host ključ Proxmoxa se promijenio (reinstalacija?). Napravi novo dopuštenje.", "hostkey")
            raise PveError(f"Ne mogu se spojiti na {host}:{port}: {err[:200] or 'nepoznata greška'}", "unreachable")
        out = (p.stdout or "").strip().splitlines()
        try:
            res = json.loads(out[-1]) if out else {}
        except ValueError:
            raise PveError("Agent na Proxmoxu je vratio neočekivan odgovor: " + (out[-1] if out else err)[:200], "agent")
        if not res.get("ok"):
            raise PveError(res.get("error") or "agent je javio grešku", "agent")
        return res

    def call(self, args, stdin=None, timeout=30):
        with self.lock:
            st = self.load()
        act = st.get("active") or {}
        if not act.get("kid"):
            raise PveError("Proxmox pristup nije postavljen - na kartici Migracija klikni 'Postavi pristup'", "none")
        try:
            return self._call(st, act["kid"], args, stdin, timeout)
        except PveError as e:
            if e.kind == "auth":
                cur = self.local_ip(st["host"], st.get("port") or 22)
                if cur and act.get("ip") and cur != act["ip"]:
                    raise PveError(f"IP adresa JellyCleana se promijenila ({act['ip']} → {cur}) pa Proxmox više ne "
                                   "prihvaća ključ. Na kartici Migracija klikni 'Novo dopuštenje'.", "ip_changed")
                raise PveError("Proxmox više ne prihvaća ključ JellyCleana. Na kartici Migracija klikni 'Novo dopuštenje'.", "auth")
            raise

    # ------------------------------------------------------------ provjera ---
    def status(self, check=True):
        with self.lock:
            st = self.load()
            host, port = st.get("host"), st.get("port") or 22
            res = {"host": host, "port": port, "state": "none", "message": None,
                   "bundled": {"migrate": _src_version(PVE_MIGRATE_SRC, r'^SCRIPT_VERSION="([^"]+)"'),
                               "agent": _src_version(PVE_AGENT_SRC, r'^AGENT_VERSION=(\d+)')},
                   "current_ip": self.local_ip(host, port) if host else None,
                   "active": None, "pending": None, "remote": None, "rotated": None, "outdated": False,
                   "checked": now_iso()}
            if not (res["bundled"]["migrate"] and res["bundled"]["agent"]):
                res["state"], res["message"] = "error", f"U instalaciji JellyCleana nedostaju skripte u {PVE_SRC_DIR} - nadogradi JellyClean."
            inst = st.get("inst")
            pend = st.get("pending")
            # 1) novi ključ čeka da se naredba pokrene na Proxmoxu - radi li već?
            if pend and check and host:
                try:
                    r = self._call(st, pend["kid"], ["ping"])
                    if r.get("key") == f"{inst}:{pend['kid']}":
                        old = (st.get("active") or {}).get("kid")
                        st["active"] = dict(pend, activated=now_iso())
                        st.pop("pending", None)
                        self.save(st)
                        if old and old != pend["kid"]:
                            self._drop_key(old)
                        res["rotated"] = {"old": old, "new": pend["kid"]}
                        log(f"Proxmox pristup: aktiviran ključ {pend['kid']}" + (f", stari {old} obrisan" if old else ""))
                        pend = None
                except PveError:
                    pass
            if pend:
                res["pending"] = {"kid": pend["kid"], "ip": pend.get("ip"), "created": pend.get("created")}
            act = st.get("active")
            if not act:
                if res["state"] == "none" and pend:
                    res["state"] = "pending"
                self.last = res
                return res
            ki = self.key_info(act["kid"])
            res["active"] = {"kid": act["kid"], "ip": act.get("ip"), "created": act.get("created"),
                             "activated": act.get("activated"), "fingerprint": ki["fingerprint"],
                             "fixed_perms": ki["fixed_perms"], "problem": ki["problem"]}
            ip_changed = bool(res["current_ip"] and act.get("ip") and res["current_ip"] != act["ip"])
            res["ip_changed"] = ip_changed
            if ki["problem"]:
                res["state"], res["message"] = "key_missing", f"Ključ za Proxmox: {ki['problem']}. Napravi novo dopuštenje."
            elif not check:
                res["state"] = "unchecked"
            else:
                try:
                    r = self._call(st, act["kid"], ["ping"])
                    res["remote"] = {k: r.get(k) for k in ("host", "pve", "agent", "migrate", "from", "running", "key")}
                    if r.get("key") != f"{inst}:{act['kid']}":
                        res["state"], res["message"] = "key_mismatch", ("Agent na Proxmoxu prijavio je drugi ključ "
                                                                         f"({r.get('key')}). Napravi novo dopuštenje.")
                    else:
                        res["state"] = "ok"
                        b = res["bundled"]
                        res["outdated"] = bool(b["migrate"] and (str(r.get("agent")) != str(b["agent"]) or r.get("migrate") != b["migrate"]))
                        if ip_changed:
                            res["message"] = (f"JellyClean sada izlazi s adrese {res['current_ip']}, a dopuštenje je za "
                                              f"{act['ip']}. Veza zasad radi, ali preporučuje se novo dopuštenje.")
                except PveError as e:
                    if e.kind == "auth":
                        res["state"] = "ip_changed" if ip_changed else "key_rejected"
                        res["message"] = (f"IP adresa JellyCleana se promijenila ({act.get('ip')} → {res['current_ip']}). "
                                          "Proxmox zato više ne prihvaća ključ - napravi novo dopuštenje; stari ključ će se obrisati."
                                          if ip_changed else
                                          "Proxmox ne prihvaća ključ (uklonjen iz authorized_keys?). Napravi novo dopuštenje.")
                    else:
                        res["state"], res["message"] = e.kind if e.kind in ("unreachable", "hostkey") else "error", str(e)
            self.last = res
            return res

    # ---------------------------------------------- postavljanje / rotacija ---
    def setup(self, host, port, http_host):
        host = (host or "").strip()
        if not _PVE_HOST_RE.match(host):
            raise PveError("Upiši IP adresu ili ime Proxmox hosta")
        try:
            port = int(port or 22)
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            raise PveError("Neispravan SSH port")
        if not (os.path.exists(PVE_AGENT_SRC) and os.path.exists(PVE_MIGRATE_SRC)):
            raise PveError(f"U instalaciji JellyCleana nedostaju skripte u {PVE_SRC_DIR} - nadogradi JellyClean.")
        ip = self.local_ip(host, port)
        if not ip:
            raise PveError(f"Ne mogu odrediti adresu s koje JellyClean dolazi do {host} (je li adresa ispravna?)")
        with self.lock:
            st = self.load()
            inst = self._inst(st)
            # korisnik upravo izričito odobrava pristup: zaboravi stari host ključ (npr. nakon reinstalacije)
            subprocess.run(["ssh-keygen", "-R", host, "-f", os.path.join(self._sshdir(), "pve_known_hosts")],
                           capture_output=True)
            old_p = (st.get("pending") or {}).get("kid")
            if old_p:
                self._drop_key(old_p)
            kid = secrets.token_hex(4)
            key = self.key_file(kid)
            r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"jellyclean-pve {inst}:{kid}", "-f", key],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise PveError("SSH ključ se ne može napraviti: " + (r.stderr or "").strip()[:200])
            os.chmod(key, 0o600)
            st.update(host=host, port=port)
            st["pending"] = {"kid": kid, "ip": ip, "created": now_iso()}
            self.save(st)
            now = time.time()
            for t, (_, exp) in list(PVE_TOKENS.items()):
                if exp < now:
                    PVE_TOKENS.pop(t, None)
            tok = secrets.token_urlsafe(18)
            PVE_TOKENS[tok] = (kid, now + self.TOKEN_MIN * 60)
        script = self.setup_script(kid)
        sha = hashlib.sha256(script.encode()).hexdigest()
        url = f"http://{http_host}/api/pve/setup.sh?t={tok}"
        tmpf = "/tmp/jellyclean-pve.sh"
        return {"url": url, "sha256": sha, "ip": ip, "host": host, "kid": kid, "expires_min": self.TOKEN_MIN,
                "replaces": (st.get("active") or {}).get("kid"),
                "command": f"curl -fsSL '{url}' -o {tmpf} && echo '{sha}  {tmpf}' | sha256sum -c --quiet && bash {tmpf}; rm -f {tmpf}",
                "simple": f"curl -fsSL '{url}' | bash"}

    def script_for_token(self, tok):
        ent = PVE_TOKENS.get(tok or "")
        if not ent or ent[1] < time.time():
            return None
        st = self.load()
        if (st.get("pending") or {}).get("kid") != ent[0]:
            return None
        return self.setup_script(ent[0])

    def setup_script(self, kid):
        st = self.load()
        pend = st.get("pending") or {}
        if pend.get("kid") != kid:
            raise PveError("ključ više nije aktualan")
        inst = st["inst"]
        with open(self.key_file(kid) + ".pub", encoding="utf-8") as f:
            pub = f.read().strip()
        with open(PVE_AGENT_SRC, encoding="utf-8") as f:
            agent = f.read()
        with open(PVE_MIGRATE_SRC, encoding="utf-8") as f:
            mig = f.read()
        for body, delim in ((agent, "JC_AGENT_EOF"), (mig, "JC_MIGRATE_EOF")):
            if re.search(rf"^{delim}$", body, re.M):
                raise PveError("skripta sadrži graničnik heredoca")
        q = shlex.quote
        return f"""#!/bin/bash
# JellyClean {APP_VERSION} - pristup Proxmoxu za migraciju Jellyfina (Docker → LXC)
# Pokreni kao root u shellu Proxmox hosta.
#  • instalira /usr/local/sbin/jellyclean-pve-agent i /usr/local/sbin/jellyfin-docker2lxc
#  • u authorized_keys roota upisuje ključ JellyCleana koji smije pokrenuti SAMO agenta i samo s {pend.get('ip')}
#  • briše sve prethodne ključeve ovog JellyCleana (ID {inst})
set -euo pipefail
INST={q(inst)}; KEYID={q(kid)}; FROM={q(pend.get('ip') or '')}
PUB={q(pub)}
AGENT=/usr/local/sbin/jellyclean-pve-agent; MIG=/usr/local/sbin/jellyfin-docker2lxc
say() {{ printf '\\033[1;36m==>\\033[0m %s\\n' "$*"; }}
die() {{ printf '\\033[1;31mGREŠKA:\\033[0m %s\\n' "$*" >&2; exit 1; }}
[ "$(id -u)" = 0 ] || die "pokreni kao root"
command -v pct >/dev/null 2>&1 && command -v pveversion >/dev/null 2>&1 || die "ovo nije Proxmox VE host ($(hostname)) - naredbu pokreni u shellu Proxmoxa"
[ -n "$FROM" ] || die "nedostaje IP adresa JellyCleana"

say "1/4 Potrebni alati"
if ! command -v jq >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq jq >/dev/null || die "jq se ne može instalirati"; fi
command -v systemd-run >/dev/null 2>&1 || die "systemd-run nije dostupan"

say "2/4 Agent i migracijska skripta"
umask 022
cat > "$AGENT.new" <<'JC_AGENT_EOF'
{agent.rstrip(chr(10))}
JC_AGENT_EOF
cat > "$MIG.new" <<'JC_MIGRATE_EOF'
{mig.rstrip(chr(10))}
JC_MIGRATE_EOF
bash -n "$AGENT.new" && bash -n "$MIG.new" || {{ rm -f "$AGENT.new" "$MIG.new"; die "preuzete skripte nisu ispravne"; }}
chown root:root "$AGENT.new" "$MIG.new"; chmod 755 "$AGENT.new" "$MIG.new"
mv -f "$AGENT.new" "$AGENT"; mv -f "$MIG.new" "$MIG"
echo "    agent v$(sed -n 's/^AGENT_VERSION=//p' "$AGENT"), migracijska skripta v$(sed -n 's/^SCRIPT_VERSION="\\(.*\\)"/\\1/p' "$MIG" | head -n1)"

say "3/4 Ključ JellyCleana: samo s $FROM, samo agent"
install -d -m 700 /root/.ssh
[ -e /root/.ssh/authorized_keys ] || {{ touch /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys; }}
AK=$(readlink -f /root/.ssh/authorized_keys)
OLD=$(grep -cF " jellyclean-pve $INST:" "$AK" || true)
T=$(mktemp)
grep -vF " jellyclean-pve $INST:" "$AK" > "$T" || true
printf 'from="%s",command="%s %s:%s",restrict %s\\n' "$FROM" "$AGENT" "$INST" "$KEYID" "$PUB" >> "$T"
cat "$T" > "$AK"; rm -f "$T"      # pisanje u postojeću datoteku čuva poveznicu na /etc/pve/priv
if [ "${{OLD:-0}}" -gt 0 ]; then echo "    obrisano starih ključeva ovog JellyCleana: $OLD"; fi

say "4/4 Provjera"
grep -qF " jellyclean-pve $INST:$KEYID" "$AK" || die "ključ nije upisan u $AK"
if ! sshd -T 2>/dev/null | grep -qiE '^permitrootlogin (yes|prohibit-password|without-password)'; then
  echo "UPOZORENJE: sshd možda ne dopušta prijavu roota ključem (PermitRootLogin)" >&2
fi
echo "Gotovo. U JellyCleanu klikni 'Pokrenuo sam - provjeri ključ'."
"""

    def revoke(self):
        with self.lock:
            st = self.load()
            act, pend = st.get("active") or {}, st.get("pending") or {}
            remote, err = False, None
            if act.get("kid"):
                try:
                    self._call(st, act["kid"], ["revoke"])
                    remote = True
                except PveError as e:
                    err = str(e)
            for k in (act.get("kid"), pend.get("kid")):
                if k:
                    self._drop_key(k)
            inst = st.get("inst")
            self.save({"inst": inst} if inst else {})
            self.last = None
            return {"ok": True, "remote_removed": remote, "error": err,
                    "manual": None if remote or not inst else
                    f"sed -i '/ jellyclean-pve {inst}:/d' \"$(readlink -f /root/.ssh/authorized_keys)\""}

    # ------------------------------------------------------------ migracija ---
    def mig_start(self):
        st = self.status(check=True)
        if st["state"] != "ok":
            raise PveError(st.get("message") or "Proxmox pristup ne radi")
        if st.get("outdated"):
            raise PveError("Agent ili migracijska skripta na Proxmoxu su zastarjeli - klikni 'Ažuriraj na Proxmoxu'.")
        return self.call(["start"])

    def mig_status(self, offset):
        return self.call(["status", int(offset or 0)])

    def mig_answer(self, qid, value, cancel=False):
        if isinstance(value, list):
            value = [str(v)[:500] for v in value][:200]
        else:
            value = str(value if value is not None else "")[:4000]
        body = json.dumps({"id": int(qid), "value": value, "cancel": bool(cancel)})
        return self.call(["answer", int(qid)], stdin=body)

    def mig_cancel(self):
        return self.call(["cancel"])

    def mig_log(self):
        return self.call(["log"])


PVE = PveAccess()


# --------------------------------------------------------------------------
# Sigurnosne kopije Jellyfina (kartica Backup)
#
#  • PUNI backup (SSH): baza, postavke, korisnici, pluginovi i STATISTIKA GLEDANJA
#    (playback_reporting.db), po želji i metapodaci. Vraća se na isti ili drugi server;
#    na drugom serveru putanje u postavkama prilagode se odredištu.
#  • KORISNIČKI backup (Jellyfin API): gledanost, pozicija, favoriti, ocjene, playliste i
#    postavke korisnika. Vraća se na BILO KOJI Jellyfin - stavke se prepoznaju po ID-u,
#    putanji, IMDb/TMDb/TVDb oznakama, sezoni i epizodi ili nazivu i godini.
# --------------------------------------------------------------------------
import tarfile

BK_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-(full|users)-[a-z0-9-]{1,40}$")
BK_FILES = {"manifest.json", "full.tar.gz", "users.json"}
BK_ITEM_TYPES = "Movie,Episode,Series,Season,Audio,MusicAlbum,MusicVideo,Video,BoxSet"


class BackupCancelled(Exception):
    pass


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "server").lower()).strip("-")[:30] or "server"


def _norm_title(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _pids(d):
    return {k.lower(): str(v).strip().lower() for k, v in (d or {}).items() if v and str(v).strip()}


class Backups:
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None
        self.proc = None
        self.cancel_ev = threading.Event()

    # ------------------------------------------------------------ spremište ---
    def root(self):
        d = os.path.join(_GCFG["data_dir"], "backups")
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d

    def roots(self):
        """(mjesto, mapa) za lokalnu pohranu i svaki montirani NAS."""
        out = []
        for l in STORE.locations():
            if l["ok"] and os.path.isdir(l["path"]):
                out.append((l, l["path"]))
        return out

    def find(self, bid, loc=None):
        if not BK_ID_RE.match(bid or ""):
            raise ValueError("Neispravan ID sigurnosne kopije")
        for l, root in self.roots():
            if loc and l["id"] != loc:
                continue
            p = os.path.join(root, bid)
            if os.path.isdir(p):
                return l, p
        raise ValueError("Sigurnosna kopija ne postoji (NAS odspojen?)")

    def path(self, bid, name=None, loc=None):
        p = self.find(bid, loc)[1]
        return os.path.join(p, name) if name else p

    def manifest(self, bid, loc=None):
        with open(self.path(bid, "manifest.json", loc), encoding="utf-8") as f:
            return json.load(f)

    def list(self):
        out = []
        for l, root in self.roots():
            try:
                names = os.listdir(root)
            except OSError:
                continue
            for bid in names:
                if not BK_ID_RE.match(bid):
                    continue
                d = os.path.join(root, bid)
                try:
                    with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
                        m = json.load(f)
                except (OSError, ValueError):
                    continue
                size = 0
                for n in BK_FILES:
                    try:
                        size += os.path.getsize(os.path.join(d, n))
                    except OSError:
                        pass
                m.update(id=bid, size=size, location={"id": l["id"], "name": l["name"]})
                out.append(m)
        out.sort(key=lambda m: m["id"], reverse=True)
        return out

    def disk(self):
        try:
            st = os.statvfs(self.root())
            return {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize, "dir": self.root()}
        except OSError:
            return None

    def delete(self, bid, loc=None):
        with self.lock:
            if self.job and self.job.get("running") and self.job.get("backup") == bid:
                raise ValueError("Ova kopija se upravo koristi")
            l, p = self.find(bid, loc)
            shutil.rmtree(p)
        log(f"Backup: obrisana kopija {bid} ({l['name']})")
        return {"ok": True}

    # ------------------------------------------------------------- poslovi ---
    def state(self):
        with self.lock:
            j = dict(self.job) if self.job else None
        if j and j.get("file"):
            try:
                j["bytes"] = os.path.getsize(j["file"])
            except OSError:
                pass
        return j

    def _start(self, kind, title, target, srv, **extra):
        with self.lock:
            if self.job and self.job.get("running"):
                raise ValueError("Već radi: " + self.job.get("title", "posao"))
            self.cancel_ev.clear()
            self.job = {"kind": kind, "title": title, "running": True, "started": now_iso(), "server": srv.name,
                        "server_id": srv.id, "phase": "Pokrećem…", "log": [], "error": None, "result": None,
                        "progress": None, **extra}
        th = CtxThread(target=self._wrap, args=(target, srv), daemon=True)
        th.start()
        return self.state()

    def _wrap(self, target, srv):
        try:
            with use_server(srv):
                res = target()
            with self.lock:
                self.job.update(running=False, phase="Gotovo", result=res, finished=now_iso())
        except BackupCancelled:
            with self.lock:
                self.job.update(running=False, phase="Prekinuto", error="Prekinuto", finished=now_iso())
        except Exception as e:
            traceback.print_exc()
            with self.lock:
                self.job.update(running=False, phase="Greška", error=str(e), finished=now_iso())
            log("Backup greška:", e)
        finally:
            self.proc = None

    def _phase(self, txt, progress=None):
        with self.lock:
            self.job["phase"] = txt
            if progress is not None:
                self.job["progress"] = progress
            self.job["log"].append(f"{time.strftime('%H:%M:%S')} {txt}")
            self.job["log"] = self.job["log"][-200:]

    def _check(self):
        if self.cancel_ev.is_set():
            raise BackupCancelled()

    def cancel(self):
        self.cancel_ev.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
        return {"ok": True}

    # -------------------------------------------------------- SSH (binarno) ---
    def _ssh_cmd(self, op, args=(), stdin_data=False):
        """Kao JFConfig._cmd, ali za binarni izlaz/ulaz. U izravnom načinu skripta ide u argument
        naredbe (base64), da stdin ostane slobodan za arhivu."""
        cmd, stdin = JFC._cmd(op, args)
        if not CFG["ssh_helper"]:
            argstr = " ".join(shlex.quote(str(a)) for a in (op, *args))
            b64 = base64.b64encode((helper_script(standalone=False) + f"\nmain {argstr}\n").encode()).decode()
            inner = 'eval "$(echo ' + b64 + ' | base64 -d)"'
            cmd[-1] = ("sudo -n " if CFG["ssh_sudo"] else "") + "sh -c " + shlex.quote(inner)
            stdin = ""
        return cmd, stdin

    def info(self):
        """Što će ući u puni backup ovog servera (veličine, statistika, Jellyfin kontrola)."""
        res = {"ssh": JFC.enabled(), "api": JF.enabled(), "server": _CUR.srv.name if getattr(_CUR, "srv", None) else None}
        if JFC.enabled():
            try:
                out = JFC.run("bkinfo", timeout=180)
                sz, info = {}, {}
                for line in out.splitlines():
                    p = line.split(" ", 2)
                    if p[0] == "SZ" and len(p) >= 3:
                        sz[p[1]] = int(p[2]) if p[2].isdigit() else None
                    elif p[0] == "STATS" and len(p) >= 3:
                        info["stats"] = {"size": int(p[1]), "path": p[2]}
                    elif p[0] == "CTL":
                        info["ctl"] = None if p[1] == "none" else p[1]
                        info["ctl_name"] = p[2] if len(p) > 2 else None
                    elif p[0] == "CONF":
                        info["conf"] = None if p[1] == "-" else line[5:]
                    elif p[0] == "DF" and len(p) >= 3:
                        f = line.split()
                        info["disk_free"] = int(f[3]) if len(f) > 3 else None
                    elif p[0] == "TAR":
                        info["tar"] = p[1] == "1"
                    elif p[0] == "PREVS":
                        info["prevs"] = int(p[1])
                    elif p[0] == "DBSZ":
                        info["db"] = int(p[1]) if p[1].lstrip("-").isdigit() else None
                info["sizes"] = sz
                res["full"] = info
            except SSHError as e:
                res["full_error"] = str(e)
        if JF.enabled():
            try:
                res["users"] = [{"id": u["Id"], "name": u["Name"], "admin": bool((u.get("Policy") or {}).get("IsAdministrator"))}
                                for u in (JF.req("GET", "/Users") or [])]
            except Exception as e:
                res["users_error"] = str(e)
        res["local_disk"] = self.disk()
        return res

    def _jf_meta(self):
        """Podaci o izvornom Jellyfinu za manifest (verzija, ID servera, putanje)."""
        m = {}
        if JF.enabled():
            try:
                si = JF.req("GET", "/System/Info") or {}
                m = {"version": si.get("Version"), "server_id": si.get("Id"), "server_name": si.get("ServerName"),
                     "os": si.get("OperatingSystemDisplayName"),
                     "paths": {k: si.get(k) for k in ("ProgramDataPath", "CachePath", "LogPath", "InternalMetadataPath",
                                                       "TranscodingTempPath", "ItemsByNamePath") if si.get(k)}}
            except Exception as e:
                m = {"error": str(e)}
        return m

    def _new_dir(self, kind, srv, loc="local"):
        root = STORE.loc(loc)["path"]
        os.makedirs(root, mode=0o700, exist_ok=True)
        bid = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{_slug(srv.name)}"
        p = os.path.join(root, bid)
        os.makedirs(p, mode=0o700)
        return bid, p

    # ----------------------------------------------------------- puni backup ---
    def start_full(self, stop=True, meta=False, loc="local"):
        if not JFC.enabled():
            raise ValueError("Puni backup treba SSH pristup Jellyfin računalu (kartica Serveri → Postavi pristup)")
        if JFC.status and JFC.status.get("helper_outdated"):
            raise ValueError("Pomoćni program na Jellyfin računalu je zastario - na kartici Serveri klikni 'Postavi pristup' i pokreni novu naredbu.")
        srv = _CUR.srv
        L = STORE.loc(loc)
        return self._start("full", f"Puni backup: {srv.name} → {L['name']}", lambda: self._full(srv, bool(stop), bool(meta), L["id"]), srv)

    def _full(self, srv, stop, meta, loc="local"):
        bid, d = self._new_dir("full", srv, loc)
        with self.lock:
            self.job["backup"] = bid
            self.job["file"] = os.path.join(d, "full.tar.gz")
        try:
            self._phase("Podaci o Jellyfinu…")
            jm = self._jf_meta()
            self._phase("Zaustavljam Jellyfin i pakiram podatke…" if stop else "Pakiram podatke (Jellyfin radi)…")
            cmd, stdin = self._ssh_cmd("backup", (1 if stop else 0, 1 if meta else 0))
            errlines = []
            with open(os.path.join(d, "full.tar.gz"), "wb") as out:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE)
                try:
                    self.proc.stdin.write(stdin.encode())
                    self.proc.stdin.close()
                except BrokenPipeError:
                    pass
                for raw in self.proc.stderr:
                    line = raw.decode("utf-8", "replace").rstrip()
                    errlines.append(line)
                    if line.startswith("INFO STOPPED"):
                        self._phase("Jellyfin zaustavljen, pakiram…")
                    elif line.startswith("INFO STARTED"):
                        self._phase("Jellyfin ponovno pokrenut")
                    elif line.startswith("WARN"):
                        self._phase("Upozorenje: " + line[5:])
                rc = self.proc.wait()
            self._check()
            err = next((l[4:] for l in errlines if l.startswith("ERR ")), None)
            if err or rc != 0:
                raise SSHError(err or ("SSH/tar greška: " + " ".join(errlines[-3:])[:300]))
            size = os.path.getsize(os.path.join(d, "full.tar.gz"))
            self._phase("Provjeravam arhivu…")
            names = []
            with tarfile.open(os.path.join(d, "full.tar.gz"), "r:gz") as t:
                for mem in t:
                    self._check()
                    names.append(mem.name)
            stats = any(n.endswith("playback_reporting.db") for n in names)
            if not any(re.search(r"(^|/)data/(data/)?(jellyfin|library)\.db$", n) for n in names):
                raise ValueError("U arhivi nema Jellyfin baze")
            m = {"id": bid, "kind": "full", "created": now_iso(), "app": APP_VERSION,
                 "server": {"id": srv.id, "name": srv.name, "url": CFG["jf_url"], "ssh_host": CFG["ssh_host"]},
                 "jellyfin": jm, "options": {"stopped": stop, "metadata": meta},
                 "contents": {"files": len(names), "stats": stats, "plugins": sum(1 for n in names if "/plugins/" in n and n.endswith(".dll")),
                              "metadata": meta}, "archive_size": size}
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
            log(f"Backup: puni backup {bid} ({human(size)}, statistika: {'da' if stats else 'nema'})")
            return {"backup": bid, "size": size, "stats": stats}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise

    # ----------------------------------------------------- korisnički backup ---
    def start_users(self, user_ids=None, loc="local"):
        if not JF.enabled():
            raise ValueError("Korisnički backup treba Jellyfin URL i API ključ (kartica Serveri)")
        srv = _CUR.srv
        L = STORE.loc(loc)
        return self._start("users", f"Korisnički backup: {srv.name} → {L['name']}", lambda: self._users(srv, user_ids or [], L["id"]), srv)

    def _items(self, uid, extra):
        out, start = [], 0
        while True:
            self._check()
            params = dict(Recursive="true", IncludeItemTypes=BK_ITEM_TYPES, EnableUserData="true", StartIndex=start, Limit=500,
                          Fields="ProviderIds,Path,ProductionYear,ParentIndexNumber,IndexNumber,SeriesId,SeriesName,MediaType")
            params.update(extra)
            r = JF.req("GET", f"/Users/{uid}/Items", params, timeout=180) or {}
            items = r.get("Items") or []
            out += items
            start += len(items)
            if not items or start >= (r.get("TotalRecordCount") or 0):
                return out

    @staticmethod
    def _key(it):
        return {"id": it.get("Id"), "type": it.get("Type"), "name": it.get("Name"), "year": it.get("ProductionYear"),
                "path": it.get("Path"), "pids": _pids(it.get("ProviderIds")), "season": it.get("ParentIndexNumber"),
                "episode": it.get("IndexNumber"), "series_id": it.get("SeriesId"), "series": it.get("SeriesName")}

    def _users(self, srv, sel, loc="local"):
        bid, d = self._new_dir("users", srv, loc)
        with self.lock:
            self.job["backup"] = bid
        try:
            jm = self._jf_meta()
            users = JF.req("GET", "/Users") or []
            if sel:
                users = [u for u in users if u["Id"] in sel]
            if not users:
                raise ValueError("Nema korisnika za backup")
            libs = [{"id": v.get("ItemId"), "name": v.get("Name")} for v in (JF.req("GET", "/Library/VirtualFolders") or [])]
            series_ids, data = set(), []
            for n, u in enumerate(users, 1):
                self._phase(f"Korisnik {u['Name']} ({n}/{len(users)})…", {"done": n - 1, "total": len(users)})
                seen = {}
                for flt in ("IsPlayed", "IsFavorite", "IsResumable", "Likes", "Dislikes"):
                    for it in self._items(u["Id"], {"Filters": flt}):
                        seen[it["Id"]] = it
                items, playlists = [], []
                for it in seen.values():
                    if it.get("Type") == "Playlist":
                        continue
                    ud = it.get("UserData") or {}
                    k = self._key(it)
                    k["ud"] = {x: ud.get(x) for x in ("Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite",
                                                       "Likes", "Rating", "LastPlayedDate") if ud.get(x) is not None}
                    if it.get("SeriesId"):
                        series_ids.add(it["SeriesId"])
                    items.append(k)
                for pl in self._items(u["Id"], {"IncludeItemTypes": "Playlist"}):
                    if pl.get("Type") != "Playlist":
                        continue
                    self._check()
                    r = JF.req("GET", f"/Playlists/{pl['Id']}/Items", {"UserId": u["Id"], "Fields": "ProviderIds,Path,ProductionYear,ParentIndexNumber,IndexNumber,SeriesId,SeriesName"}) or {}
                    pitems = []
                    for it in r.get("Items") or []:
                        pitems.append(self._key(it))
                        if it.get("SeriesId"):
                            series_ids.add(it["SeriesId"])
                    playlists.append({"name": pl.get("Name"), "media_type": pl.get("MediaType"), "items": pitems})
                full = JF.req("GET", f"/Users/{u['Id']}") or u
                data.append({"id": u["Id"], "name": u["Name"], "configuration": full.get("Configuration"),
                             "policy": full.get("Policy"), "items": items, "playlists": playlists})
                self._phase(f"Korisnik {u['Name']}: {len(items)} stavki, {len(playlists)} playlista")
            # oznake serija (za prepoznavanje epizoda na drugom serveru)
            self._phase("Oznake serija…")
            series, ids = {}, list(series_ids)
            for i in range(0, len(ids), 150):
                self._check()
                r = JF.req("GET", "/Items", {"Ids": ",".join(ids[i:i + 150]), "Fields": "ProviderIds,Path,ProductionYear"}) or {}
                for s in r.get("Items") or []:
                    series[s["Id"]] = {"name": s.get("Name"), "year": s.get("ProductionYear"), "pids": _pids(s.get("ProviderIds")), "path": s.get("Path")}
            doc = {"format": 1, "created": now_iso(), "server": {"id": srv.id, "name": srv.name, "url": CFG["jf_url"]},
                   "jellyfin": jm, "libraries": libs, "series": series, "users": data}
            with open(os.path.join(d, "users.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            cnt = {"users": len(data), "items": sum(len(u["items"]) for u in data),
                   "played": sum(1 for u in data for i in u["items"] if i["ud"].get("Played")),
                   "favorites": sum(1 for u in data for i in u["items"] if i["ud"].get("IsFavorite")),
                   "resume": sum(1 for u in data for i in u["items"] if (i["ud"].get("PlaybackPositionTicks") or 0) > 0),
                   "playlists": sum(len(u["playlists"]) for u in data)}
            m = {"id": bid, "kind": "users", "created": now_iso(), "app": APP_VERSION,
                 "server": {"id": srv.id, "name": srv.name, "url": CFG["jf_url"]}, "jellyfin": jm,
                 "contents": cnt, "user_names": [u["name"] for u in data]}
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=1)
            log(f"Backup: korisnički backup {bid} ({cnt['users']} korisnika, {cnt['items']} stavki)")
            return {"backup": bid, **cnt}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise

    # ---------------------------------------------------------------- vraćanje ---
    def start_restore(self, bid, target_sid, opts, loc=None):
        loc = self.find(bid, loc)[0]["id"]
        m = self.manifest(bid, loc)
        srv = SERVERS.get(target_sid) if target_sid else _CUR.srv
        if not srv:
            raise ValueError("Odredišni server ne postoji")
        same = m.get("server", {}).get("id") == srv.id
        fn = self._restore_full if m["kind"] == "full" else self._restore_users
        with use_server(srv):
            if m["kind"] == "full" and not JFC.enabled():
                raise ValueError(f"Za vraćanje punog backupa {srv.name} treba SSH pristup (kartica Serveri → Postavi pristup)")
            if m["kind"] == "users" and not JF.enabled():
                raise ValueError(f"Za vraćanje korisničkog backupa {srv.name} treba Jellyfin URL i API ključ")
        return self._start("restore", f"Vraćanje {'punog' if m['kind'] == 'full' else 'korisničkog'} backupa → {srv.name}",
                           lambda: fn(bid, m, srv, same, opts or {}, loc), srv, backup=bid)

    def _restore_full(self, bid, m, srv, same, opts, loc=None):
        src = (m.get("jellyfin") or {})
        pairs = []
        dst = self._jf_meta()
        if src.get("version") and dst.get("version"):
            try:
                if [int(x) for x in re.findall(r"\d+", dst["version"])[:3]] < [int(x) for x in re.findall(r"\d+", src["version"])[:3]]:
                    if not opts.get("allow_downgrade"):
                        raise ValueError(f"Odredišni Jellyfin ({dst['version']}) je stariji od izvornog ({src['version']}) - "
                                         "baza se ne može vratiti na stariju verziju. Prvo nadogradi Jellyfin na odredištu.")
            except ValueError:
                raise
        if opts.get("remap", not same):
            sp, dp = src.get("paths") or {}, dst.get("paths") or {}
            for k in ("ProgramDataPath", "CachePath", "LogPath", "InternalMetadataPath", "TranscodingTempPath"):
                a, b = (sp.get(k) or "").rstrip("/"), (dp.get(k) or "").rstrip("/")
                if a and b and a != b and a.startswith("/") and b.startswith("/") and "#" not in a + b and "|" not in a + b:
                    pairs.append((a, b))
            pairs.sort(key=lambda p: -len(p[0]))
        b64 = base64.b64encode("".join(f"{a}|{b}\n" for a, b in pairs).encode()).decode() if pairs else ""
        newid = 1 if opts.get("new_server_id") else 0
        arc = self.path(bid, "full.tar.gz", loc)
        total = os.path.getsize(arc)
        self._phase(f"Šaljem arhivu ({human(total)}) na {CFG['ssh_host']}…", {"done": 0, "total": total})
        cmd, _ = self._ssh_cmd("restore", (b64, newid))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sent = 0
        try:
            with open(arc, "rb") as f:
                while True:
                    self._check()
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.proc.stdin.write(chunk)
                    sent += len(chunk)
                    with self.lock:
                        self.job["progress"] = {"done": sent, "total": total}
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        self._phase("Jellyfin računalo raspakirava i vraća podatke…")
        out, err = self.proc.communicate(timeout=7200)
        out = out.decode("utf-8", "replace")
        res = {"pairs": pairs, "new_server_id": bool(newid), "same_server": same}
        for line in out.splitlines():
            if line.startswith("ERR "):
                raise SSHError(line[4:])
            p = line.split(" ", 1)
            if p[0] == "PREV":
                res["prev"] = p[1]
            elif p[0] == "STARTED":
                res["started"] = True
            elif p[0] == "WARN":
                res["warning"] = p[1]
            elif p[0] == "REMAP":
                res["remapped_files"] = int(p[1])
        if "DONE" not in out:
            raise SSHError("Vraćanje nije završilo: " + (err.decode("utf-8", "replace").strip() or out[-300:])[:400])
        log(f"Backup: puni backup {bid} vraćen na {srv.name}" + (f", stari podaci u {res.get('prev')}" if res.get("prev") else ""))
        if JF.enabled():
            self._phase("Čekam da Jellyfin krene…")
            for _ in range(90):
                try:
                    JF.req("GET", "/System/Info/Public", timeout=5)
                    break
                except Exception:
                    time.sleep(4)
        return res

    def _target_index(self, admin):
        idx = {"id": set(), "path": {}, "pid": {}, "ep": {}, "name": {}, "series_pid": {}, "series_name": {}}
        start = 0
        while True:
            self._check()
            r = JF.req("GET", f"/Users/{admin}/Items", dict(
                Recursive="true", IncludeItemTypes=BK_ITEM_TYPES, StartIndex=start, Limit=1000, EnableUserData="false",
                Fields="ProviderIds,Path,ProductionYear,ParentIndexNumber,IndexNumber,SeriesId"), timeout=300) or {}
            items = r.get("Items") or []
            for it in items:
                iid, t = it["Id"], it.get("Type")
                idx["id"].add(iid)
                if it.get("Path"):
                    idx["path"].setdefault(it["Path"], iid)
                for k, v in _pids(it.get("ProviderIds")).items():
                    idx["pid"].setdefault((t, k, v), iid)
                    if t == "Series":
                        idx["series_pid"].setdefault((k, v), iid)
                if t == "Series":
                    idx["series_name"].setdefault(_norm_title(it.get("Name")), iid)
                if t == "Episode" and it.get("SeriesId") and it.get("IndexNumber") is not None:
                    idx["ep"].setdefault((it["SeriesId"], it.get("ParentIndexNumber"), it["IndexNumber"]), iid)
                idx["name"].setdefault((t, _norm_title(it.get("Name")), it.get("ProductionYear")), iid)
            start += len(items)
            with self.lock:
                self.job["progress"] = {"done": start, "total": r.get("TotalRecordCount") or start}
            if not items or start >= (r.get("TotalRecordCount") or 0):
                return idx

    @staticmethod
    def _match(k, idx, series, same, path_map):
        if same and k.get("id") in idx["id"]:
            return k["id"]
        p = k.get("path")
        if p:
            if p in idx["path"]:
                return idx["path"][p]
            for a, b in path_map:
                if p.startswith(a) and (b + p[len(a):]) in idx["path"]:
                    return idx["path"][b + p[len(a):]]
        t = k.get("type")
        for kk in ("imdb", "tmdb", "tvdb"):
            v = (k.get("pids") or {}).get(kk)
            if v and (t, kk, v) in idx["pid"]:
                return idx["pid"][(t, kk, v)]
        if t == "Episode" and k.get("episode") is not None:
            s = series.get(k.get("series_id") or "") or {"name": k.get("series"), "pids": {}}
            tsid = None
            for kk in ("tvdb", "tmdb", "imdb"):
                v = (s.get("pids") or {}).get(kk)
                if v and (kk, v) in idx["series_pid"]:
                    tsid = idx["series_pid"][(kk, v)]
                    break
            tsid = tsid or idx["series_name"].get(_norm_title(s.get("name")))
            if tsid:
                return idx["ep"].get((tsid, k.get("season"), k["episode"]))
            return None
        return idx["name"].get((t, _norm_title(k.get("name")), k.get("year")))

    def _set_userdata(self, uid, iid, ud):
        body = {x: ud[x] for x in ("Played", "PlayCount", "PlaybackPositionTicks", "IsFavorite", "Likes", "Rating", "LastPlayedDate") if x in ud}
        for path in (f"/UserItems/{iid}/UserData", f"/Users/{uid}/Items/{iid}/UserData"):
            try:
                JF.req("POST", path, {"userId": uid} if path.startswith("/UserItems") else None, body)
                return True
            except urllib.error.HTTPError as e:
                if e.code not in (404, 405):
                    raise
        # stari Jellyfin: zasebne radnje
        if ud.get("Played"):
            JF.req("POST", f"/Users/{uid}/PlayedItems/{iid}", {"datePlayed": ud.get("LastPlayedDate")} if ud.get("LastPlayedDate") else None)
        if ud.get("IsFavorite"):
            JF.req("POST", f"/Users/{uid}/FavoriteItems/{iid}")
        return True

    def _restore_users(self, bid, m, srv, same, opts, loc=None):
        with open(self.path(bid, "users.json", loc), encoding="utf-8") as f:
            doc = json.load(f)
        sel = set(opts.get("users") or [])
        src_users = [u for u in doc["users"] if not sel or u["id"] in sel or u["name"] in sel]
        self._phase("Korisnici na odredištu…")
        tusers = JF.req("GET", "/Users") or []
        byname = {u["Name"].lower(): u for u in tusers}
        admin = next((u["Id"] for u in tusers if (u.get("Policy") or {}).get("IsAdministrator")), tusers[0]["Id"] if tusers else None)
        if not admin:
            raise ValueError("Na odredišnom Jellyfinu nema korisnika")
        tlibs = {(v.get("Name") or "").lower(): v.get("ItemId") for v in (JF.req("GET", "/Library/VirtualFolders") or [])}
        slibs = {l["id"]: (l.get("name") or "").lower() for l in doc.get("libraries") or []}
        libmap = {sid: tlibs.get(n) for sid, n in slibs.items() if tlibs.get(n)}
        path_map = [(a.rstrip("/"), b.rstrip("/")) for a, b in (opts.get("path_map") or []) if a and b]
        self._phase("Popis stavki na odredištu…")
        idx = self._target_index(admin)
        series = doc.get("series") or {}
        report = {"users": [], "created_users": [], "same_server": same}
        for n, su in enumerate(src_users, 1):
            self._check()
            tu = byname.get(su["name"].lower())
            if not tu and opts.get("create_users", True):
                pw = secrets.token_urlsafe(9)
                tu = JF.req("POST", "/Users/New", None, {"Name": su["name"], "Password": pw})
                report["created_users"].append({"name": su["name"], "password": pw})
                log(f"Backup: na {srv.name} stvoren korisnik {su['name']}")
            if not tu:
                report["users"].append({"name": su["name"], "skipped": "korisnik ne postoji na odredištu"})
                continue
            uid = tu["Id"]
            ur = {"name": su["name"], "matched": 0, "applied": 0, "unmatched": [], "playlists": 0, "errors": 0}
            total = len(su["items"])
            for i, k in enumerate(su["items"], 1):
                if i % 25 == 0:
                    self._check()
                    with self.lock:
                        self.job["progress"] = {"done": i, "total": total}
                        self.job["phase"] = f"Korisnik {su['name']} ({n}/{len(src_users)}): {i}/{total}"
                iid = self._match(k, idx, series, same, path_map)
                if not iid:
                    ur["unmatched"].append({"type": k.get("type"), "name": k.get("name"), "year": k.get("year"),
                                            "series": k.get("series"), "season": k.get("season"), "episode": k.get("episode")})
                    continue
                ur["matched"] += 1
                try:
                    self._set_userdata(uid, iid, k.get("ud") or {})
                    ur["applied"] += 1
                except Exception as e:
                    ur["errors"] += 1
                    if ur["errors"] <= 3:
                        log(f"Backup: {su['name']} / {k.get('name')}: {e}")
            if opts.get("playlists", True):
                existing = {}
                try:
                    r = JF.req("GET", f"/Users/{uid}/Items", {"Recursive": "true", "IncludeItemTypes": "Playlist"}) or {}
                    existing = {(p.get("Name") or "").lower(): p["Id"] for p in r.get("Items") or []}
                except Exception:
                    pass
                for pl in su.get("playlists") or []:
                    ids = [x for x in (self._match(k, idx, series, same, path_map) for k in pl["items"]) if x]
                    if not ids:
                        continue
                    try:
                        pid = existing.get((pl["name"] or "").lower())
                        if pid:
                            cur = {it["Id"] for it in ((JF.req("GET", f"/Playlists/{pid}/Items", {"UserId": uid}) or {}).get("Items") or [])}
                            add = [x for x in ids if x not in cur]
                            for j in range(0, len(add), 100):
                                JF.req("POST", f"/Playlists/{pid}/Items", {"ids": ",".join(add[j:j + 100]), "userId": uid})
                        else:
                            JF.req("POST", "/Playlists", None, {"Name": pl["name"], "Ids": ids, "UserId": uid,
                                                                 "MediaType": pl.get("media_type") or None})
                        ur["playlists"] += 1
                    except Exception as e:
                        log(f"Backup: playlista {pl['name']}: {e}")
            if opts.get("settings", True) and su.get("configuration"):
                c = dict(su["configuration"])
                for key in ("OrderedViews", "MyMediaExcludes", "LatestItemsExcludes", "GroupedFolders"):
                    if isinstance(c.get(key), list):
                        c[key] = [libmap[x] for x in c[key] if x in libmap] if not same else c[key]
                try:
                    JF.req("POST", f"/Users/{uid}/Configuration", None, c)
                    ur["settings"] = True
                except Exception as e:
                    log(f"Backup: postavke {su['name']}: {e}")
            if opts.get("policy") and su.get("policy"):
                pol = dict(tu.get("Policy") or {})
                sp = dict(su["policy"])
                for key in ("AuthenticationProviderId", "PasswordResetProviderId"):
                    sp.pop(key, None)
                if not same and isinstance(sp.get("EnabledFolders"), list):
                    sp["EnabledFolders"] = [libmap[x] for x in sp["EnabledFolders"] if x in libmap]
                    sp.pop("EnabledChannels", None)
                pol.update(sp)
                try:
                    JF.req("POST", f"/Users/{uid}/Policy", None, pol)
                    ur["policy"] = True
                except Exception as e:
                    log(f"Backup: dozvole {su['name']}: {e}")
            ur["unmatched_count"] = len(ur["unmatched"])
            report["users"].append(ur)
            self._phase(f"Korisnik {su['name']}: prepoznato {ur['matched']}/{total}, nije pronađeno {len(ur['unmatched'])}")
        rp = f"restore-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            with open(os.path.join(self.path(bid, None, loc), rp), "w", encoding="utf-8") as f:
                json.dump({"target": srv.name, **report}, f, ensure_ascii=False, indent=1)
            report["report_file"] = rp
        except OSError:
            pass
        log(f"Backup: korisnički backup {bid} vraćen na {srv.name}")
        for u in report["users"]:
            u["unmatched"] = u.get("unmatched", [])[:300]
        return report

    # ------------------------------------- kopiranje između lokalnog i NAS-a ---
    def start_copy(self, bid, loc, move=False, src_loc=None):
        src_l, src = self.find(bid, src_loc)
        L = STORE.loc(loc)
        if L["id"] == src_l["id"]:
            raise ValueError("Kopija je već na tom mjestu")
        files = [n for n in os.listdir(src) if n in BK_FILES or re.fullmatch(r"restore-[0-9-]+\.json", n)]
        total = sum(os.path.getsize(os.path.join(src, n)) for n in files)
        if L.get("free") is not None and total > L["free"] - 64 * 1024 * 1024:
            raise ValueError(f"Nema dovoljno mjesta: {L['name']} ({human(L['free'])} slobodno, treba {human(total)})")
        srv = _CUR.srv
        verb = "Premještam" if move else "Kopiram"
        return self._start("copy", f"{verb} kopiju na {L['name']}", lambda: self._copy(bid, src, L, files, total, move), srv, backup=bid)

    def _copy(self, bid, src, L, files, total, move):
        root = L["path"]
        os.makedirs(root, mode=0o700, exist_ok=True)
        dst = os.path.join(root, bid)
        if os.path.exists(dst):
            raise ValueError("Na odredištu već postoji kopija s tim ID-om")
        tmp = dst + ".part"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, mode=0o700)
        done = 0
        try:
            for n in files:
                self._phase(f"{n}…")
                with open(os.path.join(src, n), "rb") as fi, open(os.path.join(tmp, n), "wb") as fo:
                    while True:
                        self._check()
                        b = fi.read(4 << 20)
                        if not b:
                            break
                        fo.write(b)
                        done += len(b)
                        with self.lock:
                            self.job["progress"] = {"done": done, "total": total}
                    fo.flush()
                    os.fsync(fo.fileno())
                if os.path.getsize(os.path.join(tmp, n)) != os.path.getsize(os.path.join(src, n)):
                    raise ValueError(f"{n}: veličina se ne podudara nakon kopiranja")
            os.rename(tmp, dst)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if move:
            shutil.rmtree(src)
        log(f"Backup: {bid} {'premješten' if move else 'kopiran'} na {L['name']}")
        return {"backup": bid, "copied_to": L["name"], "moved": move, "size": total}

    # ------------------------------------------ kopije prije vraćanja (SSH) ---
    def prevs(self):
        out = JFC.run("restorelist", timeout=120)
        return [{"size": int(p[1]), "name": p[2]} for p in (l.split(" ", 2) for l in out.splitlines()) if p[0] == "PREVDIR" and len(p) == 3]

    def prevs_clean(self):
        out = JFC.run("restoreclean", timeout=600)
        n = next((int(l.split()[1]) for l in out.splitlines() if l.startswith("DEL ")), 0)
        log(f"Backup: obrisano {n} kopija prije vraćanja")
        return {"deleted": n}

    # -------------------------------------------- preuzimanje / učitavanje ---
    def stream_download(self, bid, wfile, loc=None):
        d = self.path(bid, None, loc)
        with tarfile.open(fileobj=wfile, mode="w|") as t:
            for n in sorted(os.listdir(d)):
                if n in BK_FILES or re.fullmatch(r"restore-[0-9-]+\.json", n):
                    t.add(os.path.join(d, n), arcname=f"{bid}/{n}", recursive=False)

    def upload(self, rfile, length, loc="local"):
        if length <= 0:
            raise ValueError("Prazna datoteka")
        L = STORE.loc(loc)
        root = L["path"]
        os.makedirs(root, mode=0o700, exist_ok=True)
        free = L.get("free") or 0
        if length > free - 256 * 1024 * 1024:
            raise ValueError(f"Nema dovoljno mjesta: {L['name']} ({human(free)} slobodno)")
        tmp = os.path.join(root, f".upload-{secrets.token_hex(4)}")
        os.makedirs(tmp, mode=0o700)
        try:
            raw = os.path.join(tmp, "in.tar")
            left = length
            with open(raw, "wb") as f:
                while left > 0:
                    chunk = rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("Prijenos prekinut")
                    f.write(chunk)
                    left -= len(chunk)
            try:
                t = tarfile.open(raw, "r:*")
            except tarfile.TarError:
                raise ValueError("Datoteka nije JellyClean sigurnosna kopija (.tar)")
            with t:
                members = t.getmembers()
                tops = {m.name.split("/", 1)[0] for m in members}
                if len(tops) != 1:
                    raise ValueError("Arhiva mora sadržavati točno jednu sigurnosnu kopiju")
                src = tops.pop()
                if not BK_ID_RE.match(src):
                    raise ValueError("Naziv kopije u arhivi nije ispravan")
                files = {}
                for mem in members:
                    rest = mem.name.split("/", 1)[1] if "/" in mem.name else ""
                    if not rest:
                        continue
                    if not mem.isfile() or "/" in rest or not (rest in BK_FILES or re.fullmatch(r"restore-[0-9-]+\.json", rest)):
                        raise ValueError(f"Neočekivana datoteka u arhivi: {mem.name}")
                    files[rest] = mem
                if "manifest.json" not in files:
                    raise ValueError("U arhivi nema manifest.json")
                m = json.load(t.extractfile(files["manifest.json"]))
                kind = m.get("kind")
                need = {"full": "full.tar.gz", "users": "users.json"}.get(kind)
                if not need or need not in files:
                    raise ValueError("Arhiva nije potpuna")
                bid = src
                try:
                    self.find(bid)
                    bid = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{_slug('ucitano-' + (m.get('server') or {}).get('name', ''))}"
                except ValueError:
                    pass
                dst = os.path.join(root, bid)
                os.makedirs(dst, mode=0o700)
                for name, mem in files.items():
                    with t.extractfile(mem) as fin, open(os.path.join(dst, name), "wb") as fout:
                        shutil.copyfileobj(fin, fout, 1 << 20)
                m["id"] = bid
                m["uploaded"] = now_iso()
                with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
                    json.dump(m, f, ensure_ascii=False, indent=1)
            log(f"Backup: učitana kopija {bid}")
            return {"ok": True, "id": bid, "kind": kind}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


BK = Backups()


import getpass

# --------------------------------------------------------------------------
# Pohrana na NAS (NFS) - montira se UNUTAR kontejnera JellyCleana preko root pomoćnog
# programa jellyclean-nas (sudo -n), samo ispod /mnt/jellyclean-nas/<id>.
# --------------------------------------------------------------------------
NAS_BASE = "/mnt/jellyclean-nas"
NAS_HELPER = "/usr/local/sbin/jellyclean-nas"
NAS_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
NAS_SRC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*:/[A-Za-z0-9._/@+-]*$")
NAS_SUB_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")


class Storage:
    def __init__(self):
        self.lock = threading.RLock()

    def _file(self):
        return os.path.join(_GCFG["data_dir"], "storage.json")

    def load(self):
        try:
            with open(self._file(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, d):
        tmp = self._file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._file())

    def nas(self):
        return [n for n in (self.load().get("nas") or []) if NAS_ID_RE.match(n.get("id") or "")]

    @staticmethod
    def helper_ok():
        return os.path.exists(NAS_HELPER)

    def _helper(self, *args, timeout=60):
        if not self.helper_ok():
            raise ValueError("Pomoćni program jellyclean-nas nije instaliran. Na Proxmox hostu pokreni update-lxc.sh "
                             "ili u kontejneru: bash /opt/jellyclean/proxmox/lxc-post-update.sh")
        cmd = [NAS_HELPER, *map(str, args)]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise ValueError("sudo nije instaliran u kontejneru - pokreni lxc-post-update.sh")
        except subprocess.TimeoutExpired:
            raise ValueError("NAS ne odgovara (isteklo vrijeme)")
        err = (p.stderr or "").strip()
        lines = (p.stdout or "").splitlines()
        # sudo zna ispisati upozorenja i kad naredba uspije (npr. "unable to resolve host") - greška je samo ako nije uspjelo
        if p.returncode != 0 and not lines and "sudo:" in err:
            msg = next((l for l in err.splitlines() if l.startswith("sudo:")), err)[:200]
            raise ValueError(f"Servis (korisnik {getpass.getuser()}) ne smije pokrenuti jellyclean-nas preko sudo ({msg}). "
                             "U kontejneru JellyCleana kao root pokreni: bash /opt/jellyclean/proxmox/lxc-post-update.sh "
                             "(s Proxmox hosta: pct exec <ID> -- bash /opt/jellyclean/proxmox/lxc-post-update.sh)")
        for l in lines:
            if l.startswith("ERR "):
                raise ValueError(l[4:])
        if p.returncode != 0:
            raise ValueError(err[:300] or f"jellyclean-nas: izlazni kod {p.returncode}")
        return lines

    @staticmethod
    def dir_of(n):
        return os.path.join(NAS_BASE, n["id"], n.get("subdir") or "jellyclean")

    @staticmethod
    def _write_test(d):
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, f".jellyclean-test-{secrets.token_hex(3)}")
            with open(t, "w") as f:
                f.write("ok")
            os.remove(t)
            return True, None
        except OSError as e:
            return False, f"{e.strerror or e} (korisnik {getpass.getuser()}, UID {os.geteuid()})"

    def status(self):
        res = {"helper": self.helper_ok(), "user": getpass.getuser(), "uid": os.geteuid(), "nas": [], "error": None}
        mounts = {}
        if res["helper"]:
            try:
                for l in self._helper("status"):
                    p = l.split()
                    if p[0] == "NAS" and len(p) >= 8:
                        num = lambda x: int(x) if x.isdigit() else None
                        mounts[p[1]] = {"mounted": p[2] == "1", "src": p[3], "total": num(p[5]), "used": num(p[6]), "free": num(p[7])}
            except ValueError as e:
                res["error"] = str(e)
        for n in self.nas():
            m = mounts.get(n["id"], {})
            e = dict(n, mounted=bool(m.get("mounted")), in_fstab=n["id"] in mounts, total=m.get("total"), used=m.get("used"),
                     free=m.get("free"), path=self.dir_of(n), writable=False, write_error=None)
            if e["mounted"]:
                e["writable"], e["write_error"] = self._write_test(e["path"])
            res["nas"].append(e)
        return res

    def exports(self, host):
        host = (host or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError("Upiši IP adresu ili ime NAS-a")
        out = []
        for l in self._helper("exports", host, timeout=30):
            p = l.split(" ", 2)
            if p[0] == "EXPORT" and len(p) >= 2:
                out.append({"path": p[1], "clients": p[2] if len(p) > 2 else ""})
        return {"host": host, "exports": out}

    @staticmethod
    def _check(src, vers):
        src = (src or "").strip()
        if not NAS_SRC_RE.match(src):
            raise ValueError("NFS izvor mora biti oblika NAS:/putanja (bez razmaka)")
        vers = str(vers or "auto")
        if vers not in ("auto", "3", "4", "4.1", "4.2"):
            raise ValueError("Neispravna verzija NFS-a")
        return src, vers

    def test(self, src, vers):
        src, vers = self._check(src, vers)
        for l in self._helper("test", src, vers, timeout=60):
            p = l.split()
            if p[0] == "OK":
                num = lambda x: int(x) if x.isdigit() else None
                return {"ok": True, "entries": num(p[1]), "total": num(p[2]), "used": num(p[3]), "free": num(p[4])}
        raise ValueError("Neočekivan odgovor pomoćnog programa")

    def add(self, name, src, vers, subdir):
        src, vers = self._check(src, vers)
        name = (name or "").strip()[:40] or src.split(":", 1)[0]
        subdir = (subdir or "jellyclean").strip().strip("/")
        if not NAS_SUB_RE.match(subdir):
            raise ValueError("Mapa na NAS-u smije sadržavati slova, brojke, točku, crticu i podvlaku")
        with self.lock:
            d = self.load()
            nas = d.get("nas") or []
            if any(n.get("src") == src for n in nas):
                raise ValueError("Taj NFS export je već dodan")
            base = _slug(name)[:20] or "nas"
            nid, i = base, 2
            while any(n["id"] == nid for n in nas) or os.path.exists(os.path.join(NAS_BASE, nid)):
                nid, i = f"{base}-{i}", i + 1
            self._helper("add", nid, src, vers, timeout=60)
            ent = {"id": nid, "name": name, "src": src, "vers": vers, "subdir": subdir, "created": now_iso()}
            nas.append(ent)
            d["nas"] = nas
            self.save(d)
        ok, err = self._write_test(self.dir_of(ent))
        log(f"Pohrana: dodan NAS {name} ({src})" + ("" if ok else f" - nema prava pisanja: {err}"))
        return dict(ent, writable=ok, write_error=err)

    def remove(self, nid):
        if not NAS_ID_RE.match(nid or ""):
            raise ValueError("Neispravan ID")
        with self.lock:
            d = self.load()
            if BK.job and BK.job.get("running"):
                raise ValueError("Pričekaj da završi posao s kopijama")
            self._helper("remove", nid, timeout=60)
            d["nas"] = [n for n in d.get("nas") or [] if n.get("id") != nid]
            self.save(d)
        log(f"Pohrana: uklonjen NAS {nid} (datoteke na NAS-u nisu dirane)")
        return {"ok": True}

    def mount(self, nid, on=True):
        if not NAS_ID_RE.match(nid or ""):
            raise ValueError("Neispravan ID")
        self._helper("mount" if on else "umount", nid, timeout=60)
        return {"ok": True}

    @staticmethod
    def is_mounted(mp):
        try:
            with open("/proc/self/mounts", encoding="utf-8") as f:
                for line in f:
                    p = line.split()
                    if len(p) > 1 and p[1].replace("\\040", " ") == mp:
                        return True
        except OSError:
            pass
        return os.path.ismount(mp)

    def locations(self):
        """Mjesta za sigurnosne kopije: lokalno + montirani NAS-ovi."""
        locs = [{"id": "local", "name": "Lokalno (JellyClean)", "path": BK.root(), "ok": True, "free": (BK.disk() or {}).get("free")}]
        for n in self.nas():
            mp = os.path.join(NAS_BASE, n["id"])
            ok = self.is_mounted(mp)
            free = None
            if ok:
                try:
                    st = os.statvfs(mp)
                    free = st.f_bavail * st.f_frsize
                except OSError:
                    ok = False
            locs.append({"id": n["id"], "name": f"NAS: {n.get('name') or n['id']}", "path": self.dir_of(n), "ok": ok, "free": free,
                         "src": n.get("src")})
        return locs

    def loc(self, lid):
        lid = lid or "local"
        for l in self.locations():
            if l["id"] == lid:
                if not l["ok"]:
                    raise ValueError(f"{l['name']} nije montiran - na kartici Backup klikni 'Montiraj'")
                return l
        raise ValueError("Nepoznato mjesto pohrane")


STORE = Storage()


def scheduler():
    last_scan = time.time()
    last_purge = 0
    last_pve = 0
    while True:
        time.sleep(60)
        try:
            if time.time() - last_pve > 1800 and PVE.load().get("active"):
                last_pve = time.time()
                r = PVE.status(check=True)
                if r["state"] not in ("ok", "unchecked"):
                    log(f"Proxmox pristup: {r['state']} - {r.get('message') or ''}")
            if CFG["scan_hours"] > 0 and time.time() - last_scan > CFG["scan_hours"] * 3600:
                last_scan = time.time()
                run_scan()
                sync_all_servers()
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

    def _pick_server(self):
        sid = self.headers.get("X-JC-Server") or ""
        if not sid:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (q.get("srv") or [""])[0]
        _CUR.srv = SERVERS.get(sid) or SERVERS.default()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/pve/setup.sh":
            t = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
            try:
                script = PVE.script_for_token(t)
            except (PveError, OSError):
                script = None
            if not script:
                return self._send(403, "echo 'JellyClean: poveznica je istekla ili nije ispravna - napravi novu u sučelju (Migracija)' >&2; exit 1\n",
                                  "text/x-shellscript; charset=utf-8")
            return self._send(200, script, "text/x-shellscript; charset=utf-8")
        if u.path == "/api/servers/bootstrap.sh":
            t = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
            ent = BOOT_TOKENS.get(t)
            sv = SERVERS.get(ent[0]) if ent and ent[1] > time.time() else None
            if not sv:
                return self._send(403, "echo 'JellyClean: poveznica je istekla ili nije ispravna - napravi novu u sučelju' >&2; exit 1\n",
                                  "text/x-shellscript; charset=utf-8")
            return self._send(200, bootstrap_script(sv), "text/x-shellscript; charset=utf-8")
        self._pick_server()
        try:
            return self._do_GET()
        finally:
            _CUR.srv = None

    def do_POST(self):
        self._pick_server()
        try:
            return self._do_POST()
        finally:
            _CUR.srv = None

    def _do_GET(self):
        if not self._auth_ok():
            return self._deny()
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/storage":
                return self._send(200, STORE.status())
            if u.path == "/api/backup":
                return self._send(200, {"backups": BK.list(), "job": BK.state(), "disk": BK.disk(), "locations": STORE.locations(),
                                        "servers": [{"id": s.id, "name": s.name} for s in SERVERS.all()],
                                        "current": _CUR.srv.id if getattr(_CUR, "srv", None) else None})
            if u.path == "/api/backup/info":
                return self._send(200, BK.info())
            if u.path == "/api/backup/prevs":
                return self._send(200, {"prevs": BK.prevs()})
            if u.path == "/api/backup/manifest":
                return self._send(200, BK.manifest(q.get("id"), q.get("loc")))
            if u.path == "/api/backup/report":
                if not re.fullmatch(r"restore-[0-9-]+\.json", q.get("file") or ""):
                    return self._send(400, {"error": "neispravna datoteka"})
                with open(BK.path(q.get("id"), q["file"], q.get("loc")), encoding="utf-8") as f:
                    return self._send(200, json.load(f))
            if u.path == "/api/backup/download":
                bid = q.get("id") or ""
                BK.manifest(bid, q.get("loc"))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-tar")
                self.send_header("Content-Disposition", f'attachment; filename="jellyclean-{bid}.tar"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    BK.stream_download(bid, self.wfile, q.get("loc"))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return
            if u.path == "/api/pve":
                return self._send(200, PVE.status(check=q.get("check", "1") != "0"))
            if u.path == "/api/pve/migrate":
                return self._send(200, PVE.mig_status(q.get("offset") or 0))
            if u.path == "/api/pve/log":
                return self._send(200, PVE.mig_log())
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
            if u.path == "/api/servers":
                try:
                    pub = ssh_pubkey()
                except Exception:
                    pub = None
                return self._send(200, {"servers": [x.public() for x in SERVERS.all()], "current": cur_server().id,
                                        "main": SERVERS.MAIN, "main_override": SERVERS.main_override,
                                        "pubkey": pub, "helper_version": HELPER_VERSION})
            if u.path == "/api/gpumon":
                try:
                    since = int(q.get("since") or 0)
                except ValueError:
                    since = 0
                return self._send(200, GMON.poll(since))
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

    def _do_POST(self):
        if not self._auth_ok():
            return self._deny()
        # Jednostavna CSRF zaštita: traži custom header koji forma s drugog sitea ne može poslati
        if self.headers.get("X-Requested-With") != "JellyClean":
            return self._send(403, {"error": "Nedostaje X-Requested-With"})
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/backup/upload":
            try:
                loc = (urllib.parse.parse_qs(u.query).get("loc") or ["local"])[0]
                return self._send(200, BK.upload(self.rfile, int(self.headers.get("Content-Length") or 0), loc))
            except (ValueError, OSError, KeyError, tarfile.TarError) as e:
                self.close_connection = True
                return self._send(400, {"error": str(e)})
        try:
            body = self._body()
            if u.path == "/api/scan":
                started = start_scan_bg()
                if body.get("jellyfin"):
                    sync_all_servers(background=True)
                return self._send(200, {"started": started})
            if u.path == "/api/jellyfin/sync":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin nije podešen"})
                CtxThread(target=JF.sync, daemon=True).start()
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
                    CtxThread(target=lambda: (time.sleep(20), JF.sync()), daemon=True).start()
                return self._send(200, {"results": res, "trash": CFG["trash"] and not body.get("permanent")})
            if u.path == "/api/paths/check":
                if not JF.enabled():
                    return self._send(400, {"error": "Jellyfin API nije podešen"})
                if SCAN.running:
                    return self._send(409, {"error": "Skeniranje diska je u tijeku - pričekaj da završi"})
                CtxThread(target=PC.run, daemon=True).start()
                return self._send(200, {"started": True})
            if u.path == "/api/diag":
                return self._send(200, diagnose((body.get("id") or "").replace("-", "").lower()))
            if u.path == "/api/diag/fix":
                return self._send(200, diag_fix((body.get("id") or "").replace("-", "").lower(), body.get("kind"),
                                                body.get("candidate_jf")))
            if u.path == "/api/enctest/start":
                return self._send(200, ENC.start((body.get("id") or "").replace("-", "").lower(), body.get("max"),
                                                 body.get("dur"), body.get("mode"), str(body.get("target") or "1080"),
                                                 str(body.get("codec") or "h264")))
            if u.path == "/api/enctest/cancel":
                return self._send(200, ENC.cancel())
            if u.path == "/api/nvpatch/apply":
                if not CFG["allow_delete"]:
                    return self._send(403, {"error": "Izmjene su isključene (ALLOW_DELETE=false)"})
                return self._send(200, NVP.apply(body.get("action")))
            if u.path.startswith("/api/servers/"):
                act = u.path.rsplit("/", 1)[1]
                if act == "save":
                    sv = SERVERS.upsert(body)
                    res = server_test(sv) if body.get("test", True) else None
                    if sv.cfg["jf_url"] and sv.cfg["jf_key"]:
                        with use_server(sv):
                            CtxThread(target=JF.sync, daemon=True).start()
                    return self._send(200, {"server": sv.public(), "test": res})
                sv = SERVERS.get(body.get("id"))
                if not sv:
                    return self._send(404, {"error": "Server ne postoji"})
                if act == "delete":
                    SERVERS.remove(sv.id)
                    return self._send(200, {"ok": True})
                if act == "test":
                    res = server_test(sv)
                    return self._send(200, {"server": sv.public(), "test": res})
                if act == "reset-main":
                    SERVERS.reset_main()
                    return self._send(200, {"server": SERVERS.default().public()})
                if act == "bootstrap":
                    if not sv.cfg["ssh_host"]:
                        return self._send(400, {"error": "Prvo upiši SSH host i korisnika i spremi server"})
                    t = boot_token(sv.id)
                    host = self.headers.get("Host") or f"{CFG['bind']}:{CFG['port']}"
                    url = f"http://{host}/api/servers/bootstrap.sh?t={t}"
                    return self._send(200, {"url": url, "command": f"curl -fsSL '{url}' | sudo sh",
                                            "wget": f"wget -qO- '{url}' | sudo sh", "expires_min": 30,
                                            "user": sv.cfg["ssh_user"], "host": sv.cfg["ssh_host"]})
                return self._send(404, {"error": "Nepoznata radnja"})
            if u.path == "/api/backup/full":
                return self._send(200, BK.start_full(body.get("stop", True), body.get("metadata", False), body.get("loc") or "local"))
            if u.path == "/api/backup/users":
                return self._send(200, BK.start_users(body.get("users"), body.get("loc") or "local"))
            if u.path == "/api/backup/copy":
                return self._send(200, BK.start_copy(body.get("id"), body.get("loc"), bool(body.get("move")), body.get("src_loc")))
            if u.path == "/api/storage/exports":
                return self._send(200, STORE.exports(body.get("host")))
            if u.path == "/api/storage/test":
                return self._send(200, STORE.test(body.get("src"), body.get("vers")))
            if u.path == "/api/storage/add":
                return self._send(200, STORE.add(body.get("name"), body.get("src"), body.get("vers"), body.get("subdir")))
            if u.path == "/api/storage/remove":
                return self._send(200, STORE.remove(body.get("id")))
            if u.path == "/api/storage/mount":
                return self._send(200, STORE.mount(body.get("id"), body.get("on", True)))
            if u.path == "/api/backup/restore":
                return self._send(200, BK.start_restore(body.get("id"), body.get("target"), body.get("options"), body.get("loc")))
            if u.path == "/api/backup/delete":
                return self._send(200, BK.delete(body.get("id"), body.get("loc")))
            if u.path == "/api/backup/cancel":
                return self._send(200, BK.cancel())
            if u.path == "/api/backup/prevs/clean":
                return self._send(200, BK.prevs_clean())
            if u.path == "/api/pve/setup":
                host = self.headers.get("Host") or f"{CFG['bind']}:{CFG['port']}"
                return self._send(200, PVE.setup(body.get("host"), body.get("port"), host))
            if u.path == "/api/pve/verify":
                return self._send(200, PVE.status(check=True))
            if u.path == "/api/pve/revoke":
                return self._send(200, PVE.revoke())
            if u.path == "/api/pve/migrate/start":
                return self._send(200, PVE.mig_start())
            if u.path == "/api/pve/migrate/answer":
                return self._send(200, PVE.mig_answer(body.get("id"), body.get("value"), body.get("cancel")))
            if u.path == "/api/pve/migrate/cancel":
                return self._send(200, PVE.mig_cancel())
            if u.path == "/api/gpumon/stop":
                return self._send(200, GMON.stop())
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
        except (DeleteError, SSHError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self._send(502, {"error": f"Jellyfin HTTP {e.code}"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def main():
    os.makedirs(CFG["data_dir"], exist_ok=True)
    SERVERS.load()
    log(f"JellyClean {APP_VERSION} na http://{CFG['bind']}:{CFG['port']}")
    for l, p in CFG["media"]:
        log(f"  medij: {l} = {p}" + ("" if os.path.isdir(p) else "  (NE POSTOJI!)"))
    for l, p in CFG["jf_dirs"]:
        log(f"  jellyfin mapa: {l} = {p}" + ("" if os.path.isdir(p) else "  (NE POSTOJI!)"))
    for sv in SERVERS.all():
        with use_server(sv):
            log(f"  Jellyfin server '{sv.name}' ({sv.id}): API " + (CFG["jf_url"] if JF.enabled() else "nije podešen") +
                ", SSH " + (f"{CFG['ssh_user']}@{CFG['ssh_host']}:{CFG['jf_config']}" if JFC.enabled() else "nije podešen"))
    log(f"  brisanje: {'da' if CFG['allow_delete'] else 'NE'}, kanta: {'da' if CFG['trash'] else 'ne'}"
        f" ({CFG['trash_days']} dana)")
    if not CFG["auth_user"]:
        log("  UPOZORENJE: AUTH_USER nije postavljen - sučelje je bez lozinke")
    start_scan_bg()
    sync_all_servers(background=True)
    CtxThread(target=scheduler, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["bind"], CFG["port"]), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    if "--helper-script" in sys.argv:
        SERVERS.load()
        sid = sys.argv[sys.argv.index("--server") + 1] if "--server" in sys.argv else SERVERS.MAIN
        sv = SERVERS.get(sid) or sys.exit(f"Server {sid} ne postoji")
        with use_server(sv):
            if not CFG["jf_config"]:
                sys.exit("JF_CONFIG_PATH nije postavljen")
            sys.stdout.write(helper_script(standalone=True))
    else:
        main()
