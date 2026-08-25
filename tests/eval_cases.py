"""The Halcyon Robotics eval set: the read path's regression suite.

Every expected answer here is invented. None of it is guessable from general
knowledge, which is the point -- if the bot answers correctly, retrieval worked
and the answer came from the corpus rather than from the model's priors.

Doc ids are the short form (`FIN-011`). Glean returns them fully qualified
(`CUSTOM_INTERVIEWDS3_Document_halcyon-FIN-011`), so assertions match on the
suffix via `helpers.short_ids`.

Cases are grouped into tuples by what they test, and CASES concatenates them:

  SINGLE_FACT   Baseline. One document, one fact, one citation.
  MULTI_DOC     Whether retrieval pulls more than the single best hit and the
                citations list every document the answer drew on.
  SUPERSEDED    The archived document that contradicts the active one. The most
                valuable group in the set: search.py filters retrieval to
                `status: Active`, so these assert FIN-007 never comes back at
                all. If it reappears, the facet filter has stopped matching --
                silently, because a facet name Glean does not recognise returns
                zero results rather than an error -- and the retired figures are
                reaching Chat again.
  AMBIGUOUS     Whether the bot disambiguates instead of guessing. Asserted
                weakly on purpose: only that the floor was cleared and several
                documents came back, never on phrasing.
  OUT_OF_SCOPE  The grounded refusal. Nothing in the corpus covers these, so
                the floor must reject them and Chat must never run. This is the
                path that proves answers are grounded rather than generated.
  PRIOR_TRAPS   Questions where a model has a strong, wrong prior for Halcyon.
                Answering from priors instead of the documents shows up here
                immediately.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    # False means the refusal path: the floor must reject and Chat must not run.
    grounded: bool = True
    # Short doc ids that must appear among the cited sources.
    expect_cited: tuple[str, ...] = ()
    # Short doc ids that must come back from search, cited or not.
    expect_retrieved: tuple[str, ...] = ()
    # Short doc ids search must not return at all, e.g. the archived FIN-007.
    # Asserted at retrieval rather than at citation because cited ids are a
    # subset of retrieved ones: keeping the document out of Chat's context is
    # the guarantee, not trusting Chat to ignore it.
    forbid_retrieved: tuple[str, ...] = ()
    # Case-insensitive substrings the answer must contain.
    must_contain: tuple[str, ...] = ()
    # The wrong prior, or the superseded figure. Must be absent.
    must_not_contain: tuple[str, ...] = ()
    top_k: int | None = None
    note: str = ""
    # A gap the system has today, with the reason. A case carrying one is
    # xfailed entirely, so the suite stays green while the gap stays visible
    # -- and reports XPASS the moment it is fixed.
    known_gap: str = ""


SINGLE_FACT = (
    EvalCase(
        id="pto-days",
        question="How many PTO days do I get?",
        expect_cited=("HR-004",),
        must_contain=("18",),
        note="Baseline. Should be fast, confident, one citation.",
    ),
    EvalCase(
        id="deploy-freeze",
        question="What's the deploy freeze period?",
        expect_cited=("ENG-018",),
        must_contain=("december", "january"),
    ),
    EvalCase(
        id="parental-leave",
        question="How long is primary caregiver parental leave?",
        expect_cited=("HR-013",),
        must_contain=("16 weeks",),
    ),
    EvalCase(
        id="vpn",
        question="What VPN does Halcyon use?",
        expect_cited=("IT-014",),
        must_contain=("tailscale",),
        note=(
            "AnyConnect is the plausible wrong answer, but naming it as "
            "decommissioned is correct, so it is not forbidden text."
        ),
    ),
    EvalCase(
        id="home-office-stipend",
        question="How much is the home office stipend?",
        expect_cited=("HR-009",),
        must_contain=("1,200", "24 months"),
    ),
)

MULTI_DOC = (
    EvalCase(
        id="new-hire-travel",
        question="I'm a new hire traveling to a customer next week. What do I need to do?",
        expect_retrieved=("FIN-015", "FIN-011"),
        must_contain=("navan",),
        top_k=10,
        note="Book in Navan, 14 days ahead, $75/day meals. Spans FIN-015, FIN-011, HR-021.",
        known_gap=(
            "Search returns only HR-021 for this phrasing even at top_k=10, so the "
            "travel and expense policies never reach Chat and the answer covers only "
            "onboarding. Multi-document synthesis is not happening here."
        ),
    ),
    EvalCase(
        id="procurement-approval",
        question="We want to buy a $60k analytics tool that stores customer data. What approvals?",
        expect_retrieved=("FIN-019", "SEC-003"),
        must_contain=("vp",),
        top_k=10,
        note="VP approval plus vendor security review, 10 business days.",
        known_gap=(
            "Search returns zero results for this phrasing, so the floor refuses a "
            "question the corpus does cover -- FIN-019 and SEC-003 are both indexed "
            "and are retrieved by other queries."
        ),
    ),
    EvalCase(
        id="tier1-outage",
        question="A tier-1 customer has been down 9 hours. What happens?",
        expect_retrieved=("CS-008",),
        top_k=10,
        note="Level 3 escalation, exec sponsor, CSM owns comms. Spans CS-008, SEC-012, ENG-032.",
    ),
    EvalCase(
        id="vacation-approval-field-ops",
        question="Who approves a two-week vacation in November if I'm on Field Ops?",
        expect_retrieved=("HR-004",),
        top_k=10,
        note="VP approval, plus the Oct 15 - Dec 15 blackout.",
        known_gap=(
            "HR-004, the correct document, IS retrieved -- but term overlap lands at "
            "0.29 against a 0.30 floor, so the answer is refused. A false negative "
            "from the relevance floor, not a retrieval miss."
        ),
    ),
)

SUPERSEDED = (
    EvalCase(
        id="meal-per-diem",
        question="What's the meal per diem for domestic travel?",
        expect_cited=("FIN-011",),
        forbid_retrieved=("FIN-007",),
        must_contain=("75",),
        must_not_contain=("$50",),
        note=(
            "Finance/Archive/Expense Policy 2023 (SUPERSEDED).docx (FIN-007) "
            "deliberately contradicts the active FIN-011 on this exact figure. The "
            "active-only facet filter should keep it out of retrieval entirely, so "
            "this asserts it never comes back at all. A $50 answer is the "
            "archived-document problem resurfacing."
        ),
    ),
    EvalCase(
        id="corporate-card",
        question="What corporate card do we use?",
        expect_cited=("FIN-011",),
        forbid_retrieved=("FIN-007",),
        must_contain=("ramp",),
        must_not_contain=("brex",),
        note="Brex is the superseded answer in FIN-007.",
    ),
)

AMBIGUOUS = (
    EvalCase(
        id="hotel-limit",
        question="How much can I spend on a hotel?",
        note="Good behavior: ask which city, or return the full table.",
    ),
    EvalCase(
        id="which-sla",
        question="What's the SLA?",
        expect_retrieved=("ENG-032", "CS-008"),
        note="Ambiguous across SEC-012, ENG-032 and CS-008. Should disambiguate.",
    ),
)

OUT_OF_SCOPE = (
    EvalCase(
        id="401k-match",
        question="What's our 401k employer match?",
        grounded=False,
        note="Not in the corpus. The refusal must name the datasource searched.",
    ),
    EvalCase(
        id="ceo-name",
        question="Who is the CEO of Halcyon Robotics?",
        grounded=False,
        note="Must not invent a name.",
        known_gap=(
            "The floor does NOT reject this: 'halcyon' and 'robotics' appear in "
            "unrelated documents, overlap reaches 0.67, and Chat is called. Nothing "
            "is invented -- the system prompt makes Chat decline from the passages -- "
            "but the grounding guarantee is resting on generation rather than on the "
            "floor, which is the hallucination path the floor exists to close."
        ),
    ),
    EvalCase(
        id="dental-deductible",
        question="What's the dental insurance deductible?",
        grounded=False,
    ),
)

PRIOR_TRAPS = (
    EvalCase(
        id="expense-submission-window",
        question="How long do I have to submit an expense?",
        must_contain=("30 days",),
        note=(
            "The common prior is 60 or 90 days; Halcyon is 30. '60 days' is not "
            "forbidden text: FIN-011 uses it for the CFO-approval cutoff, so a "
            "correct answer mentions both figures."
        ),
    ),
    EvalCase(
        id="merit-increases",
        question="When do merit increases happen?",
        must_contain=("march",),
        note="The prior is annually in January. Halcyon runs a March cycle, effective April 1.",
    ),
    EvalCase(
        id="anchor-days",
        question="How many days a week am I in the office?",
        must_contain=("anchor",),
        note="Tue/Wed/Thu, branded 'Anchor Days'.",
    ),
    EvalCase(
        id="on-call-rotation",
        question="What's the on-call rotation length?",
        must_contain=("week",),
        note="One week, Monday 10am MT handoff.",
    ),
)

CASES: tuple[EvalCase, ...] = (
    *SINGLE_FACT,
    *MULTI_DOC,
    *SUPERSEDED,
    *AMBIGUOUS,
    *OUT_OF_SCOPE,
    *PRIOR_TRAPS,
)

# The order that tells the story, if you are running these by hand: pto-days
# (a clean single-source answer), procurement-approval (multi-document
# synthesis), 401k-match (the grounded refusal), meal-per-diem (the
# archived-document problem).
