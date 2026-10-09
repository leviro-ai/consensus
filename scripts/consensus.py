#!/usr/bin/env python3
"""
consensus.py - drive a blind multi-model consensus panel.

CLI agents (defined in panel.json; currently Claude / Codex) independently solve a problem, then blindly
peer-review each other's answers, then revise. A moderator (the Claude Code session that
runs this script) writes the problem statement and synthesises the final answer.

Blindness is enforced structurally, not by asking nicely:
  * every prompt is fully self-contained, so panelists need no file access at all
  * each call runs in its own EMPTY temp directory, outside the problem tree
  * a rater never sees its own answer, and never sees author identities
  * every rater gets an INDEPENDENT random label permutation, so labels carry no
    information across ballots
  * answers are normalised (vendor names redacted, emoji and sign-offs stripped) before
    any rater sees them

Everything identity-revealing lives in the problem tree, which no panelist can reach.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"

# criterion -> weight. Weights sum to 10, each score is 0-10, so totals are out of 100.
CRITERIA = {
    "correctness": 3.0,
    "completeness": 2.0,
    "simplicity": 2.0,
    "risk_awareness": 1.5,
    "actionability": 1.5,
}

CANON = "ABCDEFGH"   # canonical anonymous ids, stable within a round
VIEW = "PQRSTUVW"    # per-rater view labels, permuted independently per rater

SOLVE_DIR = {1: "round-1-propose", 3: "round-3-revise"}
REVIEW_DIR = {2: "round-2-review", 3: "round-3-review"}
REVIEW_SOURCE = {2: "round-1-propose", 3: "round-3-revise"}

DEFAULT_REDACT = [
    "claude", "anthropic", "kimi", "moonshot", "moonshotai", "codex", "openai",
    "chatgpt", "gpt-4", "gpt-4o", "gpt-5", "o3", "o4-mini", "gemini", "deepmind",
    "sonnet", "opus", "haiku", "llama", "mistral", "deepseek", "qwen", "grok",
]

# Pictographs and dingbats only. Arrows (U+2190-21FF) are deliberately NOT stripped
# because "A -> B" carries meaning in technical writing.
EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "\U0000fe0e"
    "]",
    flags=re.UNICODE,
)

ATTRIBUTION_RE = re.compile(
    r"^\s*(?:generated with|co-authored-by|created by|written by|-{2,}\s*$).*$",
    re.IGNORECASE | re.MULTILINE,
)


# --------------------------------------------------------------------------- utilities

class Fail(Exception):
    """A user-facing error. Printed without a traceback."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "problem"


