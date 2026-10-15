#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Install one Peritia release at user scope for every coding client on this machine, read it back, or remove it.

The release tree is cloned at its tag, its skills/ folder (with CREDITS.md and the licence texts) is copied into a
store named after the tagged commit, and each client's user skill folder gets one symbolic link per skill into that
store. A manifest beside the store records the sha256 of every file and every link, so `verify` refuses a changed,
added or missing file and a link that points elsewhere. Nothing outside the store and those links is written. An
existing entry of the same name in a client folder is never replaced: it is reported as a collision and skipped.

Usage:
  install_user_skills.py plan      [--tag TAG] [--repo URL] [--home DIR] [--clients all]
  install_user_skills.py install   [--tag TAG] [--repo URL] [--home DIR] [--clients all] [--apply]
  install_user_skills.py verify    [--home DIR]
  install_user_skills.py uninstall [--home DIR] [--apply]
  install_user_skills.py selftest
Without --apply, install and uninstall only print what they would do.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/plurano/peritia"
TAG = "v0.1.0"
STORE_PARENT = ".local/share/praetor-tools/skills"
MANIFEST = "peritia-user-skills.json"
GIT_TIMEOUT = 300  # seconds for one clone or ls-remote (HISS-02)
MAX_FILES = 20000  # bounds every tree walk (HISS-02)
# Windows refuses a symbolic link without developer mode or admin rights; a directory junction needs neither.
# PERITIA_LINK_MODE=junction forces junctions (to test that path where symlinks work); the output names the mode.
LINK_MODE_ENV = "PERITIA_LINK_MODE"
# User skill folders, read from each installed client (binary strings and its own docs, 2026-10-15):
# Claude Code ~/.claude/skills; Codex $CODEX_HOME/skills (default ~/.codex/skills); Antigravity CLI
# ~/.gemini/config/skills; Gemini CLI ~/.gemini/skills; opencode auto-loads ~/.claude/skills and ~/.agents/skills
# (`opencode debug skill` lists both), so it gets the client-neutral ~/.agents/skills.
CLIENTS = (
    ("claude-code", "claude", ".claude/skills"),
    ("codex", "codex", "$CODEX_HOME/skills"),
    ("antigravity", "agy", ".gemini/config/skills"),
    ("gemini-cli", "gemini", ".gemini/skills"),
    ("opencode", "opencode", ".agents/skills"),
)


def client_dirs(home, everyone):
    """{client: skill folder} for the clients installed on PATH (all of them with everyone)."""
    out, skipped = {}, {}
    for name, binary, rel in CLIENTS:
        if not everyone and shutil.which(binary) is None:
            skipped[name] = f"{binary} is not on PATH"
            continue
        if rel.startswith("$CODEX_HOME"):
            base = Path(os.environ.get("CODEX_HOME") or home / ".codex")
            out[name] = base / rel.split("/", 1)[1]
        else:
            out[name] = home / rel
    return out, skipped


def git(args, cwd=None):
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if run.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {run.stderr.strip()[:300]}")
    return run.stdout.strip()


