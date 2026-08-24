# Demo and Eval Question Set

Fixed question set for the Halcyon Robotics corpus. Use it two ways. During
development it's a regression check, so you can tell whether a change to
retrieval or prompting made things better or worse instead of guessing. During
the live session it's your demo script, so you're never improvising a query in
front of the panel.

Every answer below is invented. None of it is guessable from general knowledge,
which is the point. If the bot answers correctly, retrieval worked.

---

## 1. Single document, single fact

Baseline. Should be fast, confident, one citation.

| Question | Expected source | Expected answer contains |
|---|---|---|
| How many PTO days do I get? | HR-004 | 18 days, 23 at Level 6+ |
| What's the deploy freeze period? | ENG-018 | Dec 15 to Jan 2 |
| How long is primary caregiver parental leave? | HR-013 | 16 weeks |
| What VPN does Halcyon use? | IT-014 | Tailscale, not AnyConnect |
| How much is the home office stipend? | HR-009 | $1,200 every 24 months |

## 2. Multiple documents

Tests whether retrieval pulls more than the single best hit, and whether
citations correctly list both.

| Question | Expected sources | Note |
|---|---|---|
| I'm a new hire traveling to a customer next week. What do I need to do? | FIN-015, FIN-011, HR-021 | Book in Navan, 14 days ahead, $75/day meals |
| We want to buy a $60k analytics tool that stores customer data. What approvals? | FIN-019, SEC-003 | VP approval plus vendor security review, 10 business days |
| A tier-1 customer has been down 9 hours. What happens? | CS-008, SEC-012, ENG-032 | Level 3 escalation, exec sponsor, CSM owns comms |
| Who approves a two-week vacation in November if I'm on Field Ops? | HR-004 | VP approval, plus blackout Oct 15 to Dec 15 |

## 3. The superseded document trap

`expense-policy-2023-ARCHIVED.md` (FIN-007) deliberately contradicts the active
FIN-011. This is the most valuable question in the set.

| Question | Correct behavior | Failure mode |
|---|---|---|
| What's the meal per diem for domestic travel? | $75/day, citing FIN-011 only | Cites both, or answers $50 from the archived doc |
| What corporate card do we use? | Ramp, citing FIN-011 | Says Brex, or hedges between both |

If your bot returns "$50 or $75 depending on the document," that's a real
finding, not a bug to hide. Show it, then show the fix. Filtering on
`status: active` in the retrieval stage is a two-line change and makes a great
live-coding demo. It also sets up the production conversation about why
archived-but-searchable content is a genuine problem in enterprise search.

## 4. Ambiguous or underspecified

Tests whether the bot asks rather than guesses.

| Question | Good behavior |
|---|---|
| How much can I spend on a hotel? | Asks which city, or returns the full table |
| What's the SLA? | Ambiguous across SEC-012, ENG-032, CS-008. Should disambiguate |
| Do I need approval? | Too vague. Should ask what for |

## 5. Out of scope, zero results

The most important path to demo, because it proves the answer is grounded and
not generated. None of these are in the corpus.

| Question | Correct behavior |
|---|---|
| What's our 401k employer match? | "No indexed content found on that." Names the datasource searched |
| Who is the CEO of Halcyon Robotics? | Same. Do not invent a name |
| What's the dental insurance deductible? | Same |

Watch for the failure where Search returns weak matches, they get passed to
Chat anyway, and Chat answers plausibly from world knowledge. That's the
hallucination path. A relevance floor on search scores is the fix, and it's
another good live change.

## 6. World knowledge traps

Questions where a model has a strong prior that's wrong for Halcyon. If it
answers from priors instead of the docs, you'll see it immediately.

| Question | Wrong prior | Halcyon answer |
|---|---|---|
| How long do I have to submit an expense? | 60 or 90 days | 30 days |
| When do merit increases happen? | Annually, January | March cycle only, effective April 1 |
| How many days a week am I in the office? | Varies | Tue/Wed/Thu, "Anchor Days" |
| What's the on-call rotation length? | Varies | One week, Monday 10am MT handoff |

---

## Suggested live demo order

1. `How many PTO days do I get?` gets a clean single-source answer
2. `We want to buy a $60k analytics tool that stores customer data. What approvals?` shows multi-doc synthesis
3. `What's our 401k employer match?` shows the grounded refusal
4. `What's the meal per diem for domestic travel?` surfaces the archived-doc problem
5. Fix it live by filtering on `status: active`, re-run, show the corrected answer

That last pair is the whole interview in two minutes. You found a real
retrieval-quality problem, you can explain why it happens, and you can fix it in
front of them.
