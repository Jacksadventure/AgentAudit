# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trace links: tie the executor's consequential actions to the exact harness text behind them.

The improver otherwise sees a harness and a trajectory side by side and must guess which
instruction produced which behavior. A trace link makes that explicit:

    action #17 bash(python3 scripts/check_totals.py ...)  <-  skills/xlsx_qa/SKILL.md:12-13
                                                              "Always recompute totals ..."

Two kinds of link, both resolved against the harness tree that produced the rollout:

* cited -- the executor quoted harness text on a tool call (the `/trace` route of
  `harness2-copilot-bridge` records these by tool-call id in `refs.jsonl`); a quote is kept
  only when it was visible verbatim in that request AND occurs in exactly one harness file.
* used  -- deterministic: a skill the executor loaded, a harness script it ran, a sub-agent it
  dispatched.

For a pool candidate the links are folded onto its `harness.diff`: every edited hunk is
reported as cited (with the actions), loaded-but-not-cited, or never exercised -- an edit the
executor never touched cannot be the cause of whatever changed in that rollout.

Links are the executor's self-report, verified verbatim, not proof of causation. They carry no
score and no grader output.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# Files injected into every turn (the OpenCode surface's always-on text). An uncited hunk there
# was still in context on every call, so "not cited" is not "not exercised" for it.
ALWAYS_ON = (
    "systemprompt.md",
    "guardrails.md",
    "LongTermMEMORY.md",
    "tool_descriptions/tool_guidance.md",
)
TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".json", ""}
TOOL_TYPES = ("tool", "tool_use", "tool_call", "tool-invocation")
MAX_ACTIONS_LISTED = 6


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------- refs index


class RefsIndex:
    """call_id -> verified refs, from one or more `refs.jsonl` files (reloaded on change)."""

    def __init__(self, paths: Iterable[Path | str]):
        self.paths = [Path(p) for p in paths]
        self._stamp: tuple = ()
        self._by_call: dict[str, dict] = {}
        self._by_session: dict[str, dict] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "RefsIndex":
        raw = os.environ.get("HARNESS2_TRACE_REFS", "")
        return cls([p for p in raw.split(os.pathsep) if p])

    def _reload(self) -> None:
        stamp = tuple((str(p), p.stat().st_mtime, p.stat().st_size) for p in self.paths if p.is_file())
        if stamp == self._stamp:
            return
        by_call: dict[str, dict] = {}
        by_session: dict[str, dict] = {}
        for p in self.paths:
            if not p.is_file():
                continue
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("kind") == "report" and rec.get("session"):
                    by_session[str(rec["session"])] = rec  # the last final reply wins
                elif rec.get("call_id"):
                    by_call[str(rec["call_id"])] = rec
        self._by_call, self._by_session, self._stamp = by_call, by_session, stamp

    def get(self, call_id: str) -> dict | None:
        with self._lock:
            self._reload()
            return self._by_call.get(call_id)

    def report(self, session: str) -> dict | None:
        """The executor's end-of-run harness report for one OpenCode session, if it gave one."""
        with self._lock:
            self._reload()
            rec = self._by_session.get(session)
            return rec.get("report") if rec else None

    def __bool__(self) -> bool:
        return any(p.is_file() for p in self.paths)


# ------------------------------------------------------------------------------ actions


@dataclass
class Action:
    """One tool call, numbered as the n-th `[ACTION]` line of the rendered trajectory."""

    n: int
    tool: str
    call_id: str
    args: dict
    status: str
    detail: str = ""

    def short(self, cap: int = 110) -> str:
        if self.tool == "bash":
            s = str(self.args.get("command", ""))
        elif self.tool in ("read", "write", "edit"):
            s = str(self.args.get("filePath", ""))
        elif self.tool == "skill":
            s = str(self.args.get("name", ""))
        else:
            s = json.dumps(self.args, ensure_ascii=False)
        s = _norm(s)
        return f"{self.tool}({s[:cap]}{'…' if len(s) > cap else ''})"

    def outcome(self) -> str:
        if self.status == "error":
            return "ERROR"
        if self.detail:
            return self.detail
        return self.status or "?"


