"""Frappe HR tools (tools/hr.py).

Mocked like the rest of the suite: the fake client below answers REST list/get/insert/
submit from in-memory rows (it applies the simple [field, op, value] filters the tools
send), so what is pinned is the sequence of calls and the exact values written.
Doctype and field names were checked against hrms v16.52.0 / ERPNext v16.51 sources; none
of this ran against a live site.
"""
from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

from gunstore_mcp import config as config_mod
from gunstore_mcp import server
from gunstore_mcp.frappe_client import FrappeAPIError
from gunstore_mcp.safety import WriteRefused
from gunstore_mcp.tools import hr

TODAY = date(2026, 10, 31)
HRMS_DOCTYPES = {"Employee Checkin", "Attendance", "Leave Application", "Leave Policy",
                 "Leave Policy Assignment", "Shift Type"}
WRITES = ("hr_leave_decide", "hr_attendance_fix", "hr_onboard")
LEAVE_METHOD = "hrms.hr.doctype.leave_application.leave_application.get_leave_details"

# a valid call per write tool
VALID = {
    "hr_leave_decide": {"leave_application": "HR-LAP-1", "decision": "approve"},
    "hr_attendance_fix": {"action": "add_checkin", "employee": "E1", "reason": "forgot",
                          "time": "2026-10-30 08:00", "log_type": "IN"},
    "hr_onboard": {"user": "sam@x.com", "first_name": "Sam", "last_name": "Lee",
                   "gender": "Male", "date_of_birth": "1990-05-05",
                   "date_of_joining": "2026-01-15"},
}


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _match(row, flt) -> bool:
    field, op, value = flt
    got = row.get(field)
    if op == "=":
        return got == value
    if op == "in":
        return got in value
    if got is None:
        return False
    if op == ">=":
        return str(got) >= str(value)
    if op == "<=":
        return str(got) <= str(value)
    if op == "<":
        return got < value
    raise AssertionError(f"filter op {op!r} not modelled")


class FakeClient:
    def __init__(self, hrms=True):
        self.hrms = hrms
        self.calls = []
        self.fail_submit = set()      # doctypes whose submit raises
        self.fail_update = set()      # (doctype, status) updates that raise
        self.skip_user_permission = False   # Employee.on_update fails to add the permission
        self.seq = 0
        self.rows = {
            "Employee": [
                {"name": "E1", "employee_name": "Ann Ames", "user_id": "ann@x.com",
                 "default_shift": "Day", "holiday_list": "HL", "leave_approver": "boss@x.com",
                 "company": "Co", "status": "Active"},
                {"name": "E2", "employee_name": "Bob Bell", "user_id": None,
                 "default_shift": None, "holiday_list": None, "leave_approver": None,
                 "company": "Co", "status": "Active"},
                {"name": "E3", "employee_name": "Old Hand", "user_id": "old@x.com",
                 "company": "Co", "status": "Left"},
            ],
            "User Permission": [{"user": "ann@x.com", "allow": "Employee", "for_value": "E1"}],
            "Employee Checkin": [], "Attendance": [], "Leave Application": [],
            "Company": [{"name": "Co", "default_holiday_list": "Default HL"}],
            "Shift Type": [{"name": "Day"}],
            "Leave Policy": [{"name": "LP-1", "docstatus": 1}],
            "Leave Policy Assignment": [], "Comment": [],
        }
        self.docs = {
            ("System Settings", "System Settings"): {"time_zone": "America/Chicago"},
            ("User", "sam@x.com"): {"name": "sam@x.com", "enabled": 1,
                                    "roles": [{"role": "Sales User"}], "role_profiles": []},
            ("Leave Policy", "LP-1"): {"name": "LP-1", "docstatus": 1},
            ("Leave Application", "HR-LAP-1"): {
                "name": "HR-LAP-1", "docstatus": 0, "status": "Open", "employee": "E2",
                "employee_name": "Bob Bell", "leave_type": "Sick", "from_date": "2026-11-02",
                "to_date": "2026-11-03", "total_leave_days": 2},
        }

    # ----- reads
    def list_documents(self, doctype, *, fields=None, filters=None, limit=20, start=0,
                       order_by=None):
        self.calls.append(("list", doctype, fields, filters))
        if not self.hrms and doctype in HRMS_DOCTYPES:
            raise FrappeAPIError(404, "DoesNotExistError", [f"DocType {doctype} not found"])
        out = [r for r in self.rows.get(doctype, [])
               if all(_match(r, f) for f in filters or [])]
        if fields:  # the server returns only the requested columns
            out = [{k: r.get(k) for k in fields} for r in out]
        return out[:limit] if limit else out

    def get_document(self, doctype, name):
        self.calls.append(("get", doctype, name))
        try:
            return dict(self.docs[(doctype, name)])
        except KeyError:
            raise FrappeAPIError(404, "DoesNotExistError", [f"{doctype} {name} not found"])

    def call_method(self, method, kwargs=None):
        self.calls.append(("method", method, kwargs))
        return {"leave_allocation": {"Sick": {
            "total_leaves": 5, "leaves_taken": 1, "leaves_pending_approval": 0.5,
            "remaining_leaves": 4, "expired_leaves": 0}}}

    # ----- writes
    def create_document(self, doctype, values):
        self.calls.append(("create", doctype, values))
        self.seq += 1
        doc = {**values, "name": f"{doctype}-{self.seq}", "docstatus": 0}
        if doctype == "Employee":
            doc["employee_name"] = f"{values['first_name']} {values['last_name']}"
            if values.get("create_user_permission") and not self.skip_user_permission:
                self.rows["User Permission"].append(
                    {"user": values["user_id"], "allow": "Employee", "for_value": doc["name"]})
        self.rows.setdefault(doctype, []).append(doc)
        self.docs[(doctype, doc["name"])] = doc
        return dict(doc)

    def update_document(self, doctype, name, values):
        self.calls.append(("update", doctype, name, values))
        if (doctype, values.get("status")) in self.fail_update:
            raise FrappeAPIError(417, "ValidationError", ["not enough leave balance"])
        self.docs[(doctype, name)].update(values)
        return dict(self.docs[(doctype, name)])

    def submit_document(self, doctype, name):
        self.calls.append(("submit", doctype, name))
        if doctype in self.fail_submit:
            raise FrappeAPIError(417, "ValidationError", ["submit refused"])
        self.docs[(doctype, name)]["docstatus"] = 1
        return {}

    def delete_document(self, doctype, name):
        self.calls.append(("delete", doctype, name))
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] in ("create", "update", "submit", "delete")]


