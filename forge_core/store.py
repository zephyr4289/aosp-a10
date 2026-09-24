"""Hybrid state store + the INDEX coordination protocol.

Backends (routed by policy, per the hybrid decision):
  ReleaseStore  — GitHub Release assets via `gh`. Content-addressed tags:
                  src-<mhash>            source snapshot  (immutable)
                  state-<key>-s<N>       out/ relay, slice N     (GC'd)
                  state-<key>-<part>     turbo partition prewarm (GC'd)
                  rom-<key>              final ROM release       (kept)
                  forge-index            the INDEX.json itself
                  Unlimited bytes for public repos, 2 GiB per asset — parts.
  ArtifactStage — files staged for actions/upload-artifact (transient:
                  logs, safety report, stats). 1-day retention in workflows.
  FsStore       — plain directory backend; makes the WHOLE pipeline runnable
                  and testable on a laptop with zero GitHub credentials.

INDEX.json (the coordination record; written only by the strictly-sequential
main chain, so it is race-free by construction):
  {
    "targets": {
      "<rom key>": {
        "mhash": "...",            # manifest fingerprint -> src tag
        "src_tag": "src-abc123",
        "slice": 3,                # last banked out/ generation
        "state_tag": "state-...-s3",
        "done": false,
        "rom_tag": null,
        "last_gate": {...}
      }
    }
  }
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import chunker, log


class StoreError(Exception):
    pass


# ---------------------------------------------------------------------------
# ReleaseStore (gh CLI)
# ---------------------------------------------------------------------------
class ReleaseStore:
    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("GH_TOKEN") or \
            os.environ.get("GITHUB_TOKEN", "")
        if not self.repo:
            raise StoreError("no repository: set GITHUB_REPOSITORY "
                             "or pass repo= (e.g. 'user/repo')")

    # -- gh plumbing ---------------------------------------------------------
    def _gh(self, *args: str, check: bool = True,
            input_text: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ)
        if self.token:
            env["GH_TOKEN"] = self.token
        r = subprocess.run(["gh", *args, "-R", self.repo],
                           capture_output=True, text=True, env=env,
                           input=input_text)
        if check and r.returncode != 0:
            raise StoreError(f"gh {' '.join(args[:3])} failed: "
                             f"{r.stderr.strip()[:300]}")
        return r

    def exists(self, tag: str) -> bool:
        return self._gh("release", "view", tag, check=False).returncode == 0

    def create(self, tag: str, title: str, notes: str,
               target: Optional[str] = None) -> None:
        args = ["release", "create", tag, "--title", title, "--notes", notes]
        if target:
            args += ["--target", target]
        else:
            args += ["--target", "main"]
        r = self._gh(*args, check=False)
        if r.returncode != 0:  # race or already exists — fine
            log.warn(f"release create {tag}: {r.stderr.strip()[:120]}")

    def delete(self, tag: str, cleanup_tag: bool = True) -> None:
        args = ["release", "delete", tag, "--yes"]
        if cleanup_tag:
            args.append("--cleanup-tag")
        self._gh(*args, check=False)

    def upload(self, tag: str, files: List[Path], clobber: bool = True) -> None:
        if not files:
            return
        args = ["release", "upload", tag, *[str(f) for f in files]]
        if clobber:
            args.append("--clobber")
        for attempt in range(3):
            r = self._gh(*args, check=False)
            if r.returncode == 0:
                return
            log.warn(f"upload retry {attempt + 1}/3 to {tag}: "
                     f"{r.stderr.strip()[:160]}")
            time.sleep(20)
        raise StoreError(f"release upload to {tag} failed after retries")

    def upload_file(self, tag: str, file: Path, name: Optional[str] = None) -> None:
        """Upload a single (small) file, optionally renamed."""
        if name and name != file.name:
            tmp = file.with_name(name)
            shutil.copy2(file, tmp)
            try:
                self.upload(tag, [tmp])
            finally:
                tmp.unlink(missing_ok=True)
            return
        self.upload(tag, [file])

    def download(self, tag: str, pattern: str, dest: Path) -> List[Path]:
        dest.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            r = self._gh("release", "download", tag, "--pattern", pattern,
                         "--dir", str(dest), "--clobber", check=False)
            if r.returncode == 0:
                return sorted(dest.glob(pattern))
            log.warn(f"download retry {attempt + 1}/3 for {tag}/{pattern}: "
                     f"{r.stderr.strip()[:160]}")
            time.sleep(15 * (attempt + 1))
        raise StoreError(f"release download {tag}/{pattern} failed after retries: "
                         f"{r.stderr.strip()[:200]}")

    def list_assets(self, tag: str) -> List[str]:
        r = self._gh("release", "view", tag, "--json", "assets", check=False)
        if r.returncode != 0:
            return []
        try:
            data = json.loads(r.stdout or "{}")
            return [a["name"] for a in data.get("assets", [])]
        except Exception:
            return []

    def download_file(self, tag: str, filename: str, dest_file: Path) -> Path:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            r = self._gh("release", "download", tag, "--pattern", filename,
                         "--dir", str(dest_file.parent), "--clobber", check=False)
            if r.returncode == 0:
                downloaded = dest_file.parent / filename
                if downloaded != dest_file and downloaded.exists():
                    downloaded.rename(dest_file)
                if dest_file.exists():
                    return dest_file
            log.warn(f"download retry {attempt + 1}/3 for {tag}/{filename}: "
                     f"{r.stderr.strip()[:160]}")
            time.sleep(10 * (attempt + 1))
        raise StoreError(f"release download {tag}/{filename} failed after retries")

    def reset(self, tag: str, title: str, notes: str) -> None:
        """Delete+recreate — upstream's atomicity trick, kept."""
        self.delete(tag)
        self.create(tag, title, notes)

    def list_tags(self, prefix: str) -> List[str]:
        r = self._gh("release", "list", "--limit", "200", "--json", "tagName")
        if r.returncode != 0:
            return []
        return [e["tagName"] for e in json.loads(r.stdout or "[]")
                if str(e.get("tagName", "")).startswith(prefix)]

    def sink_command(self, tag: str) -> str:
        """Shell snippet for chunker.stream_pack: upload $FILE to a release."""
        repo = self.repo
        tok = self.token
        prefix_env = f"GH_TOKEN={tok} " if tok else ""
        return (f'{prefix_env}gh release upload {tag} "$FILE" '
                f'-R {repo} --clobber')


