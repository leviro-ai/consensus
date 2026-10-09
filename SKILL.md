---
name: consensus
description: "Run a blind multi-model consensus panel over a hard problem. Claude, Codex and Gemini each solve it independently, blind-review each other's answers against a rubric, then revise; you moderate and synthesise the final answer. Trigger with /consensus, or when the user wants several AI tools to independently solve, cross-check, debate, or vote on a decision, or asks for a second opinion on a design, architecture, or tradeoff."
---

# /consensus

CLI agents (currently Claude, Codex and Gemini, see `panel.json`) solve one problem independently,
then peer-review each other blind, then revise. You are the **moderator**: you write the
problem statement, run the rounds, and synthesise the final answer. You never submit a
solution yourself.

A separate `claude -p` runs as an anonymous panelist. That is not you — it starts cold, in
an empty directory, with no memory of this conversation. Keep it that way.

## Usage

```
/consensus <problem description>          # full run, moderated
/consensus model                          # show models + thinking, offer to change them
/consensus status <problem>               # where a problem stands
/consensus continue <problem>             # resume an unfinished problem
```

```bash
python3 ~/.claude/skills/consensus/scripts/consensus.py --help
```

Problems live in `$CONSENSUS_ROOT` (default `./problems`), one folder each:
`problems/001-my-slug/`.

## Rounds

| Round | Command | What happens |
|---|---|---|
| 1 | `solve --round 1` | Everyone answers independently, filling one answer template |
| 2 | `review --round 2` | Each blind-scores the other's answer(s) against the rubric |
| 3 | `solve --round 3` | Each revises using the anonymous critique of its own answer |
| 3 | `review --round 3` | Blind final vote on the revised answers |
| — | you | Write `FINAL.md` |

Three rounds is the ceiling. Stop after round 2 when the result is already clear — see
*Deciding to stop* below.

## Your job, in order

**1. Understand the problem before you write it down.** If the user's description is
ambiguous, under-specified, or you can see two readings that would produce different
answers, ask now. A vague problem produces three vague answers and a meaningless vote,
and you will have spent six model invocations to learn nothing.

**2. Scaffold.**

```bash
python3 ~/.claude/skills/consensus/scripts/consensus.py new <slug> --title "<title>"
```

**3. Write `problem.md`. This is the highest-leverage thing you do.** Panelists see this
file and nothing else — not this conversation, not the repo, not the user's earlier
messages. Follow the template's sections and delete its HTML comments. Be concrete: paste
the relevant code, name the real constraints, state the non-goals. The script refuses to
run while the template placeholders are still there.

Fill in `## What matters most` deliberately — reviewers read it before scoring, so it is
how you steer the panel's attention.

**4. Show the user `problem.md` and get confirmation before round 1.** Each command below
invokes every panelist once, with real cost and latency. A full three-round run is all
four commands (propose, review, revise, review again). Do not start one on a problem
statement the user has not seen.

**5. Run the rounds.**

```bash
python3 ~/.claude/skills/consensus/scripts/consensus.py solve  <problem> --round 1
python3 ~/.claude/skills/consensus/scripts/consensus.py review <problem> --round 2
python3 ~/.claude/skills/consensus/scripts/consensus.py solve  <problem> --round 3
python3 ~/.claude/skills/consensus/scripts/consensus.py review <problem> --round 3
```

Read `SCOREBOARD.md` after each review round and report it to the user in your own words:
who won, by how much, what the disagreement was about, and what each critique actually
said. The numbers are the least interesting part — the critiques are the product.

**6. Write `FINAL.md`** (structure below).

## Deciding to stop after round 2

Stop and synthesise when **all** of these hold:

- `SCOREBOARD.md` says `CLEAR WINNER`
- no answer carries a blocking issue
- the critiques are polish, not substance — nobody found a real defect

Go to round 3 when any of these hold:

- the verdict is `NOT RESOLVED` — especially a Condorcet cycle, which means the panel
  genuinely disagrees and a revision pass may converge it