class Base(unittest.TestCase):
    def setUp(self):
        mcp = FakeMCP()
        hr.register(mcp)
        self.tools = mcp.tools
        self.client = FakeClient()
        creds = patch.dict(os.environ, {"FRAPPE_BASE_URL": "http://hr-probe.invalid",
                                        "FRAPPE_API_KEY": "k", "FRAPPE_API_SECRET": "s"})
        creds.start()
        config_mod._config = None
        self.addCleanup(lambda: (creds.stop(), setattr(config_mod, "_config", None)))
        p = patch.object(hr, "get_client", return_value=self.client)
        p.start()
        self.addCleanup(p.stop)

    def tool(self, name):
        return self.tools[name]


class Surface(Base):
    def test_registered_names(self):
        self.assertEqual(set(self.tools), {
            "hr_staff", "hr_attendance", "hr_leave", "hr_hours_summary",
            "hr_leave_decide", "hr_attendance_fix", "hr_onboard"})

    def test_no_registration_gate(self):
        self.assertFalse([n for n in dir(hr) if n.endswith("_ENV")])

    def test_cpa_surface_has_the_hours_read_and_none_of_the_writes(self):
        mcp = FakeMCP()
        server.register_tools(mcp, mode="cpa")
        self.assertIn("hr_hours_summary", mcp.tools)
        for name in ("hr_staff", "hr_attendance", "hr_leave", *WRITES):
            self.assertNotIn(name, mcp.tools)

    def test_full_mode_has_all_seven(self):
        mcp = FakeMCP()
        server.register_tools(mcp, mode="full")
        self.assertTrue(set(self.tools) <= set(mcp.tools))


class ConfirmGate(Base):
    def test_every_write_refuses_without_confirm_and_never_calls_the_pos(self):
        for name in WRITES:
            with self.subTest(tool=name):
                with self.assertRaises(WriteRefused):
                    self.tool(name)(**VALID[name])
                with self.assertRaises(WriteRefused):
                    self.tool(name)(**VALID[name], confirm=False)
        self.assertEqual(self.client.calls, [])

    def test_mark_attendance_also_refuses_without_confirm(self):
        with self.assertRaises(WriteRefused):
            self.tool("hr_attendance_fix")(
                action="mark_attendance", employee="E1", reason="r",
                attendance_date="2026-10-30", status="Present")
        self.assertEqual(self.client.calls, [])

    def test_reject_without_a_reason_is_refused_before_confirm_matters(self):
        with self.assertRaises(ValueError):
            self.tool("hr_leave_decide")("HR-LAP-1", "reject", confirm=True)
        with self.assertRaises(ValueError):
            self.tool("hr_leave_decide")("HR-LAP-1", "reject", reason="  ", confirm=True)
        self.assertEqual(self.client.calls, [])


