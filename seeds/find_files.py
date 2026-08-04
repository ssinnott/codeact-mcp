"""Walking a directory tree for files matching glob patterns."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Sequence

from codeact import helper

#: Directories never descended into. These hold generated or vendored content
#: that swamps a result list without ever being what the caller meant.
NOISE_DIRS = (".git", "__pycache__", "node_modules", ".venv")


def _matches(patterns: Sequence[str], rel: str, name: str) -> bool:
    """True if any pattern matches — path-wise if it has a '/', else name-wise."""
    for pattern in patterns:
        target = rel if "/" in pattern else name
        if fnmatch.fnmatch(target, pattern):
            return True
    return False


@helper(
    job="inspect",
    domains=["fs"],
    side_effects="filesystem",
    examples=[
        {
            "setup": (
                "import os, pathlib, tempfile\n"
                "_root = tempfile.mkdtemp()\n"
                "for _f in ['app.py', 'app_test.py', 'README.md',\n"
                "           'src/util.py', 'src/util_test.py', 'src/data.json',\n"
                "           'build/out.py', 'node_modules/pkg/setup.py',\n"
                "           '.git/config']:\n"
                "    _q = pathlib.Path(_root, _f)\n"
                "    _q.parent.mkdir(parents=True, exist_ok=True)\n"
                "    _q.write_text('x')\n"
                "_rel = lambda ps: [os.path.relpath(p, _root) for p in ps]"
            ),
            "code": "_rel(find_files(_root, include=['*.py']))",
            "note": (
                "_rel only trims the temp prefix for display; the real return is "
                "full paths. node_modules/ and .git/ are pruned without being asked"
            ),
        },
        {
            "code": "_rel(find_files(_root, include=['*.py'], exclude=['*_test.py', 'build']))",
            "note": "'*_test.py' drops files by name; 'build' matches a directory and prunes it",
        },
        {
            "code": "_rel(find_files(_root, include=['src/*']))",
            "note": "a pattern containing '/' is matched against the path relative to root",
        },
        {
            "code": "find_files(os.path.join(_root, 'does-not-exist'))",
            "note": "a missing root is an error, not an empty list",
            "raises": True,
        },
    ],
)
def find_files(
    root: str,
    *,
    include: Sequence[str] = ("*",),
    exclude: Sequence[str] = (),
    follow_symlinks: bool = False,
) -> list[str]:
    """Collect the files under a directory tree that match glob patterns.

    Use when: you need the set of files to work on — every *.py to lint, every
        test file to count, every config under a service dir — and you want the
        usual junk (.git, __pycache__, node_modules, .venv) gone without
        spelling it out each time.
    Don't use when: you want directories rather than files (this returns files
        only), you need to look inside .git/ or node_modules/ (they are always
        pruned — use os.walk directly), or the tree is huge and you want to stop
        at the first hit (this walks everything and returns a full list).

    Args:
        root: Directory to walk, as a path string. Must already exist and be a
            directory. Results are built by joining onto this string, so a
            relative root gives relative results and an absolute root gives
            absolute ones.
        include: Glob patterns; a file is kept if it matches at least one. A
            pattern containing "/" is matched against the file's path relative
            to root in POSIX form ("src/*.py", "**/x" is NOT special — use
            "*/x"); a pattern without "/" is matched against the bare filename
            ("*.py"). Default ("*",) keeps everything. Passing an empty sequence
            matches nothing and returns [].
        exclude: Glob patterns that reject. Matched the same way as include, and
            applied after it, so exclude always wins. A pattern that matches a
            directory (by name or relative path) prunes that whole subtree, which
            is the cheap way to skip "build" or "docs/_generated".
        follow_symlinks: Descend into directories that are symlinks. Default
            False. Symlinks pointing at files are returned either way; this only
            controls recursion, and setting it True on a tree with a symlink
            cycle will loop forever.

    Returns:
        list of file paths as strings, sorted lexicographically, each one
        root joined to its relative path. Directories are never included. No
        matches gives an empty list.

    Raises:
        FileNotFoundError: root does not exist — check the path before retrying;
            an empty result would otherwise hide the typo.
        NotADirectoryError: root exists but is a file. Pass its parent.

    Preconditions:
        Patterns use fnmatch syntax (* ? [seq]), not regex. Case sensitivity
        follows the platform: case-sensitive on Linux and macOS, folded on
        Windows.

    Notes:
        Unreadable subdirectories are skipped silently rather than raising, so a
        permission-denied corner of the tree yields fewer results, not a crash.
    """
    if not os.path.exists(root):
        raise FileNotFoundError(f"no such directory: {root}")
    if not os.path.isdir(root):
        raise NotADirectoryError(f"not a directory: {root}")

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        kept_dirs = []
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if name in NOISE_DIRS or _matches(exclude, rel, name):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not _matches(include, rel, name):
                continue
            if _matches(exclude, rel, name):
                continue
            found.append(full)
    return sorted(found)