def seeded_rng(*parts: str) -> random.Random:
    """Deterministic RNG. Python's str hash is salted per process, so use sha256."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_json(path: Path):
    return json.loads(read(path))


def write_json(path: Path, obj) -> None:
    write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def warn(msg: str) -> None:
    print(f"  !! {msg}", file=sys.stderr)


# ------------------------------------------------------------------------ problem tree

def default_root() -> Path:
    env = os.environ.get("CONSENSUS_ROOT")
    return Path(env).expanduser() if env else Path.cwd() / "problems"


def resolve_problem(arg: str) -> Path:
    """Accept a full path, or a slug/number resolved against the default root."""
    p = Path(arg).expanduser()
    if (p / "problem.md").exists():
        return p.resolve()
    root = default_root()
    if (root / arg / "problem.md").exists():
        return (root / arg).resolve()
    if root.is_dir():
        hits = [d for d in sorted(root.iterdir())
                if d.is_dir() and (d / "problem.md").exists() and arg in d.name]
        if len(hits) == 1:
            return hits[0].resolve()
        if len(hits) > 1:
            raise Fail(f"'{arg}' matches several problems: {', '.join(h.name for h in hits)}")
    raise Fail(f"No problem found at '{arg}' (looked in {root}). Run: consensus.py new <slug>")


def load_config(pdir: Path) -> dict:
    cfg = read_json(pdir / "config.json") if (pdir / "config.json").exists() else {}
    cfg.setdefault("id", pdir.name)
    cfg.setdefault("strip_emoji", True)
    cfg.setdefault("redact_terms", DEFAULT_REDACT)
    return cfg


def load_panel(cfg: dict, only: list[str] | None = None) -> list[dict]:
    members = cfg.get("panel") or read_json(SKILL_DIR / "panel.json")["members"]
    if only:
        wanted = {o.strip() for o in only}
        unknown = wanted - {m["id"] for m in members}
        if unknown:
            raise Fail(f"Unknown panel member(s): {', '.join(sorted(unknown))}")
        members = [m for m in members if m["id"] in wanted]
    if not members:
        raise Fail("Panel is empty.")
    return members


def problem_text(pdir: Path) -> str:
    path = pdir / "problem.md"
    if not path.exists():
        raise Fail(f"Missing {path}")
    text = read(path)
    if "MODERATOR: you write this file" in text or "<TITLE>" in text:
        raise Fail(
            f"{path} is still the unedited template.\n"
            "The moderator must write the real problem statement (and delete the HTML\n"
            "comments and <placeholders>) before spending a round on the panel."
        )
    return text.strip()


# ----------------------------------------------------------------- answer normalisation

def build_redactor(cfg: dict, problem: str):
    """
    Redact vendor/model names so answers cannot be attributed by self-reference.

    Terms that appear in the problem statement itself are NOT redacted: everyone will
    mention them, so they carry no identifying signal, and redacting them would destroy
    the meaning of the answer (this matters when the problem is itself about AI tools).
    """
    low_problem = problem.lower()
    terms = [t for t in cfg.get("redact_terms", DEFAULT_REDACT)
             if t.lower() not in low_problem]
    if not terms:
        return None, []
    pattern = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])({pattern})(?![\w-])", re.IGNORECASE), terms


def normalize_answer(text: str, cfg: dict, problem: str) -> str:
    text = (text or "").strip()

    # Unwrap an answer that the model fenced in its entirety.
    if text.startswith("```") and text.endswith("```") and text.count("```") == 2:
        text = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    text = ATTRIBUTION_RE.sub("", text)

    redactor, _ = build_redactor(cfg, problem)
    if redactor:
        text = redactor.sub("[redacted]", text)

    if cfg.get("strip_emoji", True) and not EMOJI_RE.search(problem):
        text = EMOJI_RE.sub("", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# ------------------------------------------------------------------------- invocation

# Placeholders that may legitimately be empty. When one is, its argv element and the
# flag immediately preceding it are both dropped, so the tool falls back to its own
# default instead of being handed an empty argument.
OPTIONAL_PLACEHOLDERS = {"model", "thinking"}


def substitute(value: str, subs: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(subs.get(m.group(1), m.group(0))), value)


def render_argv(argv: list[str], subs: dict) -> list[str]:
    """Expand placeholders, dropping flags whose optional value is unset."""
    out: list[str] = []
    for element in argv:
        names = re.findall(r"\{(\w+)\}", element)
        unset = any(n in OPTIONAL_PLACEHOLDERS and not str(subs.get(n) or "").strip()
                    for n in names)
        if unset:
            if out and out[-1].startswith("-"):
                out.pop()
            continue
        out.append(substitute(element, subs))
    return out


def member_subs(member: dict, **extra) -> dict:
    return {"model": member.get("model") or "",
            "thinking": member.get("thinking") or "", **extra}


def run_member(member: dict, prompt: str, keep_workdirs: bool = False) -> dict:
    """Run one panelist in an empty ephemeral workdir. Never raises."""
    mid = member["id"]
    workdir = Path(tempfile.mkdtemp(prefix=f"consensus-{mid}-"))
    outfile = workdir / "answer.md"
    subs = member_subs(member, prompt=prompt, cwd=str(workdir), outfile=str(outfile))

    def sub(value: str) -> str:
        return substitute(value, subs)

    argv = render_argv(member["argv"], subs)
    started = time.time()
    result = {"member": mid, "started": now_iso(), "argv_head": argv[0]}

    try:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=member.get("timeout", 900),
        )
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout or ""
        result["stderr"] = (proc.stderr or "")[-8000:]

        answer = ""
        if member.get("answer_file"):
            f = Path(sub(member["answer_file"]))
            if f.exists():
                answer = f.read_text(encoding="utf-8", errors="replace")
        if not answer.strip():
            answer = result["stdout"]

        result["answer"] = answer
        if proc.returncode != 0:
            result["ok"] = False
            result["error"] = f"exit code {proc.returncode}"
        elif not answer.strip():
            result["ok"] = False
            result["error"] = "produced no output"
        else:
            result["ok"] = True
    except subprocess.TimeoutExpired:
        result.update(ok=False, error=f"timed out after {member.get('timeout', 900)}s",
                      answer="", stdout="", stderr="")
    except FileNotFoundError:
        result.update(ok=False, error=f"command not found: {argv[0]}",
                      answer="", stdout="", stderr="")
    except Exception as exc:  # noqa: BLE001 - one bad member must not kill the round
        result.update(ok=False, error=f"{type(exc).__name__}: {exc}",
                      answer="", stdout="", stderr="")
    finally:
        result["seconds"] = round(time.time() - started, 1)
        if keep_workdirs:
            result["workdir"] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    return result


def run_panel(jobs: list[tuple[dict, str]], keep_workdirs: bool = False) -> dict:
    """jobs = [(member, prompt)]. Runs concurrently, returns {member_id: result}."""
    names = ", ".join(m["id"] for m, _ in jobs)
    print(f"  -> dispatching {len(jobs)} call(s) in parallel: {names}")
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {m["id"]: pool.submit(run_member, m, p, keep_workdirs) for m, p in jobs}
        results = {mid: f.result() for mid, f in futures.items()}
    for mid, r in results.items():
        status = "ok" if r.get("ok") else f"FAILED ({r.get('error')})"
        print(f"     {mid:<8} {r['seconds']:>7.1f}s  {status}")
    return results


# ---------------------------------------------------------------------------- prompts

RULES_SOLVE = """\
OUTPUT RULES (violating these disqualifies your answer)
- Output ONLY the completed template below, as plain markdown. No preamble, no closing
  remarks, no commentary about the task or about your own process.
- Keep the template's section headings, in order. Drop a section only if it genuinely
  does not apply, and say so in one line rather than deleting the heading.
- Never state or hint at which AI model, product, or vendor you are. Your answer is
  reviewed anonymously and any self-identification invalidates the round.
- Do not use tools, read files, browse, or run commands. Everything you need is in this
  prompt, and nothing else you could reach is relevant.
- No emoji. No decorative formatting.
- Be specific. Name the actual thing. Unsupported confidence and vague filler
  ("use best practices", "add proper error handling") score near zero.
- Prefer the simplest design that fully satisfies the requirements. Unrequested features,
  abstractions, and configurability count against you.
"""


def prompt_propose(problem: str, template: str, panel_size: int) -> str:
    return f"""You are an anonymous panelist on a {panel_size}-member expert panel. Each panelist \
is solving the problem below independently, without seeing the others. Your answer will \
then be scored blind by the other panelists against a published rubric.

Write the strongest answer you can. You are competing on correctness, completeness, \
simplicity, risk awareness, and actionability - in that order of weight.

<problem>
{problem}
</problem>

{RULES_SOLVE}
<answer_template>
{template}
</answer_template>