def read_actions(stream: Path) -> list[Action]:
    """Tool calls in an OpenCode `--format json` stream, numbered like `render_events`."""
    out: list[Action] = []
    try:
        raw = Path(stream).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or isinstance(ev.get("error"), dict):
            continue
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        if part.get("type", ev.get("type")) not in TOOL_TYPES:
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else part
        args = state.get("input") or part.get("input") or part.get("args") or {}
        meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        detail = ""
        if isinstance(meta.get("exit"), int):
            detail = f"exit {meta['exit']}"
        out.append(
            Action(
                n=len(out) + 1,
                tool=str(part.get("tool") or part.get("name") or "tool"),
                call_id=str(part.get("callID") or part.get("id") or ""),
                args=args if isinstance(args, dict) else {"input": args},
                status=str(state.get("status", "")),
                detail=detail,
            )
        )
    return out


def read_session(stream: Path) -> str:
    """The OpenCode session id of a `--format json` stream (its first event that names one)."""
    try:
        with open(stream, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                m = re.search(r'"sessionID"\s*:\s*"([^"]+)"', ln)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


# ------------------------------------------------------------------------ harness lookup


@dataclass
class HarnessText:
    """Whitespace-normalised harness files with a map from normalised offset to line."""

    files: dict[str, str]
    _norms: dict[str, tuple[str, list[int]]] = field(default_factory=dict)

    @classmethod
    def load(cls, harness_dir: Path | None) -> "HarnessText":
        files: dict[str, str] = {}
        if harness_dir and Path(harness_dir).is_dir():
            root = Path(harness_dir)
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if "__pycache__" in p.parts:
                    continue
                try:
                    files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        return cls(files)

    def _normed(self, rel: str) -> tuple[str, list[int]]:
        if rel not in self._norms:
            text, chars, lines = self.files[rel], [], []
            line, pending_space = 1, False
            for ch in text:
                if ch.isspace():
                    pending_space = bool(chars)
                    if ch == "\n":
                        line += 1
                    continue
                if pending_space:
                    chars.append(" ")
                    lines.append(line)
                    pending_space = False
                chars.append(ch)
                lines.append(line)
            self._norms[rel] = ("".join(chars), lines)
        return self._norms[rel]

    def locate(self, quote: str) -> tuple[str, int, int] | None:
        """(file, first line, last line) of the one harness file holding `quote`, else None."""
        q = _norm(quote)
        if len(q) < 8:
            return None
        hits = []
        for rel in self.files:
            normed, lines = self._normed(rel)
            i = normed.find(q)
            if i >= 0:
                hits.append((rel, lines[i], lines[i + len(q) - 1]))
        return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------------- links


# Wording of a line (or of the executor's reason for citing it) that holds work back: keep a
# supplied placeholder, mark a result provisional, decline to compute or answer for lack of data.
# Such a citation shows the line CHANGED behavior, possibly by suppressing work the reference did.
HOLD_BACK = re.compile(
    r"\b(provisional|placeholders?|tbd|"
    r"(?:labell?ed|as an?|separate)\s+(?:\w+\s+)?allowance|"
    r"retain(?:s|ed|ing)?\b.{0,40}\b(?:supplied|preliminary)\b|"
    r"(?:do not|don't|never|avoid)\s+(?:invent|assume|guess|estimate|compute|calculate|extrapolate)|"
    r"cannot be (?:verified|determined|computed|calculated)|"
    r"(?:leave|keep|mark|label|flag)\w*\b.{0,30}\b(?:open|unresolved|provisional|blank|pending)\b|"
    r"defer(?:s|red)?\b|rather than (?:comput|calculat|estimat))",
    re.IGNORECASE,
)


@dataclass
class Link:
    action: Action
    kind: str  # "cited" | "loaded" | "ran" | "dispatched"
    file: str
    first: int = 0
    last: int = 0
    quote: str = ""
    why: str = ""

    def where(self) -> str:
        return f"{self.file}:{self.first}-{self.last}" if self.first else self.file

    @property
    def restricts(self) -> bool:
        """A citation whose line or reason holds work back (see HOLD_BACK)."""
        return self.kind == "cited" and bool(HOLD_BACK.search(f"{self.quote} {self.why}"))


@dataclass
class Reported:
    """One entry of the executor's end-of-run report, located in the harness."""

    kind: str  # "followed" | "ignored"
    file: str
    first: int
    last: int
    quote: str
    note: str

    def where(self) -> str:
        return f"{self.file}:{self.first}-{self.last}"


@dataclass
class RolloutLinks:
    label: str
    actions: list[Action]
    links: list[Link]
    unmapped: int = 0  # verified quotes of text outside this harness (e.g. the agent's own prompt)
    reported: list[Reported] = field(default_factory=list)

    def failed_citations(self) -> list[Link]:
        """Cited links whose action errored or exited non-zero: the sharpest attribution."""
        return [
            link
            for link in self.links
            if link.kind == "cited"
            and (link.action.status == "error" or re.match(r"exit [1-9]", link.action.detail))
        ]

    def by_action(self) -> dict[int, list[Link]]:
        out: dict[int, list[Link]] = {}
        for link in self.links:
            out.setdefault(link.action.n, []).append(link)
        return out


def _skill_file(h: HarnessText, name: str) -> str | None:
    for rel in h.files:
        parts = rel.split("/")
        if len(parts) == 3 and parts[0] == "skills" and parts[1] == name and parts[2] == "SKILL.md":
            return rel
    return None


def link_rollout(
    label: str, stream: Path | None, harness_dir: Path | None, refs: RefsIndex
) -> RolloutLinks:
    actions = read_actions(stream) if stream else []
    h = HarnessText.load(harness_dir)
    scripts = {Path(rel).name: rel for rel in h.files if rel.startswith("scripts/") and not rel.endswith("README.md")}
    agents = {Path(rel).stem: rel for rel in h.files if rel.startswith("sub_agents/") and not rel.endswith("README.md")}
    links: list[Link] = []
    unmapped = 0
    for a in actions:
        rec = refs.get(a.call_id) if a.call_id else None
        for r in (rec or {}).get("refs", []):
            if not r.get("visible"):
                continue
            hit = h.locate(r.get("quote", ""))
            if hit is None:
                unmapped += 1
                continue
            links.append(Link(a, "cited", hit[0], hit[1], hit[2], _norm(r["quote"]), r.get("why", "")))
        if a.tool == "skill":
            rel = _skill_file(h, str(a.args.get("name", "")))
            if rel:
                links.append(Link(a, "loaded", rel))
        elif a.tool == "read":
            path = str(a.args.get("filePath", ""))
            m = re.search(r"\.opencode/skills/([^/]+)/SKILL\.md$", path)
            rel = _skill_file(h, m.group(1)) if m else None
            if rel:
                links.append(Link(a, "loaded", rel))
        elif a.tool == "bash":
            cmd = str(a.args.get("command", ""))
            for name, rel in scripts.items():
                if re.search(rf"(^|[\s/'\"]){re.escape(name)}\b", cmd):
                    links.append(Link(a, "ran", rel))
        elif a.tool == "task":
            sub = str(a.args.get("subagent_type", ""))
            if sub in agents:
                links.append(Link(a, "dispatched", agents[sub]))
    reported: list[Reported] = []
    session = read_session(stream) if stream else ""
    report = refs.report(session) if session else None
    for kind, note in (("followed", "effect"), ("ignored", "why")):
        for r in (report or {}).get(kind, []):
            if not r.get("visible"):
                continue
            hit = h.locate(r.get("quote", ""))
            if hit is None:
                unmapped += 1
                continue
            reported.append(Reported(kind, hit[0], hit[1], hit[2], _norm(r["quote"]), r.get(note, "")))
    return RolloutLinks(label, actions, links, unmapped, reported)


# ----------------------------------------------------------------------------- render


def _fmt_actions(links: Sequence[Link]) -> str:
    seen, parts = set(), []
    for link in links:
        if link.action.n in seen:
            continue
        seen.add(link.action.n)
        parts.append(f"#{link.action.n} {link.action.short(70)} -> {link.action.outcome()}")
    more = f" (+{len(parts) - MAX_ACTIONS_LISTED} more)" if len(parts) > MAX_ACTIONS_LISTED else ""
    return "; ".join(parts[:MAX_ACTIONS_LISTED]) + more


def render_links(rl: RolloutLinks, *, harness_name: str) -> str:
    """The per-rollout link index: harness location -> the actions it drove."""
    out = [
        f"# Trace links for `{rl.label}`",
        "",
        f"Harness: `{harness_name}`. Actions are numbered as the n-th `[ACTION]` of the "
        "rollout's trajectory, where the same links appear inline as `[HARNESS LINK]` lines.",
        "",
    ]
    if not rl.actions:
        return "\n".join(out + ["(no actions could be read from this rollout)"]) + "\n"
    cited = [link for link in rl.links if link.kind == "cited"]
    used = [link for link in rl.links if link.kind != "cited"]
    out.append(
        f"{len(rl.actions)} actions; {len({link.action.n for link in cited})} cite harness text "
        f"({len(cited)} verified quotes); {len(used)} skill loads / script runs / dispatches."
    )
    if rl.unmapped:
        out.append(
            f"{rl.unmapped} further verified quote(s) cite text that is not in this harness "
            "(the agent's built-in prompt or task material) and are omitted."
        )
    out.append("")
    if cited:
        out += ["## Harness text the executor cited for a consequential action", ""]
        groups: dict[tuple[str, int, int], list[Link]] = {}
        for link in cited:
            groups.setdefault((link.file, link.first, link.last), []).append(link)
        for (f, a, b), ls in sorted(groups.items()):
            tag = " (restricts)" if any(link.restricts for link in ls) else ""
            out.append(f"- `{f}:{a}-{b}`{tag} \"{ls[0].quote[:160]}\"")
            out.append(f"  - drove: {_fmt_actions(ls)}")
            whys = sorted({link.why for link in ls if link.why})[:2]
            if whys:
                out.append(f"  - executor's reason: {' | '.join(whys)}")
        out.append("")
    failed = rl.failed_citations()
    if failed:
        out += [
            "## Cited harness lines whose action FAILED (first suspects for a fix)",
            "",
        ]
        for link in failed:
            out.append(
                f"- `{link.where()}` \"{link.quote[:120]}\" -> #{link.action.n} "
                f"{link.action.short(70)} -> {link.action.outcome()}"
            )
        out.append("")
    if rl.reported:
        out += ["## Executor's end-of-run report on the harness", ""]
        for r in rl.reported:
            label = "FOLLOWED" if r.kind == "followed" else "IGNORED"
            out.append(f"- {label} `{r.where()}` \"{r.quote[:140]}\" -- {r.note}")
        out.append("")
    if used:
        out += ["## Harness components the executor used", ""]
        groups2: dict[tuple[str, str], list[Link]] = {}
        for link in used:
            groups2.setdefault((link.kind, link.file), []).append(link)
        for (kind, f), ls in sorted(groups2.items()):
            out.append(f"- {kind} `{f}`: {_fmt_actions(ls)}")
        out.append("")
    return "\n".join(out) + "\n"


def annotate_trajectory(rendered: str, rl: RolloutLinks) -> str:
    """Insert `[HARNESS LINK]` lines after the actions they belong to."""
    by_n = rl.by_action()
    if not by_n:
        return rendered
    out, n = [], 0
    for line in rendered.split("\n"):
        out.append(line)
        if line.startswith("[ACTION] "):
            n += 1
            for link in by_n.get(n, []):
                if link.kind == "cited":
                    out.append(
                        f"[HARNESS LINK] cites{' (restricts)' if link.restricts else ''} "
                        f"{link.where()} \"{link.quote[:200]}\""
                        + (f" -- {link.why}" if link.why else "")
                    )
                else:
                    out.append(f"[HARNESS LINK] {link.kind} {link.file}")
    return "\n".join(out)


# ---------------------------------------------------------------------- edit exercise


@dataclass
class Hunk:
    file: str
    first: int  # 1-based lines in the candidate file; 0 for a pure deletion
    last: int
    kind: str  # "added file" | "changed" | "inserted" | "deleted"
    preview: str


def edit_hunks(reference: dict[str, str], candidate: dict[str, str]) -> list[Hunk]:
    hunks: list[Hunk] = []
    for name in sorted(reference.keys() | candidate.keys()):
        old = reference.get(name, "").splitlines()
        new = candidate.get(name, "").splitlines()
        if old == new:
            continue
        if name not in reference:
            hunks.append(Hunk(name, 1, max(1, len(new)), "added file", _norm(" ".join(new))[:100]))
            continue
        if name not in candidate:
            hunks.append(Hunk(name, 0, 0, "deleted", "(file removed)"))
            continue
        sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "delete":
                hunks.append(Hunk(name, 0, 0, "deleted", _norm(" ".join(old[i1:i2]))[:100]))
                continue
            text = _norm(" ".join(new[j1:j2]))
            if not text:
                continue
            hunks.append(
                Hunk(name, j1 + 1, j2, "changed" if tag == "replace" else "inserted", text[:100])
            )
    return hunks


def hunk_status(
    hunks: Sequence[Hunk], rl: RolloutLinks | None
) -> list[tuple[Hunk, str, bool]]:
    """(hunk, status, exercised) for each edited hunk, from its own rollout's links."""
    links = rl.links if rl else []
    reported = rl.reported if rl else []
    out: list[tuple[Hunk, str, bool]] = []
    for h in hunks:
        exercised = False
        if h.kind == "deleted":
            status = "DELETED (absence cannot be traced; judge it from behavior)"
        else:
            cited = [
                link
                for link in links
                if link.kind == "cited"
                and link.file == h.file
                and link.first <= h.last
                and link.last >= h.first
            ]
            used = [link for link in links if link.kind != "cited" and link.file == h.file]
            rep_ = [
                r for r in reported if r.file == h.file and r.first <= h.last and r.last >= h.first
            ]
            ign = [r for r in rep_ if r.kind == "ignored"]
            fol = [r for r in rep_ if r.kind == "followed"]
            failed = [
                link for link in cited if link in (rl.failed_citations() if rl else [])
            ]
            if cited:
                status = f"CITED by {_fmt_actions(cited)}"
                exercised = True
                if failed:
                    status += f" [{len(failed)} cited action(s) FAILED]"
                if any(link.restricts for link in cited):
                    status += (
                        " [restricts: it held work back -- check that point against the "
                        "reference deliverable]"
                    )
            elif fol:
                status = f"FOLLOWED (end-of-run report): {fol[0].note}"
                exercised = True
            elif used:
                status = f"{used[0].kind.upper()} (file used, hunk not quoted): {_fmt_actions(used)}"
                exercised = True
            elif ign:
                status = "IGNORED"
            elif h.file in ALWAYS_ON:
                status = "ALWAYS-ON, UNCITED"
            else:
                status = "NOT EXERCISED"
            if ign and not status.startswith("IGNORED"):
                status += " (partly IGNORED, see below)"
            elif ign:
                status += f" -- {ign[0].note}"
        out.append((h, status, exercised))
    return out


def render_exercise(
    label: str, reference: dict[str, str], candidate: dict[str, str], rl: RolloutLinks | None
) -> str:
    """Each edited hunk of a candidate, against what its own rollout did with it."""
    hunks = edit_hunks(reference, candidate)
    out = [
        f"# Which of `{label}`'s edits its rollout actually exercised",
        "",
        "Hunks of `harness.diff` (line numbers in the candidate's file). CITED: the executor "
        "quoted text inside the hunk on these actions. LOADED/RAN: the file was loaded or run "
        "but no line of this hunk was quoted. FOLLOWED / IGNORED: the executor's end-of-run "
        "report named this hunk. ALWAYS-ON, UNCITED: always-on text, in context on every call "
        "but never cited -- weak evidence either way. NOT EXERCISED: nothing in the rollout "
        "touched it, so this hunk cannot explain any behavior change in that rollout.",
        "",
    ]
    if not hunks:
        return "\n".join(out + ["(no edits: identical to the current harness)"]) + "\n"
    if rl is None or not rl.actions:
        out.append("(the candidate's rollout could not be read; exercise is unknown)\n")
    n_ex = 0
    for h, status, exercised in hunk_status(hunks, rl):
        n_ex += exercised
        where = f"{h.file}:{h.first}-{h.last}" if h.first else h.file
        out.append(f"- [{h.kind}] `{where}` \"{h.preview}\"")
        out.append(f"  - {status}")
    out.insert(4, f"{n_ex}/{len(hunks)} edited hunks exercised.\n")
    return "\n".join(out) + "\n"


def attribution_lines(
    label: str, reference: dict[str, str], candidate: dict[str, str], rl: RolloutLinks | None
) -> list[str]:
    """One line per edited hunk of a candidate, short enough to sit in the mission itself.

    This is the probe's behavioral contrast split by attribution: a contrast in that rollout can
    be credited to a hunk only when the hunk was exercised there.
    """
    hunks = edit_hunks(reference, candidate)
    if not hunks:
        return [f"- `{label}`: no edits"]
    if rl is None or not rl.actions:
        return [f"- `{label}`: {len(hunks)} hunks; its rollout could not be read"]
    rows = hunk_status(hunks, rl)
    out = [f"- `{label}`: {sum(e for _, _, e in rows)}/{len(rows)} edited hunks exercised"]
    for h, status, _ in rows:
        where = f"{h.file}:{h.first}-{h.last}" if h.first else h.file
        s = status if len(status) <= 150 else status[:149] + "…"
        out.append(f"  - `{where}` {s}")
    for link in rl.failed_citations()[:3]:
        out.append(
            f"  - FAILED after citing `{link.where()}`: #{link.action.n} "
            f"{link.action.short(60)} -> {link.action.outcome()}"
        )
    # Line-level detail a hunk-level status hides (a whole added skill file is one hunk).
    for r in rl.reported:
        if r.kind == "ignored":
            out.append(f"  - executor IGNORED `{r.where()}` \"{r.quote[:100]}\": {r.note}")
    return out