- a blocking issue was raised against any answer
- the top two are within a few points
- the critiques surface something all three answers missed

Say which case you are in and why, then ask the user before spending round 3.

## Blindness rules — do not break these

The script enforces blindness structurally: self-contained prompts, empty per-call temp
directories outside the problem tree, each rater's own answer removed, an independent label
permutation per rater, and vendor names redacted from answers. You can break all of it by
hand, so:

- **Never** paste `SCOREBOARD.md`, `keys/*.json`, `raw/*.md`, or any author identity into a
  prompt for a panelist. Those files de-anonymise the panel.
- **Never** invoke `claude`/`codex` yourself to nudge a panelist mid-run. Use the
  script, which logs every prompt it sends under `prompts/`.
- **Never** tell a panelist how it scored, or that it is losing.
- Telling *the user* everything is fine and expected. The blindness is between panelists.

## Writing FINAL.md

Synthesis, not transcription. The winning answer is a starting point, not the deliverable —
the panel's value is that three independent attempts expose what one would have missed.

```markdown
# Final answer: <title>

## Recommendation
<the actual answer, written as one coherent piece the user can act on>

## Why this and not the alternatives
<the real disagreement, and what settled it>

## Adopted from other panelists
<ideas that did not come from the winning answer but belong in the result>

## Where the panel disagreed and it is still open
<honest: unresolved splits, cycles, blocking issues nobody fixed>

## Confidence
<what would change this answer, and what the panel collectively missed or assumed>
```

If the panel never converged, say so plainly. An honest "these three approaches are
genuinely in tension, here is the tradeoff you have to decide" is a real result. Do not
manufacture agreement that the scoreboard does not support.

## When something breaks

```bash
python3 ~/.claude/skills/consensus/scripts/consensus.py doctor          # cheap reachability check
python3 ~/.claude/skills/consensus/scripts/consensus.py solve <p> --round 1 --only codex
```

- A failed panelist is excluded with a warning and the round continues on the survivors;
  fewer than two and the script stops rather than pretending. Re-run the failure with
  `--only <id>`, then re-run the review. With three members one failure leaves a pair and
  the round continues; two failures abort it.
- An unparseable ballot is retried once, then excluded from the tally.
- Diagnostics per call: `<round>/raw/<id>.meta.json`, `.stderr.log`, and the exact prompt in
  `<round>/prompts/<id>.md`. Add `--keep-workdirs` to preserve temp dirs.
- **Report failures to the user.** A two-panelist round is a weaker result and they should
  know it happened.

## Problem folder layout

```
problems/001-slug/
  problem.md              you write this; the only context panelists get
  ANSWER_TEMPLATE.md      structure every answer must follow
  RUBRIC.md               scoring criteria and weights
  config.json             redaction terms, emoji stripping, optional panel override
  round-1-propose/
    prompts/  raw/        exact prompt and unmodified output per panelist
    anon/                 normalised answers as slots A/B/C
    keys/mapping.json     slot -> author            [MODERATOR ONLY]
  round-2-review/
    prompts/  raw/
    ballots/<rater>.json  parsed scores, canonical slots
    keys/<rater>.json     that rater's label permutation   [MODERATOR ONLY]
    SCOREBOARD.md         de-anonymised results            [MODERATOR ONLY]
  round-3-revise/ ...     same shape
  round-3-review/ ...     same shape
  FINAL.md                you write this
  <anything downstream>   plans, cockpits, prompts, scripts derived from FINAL.md
```

**Everything you create for a problem goes inside that problem's folder — never in
`$CONSENSUS_ROOT` and never in the repo root.** This applies to `FINAL.md` and to every
downstream artefact the user asks for afterwards: day and week plans, HTML tools or
dashboards, regeneration prompts, checklists, exports. A problem folder is the complete
record of one question, so a year from now the answer and everything built from it are in
one place.

