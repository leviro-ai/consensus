# Scoring rubric

Score each criterion from 0 to 10. Use the whole range: 5 is a competent average answer,
8+ means genuinely hard to improve, 10 should be rare. Do not give every answer the same
score — the panel needs you to discriminate between them.

| Criterion | Weight | 0-3 | 4-6 | 7-8 | 9-10 |
|---|---|---|---|---|---|
| **correctness** | 3.0 | Factually wrong, or the approach cannot work | Broadly plausible but has real errors or hand-waving at the hard part | Sound; minor gaps that do not break it | Sound, and it correctly handles the cases that make this problem hard |
| **completeness** | 2.0 | Ignores most of what was asked | Covers the obvious half, misses stated constraints | Covers everything asked | Covers everything asked plus a consequence the problem statement missed |
| **simplicity** | 2.0 | Heavily over-engineered; speculative abstraction | More machinery than the problem needs | Proportionate to the problem | Minimal — nothing could be removed without losing the solution |
| **risk_awareness** | 1.5 | No risks named, or only fake ones | Generic risks, no mitigations | Real risks with usable mitigations | Names the specific way *this* proposal fails, and how to detect it early |
| **actionability** | 1.5 | Could not be executed without inventing the plan yourself | Executable after significant guesswork | Executable as written | Executable as written, with verification criteria that would actually catch a bad result |

Weighted total is out of 100.

## Scoring discipline

- **Judge the content, not the packaging.** Length, formatting polish, and confident tone
  are not evidence. A short answer that solves the problem beats a long one that circles it.
- **Penalise unrequested scope.** Features, abstractions, and configurability nobody asked
  for are a defect under `simplicity`, not a bonus under `completeness`.
- **Penalise confident vagueness.** "Add proper error handling" is not a plan.
- **A load-bearing assumption that is clearly false is a `blocking_issue`,** not a small
  deduction.
- **Reward disagreement with the premise** when it is well argued. An answer that shows the
  problem as stated is the wrong problem, and then solves the right one, can score highly.

## blocking_issues

Reserve these for defects that make the answer unusable as-is: it does not work, it violates
a stated constraint, or it rests on a false load-bearing assumption. Style complaints,
missing polish, and "I would have done it differently" are **not** blocking. Most answers
should have zero. An answer flagged as blocking cannot win the round, so use it honestly.
