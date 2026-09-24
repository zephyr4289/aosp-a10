"""Source acquisition: shallow repo sync -> content-addressed snapshot.

The upstream flow re-enters `repo init/sync` logic on every forced rerun and
keys its source cache on a single mutable release tag. ROMForge instead
fingerprints the *resolved manifest*:

    MHASH = sha256(manifest_xml | local_manifests | device repo list
                   | rom name | branch | android version)

A source snapshot is stored once under `src-<mhash>` and reused by every
slice, turbo job and rebuild of that target — including across different
ROMs sharing the same base tree. Sync runs only when `src-<mhash>` is
missing, which is exactly the "cache invalidated by change" semantics we
want (upstream: cache invalidated by whim).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

from . import chunker, log
from .config import Plan

REPO_URL = "https://storage.googleapis.com/git-repo-downloads/repo"

SRC_EXCLUDES = [
    # never carry build state inside the source snapshot
    "aosp/out",
]
STRIP_EXTRA = [
    "prebuilts/gcc/darwin-x86",
    "prebuilts/gcc/windows-x86",
    "prebuilts/gcc/windows-x86_64",
]


class SyncError(Exception):
    pass


def _run(cmd: List[str], cwd: Optional[Path] = None, retries: int = 1,
         check: bool = True) -> "subprocess.CompletedProcess[str]":
    last = None
    for attempt in range(retries + 1):
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True)
        if r.returncode == 0:
            return r
        last = r
        if attempt < retries:
            time.sleep(20 * (attempt + 1))
    if check:
        raise SyncError(f"{' '.join(cmd[:4])} failed "
                        f"(rc={last.returncode if last else '?'}): "
                        f"{(last.stderr if last else '')[:300]}")
    return last  # type: ignore[return-value]


def ensure_repo_launcher(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    repo = bin_dir / "repo"
    if not repo.exists():
        import urllib.request
        urllib.request.urlretrieve(REPO_URL, repo)
        repo.chmod(0o755)
    return repo


# ---------------------------------------------------------------------------
# manifest fingerprinting
# ---------------------------------------------------------------------------
def mhash(plan: Plan, manifest_xml: str) -> str:
    h = hashlib.sha256()
    h.update(manifest_xml.encode("utf-8"))
    h.update(plan.rom.name.encode())
    h.update(plan.rom.manifest_branch.encode())
    h.update(str(plan.rom.android_version).encode())
    for r in sorted(plan.rom.device_repos, key=lambda x: x["path"]):
        h.update(f"{r['path']}|{r['url']}|{r['branch']}".encode())
    for lm in sorted(plan.rom.local_manifests):
        h.update(lm.encode())
    for p in sorted(plan.rom.patches, key=lambda x: x["dst"]):
        h.update(f"{p['src']}->{p['dst']}".encode())
    return h.hexdigest()[:16]


def snapshot_source(build_root: Path, store, tag: str, title: str,
                    notes: str, sink: bool = True) -> int:
    """Bank the git-stripped tree as `tag` (content-addressed, immutable)."""
    src_root = build_root.parent          # tar member is 'aosp'
    if sink and store.sink_command(tag):
        n = chunker.stream_pack(
            src_root, "aosp", "src", store.sink_command(tag),
            build_root / ".reclaim_tmp" / "SHA256SUMS.src",
            excludes=SRC_EXCLUDES)
        # the streamed SHA256SUMS lives at a tmp path; upload it as asset
        sums = build_root / ".reclaim_tmp" / "SHA256SUMS.src"
        store.upload_file(tag, sums, "SHA256SUMS")
        return n
    staging = build_root.parent / ".forge-parts-src"
    parts = chunker.pack(src_root, "aosp", staging, "src", excludes=SRC_EXCLUDES)
    store.upload(tag, [staging / "SHA256SUMS", *parts])
    for p in parts:
        p.unlink(missing_ok=True)
    (staging / "SHA256SUMS").unlink(missing_ok=True)
    return len(parts)


def restore_source(build_root: Path, store, tag: str) -> bool:
    """Restore tree from `tag`; returns False if no parts exist."""
    incoming = build_root.parent / ".forge-src-incoming"
    if incoming.exists():
        shutil.rmtree(incoming)
    try:
        chunker.unpack_from_store(store, tag, "src", incoming, strip=True)
    except Exception as e:
        log.warn(f"unpacking source from {tag} failed: {e}")
        return False
    if build_root.exists():
        shutil.rmtree(build_root)
    incoming.rename(build_root)
    if not (build_root / "build" / "envsetup.sh").exists():
        raise SyncError("restored source incomplete (no envsetup.sh) — "
                        "delete the src-* release and rerun")
    log.ok(f"source restored from {tag}")
    return True


# ---------------------------------------------------------------------------
# the actual sync
# ---------------------------------------------------------------------------
def _git_identity() -> None:
    subprocess.run(["git", "config", "--global", "user.name",
                    os.environ.get("GIT_AUTHOR_NAME", "romforge-ci")], check=False)
    subprocess.run(["git", "config", "--global", "user.email",
                    os.environ.get("GIT_AUTHOR_EMAIL",
                                   "romforge@users.noreply.github.com")], check=False)
    subprocess.run(["git", "config", "--global", "http.postBuffer",
                    "524288000"], check=False)


def sync_tree(plan: Plan, build_root: Path, sync_jobs: int = 8) -> str:
    """Shallow-sync the ROM tree + land device repos; returns MHASH."""
    if not plan.rom.manifest_url.startswith(("http://", "https://", "git@")):
        raise SyncError(f"bad manifest_url: {plan.rom.manifest_url}")
    _git_identity()
    repo_bin = ensure_repo_launcher(Path.home() / "bin")
    env = dict(os.environ, PATH=f"{Path.home() / 'bin'}:{os.environ['PATH']}")

    build_root.mkdir(parents=True, exist_ok=True)
    # local manifests (manifest clone mode)
    lm_dir = build_root / ".repo" / "local_manifests"
    if plan.rom.device_clone_mode == "manifest" and plan.rom.local_manifests:
        lm_dir.mkdir(parents=True, exist_ok=True)
        for lm in plan.rom.local_manifests:
            src = Path(lm) if os.path.isabs(lm) else \
                Path(os.environ.get("FORGE_ROOT", ".")) / lm
            shutil.copy2(src, lm_dir / src.name)

    # repo init with retries
    for attempt in range(3):
        r = subprocess.run(
            [str(repo_bin), "init", "--depth=1", "-u", plan.rom.manifest_url,
             "-b", plan.rom.manifest_branch],
            cwd=str(build_root), env=env, capture_output=True, text=True)
        if r.returncode == 0:
            break
        log.warn(f"repo init attempt {attempt + 1} failed: "
                 f"{r.stderr.strip()[:200]}")
        time.sleep(30 * (attempt + 1))
    else:
        raise SyncError("repo init failed after 3 attempts")

    # repo sync with resume
    sync_ok = False
    for attempt in range(1, 4):
        args = [str(repo_bin), "sync", "-c", "-j", str(sync_jobs),
                "--no-clone-bundle", "--no-tags", "--optimized-fetch",
                "--prune"]
        if attempt > 1:
            args.append("--force-sync")
        r = subprocess.run(args, cwd=str(build_root), env=env,
                           capture_output=True, text=True)
        if r.returncode == 0:
            sync_ok = True
            break
        log.warn(f"repo sync attempt {attempt}/3 incomplete — "
                 f"completed projects persist, resuming")
        time.sleep(60 * attempt)
    if not sync_ok:
        raise SyncError("repo sync failed after 3 attempts")

    # device repos: direct mode wipes and shallow-clones (proven Colab flow)
    for r in plan.rom.device_repos:
        path, url, branch = r["path"], r["url"], r["branch"]
        dst = build_root / path
        if plan.rom.device_clone_mode == "direct":
            if dst.exists():
                shutil.rmtree(dst)
            _run(["git", "clone", "--depth=1", "-b", branch, url, str(dst)],
                 retries=2)
        else:  # manifest mode: rescue only if sync left the path broken
            if not (dst / ".git").exists() and not (dst / "Android.mk").exists() \
                    and not (dst / "AndroidProducts.mk").exists() \
                    and not (dst / "device.mk").exists():
                log.warn(f"manifest mode left {path} empty — direct fallback")
                if dst.exists():
                    shutil.rmtree(dst)
                _run(["git", "clone", "--depth=1", "-b", branch, url, str(dst)],
                     retries=2)

    # fingerprint BEFORE stripping .repo (manifest must be readable)
    manifest_path = build_root / ".repo" / "manifests" / "manifest.xml"
    if not manifest_path.exists():
        # try the pinned copy saved at init time
        manifest_path = build_root / ".repo" / "manifest.xml"
    xml_text = manifest_path.read_text(encoding="utf-8", errors="replace")
    _validate_manifest(xml_text)
    fp = mhash(plan, xml_text)
    # keep the resolved manifest inside the tree for forensics
    (build_root / ".forge-manifest.xml").write_text(xml_text, encoding="utf-8")

    # strip git metadata (post-sync diet: -4..8 GB)
    for p in build_root.rglob(".git"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    shutil.rmtree(build_root / ".repo", ignore_errors=True)
    for extra in STRIP_EXTRA:
        shutil.rmtree(build_root / extra, ignore_errors=True)

    (build_root / ".source_ready").write_text(
        f"mhash={fp}\nrom={plan.rom.name}\nbranch={plan.rom.manifest_branch}\n",
        encoding="utf-8")
    total = _du(build_root)
    log.ok(f"tree ready: {total / 2**30:.1f} GB, mhash={fp}")
    return fp


def _validate_manifest(xml_text: str) -> None:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        raise SyncError(f"manifest XML unparsable: {e}") from e
    if root.tag != "manifest":
        raise SyncError("resolved manifest root is not <manifest>")


def _du(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def ensure_prebuilts(build_root: Path) -> None:
    """Ensure binary prebuilts (like chromium-webview) are valid zip files, not LFS stubs,
    and purge any stale/corrupted intermediate artifacts from out/."""
    webview_arm64 = build_root / "external" / "chromium-webview" / "prebuilt" / "arm64" / "webview.apk"
    if (build_root / "external" / "chromium-webview").exists():
        is_valid = False
        if webview_arm64.exists() and webview_arm64.stat().st_size > 1000000:
            try:
                with open(webview_arm64, "rb") as fh:
                    if fh.read(2) == b"PK":
                        is_valid = True
            except Exception:
                pass
        if not is_valid:
            log.log("fetching valid prebuilt chromium-webview arm64 binary...")
            try:
                import urllib.request, base64
                url = "https://android.googlesource.com/platform/external/chromium-webview/+/refs/tags/android-10.0.0_r41/prebuilt/arm64/webview.apk?format=TEXT"
                req = urllib.request.Request(url, headers={"User-Agent": "ROMForge"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = base64.b64decode(resp.read())
                    webview_arm64.parent.mkdir(parents=True, exist_ok=True)
                    webview_arm64.write_bytes(raw)
                    log.ok(f"restored prebuilt chromium-webview ({len(raw) / 2**20:.1f} MB)")
            except Exception as e:
                log.warn(f"could not download webview.apk: {e}")
        if webview_arm64.exists():
            now = time.time() + 10
            os.utime(webview_arm64, (now, now))

    webview_arm = build_root / "external" / "chromium-webview" / "prebuilt" / "arm" / "webview.apk"
    if (build_root / "external" / "chromium-webview").exists():
        is_valid = False
        if webview_arm.exists() and webview_arm.stat().st_size > 1000000:
            try:
                with open(webview_arm, "rb") as fh:
                    if fh.read(2) == b"PK":
                        is_valid = True
            except Exception:
                pass
        if not is_valid:
            log.log("fetching valid prebuilt chromium-webview arm binary...")
            try:
                import urllib.request, base64
                url = "https://android.googlesource.com/platform/external/chromium-webview/+/refs/tags/android-10.0.0_r41/prebuilt/arm/webview.apk?format=TEXT"
                req = urllib.request.Request(url, headers={"User-Agent": "ROMForge"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = base64.b64decode(resp.read())
                    webview_arm.parent.mkdir(parents=True, exist_ok=True)
                    webview_arm.write_bytes(raw)
                    log.ok(f"restored prebuilt chromium-webview arm ({len(raw) / 2**20:.1f} MB)")
            except Exception as e:
                log.warn(f"could not download webview.apk (arm): {e}")
        if webview_arm.exists():
            now = time.time() + 10
            os.utime(webview_arm, (now, now))

    # Purge any stale/broken webview intermediates in out/
    out_dir = build_root / "out"
    if out_dir.exists():
        for p in out_dir.glob("target/product/*/obj/APPS/webview*"):
            shutil.rmtree(p, ignore_errors=True)
        for p in out_dir.glob("target/product/*/system/app/webview*"):
            shutil.rmtree(p, ignore_errors=True)
        for p in out_dir.glob("target/product/*/system/product/app/webview*"):
            shutil.rmtree(p, ignore_errors=True)


def apply_patches(plan: Plan, build_root: Path, forge_root: Path) -> List[str]:
    applied = []
    for p in plan.rom.patches:
        src = Path(p["src"])
        if not src.is_absolute():
            src = forge_root / p["src"]
        dst = build_root / p["dst"].lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        applied.append(p["dst"])
    ensure_prebuilts(build_root)
    if applied:
        log.ok(f"applied {len(applied)} patches: {', '.join(applied)}")
    return applied


def validate_lunch(plan: Plan, build_root: Path) -> None:
    """Product resolution check BEFORE burning build hours (upstream 02)."""
    envsetup = build_root / "build" / "envsetup.sh"
    if not envsetup.exists():
        raise SyncError("build/envsetup.sh missing — tree incomplete")
    # the real lunch resolution happens inside the build env; here we do the
    # cheap structural checks the upstream harness proved sufficient
    lunch_parts = plan.rom.lunch.split("-")
    if len(lunch_parts) != 2:
        raise SyncError(f"bad lunch combo: {plan.rom.lunch}")
    product, variant = lunch_parts
    dev = plan.rom.lunch.split("_")[1] if "_" in plan.rom.lunch else ""
    for r in plan.rom.device_repos:
        if r["path"].startswith("device/") and dev and dev in r["path"]:
            # product mk must exist in the declared device repo path
            found = list((build_root / r["path"]).glob(f"{product}*.mk")) or \
                list((build_root / r["path"]).glob("AndroidProducts.mk"))
            if not found:
                raise SyncError(
                    f"{r['path']} has neither {product}*.mk nor "
                    f"AndroidProducts.mk — device tree incomplete")
            break
    log.ok(f"lunch {plan.rom.lunch}: device tree structurally sane")
