from datetime import datetime
from zoneinfo import ZoneInfo

from postsite.dmr import parse_dmr
from postsite.models import Site
from postsite.openclaw_bridge import handle as handle_openclaw
from postsite.questions import next_question, primary_question
from postsite.scheduling import build_schedule_plan
from postsite.store import Store
from postsite.telegram_flow import is_run_post_site, schedule_preview_text


def test_parse_real_dmr_shape():
    text = """📋 Daily Meeting Report — 2026-08-12 (Wed)
1+2. Customer Alpha 060626
📍 North District | Install
8x top hung
1x smart hub
📎 Proposal: https://example.com/proposal

6. Customer Charlie
📍 West District | Servicing
1 x LAN hub and 1 x wireless hub
collect payment
"""
    sites = parse_dmr(text)
    assert len(sites) == 2
    assert sites[0].staff_numbers == [1, 2]
    assert sites[0].visit_type == "installation"
    assert sites[0].work_items == ["8x top hung", "1x smart hub"]
    assert sites[1].payment_required is True
    assert sites[1].visit_type == "service"


def test_parse_dmr_extracts_per_site_appointment_time():
    text = """📋 Daily Meeting Report — 2026-08-16 (Sun)

1. Customer Alpha - 10:30am
📍 Central District | Servicing
DCM2 Offline

1. Customer Beta - 12pm
📍 West District | Servicing
1x 3G PW Red indicator

2. Customer Charlie - 2pm
📍 North District | Site Meeting

1. Customer Delta - 3pm
📍 Northeast District | Servicing
Install back DCM2 as gate motor replaced

1. Customer Echo - 4pm
📍 New District | Delivery
13x Atlas 75 White

1+6. Customer Foxtrot - 5pm
📍 North District | Servicing
1x 3GRWN
"""
    sites = parse_dmr(text)
    assert len(sites) == 6
    assert sites[0].deal_name == "Customer Alpha"
    assert sites[0].scheduled_time == "10:30"
    assert sites[1].deal_name == "Customer Beta"
    assert sites[1].scheduled_time == "12:00"
    assert sites[2].scheduled_time == "14:00"
    assert sites[3].scheduled_time == "15:00"
    assert sites[4].scheduled_time == "16:00"
    assert sites[5].scheduled_time == "17:00"
    assert sites[5].staff_numbers == [1, 6]


def test_parse_dmr_no_time_leaves_scheduled_time_blank():
    text = """📋 Daily Meeting Report — 2026-08-12 (Wed)
1. Customer Beta
📍 North District | Install
some work
"""
    sites = parse_dmr(text)
    assert sites[0].scheduled_time == ""


def test_schedule_plan_returns_due_sites_and_one_next_wake_up():
    tz = ZoneInfo("Asia/Singapore")
    now = datetime(2026, 8, 16, 12, 30, tzinfo=tz)
    sites = [
        Site(id=3, dmr_date="2026-08-16", scheduled_time="14:00", deal_name="Later"),
        Site(id=1, dmr_date="2026-08-16", scheduled_time="10:30", deal_name="First"),
        Site(id=2, dmr_date="2026-08-16", scheduled_time="12:00", deal_name="Second"),
        Site(id=4, dmr_date="2026-08-16", scheduled_time="17:00", deal_name="Last"),
    ]

    plan = build_schedule_plan(sites, now, tz, "18:00")

    assert [site.deal_name for site in plan.due] == ["First", "Second"]
    assert plan.next_at == datetime(2026, 8, 16, 14, 0, tzinfo=tz)


def test_schedule_plan_uses_fallback_time_and_can_sleep_indefinitely():
    tz = ZoneInfo("Asia/Singapore")
    before_fallback = datetime(2026, 8, 16, 17, 0, tzinfo=tz)
    site = Site(dmr_date="2026-08-16", deal_name="No explicit time")

    future_plan = build_schedule_plan([site], before_fallback, tz, "18:00")
    empty_plan = build_schedule_plan([], before_fallback, tz, "18:00")

    assert future_plan.due == []
    assert future_plan.next_at == datetime(2026, 8, 16, 18, 0, tzinfo=tz)
    assert empty_plan.next_at is None