Now output your completed answer.
"""


def ballot_schema(labels: list[str]) -> str:
    entries = ",\n".join(
        f"""    {{
      "label": "{lab}",
      "criteria": {{"correctness": 0, "completeness": 0, "simplicity": 0,
                    "risk_awareness": 0, "actionability": 0}},
      "blocking_issues": [],
      "strongest_element": "<one sentence: the single best thing in this answer>",
      "weakest_element": "<one sentence: the single worst thing in this answer>",
      "specific_critique": "<up to 200 words, addressed to the author, actionable. Say what to change and why. Quote the passage you mean.>"
    }}"""
        for lab in labels
    )
    ranking = ", ".join(f'"{lab}"' for lab in labels)
    return f"""{{
  "scores": [
{entries}
  ],
  "ranking": [{ranking}],
  "best_idea_worth_stealing": {{
    "label": "<{'|'.join(labels)}>",
    "idea": "<the one idea here that a competing answer should adopt, stated so it can be lifted verbatim>"
  }},
  "confidence": 0
}}"""


def prompt_review(problem: str, rubric: str, answers: dict[str, str]) -> str:
    labels = list(answers.keys())
    blocks = "\n\n".join(
        f"<answer id=\"{lab}\">\n{answers[lab].strip()}\n</answer>" for lab in labels
    )
    plural = "answers" if len(labels) > 1 else "answer"
    return f"""You are an anonymous reviewer on a blind review panel. Below is a problem, \
a scoring rubric, and {len(labels)} candidate {plural} written by other panelists.

The {plural} {'are' if len(labels) > 1 else 'is'} anonymised and randomly relabelled. You \
cannot tell who wrote what, and you must not speculate about authorship - guessing \
authorship is not part of your job and any such reasoning is discarded. Your own answer has \
been removed from this set, so every answer here was written by someone else.

Score honestly and discriminate between them. Identical scores across answers are useless \
to the panel. If one is clearly better, say so with the numbers.

<problem>
{problem}
</problem>

<rubric>
{rubric}
</rubric>

{blocks}

OUTPUT RULES
- Output ONLY a single fenced json code block. No text before or after it.
- All integer scores are 0-10. "ranking" lists every label best-first.
- "confidence" is 0-10: how sure you are of this ranking.
- Do not use tools or read files.

Emit exactly this shape, with your values substituted:

```json
{ballot_schema(labels)}
```
"""


def prompt_repair(previous: str) -> str:
    return f"""Your previous response was supposed to be a single fenced json block, but it \
could not be parsed as JSON.

Return the SAME judgement again as valid JSON only. Output nothing except one fenced json \
block. Do not add commentary. Do not change your scores - only fix the format.

Your previous response was:

{previous[:20000]}
"""


def prompt_revise(problem: str, template: str, own_answer: str,
                  critiques: list[str], ideas: list[str]) -> str:
    crit_block = (
        "\n\n".join(f"Reviewer {i + 1}:\n{c.strip()}" for i, c in enumerate(critiques))
        if critiques else
        "(No reviewer critique was recorded for your answer this round.)"
    )
    ideas_block = (
        "\n".join(f"- {i.strip()}" for i in ideas)
        if ideas else
        "(None recorded.)"
    )
    return f"""You are an anonymous panelist. This is the final round. Below is the original \
problem, the answer YOU submitted, the anonymous critiques reviewers wrote about YOUR answer, \
and a list of ideas reviewers judged worth adopting (harvested from across the panel, \
unattributed).

Produce a REVISED answer. Rules for revising:
- Fix what the critique gets right. Where a reviewer misread you, make the passage \
unmisreadable rather than arguing - no reply text, only a better answer.
- Where a critique is simply wrong, do not change the substance. Instead strengthen the \
justification at that point so the next reader is not misled.
- Adopt any harvested idea that genuinely improves your answer. Ignore the rest.
- Do NOT pad. Adding length to look thorough is penalised under simplicity. Cutting a
  section that was never needed is a legitimate revision.
- Output the full revised answer, standalone. Do not output a diff, a changelog, or notes
  about what you changed.

<problem>
{problem}
</problem>

<your_previous_answer>
{own_answer.strip()}
</your_previous_answer>

<critiques_of_your_answer>
{crit_block}
</critiques_of_your_answer>

<ideas_worth_adopting>
{ideas_block}
</ideas_worth_adopting>

{RULES_SOLVE}
<answer_template>
{template}
</answer_template>