class HrmsMissing(Base):
    def setUp(self):
        super().setUp()
        self.client.hrms = False

    def test_every_tool_gives_the_one_clear_error_after_one_probe(self):
        calls = {
            "hr_staff": {}, "hr_attendance": {"from_date": "2026-10-01", "to_date": "2026-10-31"},
            "hr_leave": {"view": "pending"},
            "hr_hours_summary": {"from_date": "2026-10-01", "to_date": "2026-10-31"},
            **{n: {**VALID[n], "confirm": True} for n in WRITES}}
        for name, kwargs in calls.items():
            with self.subTest(tool=name):
                self.client.calls.clear()
                with self.assertRaises(hr.HrmsMissing) as cm:
                    self.tool(name)(**kwargs)
                self.assertIn("Frappe HR (hrms) is not installed on this site", str(cm.exception))
                self.assertEqual(self.client.writes(), [])
                self.assertEqual(len([c for c in self.client.calls if c[0] == "list"]), 1)

    def test_other_errors_are_not_mistaken_for_a_missing_app(self):
        def forbidden(*a, **k):
            raise FrappeAPIError(403, "PermissionError", ["no access"])
        self.client.list_documents = forbidden
        with self.assertRaises(FrappeAPIError):
            self.tool("hr_staff")()


class Staff(Base):
    def test_lists_active_staff_without_personal_fields(self):
        self.client.rows["Employee Checkin"] = [
            {"employee": "E1", "time": "2026-10-31 09:00:00", "log_type": "IN"},
            {"employee": "E1", "time": "2026-10-31 07:00:00", "log_type": "OUT"}]
        out = self.tool("hr_staff")()
        self.assertEqual([e["name"] for e in out], ["E1", "E2"])
        ann = out[0]
        self.assertEqual(ann["restricted_to_employees"], ["E1"])
        self.assertEqual(out[1]["restricted_to_employees"], [])
        self.assertEqual(ann["last_checkin_today"], {"time": "2026-10-31 09:00:00", "log_type": "IN"})
        self.assertIsNone(out[1]["last_checkin_today"])
        asked = set()
        for kind, doctype, fields, *_ in self.client.calls:
            if kind == "list":
                asked |= set(fields or [])
        for banned in ("date_of_birth", "current_address", "permanent_address", "bank_ac_no",
                       "passport_number", "blood_group", "personal_email", "cell_number"):
            self.assertNotIn(banned, asked)
        self.assertEqual(set(ann), {"name", "employee_name", "user_id", "default_shift",
                                    "holiday_list", "leave_approver", "restricted_to_employees",
                                    "last_checkin_today"})