def test_natural_run_post_site_trigger():
    assert is_run_post_site("run post-site")
    assert is_run_post_site("Run post site @FieldOpsBot", "FieldOpsBot")
    assert not is_run_post_site("please run the post-site report")


def test_schedule_preview_lists_time_site_location_and_staff():
    sites = [
        Site(
            dmr_date="2026-08-16",
            scheduled_time="10:30",
            staff_numbers=[1, 6],
            deal_name="Customer & Co",
            location="Central District",
        ),
        Site(
            dmr_date="2026-08-16",
            staff_numbers=[2],
            deal_name="No explicit time",
            location="North District",
        ),
    ]

    text = schedule_preview_text(sites, "18:00", lambda number: f"Staff {number}")

    assert "POST-SITE SCHEDULE — 16 AUG" in text
    assert "10:30am" in text
    assert "Customer &amp; Co @ Central District — Staff 1 + Staff 6" in text
    assert "6pm fallback" in text
    assert "No explicit time @ North District — Staff 2" in text


def test_dmr_draft_does_not_schedule_until_atomic_approval(tmp_path):
    local_store = Store(str(tmp_path / "postsite.db"))
    site = Site(
        dmr_date="2026-08-16",
        scheduled_time="10:30",
        staff_numbers=[1],
        deal_name="Customer Alpha",
        location="Central District",
        activity_raw="Servicing",
        visit_type="service",
    )

    draft = local_store.create_draft(-100123, 42, "sample DMR", [site])

    assert draft.status == "pending"
    assert local_store.list() == []

    approved = local_store.approve_draft(draft.id)

    assert approved is not None
    assert approved[0].status == "approved"
    assert [stored.deal_name for stored in local_store.list(status="pending")] == ["Customer Alpha"]
    assert local_store.approve_draft(draft.id) is None


def test_new_draft_replaces_unapproved_draft_in_same_chat(tmp_path):
    local_store = Store(str(tmp_path / "postsite.db"))
    first = local_store.create_draft(-100123, 42, "first", [Site(dmr_date="2026-08-16", deal_name="First")])
    second = local_store.create_draft(-100123, 42, "second", [Site(dmr_date="2026-08-16", deal_name="Second")])

    assert local_store.get_draft(first.id).status == "replaced"
    assert local_store.get_draft(second.id).status == "pending"
    assert local_store.list() == []


def test_openclaw_review_claim_rollback_and_commit(tmp_path):
    local_store = Store(str(tmp_path / "postsite.db"))
    dmr = """📋 Daily Meeting Report — 2026-08-16 (Sun)
1. Customer Alpha - 10:30am
📍 Central District | Servicing
DCM2 Offline
"""
    identity = {"group_id": "-1001234567890", "sender_id": "123456789"}

    review = handle_openclaw("review", {**identity, "source_text": dmr}, local_store)
    review_id = review["review_id"]

    assert review["ok"] is True
    assert "Customer Alpha @ Central District — Staff 1" in review["reply"]
    assert local_store.list() == []

    prepared = handle_openclaw("prepare", {**identity, "review_id": review_id}, local_store)

    assert prepared["ok"] is True
    assert prepared["jobs"][0]["at"] == "2026-08-16T10:30:00+08:00"
    assert "Was the issue resolved?" in prepared["jobs"][0]["prompt"]
    assert local_store.get_draft(int(review_id)).status == "scheduling"

    rolled_back = handle_openclaw("rollback", {**identity, "review_id": review_id}, local_store)
    assert rolled_back["ok"] is True
    assert local_store.get_draft(int(review_id)).status == "pending"

    handle_openclaw("prepare", {**identity, "review_id": review_id}, local_store)
    committed = handle_openclaw(
        "commit",
        {**identity, "review_id": review_id, "job_ids": ["cron-job-1"]},
        local_store,
    )

    assert committed["ok"] is True
    assert "APPROVED" in committed["reply"]
    assert local_store.get_draft(int(review_id)).schedule_job_ids == ["cron-job-1"]
    assert local_store.list() == []


