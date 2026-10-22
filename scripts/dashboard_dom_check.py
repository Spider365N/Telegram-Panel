"""Run the real-DOM (jsdom) check of the admin dashboard's soft refresh.

The JavaScript lives inside ``src/webadmin.py`` as a string, so the check first
exports the CSS/JS to a temp dir, then runs ``scripts/dashboard_dom_test.js``
with Node:

    python scripts/dashboard_dom_check.py            # uses an existing jsdom
    python scripts/dashboard_dom_check.py --install  # npm installs jsdom first

Skipped (exit 0) when Node or jsdom is unavailable — the pytest suite still
covers the same contract statically.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dashboard_dom_test.js"


def export_assets(target: pathlib.Path) -> None:
    source = (ROOT / "src" / "webadmin.py").read_text(encoding="utf-8")
    css = source.split('BRUTAL_CSS = r"""', 1)[1].split('"""', 1)[0]
    js = source.split('BRUTAL_JS = r"""', 1)[1].split('"""', 1)[0]
    (pathlib.Path("/tmp/admin.css")).write_text(css, encoding="utf-8")
    (pathlib.Path("/tmp/admin.js")).write_text(js, encoding="utf-8")
    (target / "admin.css").write_text(css, encoding="utf-8")
    (target / "admin.js").write_text(js, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="npm install jsdom first")
    args = parser.parse_args()

    node = shutil.which("node")
    if not node:
        print("ℹ Node.js not installed — DOM check skipped")
        return 0

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="tp-dom-"))
    # the jsdom test reads /tmp/admin.js and /tmp/admin.css (exported below) and
    # resolves jsdom from its own directory
    shutil.copy(SCRIPT, workdir / "dashboard_dom_test.js")
    export_assets(workdir)

    if args.install:
        print("ℹ installing jsdom …")
        subprocess.run(["npm", "install", "jsdom", "--silent"], cwd=workdir, check=False)

    import os

    # reuse a jsdom that is already on disk instead of downloading it again
    env = dict(os.environ)
    for candidate in (env.get("NODE_PATH", ""), str(ROOT), "/tmp/domtest/node_modules"):
        if candidate and (pathlib.Path(candidate) / "jsdom").is_dir():
            env["NODE_PATH"] = candidate
            break
    else:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True)
        if root.returncode == 0 and (pathlib.Path(root.stdout.strip()) / "jsdom").is_dir():
            env["NODE_PATH"] = root.stdout.strip()

    probe = subprocess.run([node, "-e", "require.resolve('jsdom')"], cwd=workdir,
                           capture_output=True, text=True, env=env)
    if probe.returncode != 0:
        print("ℹ jsdom not installed — DOM check skipped (run with --install)")
        return 0

    print(f"ℹ running the DOM check in {workdir}")
    result = subprocess.run([node, "dashboard_dom_test.js"], cwd=workdir, env=env)
    if result.returncode != 0:
        print("✘ dashboard DOM check failed")
        return result.returncode
    print("✔ dashboard DOM check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
