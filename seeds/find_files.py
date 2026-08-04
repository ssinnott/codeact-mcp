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


def _clean(patterns: Sequence[str], argname: str) -> list[str]:
    """Reject unusable pattern arguments loudly; return patterns ready to match.

    The bare string is the trap worth raising over: "*.py" satisfies
    Sequence[str] and then iterates one character at a time into the patterns
    "*", ".", "p", "y" — and "*" matches everything, so the caller gets a
    plausible-looking list of the wrong files with no error anywhere.
    """
    if isinstance(patterns, (str, bytes)):
        shown = patterns.decode(errors="replace") if isinstance(patterns, bytes) else patterns
        raise TypeError(
            f"{argname} must be a sequence of patterns, not a bare string: pass "
            f"[{shown!r}], not {shown!r}. A bare string is iterated character by "
            "character, so '*' alone would match every file."
        )

    cleaned: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise TypeError(
                f"{argname} patterns must be strings, got "
                f"{type(pattern).__name__}: {pattern!r}"
            )
        if pattern.startswith("/") or os.path.isabs(pattern):
            raise ValueError(
                f"{argname} pattern {pattern!r} is absolute, and patterns are matched "
                "against paths relative to root — it would never match. Drop the root "
                "prefix: pass 'src/*.py', not '/home/me/proj/src/*.py'."
            )
        stripped = (pattern[2:] if pattern.startswith("./") else pattern).rstrip("/")
        if not stripped:
            raise ValueError(f"{argname} pattern {pattern!r} is empty; it can never match")
        cleaned.append(stripped)
    return cleaned


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
            "note": (
                "a pattern containing '/' is matched against the path relative to "
                "root, never against the bare filename"
            ),
        },
        {
            "code": "_rel(find_files(_root, include=['util.py']))",
            "note": (
                "no '/' in the pattern, so it is matched against the bare filename "
                "at any depth — src/util.py is found without naming src"
            ),
        },
        {
            "code": "find_files(_root, include='*.py')",
            "note": (
                "a bare string is refused: it satisfies Sequence[str] but iterates "
                "into '*', '.', 'p', 'y', and '*' would match every file. "
                "Pass ['*.py'], not '*.py'"
            ),
            "raises": True,
        },
        {
            "code": "find_files(_root, include=[os.path.join(_root, 'src', '*.py')])",
            "note": (
                "patterns are matched against root-relative paths, so an absolute "
                "pattern is an error rather than a silent empty result"
            ),
            "raises": True,
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
        include: Glob patterns; a file is kept if it matches at least one. Must
            be a sequence of strings, such as a list — a bare string raises
            TypeError rather than being iterated one character at a time, since
            include="*.py" would otherwise mean the four patterns "*", ".",
            "p", "y" and quietly keep every file. What a pattern
            is matched against depends on whether it contains "/": with a "/"
            it is matched against the file's path relative to root and nothing
            else ("src/*.py" tested against "src/util.py"); without a "/" it is
            matched against the bare filename and nothing else ("*.py" tested
            against "util.py"), which hits files at every depth. Relative paths
            are built with "/" separators on every platform, so patterns always
            use "/" too. Patterns are relative to root and never absolute: a
            leading "./" and any trailing "/" are ignored ("./src/" means
            "src"), and an absolute pattern raises ValueError instead of
            quietly matching nothing. This is fnmatch, not pathlib.glob — "*"
            also matches "/", so "src/*.py" reaches nested files at any depth
            and "**" buys you nothing. Default ("*",) keeps everything; an
            empty sequence ([]) keeps nothing and returns [], whereas an empty
            pattern ([""]) is an error.
        exclude: Glob patterns that reject, with the same type rule and the same
            "/" rule as include, applied after it, so exclude always wins. A
            directory is tested the same way — by its bare name if the pattern
            has no "/", by its path relative to root if it does — and a match
            prunes that whole subtree, which is the cheap way to skip "build" or
            "docs/_generated". Default () rejects nothing.
        follow_symlinks: Descend into directories that are symlinks. Default
            False. Symlinks pointing at files are returned either way; this only
            controls recursion, and setting it True on a tree with a symlink
            cycle will loop forever.

    Returns:
        list of file paths as strings, sorted lexicographically, each one
        root joined to its relative path. Directories are never included. No
        matches gives an empty list.

    Raises:
        TypeError: include or exclude was a bare string instead of a sequence of
            them — pass ['*.py'], not '*.py' — or an element of one was not a
            string. Checked before root, and before any pattern is used.
        ValueError: a pattern is absolute, or is empty once "./" and trailing
            "/" are removed. Absolute patterns are rejected because matching
            happens against root-relative paths, so strip the root prefix and
            pass 'src/*.py'.
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
    # Argument shape first: a bad pattern is the caller's typo either way, and
    # reporting it before touching the disk keeps the message about the typo.
    includes = _clean(include, "include")
    excludes = _clean(exclude, "exclude")

    if not os.path.exists(root):
        raise FileNotFoundError(f"no such directory: {root}")
    if not os.path.isdir(root):
        raise NotADirectoryError(f"not a directory: {root}")

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        kept_dirs = []
        for name in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if name in NOISE_DIRS or _matches(excludes, rel, name):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not _matches(includes, rel, name):
                continue
            if _matches(excludes, rel, name):
                continue
            found.append(full)
    return sorted(found)