def test_openclaw_draft_actions_are_bound_to_group_and_requester(tmp_path):
    local_store = Store(str(tmp_path / "postsite.db"))
    dmr = """📋 Daily Meeting Report — 2026-08-16 (Sun)
1. Customer Alpha - 10:30am
📍 Central District | Servicing
"""
    owner = {"group_id": "-1001234567890", "sender_id": "123456789"}
    review = handle_openclaw("review", {**owner, "source_text": dmr}, local_store)

    denied = handle_openclaw(
        "prepare",
        {"group_id": "-1001234567890", "sender_id": "999", "review_id": review["review_id"]},
        local_store,
    )

    assert denied["ok"] is False
    assert denied["error_code"] == "unauthorized"
    assert local_store.get_draft(int(review["review_id"])).status == "pending"


def test_install_yes_finishes():
    site = Site(visit_type="installation", stage="primary")
    q, changes = next_question(site, "yes")
    assert q is None
    assert changes["status"] == "ready"
    assert changes["responses"]["completed"] is True


def test_install_no_asks_one_compact_exception_question():
    site = Site(visit_type="installation", stage="primary")
    q, changes = next_question(site, "no")
    assert "what remains outstanding" in q.lower()
    assert changes["stage"] == "outstanding"


def test_descriptive_incomplete_install_is_not_marked_complete():
    site = Site(visit_type="installation", stage="primary")
    q, changes = next_question(site, "2 switches pending")
    assert changes["responses"]["completed"] is False
    assert changes["responses"]["outstanding"] == "2 switches pending"
    assert changes["stage"] == "next_action"
    assert "needed next" in q.lower()


def test_descriptive_incomplete_with_next_step_finishes_without_extra_question():
    site = Site(visit_type="installation", stage="primary")
    q, changes = next_question(site, "2 switches pending. Need to return Friday after electrician fixes wiring.")
    assert changes["responses"]["completed"] is False
    assert q is None
    assert changes["status"] == "ready"


def test_service_yes_requires_resolution_detail():
    site = Site(visit_type="service", stage="primary")
    q, changes = next_question(site, "yes")
    assert "what did you find/do" in q.lower()
    assert changes["stage"] == "service_detail"


def test_unresolved_service_requires_next_action_when_missing():
    site = Site(visit_type="service", stage="primary")
    q, changes = next_question(site, "Hub still offline")
    assert changes["responses"]["completed"] is False
    assert changes["stage"] == "next_action"
    assert q is not None


def test_payment_is_only_asked_when_dmr_requires_it():
    site = Site(visit_type="installation", stage="primary", payment_required=True)
    q, changes = next_question(site, "yes")
    assert "payment" in q.lower()
    assert changes["stage"] == "payment"


def test_assessment_asks_for_outcome_not_yes_no():
    site = Site(visit_type="assessment")
    assert "what was agreed/found" in primary_question(site).lower()


def test_no_outstanding_phrase_does_not_flip_a_positive_reply_negative():
    site = Site(visit_type="service", stage="primary")
    q, changes = next_question(site, "Reset hub. All working, no outstanding.")
    assert changes["responses"]["completed"] is True
    assert q is None


def test_payment_not_yet_is_not_marked_collected():
    site = Site(visit_type="installation", stage="payment", payment_required=True, responses={"completed": True})
    q, changes = next_question(site, "not yet, owner will PayNow tomorrow")
    assert q is None
    assert changes["responses"]["payment_collected"] is False
