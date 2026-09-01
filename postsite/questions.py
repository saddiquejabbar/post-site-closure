from __future__ import annotations

import re

from .models import Site

YES_RE = re.compile(r"^(yes|y|done|completed|complete|all done|settled)[.! ]*$", re.I)
NO_RE = re.compile(r"^(no|n|not done|incomplete)[.! ]*$", re.I)
NEGATIVE_RE = re.compile(
    r"\b(pending|outstanding|missing|damaged|failed|unable|unresolved|incomplete)\b|"
    r"\b(not|isn['’]?t|wasn['’]?t|still)\s+(installed|completed|working|resolved|online|offline|done|ready)\b|"
    r"\b(can['’]?t|cannot|couldn['’]?t|could not)\b|"
    r"\bneed(?:s|ed)?\s+(?:to\s+)?(?:return|come back)\b",
    re.I,
)
NEXT_STEP_RE = re.compile(
    r"\b(return|come back|next visit|follow[- ]?up|replace|replacement|order|await|waiting|"
    r"schedule|reschedule|need(?:s|ed)?|require(?:s|d)?|electrician|contractor|owner to|stock)\b",
    re.I,
)


def is_simple_yes(text: str) -> bool:
    return bool(YES_RE.match(text.strip()))


def is_simple_no(text: str) -> bool:
    return bool(NO_RE.match(text.strip()))


def sounds_incomplete(text: str) -> bool:
    cleaned = re.sub(
        r"\b(?:no|nothing)\s+(?:items?\s+)?(?:outstanding|pending|missing|damaged)\b",
        "",
        text,
        flags=re.I,
    )
    return bool(NEGATIVE_RE.search(cleaned))


def has_next_step(text: str) -> bool:
    return bool(NEXT_STEP_RE.search(text))


def work_summary(site: Site, max_items: int = 10) -> str:
    items = [x for x in site.work_items if "collect payment" not in x.lower()]
    shown = items[:max_items]
    out = "; ".join(shown) if shown else ""
    if len(items) > max_items:
        out += f"; +{len(items) - max_items} more"
    return out


def primary_question(site: Site) -> str:
    kind = site.visit_type
    if kind == "installation":
        return "Was everything installed/completed as planned? Reply YES, or tell me what was not completed."
    if kind == "service":
        return "Was the issue resolved? Reply with what you found/did, or NO if it is still unresolved."
    if kind == "handover":
        return "Was the handover completed and the system working? Reply YES, or tell me what is still outstanding."
    if kind == "delivery":
        return "Were all items delivered? Reply YES + who received them, or tell me what was missing/damaged."
    if kind == "assessment":
        return "What was agreed/found on site, and what is the next action? One short reply is enough."
    if kind == "testing":
        return "What was tested and what was the result? Include any follow-up needed."
    return "Was this completed? Reply YES, or tell me what remains outstanding."


def next_question(site: Site, reply: str) -> tuple[str | None, dict]:
    """Return the minimum next question and state update.

    Natural-language replies are preserved verbatim. A small set of conservative
    heuristics prevents obvious incomplete replies from being marked completed,
    without pretending to be a full NLP/AI system.
    """
    text = reply.strip()
    r = dict(site.responses)
    kind = site.visit_type

    if site.stage == "primary":
        if kind == "service":
            if is_simple_yes(text):
                r["completed"] = True
                return "What did you find/do to resolve it? One short reply.", {"responses": r, "stage": "service_detail"}
            if is_simple_no(text):
                r["completed"] = False
                return "What remains unresolved and what is needed next?", {"responses": r, "stage": "outstanding"}
            r["completed"] = not sounds_incomplete(text)
            r["resolution"] = text
            if r["completed"] is False and not has_next_step(text):
                return _ask_next_action(r)
            return _payment_or_finish(site, r)

        if kind in {"assessment", "testing"}:
            if len(text) < 5:
                return primary_question(site), {"responses": r, "stage": "primary"}
            r["outcome"] = text
            r["completed"] = not sounds_incomplete(text)
            if r["completed"] is False and not has_next_step(text):
                return _ask_next_action(r)
            return _payment_or_finish(site, r)

        if kind == "delivery":
            if is_simple_yes(text):
                r["completed"] = True
                return "Who received the items on site?", {"responses": r, "stage": "delivery_recipient"}
            if is_simple_no(text):
                r["completed"] = False
                return "What was missing/damaged, and what is needed next?", {"responses": r, "stage": "outstanding"}
            r["completed"] = not sounds_incomplete(text)
            r["delivery_result"] = text
            if r["completed"] is False and not has_next_step(text):
                return _ask_next_action(r)
            return _payment_or_finish(site, r)

        if is_simple_yes(text):
            r["completed"] = True
            return _payment_or_finish(site, r)
        if is_simple_no(text):
            r["completed"] = False
            return (
                "What remains outstanding, why, and what is needed next? "
                "If another visit is needed, include what was told to the owner.",
                {"responses": r, "stage": "outstanding"},
            )

        incomplete = sounds_incomplete(text)
        r["completed"] = not incomplete
        if incomplete:
            r["outstanding"] = text
            if not has_next_step(text):
                return _ask_next_action(r)
        else:
            r["result"] = text
        return _payment_or_finish(site, r)

    if site.stage == "service_detail":
        r["resolution"] = text
        if sounds_incomplete(text):
            r["completed"] = False
            if not has_next_step(text):
                return _ask_next_action(r)
        return _payment_or_finish(site, r)

    if site.stage == "delivery_recipient":
        r["recipient"] = text
        return _payment_or_finish(site, r)

    if site.stage == "outstanding":
        r["outstanding"] = text
        return _payment_or_finish(site, r)

    if site.stage == "next_action":
        r["next_action"] = text
        return _payment_or_finish(site, r)

    if site.stage == "payment":
        if is_simple_yes(text):
            r["payment_collected"] = True
        elif is_simple_no(text):
            r["payment_collected"] = False
            return "Why was payment not collected?", {"responses": r, "stage": "payment_reason"}
        else:
            r["payment_note"] = text
            r["payment_collected"] = not (text.lower().startswith(("no", "not")) or sounds_incomplete(text))
        return None, {"responses": r, "stage": "done", "status": "ready"}

    if site.stage == "payment_reason":
        r["payment_collected"] = False
        r["payment_note"] = text
        return None, {"responses": r, "stage": "done", "status": "ready"}

    return None, {"responses": r, "stage": "done", "status": "ready"}


def _ask_next_action(responses: dict) -> tuple[str, dict]:
    return (
        "What is needed next? If another visit is required, include what was told to the owner.",
        {"responses": responses, "stage": "next_action"},
    )


def _payment_or_finish(site: Site, responses: dict) -> tuple[str | None, dict]:
    if site.payment_required and "payment_collected" not in responses and "payment_note" not in responses:
        return "Was payment collected? Reply YES/NO. If needed, add a short note.", {
            "responses": responses,
            "stage": "payment",
        }
    return None, {"responses": responses, "stage": "done", "status": "ready"}