If the user asks for a deliverable and you are unsure which problem it belongs to, ask —
do not default to the root. When a problem's answer supersedes an earlier one (as `003`
supersedes `002`), leave the old folder untouched and say in the new `FINAL.md` what it
replaces; never edit a closed problem to match a newer decision.

## /consensus model

Reports which model and thinking effort each panelist will *actually* use, then offers
changes.

```bash
python3 ~/.claude/skills/consensus/scripts/consensus.py models --show-argv
python3 ~/.claude/skills/consensus/scripts/consensus.py models --problem <id>
```

Run it, relay the table to the user in your own words, then **ask whether they want
changes** with `AskUserQuestion` — one question per panelist worth changing, offering that
tool's real values as options. Apply with `--set`, re-run `models` to confirm the new
state, and run `doctor` afterwards if you changed a model id or set an undocumented effort
value, since a bad one only surfaces when a round fails.

Always establish scope before writing. Without `--problem` the change is **global** and
affects every future problem; with `--problem <id>` it is written to that problem's
`config.json` and affects only it. Ask which they mean whenever it is not obvious.

| Panelist | Model | Thinking |
|---|---|---|
| Claude | `--model` — opus / sonnet / haiku / fable | `--effort` — low / medium / high / xhigh / max |
| Codex | `-m` | `-c model_reasoning_effort` — minimal / low / medium / high |
| Gemini | `-m` (null = CLI default) | none — the CLI has no effort flag |

One thing to be straight with the user about:

- **Codex runs with `--ignore-user-config`,** so it ignores `~/.codex/config.toml`
  entirely. Its model and effort are therefore pinned in `panel.json`. If you unset them,
  Codex silently falls back to a built-in default you cannot inspect from here. Keep them
  pinned.
- **Gemini is not config-isolated.** Its CLI has no flag to skip `~/.gemini/settings.json`
  or `~/.gemini/GEMINI.md`, so anything added there (MCP servers, a global GEMINI.md)
  reaches the panelist. Neither exists today. It has no effort flag either.

Raising effort costs more and takes longer on every round. A full three-round run invokes
every panelist four times, so `max` everywhere is a real bill — say so before applying it.

## Tuning

- `--margin N` (default 5.0, out of 100): how far ahead the leader must be to count as
  decisive. Raise it to demand stronger agreement.
- `config.json` → `redact_terms`, `strip_emoji`. Terms appearing in `problem.md` are never
  redacted, so a problem *about* these tools still reads correctly.
- `config.json` → `panel` overrides `~/.claude/skills/consensus/panel.json` for one problem.
  Prefer `models --problem <id> --set ...` over hand-editing it.
- Panel defaults isolate each tool from its local config (`claude --safe-mode --tools ""`,
  `codex --ignore-user-config --ignore-rules`) so panelists start symmetric. Edit
  `panel.json` if you want them to see their normal setup.
- In `argv`, `{model}` and `{thinking}` are optional: when the field is null, that element
  *and the flag before it* are dropped, so the tool uses its own default. A literal `""`
  (claude's `--tools`) is not a placeholder and is always kept.

## Design notes

- **Two aggregations, reported together.** *Adjusted mean* re-centres each rater's ballot on
  the panel mean, cancelling a systematically harsh or generous rater. *Pairwise* records
  who beat whom, which ignores score scale entirely but can cycle. A result is only called
  clear when both agree and no blocking issue was raised.
- **Round 3 shows critiques, not rival answers.** Each panelist sees the critique of its own
  answer plus a list of unattributed "ideas worth adopting" harvested from all ballots. It
  never sees a competitor's full answer, so revisions stay independent instead of collapsing
  onto whichever answer was read first.
- **Each pair is judged exactly once**, so a Condorcet winner is an answer that beat every
  other answer. With more than two panelists a cycle is real information: the panel
  disagrees. With exactly two, there's no cycle to have — the pairwise result and the
  adjusted-mean result either agree or don't.