# ---------------------------------------------------------------------------
# FsStore (local dev + tests)
# ---------------------------------------------------------------------------
class FsStore:
    """Directory-backed stand-in: tags are directories, assets are files."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, tag: str) -> Path:
        return self.root / tag

    def exists(self, tag: str) -> bool:
        return self._dir(tag).exists()

    def create(self, tag: str, title: str = "", notes: str = "",
               target: Optional[str] = None) -> None:
        d = self._dir(tag)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".meta").write_text(json.dumps({"title": title, "notes": notes}),
                                 encoding="utf-8")

    def delete(self, tag: str, cleanup_tag: bool = True) -> None:
        shutil.rmtree(self._dir(tag), ignore_errors=True)

    def upload(self, tag: str, files: List[Path], clobber: bool = True) -> None:
        d = self._dir(tag)
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            dst = d / f.name
            if dst.exists() and not clobber:
                continue
            shutil.copy2(f, dst)

    def upload_file(self, tag: str, file: Path, name: Optional[str] = None) -> None:
        d = self._dir(tag)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, d / (name or file.name))

    def download(self, tag: str, pattern: str, dest: Path) -> List[Path]:
        import glob as _glob
        d = self._dir(tag)
        if not d.exists():
            raise StoreError(f"fs-store tag missing: {tag}")
        dest.mkdir(parents=True, exist_ok=True)
        got: List[Path] = []
        for p in _glob.glob(str(d / pattern)):
            shutil.copy2(p, dest / Path(p).name)
            got.append(dest / Path(p).name)
        if not got:
            raise StoreError(f"fs-store {tag}/{pattern}: no match")
        return got

    def list_assets(self, tag: str) -> List[str]:
        d = self._dir(tag)
        if not d.exists():
            return []
        return [p.name for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]

    def download_file(self, tag: str, filename: str, dest_file: Path) -> Path:
        d = self._dir(tag)
        src = d / filename
        if not src.exists():
            raise StoreError(f"fs-store asset {filename} missing in {tag}")
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_file)
        return dest_file

    def list_tags(self, prefix: str) -> List[str]:
        return [p.name for p in self.root.iterdir()
                if p.is_dir() and p.name.startswith(prefix)]

    def sink_command(self, tag: str) -> Optional[str]:  # unsupported
        return None   # stage-mode fallback; Router/syncer handle falsy


# ---------------------------------------------------------------------------
# ArtifactStage (workflow-mediated transient store)
# ---------------------------------------------------------------------------
class ArtifactStage:
    """Transient state staged for actions/upload-artifact.

    The CLI writes files into FORGE_ARTIFACT_STAGING (env or
    .forge-artifacts/); the workflow uploads them with retention-days: 1.
    Small stuff only: logs, SAFETY_REPORT.json, ninja stats. Large state
    always goes through ReleaseStore — artifacts cap out per-asset and
    expire in 90 days, which would eat the campaign's caches.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or os.environ.get("FORGE_ARTIFACT_STAGING",
                                                ".forge-artifacts"))
        self.root.mkdir(parents=True, exist_ok=True)

    def stage(self, name: str, files: List[Path]) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, d / f.name)
        log.log(f"staged artifact '{name}': {len(files)} files")
        return d

    def staged(self) -> List[str]:
        return [p.name for p in self.root.iterdir() if p.is_dir()]


