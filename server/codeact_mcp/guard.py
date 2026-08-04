"""Capability policy, layer 1: a static AST walk over code before it runs.

This is the *policy* layer, not a security boundary. It exists to catch
unreviewed capability use, explain it clearly, and route it into the approval
flow. Sufficiently dynamic code defeats it, which is expected — containment is
the OS layer's job, not this one's.

Phase 1 runs it in audit mode: findings are recorded, nothing is blocked. The
tier lists below are guesses, and the audit log is what will correct them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

# Tier 0 — always available. Deliberately generous: pure computation over data
# already in the namespace cannot hurt anyone, and a stingy tier 0 produces an
# agent that fights the guard instead of working.
TIER0_MODULES = frozenset(
    """
    abc argparse ast base64 bisect calendar cmath collections colorsys copy csv
    dataclasses datetime decimal difflib enum fnmatch fractions functools gzip
    hashlib heapq hmac html inspect io ipaddress itertools json keyword locale
    math numbers operator pprint queue random re secrets statistics string
    stringprep struct textwrap time timeit token tokenize types typing
    unicodedata unittest uuid warnings weakref zlib
    """.split()
)

# Tier 1 — allowed and logged, not prompted. Claude Code already grants Read and
# Bash, so gating reads inside Python buys no security and costs real friction.
TIER1_MODULES = frozenset("glob os os.path pathlib shutil stat tempfile".split())

# Tier 2 — privileged; needs an approved helper or an explicit grant.
TIER2_MODULES = frozenset(
    """
    asyncio ctypes ftplib http httpx imaplib marshal multiprocessing pickle
    poplib requests selectors smtplib socket socketserver ssl subprocess
    telnetlib urllib urllib3 webbrowser xmlrpc
    """.split()
)

TIER2_BUILTINS = frozenset("eval exec compile __import__ breakpoint input open".split())

# Tier 3 — refused outright. Attribute traversal used to walk out of the
# sandbox; the agent must never be able to reach the guard or the library.
TIER3_ATTRS = frozenset(
    """
    __bases__ __builtins__ __class__ __closure__ __code__ __globals__
    __mro__ __subclasses__ f_back f_globals f_locals gi_frame tb_frame
    """.split()
)


@dataclass(frozen=True)
class Finding:
    tier: int
    kind: str
    name: str
    line: int

    def describe(self) -> str:
        return f"line {self.line}: {self.kind} `{self.name}` (tier {self.tier})"


def _module_tier(name: str) -> int | None:
    root = name.split(".")[0]
    if root in TIER0_MODULES:
        return None
    if root in TIER1_MODULES:
        return 1
    if root in TIER2_MODULES:
        return 2
    return 2  # unknown module: treat as privileged until proven otherwise


def scan(code: str) -> list[Finding]:
    """Every violation in the block, not just the first.

    Reporting one at a time would cost a round trip per violation, which turns a
    five-problem snippet into five exchanges.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tier = _module_tier(alias.name)
                if tier:
                    findings.append(Finding(tier, "import", alias.name, node.lineno))

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                tier = _module_tier(node.module)
                if tier:
                    findings.append(Finding(tier, "import", node.module, node.lineno))

        elif isinstance(node, ast.Attribute):
            if node.attr in TIER3_ATTRS:
                findings.append(Finding(3, "attribute", node.attr, node.lineno))

        elif isinstance(node, ast.Name):
            if node.id in TIER2_BUILTINS and isinstance(node.ctx, ast.Load):
                findings.append(Finding(2, "builtin", node.id, node.lineno))

    findings.sort(key=lambda f: (f.line, f.name))
    return findings


def summarize(findings: list[Finding]) -> str:
    """Audit-mode note appended to results. Informative, never blocking."""
    if not findings:
        return ""
    worst = max(f.tier for f in findings)
    lines = [f.describe() for f in dict.fromkeys(findings)]
    header = (
        f"[guard: audit only, nothing was blocked] {len(lines)} capability use(s), "
        f"highest tier {worst}"
    )
    return header + "\n" + "\n".join("  " + line for line in lines)


# Which capability each finding needs, so a grant can be named rather than
# expressed as a list of modules.
_CAPABILITY = {
    "socket": "network",
    "ssl": "network",
    "http": "network",
    "httpx": "network",
    "requests": "network",
    "urllib": "network",
    "urllib3": "network",
    "ftplib": "network",
    "smtplib": "network",
    "imaplib": "network",
    "poplib": "network",
    "telnetlib": "network",
    "xmlrpc": "network",
    "webbrowser": "network",
    "subprocess": "process",
    "multiprocessing": "process",
    "asyncio": "process",
    "ctypes": "process",
    "pickle": "deserialize",
    "marshal": "deserialize",
    "eval": "dynamic",
    "exec": "dynamic",
    "compile": "dynamic",
    "__import__": "dynamic",
    "breakpoint": "dynamic",
    "input": "dynamic",
    "open": "filesystem",
}


def capability_for(finding: Finding) -> str:
    root = finding.name.split(".")[0]
    return _CAPABILITY.get(root, "unknown")


def blocking(findings: list[Finding], granted: set[str]) -> list[Finding]:
    """Findings that enforcement should refuse, given the capabilities on hand."""
    out = []
    for finding in findings:
        if finding.tier >= 3:
            out.append(finding)  # never grantable
        elif finding.tier == 2 and capability_for(finding) not in granted:
            out.append(finding)
    return out


def refusal(blocked: list[Finding]) -> str:
    """Explain a refusal and name the two ways forward.

    The second route is the important one: the friction of the capability
    boundary is what channels privileged work into named, reviewed, reusable
    helpers, so every wall the agent hits is an invitation to propose something.
    """
    lines = ["BLOCKED — this session does not have the capabilities this code needs:"]
    for finding in dict.fromkeys(blocked):
        capability = capability_for(finding)
        if finding.tier >= 3:
            lines.append(f"  {finding.describe()} — refused outright, not grantable")
        else:
            lines.append(f"  {finding.describe()} — needs capability `{capability}`")

    grantable = sorted(
        {capability_for(f) for f in blocked if f.tier == 2 and capability_for(f) != "unknown"}
    )
    lines.append("")
    lines.append("Two ways forward:")
    lines.append(
        "  1. propose_helper(...) — package this as a reviewed helper. Approved once, "
        "it may use the capability forever, and every later session gets it too. This "
        "is usually the right answer."
    )
    if grantable:
        lines.append(
            f"  2. request_capability({grantable[0]!r}) — a one-off grant for this "
            "session only. The human is asked every time."
        )
    return "\n".join(lines)