class Attendance(Base):
    def seed(self):
        self.client.rows["Employee Checkin"] = [
            {"employee": "E1", "employee_name": "Ann Ames", "time": "2026-10-05 08:00:00", "log_type": "IN"},
            {"employee": "E1", "employee_name": "Ann Ames", "time": "2026-10-05 16:30:00", "log_type": "OUT"},
            {"employee": "E1", "employee_name": "Ann Ames", "time": "2026-10-06 08:00:00", "log_type": "IN"},
            {"employee": "E2", "employee_name": "Bob Bell", "time": "2026-10-06 09:00:00", "log_type": "IN"}]
        self.client.rows["Attendance"] = [
            {"name": "A1", "employee": "E1", "employee_name": "Ann Ames",
             "attendance_date": "2026-10-05", "status": "Present", "working_hours": 8.5,
             "in_time": "2026-10-05 08:00:00", "out_time": "2026-10-05 16:30:00", "docstatus": 1},
            {"name": "A2", "employee": "E2", "employee_name": "Bob Bell",
             "attendance_date": "2026-10-06", "status": "Half Day", "working_hours": 4,
             "docstatus": 0},
            {"name": "A3", "employee": "E2", "employee_name": "Bob Bell",
             "attendance_date": "2026-10-07", "status": "On Leave", "working_hours": 0,
             "docstatus": 1}]

    def test_range_is_capped_and_validated(self):
        t = self.tool("hr_attendance")
        with self.assertRaises(ValueError) as cm:
            t("2026-08-01", "2026-10-31")
        self.assertIn("62 days", str(cm.exception))
        t("2026-09-01", "2026-10-31")  # 61 days, fine
        t("2026-09-01", "2026-11-01")  # exactly 62, fine
        with self.assertRaises(ValueError):
            t("2026-09-01", "2026-11-02")
        with self.assertRaises(ValueError):
            t("2026-10-05", "2026-10-01")
        with self.assertRaises(ValueError):
            t("10/01/2026", "2026-10-31")
        with self.assertRaises(ValueError):
            self.tool("hr_hours_summary")("2026-01-01", "2026-12-31")

    def test_days_and_totals(self):
        self.seed()
        out = self.tool("hr_attendance")("2026-10-01", "2026-10-31")
        by = {e["employee"]: e for e in out["employees"]}
        ann = by["E1"]
        self.assertEqual([d["date"] for d in ann["days"]], ["2026-10-05", "2026-10-06"])
        self.assertEqual(len(ann["days"][0]["checkins"]), 2)
        self.assertTrue(ann["days"][0]["attendance"][0]["submitted"])
        self.assertEqual(ann["totals"]["days_present"], 1)
        self.assertEqual(ann["totals"]["working_hours"], 8.5)
        # 10-06 has a check-in and no submitted Attendance
        self.assertEqual(ann["totals"]["checkin_days"], 2)
        self.assertEqual(ann["totals"]["checkin_days_without_attendance"], 1)
        bob = by["E2"]
        # his Half Day row is a draft: shown, not counted
        self.assertFalse(bob["days"][0]["attendance"][0]["submitted"])
        self.assertEqual(bob["totals"]["days_half_day"], 0)
        self.assertEqual(bob["totals"]["days_on_leave"], 1)
        self.assertEqual(bob["totals"]["checkin_days_without_attendance"], 1)

    def test_one_employee_is_filtered_in_the_query(self):
        self.seed()
        self.tool("hr_attendance")("2026-10-01", "2026-10-31", employee="E1")
        for kind, doctype, _f, filters in self.client.calls:
            if kind == "list" and doctype in ("Employee Checkin", "Attendance"):
                if filters:
                    self.assertIn(["employee", "=", "E1"], filters)

    def test_hours_summary(self):
        self.seed()
        out = self.tool("hr_hours_summary")("2026-10-01", "2026-10-31")
        by = {e["employee"]: e for e in out["employees"]}
        self.assertEqual(by["E1"]["days_present"], 1)
        self.assertEqual(by["E1"]["working_hours"], 8.5)
        self.assertEqual(by["E1"]["checkin_days_without_attendance"], 1)
        self.assertEqual(by["E2"]["days_half_day"], 0)   # draft
        self.assertEqual(by["E2"]["days_on_leave"], 1)
        self.assertEqual(out["total_working_hours"], 8.5)
        self.assertEqual(out["employees_with_unprocessed_days"], 2)
        # only names, days and hours
        self.assertEqual(set(by["E1"]), {
            "employee", "employee_name", "days_present", "days_half_day", "days_absent",
            "days_on_leave", "days_work_from_home", "working_hours", "checkin_days",
            "checkin_days_without_attendance"})

    def test_hours_summary_lists_active_staff_with_nothing_as_zeros(self):
        out = self.tool("hr_hours_summary")("2026-10-01", "2026-10-31")
        self.assertEqual([e["employee"] for e in out["employees"]], ["E1", "E2"])
        self.assertEqual(out["employees"][0]["working_hours"], 0)

    def test_hours_summary_runs_in_a_real_cpa_client_with_only_rest_reads(self):
        """The cpa client refuses every write and every method off its allowlist; the tool
        must get through on plain list reads alone."""
        from tests.test_modes import _client
        real = _client("cpa")
        seen = []

        def request(method, path, **kw):
            seen.append((method, path))
            return []
        real._request = request
        with patch.object(hr, "get_client", return_value=real), \
                patch.dict(os.environ, {"GUNSTORE_MCP_MODE": "cpa"}):
            out = self.tool("hr_hours_summary")("2026-10-01", "2026-10-31")
        self.assertEqual(out["employees"], [])
        self.assertTrue(seen and all(m == "GET" and p.startswith("/api/resource/")
                                     for m, p in seen), seen)


class Leave(Base):
    def test_pending_lists_open_applications_without_the_reason_text(self):
        self.client.rows["Leave Application"] = [
            {"name": "HR-LAP-1", "employee": "E2", "status": "Open", "docstatus": 0},
            {"name": "HR-LAP-2", "employee": "E1", "status": "Approved", "docstatus": 1}]
        out = self.tool("hr_leave")("pending")
        self.assertEqual([r["name"] for r in out], ["HR-LAP-1"])
        (_k, _d, fields, filters), = [c for c in self.client.calls
                                      if c[0] == "list" and c[1] == "Leave Application"
                                      and c[2] != ["name"]]
        self.assertNotIn("description", fields)
        self.assertIn(["status", "=", "Open"], filters)
        self.assertIn(["docstatus", "=", 0], filters)

    def test_balances_use_the_hrms_method_per_active_employee(self):
        out = self.tool("hr_leave")("balances")
        self.assertRegex(out["as_of"], r"^\d{4}-\d{2}-\d{2}$")
        methods = [c for c in self.client.calls if c[0] == "method"]
        self.assertEqual([m[1] for m in methods], [LEAVE_METHOD] * 2)
        self.assertEqual([m[2]["employee"] for m in methods], ["E1", "E2"])
        self.assertEqual(out["employees"][0]["balances"]["Sick"],
                         {"allocated": 5, "taken": 1, "pending_approval": 0.5,
                          "remaining": 4, "expired": 0})

    def test_view_is_checked(self):
        with self.assertRaises(ValueError):
            self.tool("hr_leave")("everything")


