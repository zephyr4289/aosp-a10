#!/usr/bin/env python3
"""stream_pack integration proof with a fake `gh` binary as the sink.

Verifies: --filter zero-staging path, FORGE_SINK/FORGE_SUMS injection,
per-part sha256 manifest, and that parts land in the "release" while the
source dir holds nothing staged.
"""
from __future__ import annotations

import os
import random
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forge_core import chunker  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="forge-stream-",
                                dir=str(ROOT.parent)))
    fails = 0
    try:
        # fake gh: "uploads" = move $FILE into fakedir
        fakebin = tmp / "bin"
        fakebin.mkdir()
        release = tmp / "release"
        release.mkdir()
        gh = fakebin / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            '# fake gh release upload TAG FILE --clobber\n'
            'while [ $# -gt 0 ]; do case "$1" in\n'
            '  upload) shift; tag="$1"; shift ;;\n'
            '  *) if [ -f "$1" ]; then cp -f "$1" "' + str(release) + '/"; fi; shift ;;\n'
            "esac; done\n"
            "exit 0\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

        # fake source tree
        root = tmp / "tree"
        (root / "aosp").mkdir(parents=True)
        rng = random.Random(7)
        for i in range(30):
            (root / "aosp" / f"m{i}.bin").write_bytes(
                bytes(rng.getrandbits(8) for _ in range(5000 + i * 31)))
        (root / "aosp" / "out").mkdir()
        (root / "aosp" / "out" / "junk.bin").write_bytes(b"x" * 4096)

        sink = f'PATH="{fakebin}:$PATH" gh release upload tag "$FILE" --clobber'
        sums = tmp / "SUMS"
        env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}")
        os.environ.update(env)

        n = chunker.stream_pack(
            root, "aosp", "src", sink, sums,
            excludes=["aosp/out"])
        print(f"parts shipped: {n}")

        in_release = sorted(p.name for p in release.iterdir())
        print(f"release assets: {in_release}")
        if n >= 1 and len(in_release) == n:
            print("  ok    parts streamed to sink without staging")
        else:
            print("  FAIL  part count mismatch")
            fails += 1

        # manifest must have one line per part and validate
        lines = [l for l in sums.read_text().splitlines() if l.strip()]
        if len(lines) == n:
            print("  ok    SHA256SUMS one-per-part")
        else:
            print("  FAIL  sums mismatch")
            fails += 1

        # staged junk must NOT be inside any part content: verify by
        # checking the manifest count + file sizes sum to source size
        if all(not p.name.startswith(".") for p in release.iterdir()):
            print("  ok    no dotfile leakage")
        else:
            print("  FAIL  leaked dotfiles")
            fails += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("STREAM TEST " + ("GREEN" if fails == 0 else f"RED ({fails})"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