Now output your revised answer.
"""


# ------------------------------------------------------------------------ ballot parsing

def extract_json(text: str):
    """Pull the last parseable JSON object out of a model response."""
    candidates = re.findall(r"```(?:json)?\s*\n(.*?)```", text or "", re.S)
    if not candidates:
        i, j = (text or "").find("{"), (text or "").rfind("}")
        if i != -1 and j > i:
            candidates = [text[i:j + 1]]
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except Exception:
            i, j = cand.find("{"), cand.rfind("}")
            if i != -1 and j > i:
                try:
                    return json.loads(cand[i:j + 1])
                except Exception:
                    continue
    return None


def clamp(value, lo=0.0, hi=10.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.0


def weighted_total(criteria: dict) -> float:
    return round(sum(clamp(criteria.get(k, 0)) * w for k, w in CRITERIA.items()), 2)


def normalize_ballot(raw, labels: list[str], key: dict[str, str]) -> dict | None:
    """Validate a ballot and translate view labels -> canonical slot ids."""
    if not isinstance(raw, dict) or not isinstance(raw.get("scores"), list):
        return None

    scores = {}
    for entry in raw["scores"]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).strip().upper()[:1]
        if label not in labels or label not in key:
            continue
        criteria = entry.get("criteria")
        if not isinstance(criteria, dict):
            continue
        crit = {k: clamp(criteria.get(k, 0)) for k in CRITERIA}
        blocking = entry.get("blocking_issues") or []
        if isinstance(blocking, str):
            blocking = [blocking]
        scores[key[label]] = {
            "criteria": crit,
            "total": weighted_total(crit),
            "blocking_issues": [str(b) for b in blocking if str(b).strip()],
            "strongest_element": str(entry.get("strongest_element", "")).strip(),
            "weakest_element": str(entry.get("weakest_element", "")).strip(),
            "specific_critique": str(entry.get("specific_critique", "")).strip(),
        }

    if not scores:
        return None

    ranking = []
    for label in (raw.get("ranking") or []):
        label = str(label).strip().upper()[:1]
        slot = key.get(label)
        if slot in scores and slot not in ranking:
            ranking.append(slot)
    # Fall back to score order for anything the rater left out of its ranking.
    for slot in sorted(scores, key=lambda s: -scores[s]["total"]):
        if slot not in ranking:
            ranking.append(slot)

    best = raw.get("best_idea_worth_stealing") or {}
    best_slot = key.get(str(best.get("label", "")).strip().upper()[:1]) if isinstance(best, dict) else None

    return {
        "scores": scores,
        "ranking": ranking,
        "best_idea": {"slot": best_slot, "idea": str(best.get("idea", "")).strip()}
        if isinstance(best, dict) else {"slot": None, "idea": ""},
        "confidence": clamp(raw.get("confidence", 0)),
    }


# ------------------------------------------------------------------------- aggregation

def aggregate(ballots: dict[str, dict], slots: list[str]) -> dict:
    """
    Combine ballots into a scoreboard.

    Two views, because they fail differently:
      * adj_mean  - each rater's ballot is re-centred on the global mean before averaging,
                    which cancels out a rater being systematically harsh or generous.
      * pairwise  - who beat whom. Each pair is judged exactly once, so a Condorcet winner
                    is a slot that beat every other slot. Immune to score scale entirely,
                    but can produce a rock-paper-scissors cycle with 3+ panelists.
    A result is only called clear when both agree.
    """
    all_totals = [s["total"] for b in ballots.values() for s in b["scores"].values()]
    global_mean = sum(all_totals) / len(all_totals) if all_totals else 0.0

    raw: dict[str, list[float]] = {s: [] for s in slots}
    adj: dict[str, list[float]] = {s: [] for s in slots}
    blocking: dict[str, list[dict]] = {s: [] for s in slots}
    wins: dict[str, int] = {s: 0 for s in slots}
    losses: dict[str, int] = {s: 0 for s in slots}
    pairs = []

    for rater, ballot in ballots.items():
        totals = [s["total"] for s in ballot["scores"].values()]
        centre = sum(totals) / len(totals) if totals else 0.0
        for slot, score in ballot["scores"].items():
            raw[slot].append(score["total"])
            adj[slot].append(score["total"] - centre + global_mean)
            for issue in score["blocking_issues"]:
                blocking[slot].append({"rater": rater, "issue": issue})
        ranking = ballot["ranking"]
        for i in range(len(ranking)):
            for j in range(i + 1, len(ranking)):
                wins[ranking[i]] += 1
                losses[ranking[j]] += 1
                pairs.append({"rater": rater, "winner": ranking[i], "loser": ranking[j]})

    def mean(values):
        return round(sum(values) / len(values), 1) if values else None

    table = {
        s: {
            "n": len(raw[s]),
            "raw_mean": mean(raw[s]),
            "adj_mean": mean(adj[s]),
            "wins": wins[s],
            "losses": losses[s],
            "blocking": blocking[s],
        }
        for s in slots
    }

    scored = [s for s in slots if table[s]["adj_mean"] is not None]
    order = sorted(scored, key=lambda s: (-table[s]["adj_mean"], -table[s]["wins"], s))
    condorcet = next((s for s in scored if table[s]["wins"] == len(scored) - 1), None) \
        if len(scored) > 1 else (scored[0] if scored else None)

    leader = order[0] if order else None
    margin = (round(table[order[0]]["adj_mean"] - table[order[1]]["adj_mean"], 1)
              if len(order) > 1 else None)

    return {
        "global_mean": round(global_mean, 1),
        "table": table,
        "order": order,
        "condorcet": condorcet,
        "leader": leader,
        "margin": margin,
        "pairs": pairs,
    }


def verdict(agg: dict, min_margin: float) -> dict:
    leader, condorcet, margin = agg["leader"], agg["condorcet"], agg["margin"]
    if leader is None:
        return {"clear": False, "winner": None,
                "reason": "No valid ballots were recorded."}
    if agg["table"][leader]["blocking"]:
        return {"clear": False, "winner": leader,
                "reason": f"Top-scoring answer {leader} was flagged with a blocking issue. "
                          "It cannot win as-is; the defect must be resolved."}
    if condorcet is None:
        return {"clear": False, "winner": leader,
                "reason": "No Condorcet winner - the pairwise preferences form a cycle. "
                          "The panel genuinely disagrees; synthesise rather than pick."}
    if condorcet != leader:
        return {"clear": False, "winner": leader,
                "reason": f"Score leader ({leader}) and pairwise winner ({condorcet}) "
                          "disagree. Treat as unresolved."}
    if margin is not None and margin < min_margin:
        return {"clear": False, "winner": leader,
                "reason": f"Margin of {margin} pts over second place is below the "
                          f"{min_margin} pt threshold - too close to call."}
    return {"clear": True, "winner": leader,
            "reason": f"{leader} leads on adjusted score by {margin} pts and won every "
                      "pairwise comparison, with no blocking issues."}


# ---------------------------------------------------------------------------- scoreboard

def render_scoreboard(pdir: Path, review_dir: Path, source_dir: Path,
                      agg: dict, verd: dict, mapping: dict, ballots: dict,
                      failures: dict, min_margin: float) -> str:
    inv = {slot: member for slot, member in mapping.items()}
    lines = [
        f"# Scoreboard - {review_dir.name}",
        "",
        f"Problem: `{pdir.name}`  ",
        f"Answers reviewed: `{source_dir.name}`  ",
        f"Generated: {now_iso()}",
        "",
        "**This file de-anonymises the panel. It is the moderator's record. Never paste it,",
        "or any part of it, into a prompt for a panelist while more rounds remain.**",
        "",
        "## Result",
        "",
        f"> {'CLEAR WINNER' if verd['clear'] else 'NOT RESOLVED'} - {verd['reason']}",
        "",
    ]
    if verd["winner"]:
        lines += [f"Leading answer: **{verd['winner']}** = **{inv.get(verd['winner'], '?')}**", ""]

    lines += [
        "## Scores",
        "",
        "| Slot | Author | Raters | Raw mean | Adjusted | W-L | Blocking |",
        "|---|---|---|---|---|---|---|",
    ]
    for slot in agg["order"]:
        row = agg["table"][slot]
        lines.append(
            f"| {slot} | {inv.get(slot, '?')} | {row['n']} | {row['raw_mean']} | "
            f"**{row['adj_mean']}** | {row['wins']}-{row['losses']} | "
            f"{len(row['blocking']) or '-'} |"
        )
    lines += [
        "",
        f"Scores are out of 100. Adjusted re-centres each rater's ballot on the panel mean "
        f"({agg['global_mean']}) to cancel harsh/generous raters. "
        f"Clear-winner threshold: {min_margin} pts.",
        "",
    ]

    if agg["pairs"]:
        lines += ["## Pairwise", ""]
        for p in agg["pairs"]:
            lines.append(
                f"- `{p['rater']}` preferred **{p['winner']}** ({inv.get(p['winner'], '?')}) "
                f"over {p['loser']} ({inv.get(p['loser'], '?')})"
            )
        lines.append("")

    blocking_any = [(s, agg["table"][s]["blocking"]) for s in agg["order"]
                    if agg["table"][s]["blocking"]]
    if blocking_any:
        lines += ["## Blocking issues", ""]
        for slot, issues in blocking_any:
            lines.append(f"### {slot} ({inv.get(slot, '?')})")
            for issue in issues:
                lines.append(f"- *{issue['rater']}*: {issue['issue']}")
            lines.append("")

    lines += ["## Critiques", ""]
    for slot in agg["order"]:
        lines.append(f"### {slot} = {inv.get(slot, '?')}")
        lines.append("")
        for rater, ballot in ballots.items():
            score = ballot["scores"].get(slot)
            if not score:
                continue
            crit = " / ".join(f"{k}:{int(v)}" for k, v in score["criteria"].items())
            lines += [
                f"**Reviewer `{rater}`** - total {score['total']} ({crit})",
                "",
                f"- Strongest: {score['strongest_element'] or '-'}",
                f"- Weakest: {score['weakest_element'] or '-'}",
                "",
                (score["specific_critique"] or "_(no critique text)_"),
                "",
            ]

    ideas = [(r, b["best_idea"]) for r, b in ballots.items() if b["best_idea"].get("idea")]
    if ideas:
        lines += ["## Ideas worth adopting", ""]
        for rater, idea in ideas:
            src = idea.get("slot")
            lines.append(
                f"- *{rater}* flagged in {src or '?'} ({inv.get(src, '?')}): {idea['idea']}"
            )
        lines.append("")

    if failures:
        lines += ["## Failures", ""]
        for mid, err in failures.items():
            lines.append(f"- `{mid}`: {err}")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------------- solve rounds

def collect_ideas(ballots: dict) -> list[str]:
    seen, ideas = set(), []
    for ballot in ballots.values():
        idea = ballot["best_idea"].get("idea", "").strip()
        norm = idea.lower()[:120]
        if idea and norm not in seen:
            seen.add(norm)
            ideas.append(idea)
    return ideas


def load_ballots(review_dir: Path) -> dict:
    bdir = review_dir / "ballots"
    if not bdir.is_dir():
        return {}
    return {p.stem: read_json(p) for p in sorted(bdir.glob("*.json"))}


def anonymize_round(pdir: Path, rdir: Path, cfg: dict, problem: str,
                    results: dict) -> dict:
    """Normalise successful answers, assign shuffled canonical slots, persist mapping."""
    ok = [mid for mid in sorted(results) if results[mid].get("ok")]
    if not ok:
        raise Fail("Every panelist failed. Nothing to anonymise. See raw/*.meta.json.")

    rng = seeded_rng(cfg["id"], rdir.name, "slots")
    shuffled = ok[:]
    rng.shuffle(shuffled)
    mapping = {CANON[i]: mid for i, mid in enumerate(shuffled)}

    # Drop slots from an earlier run; a stale answer file would be reviewed as if real.
    for old in (rdir / "anon").glob("*.md"):
        if old.stem not in mapping:
            old.unlink()

    for slot, mid in mapping.items():
        write(rdir / "anon" / f"{slot}.md",
              normalize_answer(results[mid]["answer"], cfg, problem))
    write_json(rdir / "keys" / "mapping.json", mapping)
    return mapping


def merge_prior_results(rdir: Path, results: dict) -> dict:
    """--only re-runs a subset. Fold in members that already succeeded in an earlier
    invocation, so anonymisation covers the whole panel instead of just this subset."""
    merged = dict(results)
    for meta_path in sorted((rdir / "raw").glob("*.meta.json")):
        mid = meta_path.name[: -len(".meta.json")]
        answer_path = rdir / "raw" / f"{mid}.md"
        if mid in merged or not answer_path.exists():
            continue
        if read_json(meta_path).get("ok"):
            merged[mid] = {"ok": True, "answer": read(answer_path)}
    return merged


def persist_results(rdir: Path, results: dict, prompts: dict) -> dict:
    failures = {}
    for mid, r in results.items():
        write(rdir / "prompts" / f"{mid}.md", prompts[mid])
        if r.get("answer"):
            write(rdir / "raw" / f"{mid}.md", r["answer"])
        if r.get("stderr"):
            write(rdir / "raw" / f"{mid}.stderr.log", r["stderr"])
        meta = {k: v for k, v in r.items() if k not in ("answer", "stdout", "stderr")}
        write_json(rdir / "raw" / f"{mid}.meta.json", meta)
        if not r.get("ok"):
            failures[mid] = r.get("error", "unknown error")
    return failures


def cmd_solve(args) -> int:
    rnd = args.round
    if rnd not in SOLVE_DIR:
        raise Fail(f"--round must be 1 (propose) or 3 (revise), got {rnd}")

    pdir = resolve_problem(args.problem)
    cfg = load_config(pdir)
    problem = problem_text(pdir)
    template = read(pdir / "ANSWER_TEMPLATE.md")
    members = load_panel(cfg, args.only)
    rdir = pdir / SOLVE_DIR[rnd]

    if rnd == 1:
        shared = prompt_propose(problem, template, len(members))
        prompts = {m["id"]: shared for m in members}
    else:
        prev_dir = pdir / SOLVE_DIR[1]
        review_dir = pdir / REVIEW_DIR[2]
        prev_map_path = prev_dir / "keys" / "mapping.json"
        if not prev_map_path.exists():
            raise Fail(f"Round 1 has not run - {prev_map_path} is missing.")
        ballots = load_ballots(review_dir)
        if not ballots:
            raise Fail(f"Round 2 review has not run - no ballots in {review_dir}.")

        prev_map = read_json(prev_map_path)
        slot_of = {mid: slot for slot, mid in prev_map.items()}
        ideas = collect_ideas(ballots)
        prompts = {}
        for m in members:
            mid = m["id"]
            slot = slot_of.get(mid)
            answer_path = prev_dir / "raw" / f"{mid}.md"
            if slot is None or not answer_path.exists():
                warn(f"{mid} has no round-1 answer; it will re-solve from scratch.")
                prompts[mid] = prompt_propose(problem, template, len(members))
                continue
            critiques = [
                b["scores"][slot]["specific_critique"]
                for b in ballots.values()
                if slot in b["scores"] and b["scores"][slot]["specific_critique"]
            ]
            prompts[mid] = prompt_revise(problem, template, read(answer_path),
                                         critiques, ideas)

    print(f"\n[{pdir.name}] round {rnd} - "
          f"{'propose' if rnd == 1 else 'revise'} ({len(members)} panelists)")
    results = run_panel([(m, prompts[m["id"]]) for m in members], args.keep_workdirs)

    failures = persist_results(rdir, results, prompts)
    if args.only:
        results = merge_prior_results(rdir, results)
    mapping = anonymize_round(pdir, rdir, cfg, problem, results)
    write_json(rdir / "meta.json", {
        "round": rnd, "kind": "solve", "completed": now_iso(),
        "succeeded": sorted(mapping.values()), "failures": failures,
    })

    print(f"\n  answers -> {rdir}/anon/  ({len(mapping)} anonymised)")
    if failures:
        warn(f"{len(failures)} panelist(s) failed: "
             + ", ".join(f"{k} ({v})" for k, v in failures.items()))
        if len(mapping) < 2:
            raise Fail("Fewer than 2 answers survived. Fix the failures and re-run "
                       "with --only before reviewing.")
    review_round = 2 if rnd == 1 else 3
    print(f"  next: consensus.py review {pdir.name} --round {review_round}")
    return 0


# ------------------------------------------------------------------------ review rounds

def cmd_review(args) -> int:
    rnd = args.round
    if rnd not in REVIEW_DIR:
        raise Fail(f"--round must be 2 or 3 for review, got {rnd}")

    pdir = resolve_problem(args.problem)
    cfg = load_config(pdir)
    problem = problem_text(pdir)
    rubric = read(pdir / "RUBRIC.md")
    source = pdir / REVIEW_SOURCE[rnd]
    rdir = pdir / REVIEW_DIR[rnd]

    map_path = source / "keys" / "mapping.json"
    if not map_path.exists():
        raise Fail(f"No answers to review - {map_path} is missing. "
                   f"Run: consensus.py solve {pdir.name} --round {1 if rnd == 2 else 3}")
    mapping = read_json(map_path)
    slot_of = {mid: slot for slot, mid in mapping.items()}
    answers = {slot: read(source / "anon" / f"{slot}.md") for slot in mapping}
    slots = sorted(answers)

    members = load_panel(cfg, args.only)
    if len(slots) < 2:
        raise Fail("Need at least 2 answers to run a blind review.")

    # Build one blind view per rater: own answer removed, remaining answers relabelled
    # under an independent permutation so labels cannot be correlated across ballots.
    jobs, keys = [], {}
    for m in members:
        mid = m["id"]
        visible = [s for s in slots if s != slot_of.get(mid)]
        if not visible:
            warn(f"{mid} has nothing to review; skipping.")
            continue
        rng = seeded_rng(cfg["id"], rdir.name, mid)
        shuffled = visible[:]
        rng.shuffle(shuffled)
        key = {VIEW[i]: slot for i, slot in enumerate(shuffled)}
        keys[mid] = key
        view_answers = {label: answers[slot] for label, slot in key.items()}
        jobs.append((m, prompt_review(problem, rubric, view_answers)))

    if not jobs:
        raise Fail("No eligible raters.")

    print(f"\n[{pdir.name}] round {rnd} - blind review ({len(jobs)} raters, "
          f"{len(slots)} answers, each rater sees {len(slots) - 1})")
    results = run_panel(jobs, args.keep_workdirs)

    failures, ballots = {}, {}
    for m, prompt in jobs:
        mid = m["id"]
        r = results[mid]
        write(rdir / "prompts" / f"{mid}.md", prompt)
        write_json(rdir / "keys" / f"{mid}.json", keys[mid])
        if r.get("answer"):
            write(rdir / "raw" / f"{mid}.md", r["answer"])
        write_json(rdir / "raw" / f"{mid}.meta.json",
                   {k: v for k, v in r.items() if k not in ("answer", "stdout", "stderr")})

        if not r.get("ok"):
            failures[mid] = r.get("error", "unknown error")
            continue

        labels = list(keys[mid])
        ballot = normalize_ballot(extract_json(r["answer"]), labels, keys[mid])
        if ballot is None:
            warn(f"{mid}: ballot did not parse - retrying once with a repair prompt")
            retry = run_member(m, prompt_repair(r["answer"]), args.keep_workdirs)
            write(rdir / "raw" / f"{mid}.retry.md", retry.get("answer", ""))
            ballot = normalize_ballot(extract_json(retry.get("answer", "")), labels, keys[mid])
        if ballot is None:
            failures[mid] = "ballot could not be parsed as JSON (after retry)"
            warn(f"{mid}: ballot still unparseable - EXCLUDED from the tally")
            continue

        ballot["rater"] = mid
        ballots[mid] = ballot
        write_json(rdir / "ballots" / f"{mid}.json", ballot)

    if not ballots:
        raise Fail("No usable ballots. Inspect " + str(rdir / "raw"))
    if failures:
        warn(f"{len(failures)} rater(s) excluded: "
             + ", ".join(f"{k} ({v})" for k, v in failures.items()))

    agg = aggregate(ballots, slots)
    verd = verdict(agg, args.margin)
    write(rdir / "SCOREBOARD.md",
          render_scoreboard(pdir, rdir, source, agg, verd, mapping, ballots,
                            failures, args.margin))
    write_json(rdir / "meta.json", {
        "round": rnd, "kind": "review", "completed": now_iso(),
        "raters": sorted(ballots), "failures": failures,
        "verdict": verd, "order": agg["order"],
        "table": {s: {k: v for k, v in row.items() if k != "blocking"}
                  for s, row in agg["table"].items()},
    })

    print(f"\n  {'CLEAR WINNER' if verd['clear'] else 'NOT RESOLVED'}: {verd['reason']}")
    for slot in agg["order"]:
        row = agg["table"][slot]
        flag = "  [BLOCKING]" if row["blocking"] else ""
        print(f"     {slot} = {mapping.get(slot, '?'):<8} adj {row['adj_mean']:>5}  "
              f"raw {row['raw_mean']:>5}  W-L {row['wins']}-{row['losses']}{flag}")
    print(f"\n  scoreboard -> {rdir}/SCOREBOARD.md")
    if rnd == 2:
        print("  next: read the scoreboard, then either")
        print(f"        consensus.py solve {pdir.name} --round 3   (revise + final vote)")
        print("        or synthesise FINAL.md now if the result is already clear")
    else:
        print("  next: moderator writes FINAL.md")
    return 0


# ------------------------------------------------------------------------ other commands

def cmd_new(args) -> int:
    root = Path(args.root).expanduser() if args.root else default_root()
    root.mkdir(parents=True, exist_ok=True)
    existing = [d.name for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    numbers = [int(m.group(1)) for n in existing
               if (m := re.match(r"^(\d{3})-", n))]
    number = max(numbers, default=0) + 1
    slug = slugify(args.slug)
    pdir = root / f"{number:03d}-{slug}"
    if pdir.exists():
        raise Fail(f"{pdir} already exists.")

    title = args.title or args.slug
    write(pdir / "problem.md", read(TEMPLATES / "problem.md").replace("<TITLE>", title))
    shutil.copy(TEMPLATES / "ANSWER_TEMPLATE.md", pdir / "ANSWER_TEMPLATE.md")
    shutil.copy(TEMPLATES / "RUBRIC.md", pdir / "RUBRIC.md")
    write_json(pdir / "config.json", {
        "id": pdir.name,
        "title": title,
        "created": now_iso(),
        "strip_emoji": True,
        "redact_terms": DEFAULT_REDACT,
    })
    print(pdir)
    return 0


def cmd_status(args) -> int:
    pdir = resolve_problem(args.problem)
    cfg = load_config(pdir)
    print(f"{pdir}\n  title: {cfg.get('title', '-')}")
    for name in ["round-1-propose", "round-2-review", "round-3-revise", "round-3-review"]:
        meta_path = pdir / name / "meta.json"
        if not meta_path.exists():
            print(f"  {name:<18} not run")
            continue
        meta = read_json(meta_path)
        if meta["kind"] == "solve":
            detail = f"answers from {', '.join(meta['succeeded'])}"
        else:
            v = meta["verdict"]
            detail = f"{'CLEAR' if v['clear'] else 'unresolved'} -> {v.get('winner') or '-'}"
        fails = f"  [{len(meta.get('failures', {}))} failed]" if meta.get("failures") else ""
        print(f"  {name:<18} {meta['completed']}  {detail}{fails}")
    final = pdir / "FINAL.md"
    print(f"  FINAL.md           {'written' if final.exists() else 'not written'}")
    return 0


def effective_settings(member: dict) -> dict:
    """What this member will actually run with, including values it inherits."""
    tuning = member.get("tuning", {})
    model, thinking = member.get("model"), member.get("thinking")
    info = {"model": model or "(tool default)", "thinking": thinking or "(tool default)",
            "notes": []}

    if member["id"] == "codex":
        info["notes"].append(
            "--ignore-user-config means ~/.codex/config.toml is NOT read, so model and "
            "reasoning effort must be set here - which is why they are pinned rather "
            "than left to inherit.")

    if tuning.get("thinking_values"):
        info["notes"].append("thinking values: " + " | ".join(tuning["thinking_values"]))
    if tuning.get("model_hint"):
        info["notes"].append("model: " + tuning["model_hint"])
    if tuning.get("isolation"):
        info["notes"].append("isolation: " + tuning["isolation"])
    return info


def apply_sets(members: list[dict], assignments: list[str]) -> list[str]:
    index = {m["id"]: m for m in members}
    changed = []
    for item in assignments:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise Fail(f"--set expects <member>.<field>=<value>, got '{item}'")
        target, value = item.split("=", 1)
        mid, field = target.split(".", 1)
        member = index.get(mid)
        if member is None:
            raise Fail(f"Unknown member '{mid}'. Known: {', '.join(index)}")
        if field not in ("model", "thinking", "timeout"):
            raise Fail(f"Cannot set '{field}'. Settable fields: model, thinking, timeout")

        tuning = member.get("tuning", {})
        if field == "thinking" and not tuning.get("thinking_flag"):
            raise Fail(
                f"{mid} exposes no per-invocation thinking flag, so consensus cannot set it.\n"
                f"Its effort is read from {tuning.get('thinking_source', 'its own config')} "
                f"and applies to every {mid} run on this machine.\n"
                f"Edit that file directly if you want to change it.")

        if field == "timeout":
            try:
                member[field] = int(value)
            except ValueError:
                raise Fail(f"timeout must be a whole number of seconds, got '{value}'") from None
        elif value.strip().lower() in ("none", "null", "default", ""):
            member[field] = None
        else:
            allowed = tuning.get("thinking_values") if field == "thinking" else None
            if allowed and value not in allowed:
                warn(f"'{value}' is not a documented {mid} thinking value "
                     f"({'|'.join(allowed)}). Setting it anyway - run doctor to verify.")
            member[field] = value
        changed.append(f"{mid}.{field} = {member[field]}")
    return changed


def cmd_models(args) -> int:
    pdir = resolve_problem(args.problem) if args.problem else None
    cfg = load_config(pdir) if pdir else {}
    members = load_panel(cfg)
    overridden = bool(cfg.get("panel"))

    if args.set:
        changed = apply_sets(members, args.set)
        if pdir:
            cfg["panel"] = members
            cfg.pop("_comment", None)
            write_json(pdir / "config.json", cfg)
            where = f"{pdir.name}/config.json (this problem only)"
        else:
            panel = read_json(SKILL_DIR / "panel.json")
            panel["members"] = members
            write_json(SKILL_DIR / "panel.json", panel)
            where = "panel.json (all future problems)"
        print("updated " + where)
        for line in changed:
            print(f"  {line}")
        print()
        overridden = overridden or bool(pdir)

    scope = f"problem {pdir.name}" if pdir else "global default"
    origin = (f"{pdir.name}/config.json" if overridden and pdir
              else "~/.claude/skills/consensus/panel.json")
    print(f"Consensus panel - what will actually run")
    print(f"scope: {scope}    source: {origin}")
    if pdir and not overridden:
        print("       (this problem has no override, so it uses the global panel)")
    print()

    rows = [(m["id"], effective_settings(m), m) for m in members]
    w_id = max(6, max(len(r[0]) for r in rows))
    w_model = max(5, max(len(r[1]["model"]) for r in rows))
    w_think = max(8, max(len(r[1]["thinking"]) for r in rows))
    print(f"  {'member'.ljust(w_id)}  {'model'.ljust(w_model)}  "
          f"{'thinking'.ljust(w_think)}  timeout")
    print(f"  {'-' * w_id}  {'-' * w_model}  {'-' * w_think}  -------")
    for mid, info, member in rows:
        print(f"  {mid.ljust(w_id)}  {info['model'].ljust(w_model)}  "
              f"{info['thinking'].ljust(w_think)}  {member.get('timeout', 900)}s")

    print()
    for mid, info, member in rows:
        print(f"  {mid}")
        for note in info["notes"]:
            print(f"      - {note}")
        if args.show_argv:
            subs = member_subs(member, prompt="<PROMPT>", cwd="<TMPDIR>",
                               outfile="<TMPDIR>/answer.md")
            print("      $ " + " ".join(
                f"'{a}'" if " " in a or a == "" else a
                for a in render_argv(member["argv"], subs)))
        print()

    print("to change:")
    print("  consensus.py models --set claude.thinking=max --set codex.model=gpt-5.6-sol")
    print("  consensus.py models --problem <id> --set claude.model=sonnet   "
          "# that problem only")
    print("  fields: model, thinking, timeout    (value 'none' = fall back to tool default)")
    print("to verify a change actually works: consensus.py doctor")
    return 0


def cmd_doctor(args) -> int:
    members = load_panel({}, args.only)
    print("Checking panel members with a trivial prompt (a few seconds each, minimal cost).\n")
    probe = "Reply with exactly one word and nothing else: PONG"
    results = run_panel([(dict(m, timeout=args.timeout), probe) for m in members])
    bad = 0
    for mid, r in results.items():
        answer = (r.get("answer") or "").strip()
        if r.get("ok") and "PONG" in answer.upper():
            print(f"  {mid:<8} OK")
        else:
            bad += 1
            print(f"  {mid:<8} PROBLEM: {r.get('error') or 'unexpected output'}")
            snippet = (r.get("stderr") or answer or "")[:400].strip()
            if snippet:
                print(f"           {snippet}")
    print("\nAll panel members reachable." if not bad
          else f"\n{bad} member(s) need attention before running a round.")
    return 1 if bad else 0


# ------------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="consensus.py",
        description="Blind multi-model consensus panel (Claude / Codex).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="scaffold a new problem folder")
    p.add_argument("slug")
    p.add_argument("--title")
    p.add_argument("--root")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("solve", help="round 1 (propose) or round 3 (revise)")
    p.add_argument("problem")
    p.add_argument("--round", type=int, required=True, choices=[1, 3])
    p.add_argument("--only", nargs="+", help="run only these members (failure recovery)")
    p.add_argument("--keep-workdirs", action="store_true")
    p.set_defaults(func=cmd_solve)

    p = sub.add_parser("review", help="blind cross-review of the previous solve round")
    p.add_argument("problem")
    p.add_argument("--round", type=int, required=True, choices=[2, 3])
    p.add_argument("--only", nargs="+")
    p.add_argument("--margin", type=float, default=5.0,
                   help="points (of 100) the leader must clear to count as decisive")
    p.add_argument("--keep-workdirs", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("status", help="show which rounds have run")
    p.add_argument("problem")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", aliases=["model"],
                       help="show (or change) which models and thinking effort will be used")
    p.add_argument("--problem", help="scope to one problem instead of the global default")
    p.add_argument("--set", nargs="+", metavar="ID.FIELD=VALUE",
                   help="e.g. claude.thinking=max codex.model=gpt-5.6-sol")
    p.add_argument("--show-argv", action="store_true",
                   help="also print the exact command line each member will run")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("doctor", help="check each CLI responds")
    p.add_argument("--only", nargs="+")
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_doctor)

    # Keep stdout progress and stderr warnings in the order they were emitted.
    sys.stdout.reconfigure(line_buffering=True)

    args = ap.parse_args()
    try:
        return args.func(args)
    except Fail as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