class LeaveDecide(Base):
    def decide(self, decision="approve", **kw):
        return self.tool("hr_leave_decide")("HR-LAP-1", decision, confirm=True, **kw)

    def test_approve_sets_status_then_submits(self):
        out = self.decide("approve")
        self.assertEqual([(c[0], c[1]) for c in self.client.writes()],
                         [("update", "Leave Application"), ("submit", "Leave Application")])
        self.assertEqual(self.client.writes()[0][3], {"status": "Approved"})
        self.assertEqual(out["status"], "Approved")
        self.assertFalse(out["comment_recorded"])

    def test_reject_records_the_reason_as_a_comment(self):
        out = self.decide("reject", reason="Short staffed that week")
        self.assertEqual(self.client.writes()[0][3], {"status": "Rejected"})
        comment = [c for c in self.client.writes() if c[1] == "Comment"][0][2]
        self.assertEqual((comment["reference_doctype"], comment["reference_name"]),
                         ("Leave Application", "HR-LAP-1"))
        self.assertIn("Short staffed", comment["content"])
        self.assertTrue(out["comment_recorded"])

    def test_refuses_anything_that_is_not_an_open_draft(self):
        for status, docstatus in (("Approved", 1), ("Rejected", 1), ("Cancelled", 2),
                                  ("Open", 1)):
            with self.subTest(status=status, docstatus=docstatus):
                self.client.docs[("Leave Application", "HR-LAP-1")].update(
                    {"status": status, "docstatus": docstatus})
                self.client.calls.clear()
                with self.assertRaises(WriteRefused):
                    self.decide("approve")
                self.assertEqual(self.client.writes(), [])

    def test_a_failed_submit_puts_the_status_back_to_open(self):
        self.client.fail_submit = {"Leave Application"}
        with self.assertRaises(RuntimeError) as cm:
            self.decide("approve")
        self.assertIn("put back to Open", str(cm.exception))
        updates = [c[3] for c in self.client.calls if c[0] == "update"]
        self.assertEqual(updates, [{"status": "Approved"}, {"status": "Open"}])

    def test_unknown_application_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            self.tool("hr_leave_decide")("NOPE", "approve", confirm=True)

    def test_decision_is_checked(self):
        with self.assertRaises(ValueError):
            self.tool("hr_leave_decide")("HR-LAP-1", "maybe", confirm=True)