# ---------------------------------------------------------------------------
# Router + INDEX
# ---------------------------------------------------------------------------
class Router:
    def __init__(self, backend: str = "auto", repo: Optional[str] = None,
                 fs_root: Optional[Path] = None):
        if backend == "auto":
            backend = "release" if (repo or os.environ.get("GITHUB_REPOSITORY")) \
                and shutil.which("gh") else "fs"
        self.backend = backend
        if backend == "release":
            self.rel = ReleaseStore(repo=repo)
        else:
            self.rel = FsStore(fs_root or Path(".forge-store"))
        self.artifacts = ArtifactStage()

    # convenience passthroughs ------------------------------------------------
    def exists(self, tag): return self.rel.exists(tag)
    def create(self, tag, title, notes, target=None):
        return self.rel.create(tag, title, notes, target)
    def delete(self, tag): return self.rel.delete(tag)
    def upload(self, tag, files, clobber=True):
        return self.rel.upload(tag, files, clobber)
    def upload_file(self, tag, file, name=None):
        return self.rel.upload_file(tag, file, name)
    def download(self, tag, pattern, dest):
        return self.rel.download(tag, pattern, dest)
    def download_file(self, tag, filename, dest_file):
        return self.rel.download_file(tag, filename, dest_file)
    def list_assets(self, tag): return self.rel.list_assets(tag)
    def list_tags(self, prefix): return self.rel.list_tags(prefix)

    def sink_command(self, tag: str) -> Optional[str]:
        try:
            return self.rel.sink_command(tag)
        except StoreError:
            return None

    # -- INDEX ---------------------------------------------------------------
    INDEX_TAG = "forge-index"

    def index_load(self) -> Dict:
        if not self.exists(self.INDEX_TAG):
            return {"schema": 1, "targets": {}}
        tmp = Path(".forge-tmp-index")
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            self.download(self.INDEX_TAG, "INDEX.json", tmp)
            return json.loads((tmp / "INDEX.json").read_text(encoding="utf-8"))
        except (StoreError, json.JSONDecodeError):
            return {"schema": 1, "targets": {}}

    def index_save(self, index: Dict) -> None:
        if not self.exists(self.INDEX_TAG):
            self.create(self.INDEX_TAG, "ROMForge pipeline index",
                        "Auto-generated coordination record. Do not edit.")
        tmp = Path(".forge-tmp-index")
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "INDEX.json").write_text(json.dumps(index, indent=2, sort_keys=True),
                                         encoding="utf-8")
        self.upload_file(self.INDEX_TAG, tmp / "INDEX.json")

    def target(self, key: str) -> Dict:
        idx = self.index_load()
        return idx["targets"].get(key, {
            "mhash": "", "src_tag": "", "slice": 0, "state_tag": "",
            "done": False, "rom_tag": "", "last_gate": None})

    def target_update(self, key: str, **kv) -> None:
        idx = self.index_load()
        t = idx["targets"].setdefault(key, {"mhash": "", "src_tag": "",
                                            "slice": 0, "state_tag": "",
                                            "done": False, "rom_tag": "",
                                            "last_gate": None})
        t.update(kv)
        self.index_save(idx)

    # -- gc -------------------------------------------------------------------
    def gc_state(self, key: str, keep: int = 2) -> List[str]:
        """Drop stale slice/turbo state tags for `key`, keep newest N."""
        tags = sorted(self.list_tags(f"state-{key}-s"))
        dropped = []
        for tag in tags[:-keep] if len(tags) > keep else []:
            self.delete(tag)
            dropped.append(tag)
        for tag in self.list_tags(f"state-{key}-turbo-"):
            self.delete(tag)
            dropped.append(tag)
        if dropped:
            log.log(f"gc {key}: dropped {len(dropped)} state tags "
                    f"({', '.join(dropped[:5])}{'...' if len(dropped) > 5 else ''})")
        return dropped
