#!/usr/bin/env python3
"""Rehearses the cutover push without touching the live `edition` branch or the worker's own edition directory.

The worker's push path (worker/mirror.py: one commit, amended and force-pushed) had never run on the VPS, and the
edition's image URLs point at publish.base_url, where nothing from the VPS is served. This tool runs that same
push against a scratch target and then checks what a 1.2 reader would get from it:

  1. copies edition.json and img/ from the edition directory into a work directory (the edition directory itself
     never becomes a git repository here);
  2. rewrites the image URLs from publish.base_url to --base-url, when given (where the scratch target is served);
  3. pushes with mirror.push to --remote / --branch. Branch `edition` is refused unless the remote is a local path;
  4. clones the pushed branch into a fresh directory and checks, there: the 1.2 decoder accepts edition.json, and
     every article's image file is in the tree under the path its URL names;
  5. with an http(s) --base-url, fetches edition.json and every image URL and reports each status.

Usage (from server/, as the worker's user, with TECHGIST_STATE set):
  # On the VPS, against a bare repository on local disk (no GitHub, no key): proves the push mechanics.
  git init --bare /var/lib/techgist/rehearsal.git
  python3 tools/rehearse_cutover.py --remote /var/lib/techgist/rehearsal.git --branch rehearsal

  # Against a scratch branch of a scratch repository on GitHub (the owner creates it and a deploy key for it):
  python3 tools/rehearse_cutover.py --remote git@github.com:OWNER/techgist-rehearsal.git --branch rehearsal \\
      --base-url https://raw.githubusercontent.com/OWNER/techgist-rehearsal/rehearsal/ --deploy-key ~/.ssh/KEY
Exit status 0 when every check passes.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import check_edition  # noqa: E402
from worker import config, mirror  # noqa: E402
from worker.db import utcnow  # noqa: E402


class Meta:
    """The two calls mirror.push makes on the database."""

    def __init__(self):
        self.values = {}

    def get_meta(self, key, default=None):
        return self.values.get(key, default)

    def set_meta(self, key, value):
        self.values[key] = value


def image_path(url, base_url):
    """The path inside the published tree that an image URL names, or None when it is not under base_url."""
    base = base_url.rstrip("/") + "/"
    return url[len(base):] if url.startswith(base) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--remote", required=True, help="git URL or local path of the scratch repository")
    ap.add_argument("--branch", required=True)
    ap.add_argument("--base-url", help="where the scratch branch is served; image URLs are rewritten to it")
    ap.add_argument("--deploy-key", help="SSH key for the remote (default publish.deploy_key)")
    ap.add_argument("--out", help="the edition directory to rehearse with (default <state_dir>/edition)")
    args = ap.parse_args()
    cfg = config.load()
    pub = cfg.get("publish") or {}
    local = os.path.isabs(args.remote) or args.remote.startswith("file://")
    if args.branch == pub.get("git_branch", "edition") and not local:
        sys.exit(f"refusing to rehearse on branch {args.branch!r}: that is the branch live readers are served from")
    src = os.path.expanduser(args.out or pub.get("out_dir") or os.path.join(cfg["state_dir"], "edition"))
    live_base = (pub.get("base_url") or "").rstrip("/") + "/"
    base = (args.base_url or live_base).rstrip("/") + "/"
    problems = []
    with tempfile.TemporaryDirectory(prefix="techgist-rehearsal-") as tmp:
        work, clone = os.path.join(tmp, "work"), os.path.join(tmp, "clone")
        os.makedirs(work)
        edition = json.load(open(os.path.join(src, "edition.json")))
        for a in edition["articles"]:
            if a.get("image", "").startswith(live_base):
                a["image"] = base + a["image"][len(live_base):]
        with open(os.path.join(work, "edition.json"), "w") as f:
            json.dump(edition, f, separators=(",", ":"), ensure_ascii=False)
        for name in ("health.json", "notifications.json"):
            if os.path.exists(os.path.join(src, name)):
                shutil.copy(os.path.join(src, name), work)
        shutil.copytree(os.path.join(src, "img"), os.path.join(work, "img"))
        n_img = len(os.listdir(os.path.join(work, "img")))

        run_cfg = {**cfg, "publish": {**pub, "git_remote": args.remote, "git_branch": args.branch,
                                      "deploy_key": args.deploy_key or pub.get("deploy_key")}}
        os.environ[mirror.ENV] = "1"        # this tool is the one other place a push is meant (to a scratch branch)
        result = mirror.push(Meta(), run_cfg, work, utcnow(), force=True)
        print(f"push to {args.remote} branch {args.branch}: {result} ({len(edition['articles'])} articles, "
              f"{n_img} image files)")
        if result != "pushed":
            sys.exit(1)
        # A second push must amend, not add a commit (the branch never grows).
        again = mirror.push(Meta(), run_cfg, work, utcnow(), force=True)
        env = {**os.environ}
        key = os.path.expanduser(args.deploy_key or pub.get("deploy_key") or "")
        if key and os.path.exists(key):
            env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        r = subprocess.run(["git", "clone", "-q", "--depth", "5", "-b", args.branch, args.remote, clone],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            sys.exit(f"clone failed: {r.stderr.strip()[:300]}")
        commits = subprocess.run(["git", "-C", clone, "rev-list", "--count", "HEAD"], capture_output=True,
                                 text=True).stdout.strip()
        print(f"second push: {again}; commits on the branch: {commits}")
        if again != "pushed" or commits != "1":
            problems.append(f"the branch has {commits} commits after two pushes (expected 1)")

        res = check_edition.check(os.path.join(clone, "edition.json"), cfg)
        print(f"1.2 decoder on the pushed edition.json: ok={res.ok} decoder={res.decoder}")
        problems += res.problems[:5]
        pushed = json.load(open(os.path.join(clone, "edition.json")))
        missing = []
        for a in pushed["articles"]:
            path = image_path(a.get("image", ""), base) if a.get("image") else None
            if a.get("image") and (path is None or not os.path.exists(os.path.join(clone, path))):
                missing.append(a["image"])
        with_image = sum(bool(a.get("image")) for a in pushed["articles"])
        print(f"image files in the pushed tree: {with_image - len(missing)} of {with_image} articles' images present")
        if missing:
            problems.append(f"{len(missing)} image URLs name no file in the pushed tree, e.g. {missing[0]}")

        if args.base_url and args.base_url.startswith(("http://", "https://")):
            import requests
            bad = []
            try:
                r = requests.get(base + "edition.json", timeout=20)
                served = r.status_code == 200 and r.json().get("publishedAt") == pushed["publishedAt"]
            except (requests.RequestException, ValueError):
                served = False
            print(f"GET {base}edition.json: {'the pushed edition' if served else 'NOT the pushed edition'}")
            if not served:
                problems.append("edition.json is not served at --base-url (a CDN may lag a few minutes; re-run)")
            urls = sorted({a["image"] for a in pushed["articles"] if a.get("image")})
            for u in urls:
                try:
                    code = requests.head(u, timeout=15, allow_redirects=True).status_code
                except requests.RequestException as e:
                    code = type(e).__name__
                if code != 200:
                    bad.append((u, code))
            print(f"image URLs returning 200: {len(urls) - len(bad)} of {len(urls)}")
            if bad:
                problems.append(f"{len(bad)} image URLs do not return 200, e.g. {bad[0][0]} -> {bad[0][1]}")
        else:
            print("no http(s) --base-url: the image URLs were checked against the pushed tree, not fetched")
    for p in problems:
        print("PROBLEM:", p)
    print("rehearsal " + ("FAILED" if problems else "passed"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