class AttendanceFix(Base):
    def fix(self, **kw):
        base = {"employee": "E1", "reason": "Forgot to clock in", "confirm": True}
        return self.tool("hr_attendance_fix")(**{**base, **kw})

    def setUp(self):
        super().setUp()
        p = patch.object(hr, "_site_today", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def test_stray_arguments_are_refused_for_each_action(self):
        add = {"action": "add_checkin", "time": "2026-10-30 08:00", "log_type": "IN"}
        mark = {"action": "mark_attendance", "attendance_date": "2026-10-30", "status": "Present"}
        for extra in ({"attendance_date": "2026-10-30"}, {"status": "Present"},
                      {"working_hours": 8}):
            with self.subTest(action="add_checkin", extra=extra):
                with self.assertRaises(ValueError) as cm:
                    self.fix(**add, **extra)
                self.assertIn("do not apply", str(cm.exception))
        for extra in ({"time": "2026-10-30 08:00"}, {"log_type": "OUT"}):
            with self.subTest(action="mark_attendance", extra=extra):
                with self.assertRaises(ValueError):
                    self.fix(**mark, **extra)
        self.assertEqual(self.client.calls, [])

    def test_required_arguments_per_action(self):
        for kwargs in ({"action": "add_checkin", "time": "2026-10-30 08:00"},
                       {"action": "add_checkin", "log_type": "IN"},
                       {"action": "mark_attendance", "status": "Present"},
                       {"action": "mark_attendance", "attendance_date": "2026-10-30"}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.fix(**kwargs)
        with self.assertRaises(ValueError):
            self.fix(action="add_checkin", time="2026-10-30 08:00", log_type="IN", reason="  ")
        with self.assertRaises(ValueError):
            self.fix(action="nope")
        self.assertEqual(self.client.calls, [])

    def test_on_leave_cannot_be_marked_by_hand(self):
        with self.assertRaises(ValueError):
            self.fix(action="mark_attendance", attendance_date="2026-10-30", status="On Leave")

    def test_add_checkin_creates_it_and_records_the_reason(self):
        out = self.fix(action="add_checkin", time="2026-10-30T08:05", log_type="IN")
        create = [c for c in self.client.writes() if c[1] == "Employee Checkin"][0]
        self.assertEqual(create[2], {"employee": "E1", "time": "2026-10-30 08:05:00",
                                     "log_type": "IN"})
        comment = [c for c in self.client.writes() if c[1] == "Comment"][0][2]
        self.assertEqual(comment["reference_doctype"], "Employee Checkin")
        self.assertIn("Forgot to clock in", comment["content"])
        self.assertEqual(out["log_type"], "IN")
        self.assertFalse([c for c in self.client.writes() if c[0] == "submit"])

    def test_a_time_with_an_offset_or_in_the_wrong_shape_is_refused(self):
        for bad in ("2026-10-30 08:00+00:00", "2026-10-30T08:00Z", "8am yesterday", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.fix(action="add_checkin", time=bad, log_type="IN")
        self.assertEqual(self.client.calls, [])

    def test_future_times_and_dates_are_refused(self):
        with self.assertRaises(ValueError):
            self.fix(action="add_checkin", time="2026-11-01 08:00", log_type="IN")
        with self.assertRaises(ValueError):
            self.fix(action="mark_attendance", attendance_date="2026-11-01", status="Present")
        self.assertEqual(self.client.writes(), [])

    def test_inactive_or_unknown_employee_is_refused(self):
        with self.assertRaises(WriteRefused):
            self.fix(employee="E3", action="add_checkin", time="2026-10-30 08:00", log_type="IN")
        with self.assertRaises(ValueError):
            self.fix(employee="ZZ", action="add_checkin", time="2026-10-30 08:00", log_type="IN")
        self.assertEqual(self.client.writes(), [])

    def test_mark_attendance_creates_comments_and_submits(self):
        out = self.fix(action="mark_attendance", attendance_date="2026-10-30",
                       status="Work From Home", working_hours=7.5)
        create = [c for c in self.client.writes() if c[1] == "Attendance"][0][2]
        self.assertEqual(create, {"employee": "E1", "attendance_date": "2026-10-30",
                                  "status": "Work From Home", "company": "Co",
                                  "working_hours": 7.5})
        order = [(c[0], c[1]) for c in self.client.writes()]
        self.assertEqual(order, [("create", "Attendance"), ("create", "Comment"),
                                 ("submit", "Attendance")])
        self.assertTrue(out["submitted"])

    def test_a_submitted_attendance_that_day_is_refused_with_the_desk_route(self):
        self.client.rows["Attendance"] = [{"name": "A9", "employee": "E1",
                                           "attendance_date": "2026-10-30", "status": "Absent",
                                           "docstatus": 1}]
        with self.assertRaises(WriteRefused) as cm:
            self.fix(action="mark_attendance", attendance_date="2026-10-30", status="Present")
        self.assertIn("A9", str(cm.exception))
        self.assertIn("Cancel", str(cm.exception))
        self.assertIn("Amend", str(cm.exception))
        self.assertEqual(self.client.writes(), [])

    def test_a_draft_attendance_that_day_is_refused_too(self):
        self.client.rows["Attendance"] = [{"name": "A9", "employee": "E1",
                                           "attendance_date": "2026-10-30", "status": "Absent",
                                           "docstatus": 0}]
        with self.assertRaises(WriteRefused) as cm:
            self.fix(action="mark_attendance", attendance_date="2026-10-30", status="Present")
        self.assertIn("draft", str(cm.exception))

    def test_a_failed_submit_removes_the_draft_it_made(self):
        self.client.fail_submit = {"Attendance"}
        with self.assertRaises(RuntimeError) as cm:
            self.fix(action="mark_attendance", attendance_date="2026-10-30", status="Present")
        self.assertIn("draft Attendance was removed", str(cm.exception))
        self.assertEqual([c[:2] for c in self.client.calls if c[0] == "delete"],
                         [("delete", "Attendance")])

    def test_working_hours_are_bounded(self):
        with self.assertRaises(ValueError):
            self.fix(action="mark_attendance", attendance_date="2026-10-30", status="Present",
                     working_hours=30)


class Periods(unittest.TestCase):
    def test_current_anniversary_year(self):
        self.assertEqual(hr.leave_period(date(2020, 3, 10), date(2026, 10, 31)),
                         (date(2026, 3, 10), date(2027, 3, 9)))

    def test_before_this_years_anniversary_it_is_last_years_period(self):
        self.assertEqual(hr.leave_period(date(2020, 12, 1), date(2026, 10, 31)),
                         (date(2025, 12, 1), date(2026, 11, 30)))

    def test_on_the_anniversary_itself(self):
        self.assertEqual(hr.leave_period(date(2020, 10, 31), date(2026, 10, 31)),
                         (date(2026, 10, 31), date(2027, 10, 30)))

    def test_joined_this_year_starts_at_the_joining_date(self):
        self.assertEqual(hr.leave_period(date(2026, 1, 15), date(2026, 10, 31)),
                         (date(2026, 1, 15), date(2027, 1, 14)))

    def test_joining_in_the_future_starts_on_the_joining_date(self):
        self.assertEqual(hr.leave_period(date(2026, 12, 1), date(2026, 10, 31)),
                         (date(2026, 12, 1), date(2027, 11, 30)))

    def test_feb_29_falls_on_feb_28_in_common_years(self):
        joined = date(2024, 2, 29)
        self.assertEqual(hr.leave_period(joined, date(2026, 10, 31)),
                         (date(2026, 2, 28), date(2027, 2, 27)))
        self.assertEqual(hr.leave_period(joined, date(2026, 2, 27)),
                         (date(2025, 2, 28), date(2026, 2, 27)))
        # in a leap year the anniversary is the real day, and the year before ends on Feb 28
        self.assertEqual(hr.leave_period(joined, date(2028, 3, 1)),
                         (date(2028, 2, 29), date(2029, 2, 27)))
        self.assertEqual(hr.leave_period(joined, date(2028, 2, 28)),
                         (date(2027, 2, 28), date(2028, 2, 28)))

    def test_consecutive_periods_tile_without_gap_or_overlap(self):
        from datetime import timedelta
        for joined in (date(2020, 3, 10), date(2024, 2, 29), date(2023, 12, 31)):
            day = date(2024, 1, 1)
            prev_end = None
            while day < date(2031, 1, 1):
                start, end = hr.leave_period(joined, day)
                if joined <= day:
                    self.assertTrue(start <= day <= end, (joined, day, start, end))
                if prev_end and start > prev_end:
                    self.assertEqual(start, prev_end + timedelta(days=1), (joined, day))
                prev_end = end
                day += timedelta(days=37)


class Onboard(Base):
    def setUp(self):
        super().setUp()
        p = patch.object(hr, "_site_today", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def onboard(self, **kw):
        return self.tool("hr_onboard")(**{**VALID["hr_onboard"], "confirm": True, **kw})

    def employee_values(self):
        return [c for c in self.client.writes() if c[1] == "Employee"][0][2]

    def test_staff_user_gets_a_user_permission(self):
        out = self.onboard()
        self.assertEqual(self.employee_values()["create_user_permission"], 1)
        self.assertEqual(out["create_user_permission"], 1)

    def test_admin_and_approver_roles_get_none(self):
        for role in ("System Manager", "HR Manager"):
            with self.subTest(role=role):
                self.client.docs[("User", "sam@x.com")]["roles"] = [
                    {"role": "Sales User"}, {"role": role}]
                self.client.calls.clear()
                self.onboard()
                self.assertEqual(self.employee_values()["create_user_permission"], 0)
                self.client.rows["Employee"] = [r for r in self.client.rows["Employee"]
                                                if r.get("user_id") != "sam@x.com"]

    def test_the_employee_is_made_with_the_defaults_and_the_given_fields(self):
        self.onboard(leave_approver="boss@x.com")
        self.assertEqual(self.employee_values(), {
            "first_name": "Sam", "last_name": "Lee", "gender": "Male",
            "date_of_birth": "1990-05-05", "date_of_joining": "2026-01-15",
            "company": "Co", "status": "Active", "user_id": "sam@x.com",
            "create_user_permission": 1, "holiday_list": "Default HL",
            "default_shift": "Day", "leave_approver": "boss@x.com"})

    def test_explicit_holiday_list_and_shift_win_and_two_shifts_pick_none(self):
        self.client.rows["Shift Type"] = [{"name": "Day"}, {"name": "Night"}]
        self.onboard(holiday_list="My HL")
        vals = self.employee_values()
        self.assertEqual(vals["holiday_list"], "My HL")
        self.assertNotIn("default_shift", vals)
        self.client.rows["Employee"] = [r for r in self.client.rows["Employee"]
                                        if r.get("user_id") != "sam@x.com"]
        self.client.calls.clear()
        self.onboard(shift="Night")
        self.assertEqual(self.employee_values()["default_shift"], "Night")

    def test_assignment_covers_the_anniversary_year_containing_today(self):
        out = self.onboard(date_of_joining="2020-03-10")
        lpa = [c for c in self.client.writes() if c[1] == "Leave Policy Assignment"
               and c[0] == "create"][0][2]
        self.assertEqual(lpa["effective_from"], "2026-03-10")
        self.assertEqual(lpa["effective_to"], "2027-03-09")
        self.assertEqual(lpa["leave_policy"], "LP-1")
        self.assertNotIn("assignment_based_on", lpa)
        self.assertIn(("submit", "Leave Policy Assignment"),
                      [(c[0], c[1]) for c in self.client.writes()])
        self.assertEqual(out["effective_from"], "2026-03-10")
        self.assertTrue(out["leave_policy_assignment"])

    def test_employee_is_created_before_its_assignment(self):
        self.onboard()
        order = [(c[0], c[1]) for c in self.client.writes()]
        self.assertLess(order.index(("create", "Employee")),
                        order.index(("create", "Leave Policy Assignment")))

    def test_an_unsubmitted_or_unknown_policy_is_refused_before_anything_is_written(self):
        self.client.docs[("Leave Policy", "LP-1")]["docstatus"] = 0
        with self.assertRaises(WriteRefused):
            self.onboard(leave_policy="LP-1")
        with self.assertRaises(ValueError):
            self.onboard(leave_policy="NOPE")
        self.assertEqual(self.client.writes(), [])

    def test_several_policies_and_none_named_skips_the_allocation_with_a_note(self):
        self.client.rows["Leave Policy"] = [{"name": "A", "docstatus": 1},
                                            {"name": "B", "docstatus": 1}]
        out = self.onboard()
        self.assertIsNone(out["leave_policy_assignment"])
        self.assertTrue(any("2 submitted Leave Policies" in w for w in out["warnings"]))
        self.assertFalse([c for c in self.client.writes() if c[1] == "Leave Policy Assignment"])

    def test_refusals_write_nothing(self):
        # already an employee
        self.client.rows["Employee"].append({"name": "E9", "user_id": "sam@x.com",
                                             "status": "Left"})
        with self.assertRaises(WriteRefused) as cm:
            self.onboard()
        self.assertIn("E9", str(cm.exception))
        self.client.rows["Employee"].pop()
        # disabled
        self.client.docs[("User", "sam@x.com")]["enabled"] = 0
        with self.assertRaises(WriteRefused):
            self.onboard()
        self.client.docs[("User", "sam@x.com")]["enabled"] = 1
        # no such user
        with self.assertRaises(ValueError):
            self.onboard(user="ghost@x.com")
        # two companies
        self.client.rows["Company"].append({"name": "Co 2"})
        with self.assertRaises(WriteRefused) as cm:
            self.onboard()
        self.assertIn("2 companies", str(cm.exception))
        self.assertEqual(self.client.writes(), [])

    def test_argument_checks(self):
        for kwargs in ({"date_of_birth": "05/05/1990"}, {"date_of_joining": "2026-13-40"},
                       {"date_of_birth": "2027-01-01"}, {"first_name": " "}, {"gender": ""}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.onboard(**kwargs)
        # born in the future relative to the site's today, though before the joining date
        with self.assertRaises(ValueError):
            self.onboard(date_of_birth="2026-12-01", date_of_joining="2026-12-31")
        self.assertEqual(self.client.writes(), [])

    def test_role_profile_without_the_ess_roles_warns_and_changes_nothing(self):
        self.client.docs[("User", "sam@x.com")]["role_profiles"] = [{"role_profile": "Counter"}]
        self.client.docs[("Role Profile", "Counter")] = {
            "name": "Counter", "roles": [{"role": "Sales User"}, {"role": "Employee"}]}
        out = self.onboard()
        warn = [w for w in out["warnings"] if "Role Profile" in w]
        self.assertEqual(len(warn), 1)
        self.assertIn("'Counter'", warn[0])
        self.assertIn("Employee Self Service", warn[0])
        self.assertFalse([c for c in self.client.writes() if c[1] in ("User", "Role Profile")])

    def test_role_profile_with_both_roles_is_quiet(self):
        self.client.docs[("User", "sam@x.com")]["role_profiles"] = [{"role_profile": "Counter"}]
        self.client.docs[("Role Profile", "Counter")] = {
            "name": "Counter", "roles": [{"role": "Employee"}, {"role": "Employee Self Service"}]}
        self.assertEqual(self.onboard()["warnings"], [])

    def test_a_missing_user_permission_is_reported(self):
        self.client.skip_user_permission = True
        out = self.onboard()
        self.assertTrue(any("no User Permission" in w for w in out["warnings"]))

    def test_a_failed_assignment_leaves_a_loud_warning_and_the_employee(self):
        self.client.fail_submit = {"Leave Policy Assignment"}
        out = self.onboard()
        self.assertTrue(out["employee"])
        self.assertTrue(any(w.startswith("INCOMPLETE") and "assignment" in w
                            for w in out["warnings"]))


class WriteDenylist(Base):
    def test_the_write_denylist_is_honoured(self):
        config_mod._config = None
        with patch.dict(os.environ, {"FRAPPE_WRITE_DENYLIST": "Attendance"}):
            config_mod._config = None
            with patch.object(hr, "_site_today", return_value=TODAY), \
                    self.assertRaises(WriteRefused):
                self.tool("hr_attendance_fix")(
                    action="mark_attendance", employee="E1", reason="r",
                    attendance_date="2026-10-30", status="Present", confirm=True)
        self.assertEqual(self.client.writes(), [])


if __name__ == "__main__":
    unittest.main()