def fetch(repo, tag, into):
    """Clone repo at tag into `into`; refuse when the clone's head is not the commit the tag names remotely."""
    git(["-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", tag, repo, str(into)])
    head = git(["rev-parse", "HEAD"], cwd=into)
    remote = git(["ls-remote", repo, f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"])
    peeled = [line.split()[0] for line in remote.splitlines() if line.endswith("^{}")]
    named = peeled[0] if peeled else (remote.split()[0] if remote else "")
    if named != head:
        raise SystemExit(f"refused: tag {tag} names {named or 'nothing'} remotely, the clone is at {head}")
    return head


def release_files(tree):
    """Relative paths the store keeps: the skills folder, the credits and the licence texts."""
    picked = []
    for top in ("skills", "LICENSES"):
        root = tree / top
        walked = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
        if len(walked) > MAX_FILES:
            raise SystemExit(f"refused: {top} holds more than {MAX_FILES} files")
        picked += [p.relative_to(tree).as_posix() for p in walked]
    return picked + [name for name in ("CREDITS.md", "LICENSE") if (tree / name).is_file()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_names(tree):
    return sorted(p.name for p in (tree / "skills").iterdir() if (p / "SKILL.md").is_file())


def is_junction(path):
    return bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def link_target(link):
    """The folder a symlink or junction at link points to, or None when link is neither."""
    if not (link.is_symlink() or is_junction(link)):
        return None
    raw = os.readlink(link)
    return Path(raw[4:] if raw.startswith("\\\\?\\") else raw)


def make_link(link, target):
    """Link link to target; returns the mode used. A refused symlink on Windows becomes a junction, named."""
    forced = os.environ.get(LINK_MODE_ENV, "")
    if forced == "junction" and os.name != "nt":
        raise SystemExit("refused: junctions exist only on Windows; unset " + LINK_MODE_ENV)
    if forced != "junction":
        try:
            link.symlink_to(target, target_is_directory=True)
            return "symlink"
        except OSError as exc:
            if os.name != "nt":
                raise
            why = f"symlink refused: {exc.strerror or exc}"
    else:
        why = f"forced by {LINK_MODE_ENV}"
    run = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True,
                         timeout=GIT_TIMEOUT)
    if run.returncode != 0:
        raise SystemExit(f"refused: junction {link} failed: {run.stdout.strip()} {run.stderr.strip()}"[:400])
    return f"junction ({why})"


def remove_link(link):
    if is_junction(link):
        os.rmdir(link)
    else:
        link.unlink()


def plan_links(names, dirs, store):
    """[(client, link path, target)] for every skill and client, plus collisions that are never replaced."""
    links, collisions = [], []
    for client, folder in sorted(dirs.items()):
        for name in names:
            link, target = folder / name, store / "skills" / name
            if link_target(link) == target:
                links.append((client, link, target))
            elif link.exists() or link.is_symlink() or is_junction(link):
                collisions.append(f"{client}: {link} exists and is not this release's link")
            else:
                links.append((client, link, target))
    return links, collisions


def write_store(tree, store, files):
    """Copy the release files into the store and make them read-only; returns {relative path: sha256}."""
    sums = {}
    for rel in files:
        dest = store / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree / rel, dest)
        dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        sums[rel] = digest(dest)
    return sums


def install(args, home, apply):
    dirs, skipped = client_dirs(home, args.get("--clients") == "all")
    tag, repo = args.get("--tag", TAG), args.get("--repo", REPO)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "release"
        commit = fetch(repo, tag, tree)
        store = home / STORE_PARENT / f"peritia@{commit}"
        names, files = skill_names(tree), release_files(tree)
        links, collisions = plan_links(names, dirs, store)
        report(tag, commit, store, dirs, skipped, links, collisions, apply)
        if not apply:
            return 0
        if (home / STORE_PARENT / MANIFEST).exists():
            raise SystemExit("refused: a Peritia user install exists; run uninstall --apply first")
        sums = write_store(tree, store, files)
    modes = set()
    for _client, link, target in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link_target(link) is None:
            modes.add(make_link(link, target))
    print("link mode: " + (", ".join(sorted(modes)) or "links already present"))
    manifest = {"repo": repo, "tag": tag, "commit": commit, "store": str(store), "files": sums,
                "links": [{"client": c, "link": str(l), "target": str(t)} for c, l, t in links],
                "skipped_clients": skipped, "collisions": collisions}
    (home / STORE_PARENT / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return verify(home)


def report(tag, commit, store, dirs, skipped, links, collisions, apply):
    print(f"{'install' if apply else 'plan (dry run)'}: Peritia {tag} at {commit} -> {store}")
    for client, folder in sorted(dirs.items()):
        print(f"  {client}: {sum(1 for c, _l, _t in links if c == client)} links in {folder}")
    for client, why in sorted(skipped.items()):
        print(f"  {client}: skipped ({why})")
    for line in collisions:
        print(f"  collision, left as it is: {line}")


def load_manifest(home):
    path = home / STORE_PARENT / MANIFEST
    if not path.is_file():
        raise SystemExit(f"no Peritia user install: {path} is absent")
    return json.loads(path.read_text(encoding="utf-8"))


def store_problems(manifest):
    store, sums = Path(manifest["store"]), manifest["files"]
    found = sorted(p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()) if store.is_dir() else []
    if len(found) > MAX_FILES:
        return [f"store holds more than {MAX_FILES} files"]
    bad = [f"missing: {rel}" for rel in sorted(set(sums) - set(found))]
    bad += [f"not in the release: {rel}" for rel in found if rel not in sums]
    bad += [f"changed: {rel}" for rel in found if rel in sums and digest(store / rel) != sums[rel]]
    return bad


def link_problems(manifest):
    bad = []
    for row in manifest["links"]:
        link = Path(row["link"])
        target = link_target(link)
        if target is None:
            bad.append(f"link missing or replaced: {link}")
        elif target != Path(row["target"]):
            bad.append(f"link points elsewhere: {link} -> {target}")
    return bad


def verify(home):
    manifest = load_manifest(home)
    bad = store_problems(manifest) + link_problems(manifest)
    for line in bad:
        print(f"verify: {line}")
    clients = sorted({row["client"] for row in manifest["links"]})
    print(f"verify: Peritia {manifest['tag']} {manifest['commit'][:12]}: {len(manifest['files'])} files, "
          f"{len(manifest['links'])} links for {', '.join(clients) or 'no client'}: {'FAIL' if bad else 'ok'}")
    return 1 if bad else 0


def uninstall(home, apply):
    manifest = load_manifest(home)
    store = Path(manifest["store"])
    ours = [Path(r["link"]) for r in manifest["links"] if link_target(Path(r["link"])) == Path(r["target"])]
    print(f"{'uninstall' if apply else 'uninstall plan (dry run)'}: {len(ours)} links, store {store}")
    if not apply:
        return 0
    for link in ours:
        remove_link(link)
    for path in sorted(store.rglob("*"), reverse=True)[:MAX_FILES]:
        path.chmod(stat.S_IRWXU) if path.is_dir() else path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(store, ignore_errors=False)
    (home / STORE_PARENT / MANIFEST).unlink()
    left = leftovers(home, manifest)
    print(f"uninstall: {len(left)} Peritia files or links left")
    return 1 if left else 0


def leftovers(home, manifest):
    """Every path of this install that still exists: store, manifest, and links into the store."""
    store = Path(manifest["store"])
    left = [str(p) for p in (store, home / STORE_PARENT / MANIFEST) if p.exists() or p.is_symlink()]
    for row in manifest["links"]:
        target = link_target(Path(row["link"]))
        if target is not None and str(target).startswith(str(store)):
            left.append(row["link"])
    return left


def fixture_repo(root):
    """A local repository with two skills and an annotated tag v9.9.9, standing in for the release."""
    src = root / "src"
    for name in ("alpha", "beta"):
        (src / "skills" / name).mkdir(parents=True)
        (src / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")
    (src / "CREDITS.md").write_text("# Credits\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "fixture"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "tag", "-a", "v9.9.9", "-m", "v9.9.9"]):
        subprocess.run(["git", *args], cwd=src, env=env, check=True, capture_output=True, timeout=GIT_TIMEOUT)
    return src.as_uri()


def quiet(fn, *args):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def selftest_cases(root):
    home, repo = root / "home", fixture_repo(root)
    os.environ["CODEX_HOME"] = str(home / ".codex")
    args = {"--tag": "v9.9.9", "--repo": repo, "--clients": "all"}
    cases = [("plan writes nothing", quiet(install, args, home, False) == 0 and not (home / STORE_PARENT).exists())]
    (home / ".claude/skills/beta").mkdir(parents=True)  # a skill of the same name from somewhere else
    cases.append(("install reads back ok", quiet(install, args, home, True) == 0))
    manifest = load_manifest(home)
    cases.append(("9 links: 2 skills x 5 clients minus 1 collision", len(manifest["links"]) == 9))
    cases.append(("collision left as it is", (home / ".claude/skills/beta").is_dir()
                  and not (home / ".claude/skills/beta").is_symlink() and len(manifest["collisions"]) == 1))
    target = Path(manifest["store"]) / "skills/alpha/SKILL.md"
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    target.write_text("tampered\n", encoding="utf-8")
    cases.append(("tampered file refused", quiet(verify, home) == 1))
    target.write_text("---\nname: alpha\n---\n# alpha\n", encoding="utf-8")
    cases.append(("restored file passes", quiet(verify, home) == 0))
    extra = Path(manifest["store"]) / "skills/alpha/extra.md"
    extra.write_text("x\n", encoding="utf-8")
    cases.append(("added file refused", quiet(verify, home) == 1))
    extra.unlink()
    remove_link(Path(manifest["links"][0]["link"]))
    cases.append(("missing link refused", quiet(verify, home) == 1))
    make_link(Path(manifest["links"][0]["link"]), Path(manifest["links"][0]["target"]))
    cases.append(("second install refused", refused(lambda: quiet(install, args, home, True))))
    cases.append(("uninstall leaves zero Peritia files", quiet(uninstall, home, True) == 0
                  and not leftovers(home, manifest) and (home / ".claude/skills/beta").is_dir()))
    return cases


def refused(call):
    try:
        call()
    except SystemExit:
        return True
    return False


def selftest():
    keep = {name: os.environ.get(name) for name in ("CODEX_HOME", LINK_MODE_ENV)}
    modes = ["symlink", "junction"] if os.name == "nt" else ["symlink"]
    cases = []
    try:
        for mode in modes:
            os.environ[LINK_MODE_ENV] = mode
            with tempfile.TemporaryDirectory() as tmp:
                cases += [(f"{mode}: {name}", ok) for name, ok in selftest_cases(Path(tmp))]
        if os.name != "nt":
            os.environ[LINK_MODE_ENV] = "junction"
            cases.append(("junction mode refused off Windows", refused(lambda: make_link(Path("x"), Path("y")))))
    finally:
        for name, value in keep.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
    bad = [name for name, ok in cases if not ok]
    if bad:
        raise SystemExit(f"selftest failed: {bad}")
    print(f"selftest ok: {len(cases)} cases, link modes {', '.join(modes)} "
          "(plan, install, collision, tamper, add, link, reinstall, uninstall)")
    return 0


def main(argv):
    verb = argv[0] if argv else "plan"
    flags = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--") and argv[i] != "--apply"}
    apply, home = "--apply" in argv, Path(flags.get("--home") or Path.home())
    if verb == "selftest":
        return selftest()
    if verb in ("plan", "install"):
        return install(flags, home, verb == "install" and apply)
    if verb == "verify":
        return verify(home)
    if verb == "uninstall":
        return uninstall(home, apply)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
