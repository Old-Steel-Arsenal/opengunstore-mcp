"""Frappe HR tools: staff, attendance / clock-in, shifts and leave.

Scope is what a counter store needs from the ``hrms`` app: who works here, who clocked in,
how many hours, who is on leave, and fixing the odd missed punch. Payroll is out of scope.

Reads (never write):  ``hr_staff``, ``hr_attendance``, ``hr_leave``, ``hr_hours_summary``
(the last is also on the cpa surface: it reads names, days and hours only).
Writes (every one needs ``confirm=true``):  ``hr_leave_decide``, ``hr_attendance_fix``,
``hr_onboard``.

Every tool first reads one hrms doctype and answers "Frappe HR (hrms) is not installed on
this site" when the site has none, instead of leaking a raw 404.

Everything here rides REST list/get/insert/submit plus one hrms method
(``get_leave_details``), so the cpa method allowlist needs no new entry. Checked against
hrms v16.52.0 (ERPNext v16.51):

* Employee Checkin: employee, time (Datetime, required), log_type IN | OUT; ``shift`` is
  filled by the server (``fetch_shift``); a duplicate (employee, time, log_type) is refused.
* Attendance (submittable, naming series): employee, attendance_date, company (required),
  status Present | Absent | On Leave | Half Day | Work From Home, working_hours; the server
  refuses a second non-cancelled row for the same employee/date (``validate_duplicate_record``).
* Leave Application (submittable): status Open | Approved | Rejected | Cancelled;
  ``on_submit`` throws unless the status is Approved or Rejected, and ``validate`` enforces
  the leave balance and the "prevent self approval" setting.
* Leave Policy Assignment (submittable): ``effective_from`` / ``effective_to`` are used as
  given unless ``assignment_based_on`` is set; ``on_submit`` creates the Leave Allocations;
  two submitted assignments may not overlap for one employee.
* Employee (ERPNext): gender, date_of_birth, date_of_joining, company are required;
  ``create_user_permission`` makes ``on_update`` add the Employee + Company User Permissions
  for ``user_id``; ``default_shift`` and ``leave_approver`` are custom fields hrms adds.
* ``hrms.hr.doctype.leave_application.leave_application.get_leave_details(employee, date)``
  is whitelisted and returns the balances per leave type.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ..frappe_client import FrappeAPIError, get_client
from ..safety import (WriteRefused, check_fields_writable, check_write_allowed, require_confirm,
                      require_reason)
from .services import _choice, _given, _refuse_stray

_LEAVE_METHOD = "hrms.hr.doctype.leave_application.leave_application.get_leave_details"
_LA = "Leave Application"
_ATT = "Attendance"
_CHECKIN = "Employee Checkin"

NOT_INSTALLED = "Frappe HR (hrms) is not installed on this site"
MAX_DAYS = 62
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Roles that must see every employee, so their Employee never carries a User Permission.
_ADMIN_ROLES = frozenset({"System Manager", "HR Manager", "HR User"})
# What a user needs to open /hrms, and what a Role Profile sync would strip if the
# profile does not list it (User.populate_role_profile_roles runs on every User save).
_ESS_ROLES = ("Employee", "Employee Self Service")

# hr_attendance_fix: which optional arguments each action takes / needs.
_FIX_ARGS = {"add_checkin": ("time", "log_type"),
             "mark_attendance": ("attendance_date", "status", "working_hours")}
_FIX_REQUIRED = {"add_checkin": ("time", "log_type"),
                 "mark_attendance": ("attendance_date", "status")}
# "On Leave" is deliberately absent: leave belongs to a Leave Application, whose approval
# writes the Attendance itself. A hand-made "On Leave" row would have no leave behind it.
AttendanceStatus = Literal["Present", "Absent", "Half Day", "Work From Home"]

_STATUS_COLUMN = {"Present": "days_present", "Half Day": "days_half_day",
                  "Absent": "days_absent", "On Leave": "days_on_leave",
                  "Work From Home": "days_work_from_home"}


class HrmsMissing(RuntimeError):
    """The site has no hrms app."""


# ---------------------------------------------------------------- helpers

def _day(name: str, value: str | None) -> date:
    v = (value or "").strip()
    if not _DAY_RE.match(v):
        raise ValueError(f"{name} is YYYY-MM-DD (got {value!r}).")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{name} is not a real date (got {value!r}).") from None


def _span(from_date: str, to_date: str) -> tuple[date, date]:
    d0, d1 = _day("from_date", from_date), _day("to_date", to_date)
    if d0 > d1:
        raise ValueError("from_date is after to_date.")
    days = (d1 - d0).days + 1
    if days > MAX_DAYS:
        raise ValueError(f"the range is capped at {MAX_DAYS} days (got {days}) — ask for it in "
                         "pieces, e.g. one month at a time.")
    return d0, d1


def _stamp(value: str | None) -> datetime:
    v = (value or "").strip().replace("T", " ")
    try:
        got = datetime.fromisoformat(v)
    except ValueError:
        got = None
    if got is None or got.tzinfo is not None:  # an offset would be dropped silently
        raise ValueError(f"time is 'YYYY-MM-DD HH:MM[:SS]' in the store's time zone, no "
                         f"offset (got {value!r}).")
    return got


def _need(what: str, **fields) -> None:
    missing = [k for k, v in fields.items() if not (v or "").strip()]
    if missing:
        raise ValueError(f"{what} needs {', '.join(missing)}.")


def _require_hrms(client, doctype: str) -> None:
    """One cheap read of an hrms doctype; a site without hrms answers 404 DoesNotExist."""
    try:
        client.list_documents(doctype, fields=["name"], limit=1)
    except FrappeAPIError as e:
        if e.status == 404 or e.exc_type == "DoesNotExistError":
            raise HrmsMissing(NOT_INSTALLED + ".") from e
        raise


def _rows(client, doctype: str, fields: list[str], filters=None, order_by: str | None = None):
    return client.list_documents(doctype, fields=fields, filters=filters or None, limit=0,
                                 order_by=order_by) or []


def _get(client, doctype: str, name: str) -> dict:
    try:
        return client.get_document(doctype, name) or {}
    except FrappeAPIError as e:
        if e.status == 404:
            raise ValueError(f"{doctype} {name!r} was not found.") from e
        raise


def _site_now(client) -> datetime:
    """Now in the store's time zone (System Settings), naive like the POS's own stamps, so
    an evening in California is not already tomorrow in UTC. lazy: falls back to the MCP
    host's clock when System Settings is unreadable for this user."""
    try:
        tz = (client.get_document("System Settings", "System Settings") or {}).get("time_zone")
        return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    except (FrappeAPIError, KeyError, TypeError, ValueError):  # ZoneInfoNotFoundError is a KeyError
        return datetime.now()


def _site_today(client) -> date:
    return _site_now(client).date()


def _insert(client, doctype: str, values: dict) -> dict:
    check_write_allowed(doctype)
    check_fields_writable(doctype, values)
    return client.create_document(doctype, values) or {}


def _update(client, doctype: str, name: str, values: dict) -> Any:
    check_write_allowed(doctype)
    check_fields_writable(doctype, values)
    return client.update_document(doctype, name, values)


def _submit(client, doctype: str, name: str) -> Any:
    check_write_allowed(doctype)
    return client.submit_document(doctype, name)


def _comment(client, doctype: str, name: str, text: str) -> None:
    _insert(client, "Comment", {"comment_type": "Comment", "reference_doctype": doctype,
                                "reference_name": name, "content": html.escape(text)})


def _employee(client, employee: str) -> dict:
    """The few Employee fields a write needs, never the personal ones."""
    got = _rows(client, "Employee", ["name", "employee_name", "company", "status"],
                [["name", "=", employee]])
    if not got:
        raise ValueError(f"Employee {employee!r} was not found.")
    if got[0].get("status") != "Active":
        raise WriteRefused(f"Employee {employee} is {got[0].get('status')}, not Active.")
    return got[0]


def anniversary(joined: date, year: int) -> date:
    """The joining anniversary in `year`; Feb 29 falls on Feb 28 in a common year."""
    try:
        return joined.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def leave_period(joined: date, today: date) -> tuple[date, date]:
    """The joining-anniversary year that contains `today`, as (effective_from, effective_to).

    effective_from is the most recent anniversary on or before today and effective_to the
    day before the next one, so consecutive years tile with neither a gap nor an overlap
    (hrms refuses overlapping assignments). Joining in the future: the first period starts
    on the joining date. Unlike assignment_based_on = Joining Date, which would pin a
    long-tenured hire's allocation to the year they joined, this is the year they are in."""
    if joined > today:
        start_year = joined.year
        start = joined
    else:
        start_year = today.year
        start = anniversary(joined, start_year)
        if start > today:
            start_year -= 1
            start = anniversary(joined, start_year)
    return start, anniversary(joined, start_year + 1) - timedelta(days=1)


def _role_profile_gap(client, user_doc: dict) -> str | None:
    """A warning when the user's Role Profile(s) lack the roles /hrms needs, else None."""
    names = [r.get("role_profile") for r in user_doc.get("role_profiles") or []
             if r.get("role_profile")]
    legacy = user_doc.get("role_profile_name")
    if legacy and legacy not in names:
        names.append(legacy)
    if not names:
        return None  # roles are set on the user directly; no profile sync to strip them
    granted: set = set()
    for name in names:
        granted |= {r.get("role") for r in _get(client, "Role Profile", name).get("roles") or []}
    missing = [r for r in _ESS_ROLES if r not in granted]
    if not missing:
        return None
    profiles = ", ".join(repr(n) for n in names)
    return (f"Role Profile {profiles} does not include {' and '.join(missing)}. The next "
            "profile sync would strip the role from this user, who then could not use /hrms. "
            "Add the role(s) to the profile in Desk (this tool never edits a Role Profile).")


def _per_employee(employees: list[dict], checkins: list[dict], attendance: list[dict],
                  today: date | None = None) -> dict:
    """Totals per employee. Hours and day counts come from SUBMITTED Attendance rows only;
    `checkin_days_without_attendance` counts days before `today` that have a check-in but no
    submitted Attendance (auto-attendance not processed), so the totals are not silently
    low. Today itself is left out: hrms marks it only after the shift ends."""
    out: dict[str, dict] = {}

    def slot(emp: str, name: str | None = None) -> dict:
        s = out.setdefault(emp, {
            "employee": emp, "employee_name": None, "days_present": 0, "days_half_day": 0,
            "days_absent": 0, "days_on_leave": 0, "days_work_from_home": 0,
            "working_hours": 0.0, "checkin_days": 0, "checkin_days_without_attendance": 0})
        if name and not s["employee_name"]:
            s["employee_name"] = name
        return s

    for e in employees:
        slot(e["name"], e.get("employee_name"))
    covered = set()
    for a in attendance:
        if a.get("docstatus") != 1:
            continue
        s = slot(a["employee"], a.get("employee_name"))
        column = _STATUS_COLUMN.get(a.get("status"))
        if column:
            s[column] += 1
        s["working_hours"] += float(a.get("working_hours") or 0)
        covered.add((a["employee"], str(a["attendance_date"])[:10]))
    days: dict[str, set] = {}
    for c in checkins:
        slot(c["employee"], c.get("employee_name"))
        days.setdefault(c["employee"], set()).add(str(c["time"])[:10])
    for emp, ds in days.items():
        out[emp]["checkin_days"] = len(ds)
        out[emp]["checkin_days_without_attendance"] = sum(
            1 for d in ds if (emp, d) not in covered and (today is None or d < str(today)))
    for s in out.values():
        s["working_hours"] = round(s["working_hours"], 2)
    return out


def _window(client, d0: date, d1: date, employee: str | None = None):
    """Check-ins and non-cancelled Attendance rows for the days d0..d1 (inclusive).
    lazy: reads the whole window in one list; a store-sized staff over <= 62 days is a few
    thousand rows. Page it by employee if a site ever outgrows that."""
    by_emp = [["employee", "=", employee]] if employee else []
    checkins = _rows(
        client, _CHECKIN, ["employee", "employee_name", "time", "log_type"],
        [["time", ">=", f"{d0} 00:00:00"], ["time", "<=", f"{d1} 23:59:59"], *by_emp],
        order_by="time asc")
    attendance = _rows(
        client, _ATT,
        ["name", "employee", "employee_name", "attendance_date", "status", "working_hours",
         "in_time", "out_time", "docstatus"],
        [["attendance_date", ">=", str(d0)], ["attendance_date", "<=", str(d1)],
         ["docstatus", "<", 2], *by_emp],
        order_by="attendance_date asc")
    return checkins, attendance


def register(mcp: Any) -> None:
    # ===================================================== reads

    @mcp.tool()
    def hr_staff() -> Any:
        """The active staff in Frappe HR. Read-only. Per employee: name (the Employee id),
        employee_name, user_id (the login), default_shift, holiday_list, leave_approver,
        restricted_to_employees (the Employee ids a User Permission limits this login to:
        [] means they see everyone, [their own id] means they see only themselves; null when
        the caller cannot read User Permission, which needs System Manager) and
        last_checkin_today {time, log_type} when they have clocked in today (store time zone).

        Personal data (birth date, address, bank, passport, health) is never returned.
        Needs HR read access."""
        client = get_client()
        _require_hrms(client, _CHECKIN)
        staff = _rows(client, "Employee",
                      ["name", "employee_name", "user_id", "default_shift", "holiday_list",
                       "leave_approver"],
                      [["status", "=", "Active"]], order_by="employee_name asc")
        users = sorted({e["user_id"] for e in staff if e.get("user_id")})
        restricted: dict[str, list[str]] | None = {}
        if users:
            try:
                for p in _rows(client, "User Permission", ["user", "for_value"],
                               [["allow", "=", "Employee"], ["user", "in", users]]):
                    restricted.setdefault(p["user"], []).append(p["for_value"])
            except FrappeAPIError as e:
                if e.status != 403:
                    raise
                restricted = None  # only System Manager reads User Permission
        last: dict[str, dict] = {}
        if staff:
            today = _site_today(client)
            for c in _rows(client, _CHECKIN, ["employee", "time", "log_type"],
                           [["time", ">=", f"{today} 00:00:00"],
                            ["employee", "in", [e["name"] for e in staff]]],
                           order_by="time desc"):
                last.setdefault(c["employee"], {"time": c["time"], "log_type": c.get("log_type")})
        return [{**e, "restricted_to_employees": (None if restricted is None
                                                  else restricted.get(e.get("user_id"), [])),
                 "last_checkin_today": last.get(e["name"])} for e in staff]

    @mcp.tool()
    def hr_attendance(from_date: str, to_date: str, employee: str | None = None) -> Any:
        """Check-ins and Attendance per employee per day, with totals. Read-only.
        Dates are YYYY-MM-DD; the range is capped at 62 days (ask month by month).
        employee limits it to one Employee id.

        Returns {from_date, to_date, employees: [{employee, employee_name, totals, days}]}.
        days: only days with something on them, each {date, checkins: [{time, log_type}],
        attendance: [{name, status, working_hours, in_time, out_time, submitted}]}; a draft
        Attendance is shown (submitted=false) but not counted. totals: days_present,
        days_half_day, days_absent, days_on_leave, days_work_from_home, working_hours (from
        submitted Attendance), checkin_days and checkin_days_without_attendance (days with a
        check-in but no submitted Attendance yet: auto-attendance has not run for them).
        Needs HR read access."""
        d0, d1 = _span(from_date, to_date)
        client = get_client()
        _require_hrms(client, _CHECKIN)
        checkins, attendance = _window(client, d0, d1, (employee or "").strip() or None)
        totals = _per_employee([], checkins, attendance, _site_today(client))
        days: dict[str, dict[str, dict]] = {}

        def day(emp: str, stamp) -> dict:
            return days.setdefault(emp, {}).setdefault(
                str(stamp)[:10], {"date": str(stamp)[:10], "checkins": [], "attendance": []})

        for c in checkins:
            day(c["employee"], c["time"])["checkins"].append(
                {"time": c["time"], "log_type": c.get("log_type")})
        for a in attendance:
            day(a["employee"], a["attendance_date"])["attendance"].append({
                "name": a["name"], "status": a.get("status"),
                "working_hours": a.get("working_hours"), "in_time": a.get("in_time"),
                "out_time": a.get("out_time"), "submitted": a.get("docstatus") == 1})
        return {"from_date": str(d0), "to_date": str(d1), "employees": [
            {"employee": emp, "employee_name": t["employee_name"],
             "totals": {k: v for k, v in t.items() if k not in ("employee", "employee_name")},
             "days": [days[emp][k] for k in sorted(days.get(emp, {}))]}
            for emp, t in sorted(totals.items(), key=lambda kv: kv[1]["employee_name"] or kv[0])]}

    @mcp.tool()
    def hr_leave(view: Literal["balances", "pending"], employee: str | None = None) -> Any:
        """Leave, two views. Read-only. employee limits either view to one Employee id.

        * balances — per active employee, per leave type valid today: {allocated, taken,
          pending_approval, remaining, expired}, as hrms computes them (get_leave_details).
          One POS call per employee. Returns {as_of, employees: [{employee, employee_name,
          balances}]}.
        * pending — Leave Applications awaiting a decision (status Open): name, employee,
          employee_name, leave_type, from_date, to_date, total_leave_days, half_day,
          leave_approver, posting_date. The employee's own reason text is not returned.
          Decide one with hr_leave_decide.

        Needs HR read access."""
        view = _choice("view", view, ("balances", "pending"))
        emp = (employee or "").strip() or None
        client = get_client()
        _require_hrms(client, _LA)
        if view == "pending":
            return _rows(
                client, _LA,
                ["name", "employee", "employee_name", "leave_type", "from_date", "to_date",
                 "total_leave_days", "half_day", "leave_approver", "leave_approver_name",
                 "posting_date"],
                [["status", "=", "Open"], ["docstatus", "=", 0],
                 *([["employee", "=", emp]] if emp else [])],
                order_by="from_date asc")
        # lazy: one get_leave_details call per employee; fine at a store's headcount.
        today = _site_today(client)
        staff = _rows(client, "Employee", ["name", "employee_name"],
                      [["status", "=", "Active"], *([["name", "=", emp]] if emp else [])],
                      order_by="employee_name asc")
        out = []
        for e in staff:
            got = client.call_method(_LEAVE_METHOD, {"employee": e["name"], "date": str(today)})
            alloc = (got or {}).get("leave_allocation") or {}
            out.append({"employee": e["name"], "employee_name": e.get("employee_name"),
                        "balances": {lt: {"allocated": v.get("total_leaves"),
                                          "taken": v.get("leaves_taken"),
                                          "pending_approval": v.get("leaves_pending_approval"),
                                          "remaining": v.get("remaining_leaves"),
                                          "expired": v.get("expired_leaves")}
                                     for lt, v in alloc.items()}})
        return {"as_of": str(today), "employees": out}

    @mcp.tool()
    def hr_hours_summary(from_date: str, to_date: str) -> Any:
        """Hours worked per employee over a period, for payroll hand-off. Read-only; also
        available in cpa mode (names, days and hours only, nothing personal).
        Dates are YYYY-MM-DD; the range is capped at 62 days.

        Per employee: days_present, days_half_day, days_absent, days_on_leave,
        days_work_from_home (one per submitted Attendance row) and working_hours (their sum).
        checkin_days_without_attendance counts days that have check-ins but no submitted
        Attendance yet (auto-attendance not processed): when it is above 0 the totals for
        that employee are LOW, so process or fix those days first (hr_attendance shows which).
        Active employees with nothing in the period are listed with zeros.
        Needs read access to Employee, Employee Checkin and Attendance."""
        d0, d1 = _span(from_date, to_date)
        client = get_client()
        _require_hrms(client, _ATT)
        staff = _rows(client, "Employee", ["name", "employee_name"], [["status", "=", "Active"]])
        checkins, attendance = _window(client, d0, d1)
        totals = _per_employee(staff, checkins, attendance, _site_today(client))
        rows = sorted(totals.values(), key=lambda t: t["employee_name"] or t["employee"])
        return {"from_date": str(d0), "to_date": str(d1), "employees": rows,
                "total_working_hours": round(sum(t["working_hours"] for t in rows), 2),
                "employees_with_unprocessed_days": sum(
                    1 for t in rows if t["checkin_days_without_attendance"])}

    # ===================================================== writes

    @mcp.tool()
    def hr_leave_decide(
        leave_application: str, decision: Literal["approve", "reject"],
        reason: str | None = None, confirm: bool = False,
    ) -> Any:
        """Approve or reject an Open Leave Application: sets the status and submits it
        (approval allocates the days and writes the Attendance for them). Refused unless the
        application is Open. A reject needs a reason; an approve may give one. hrms has no
        decision-reason field, so the reason is recorded as a Comment on the application.

        If the submit fails (for example the balance is too low) the status is put back to
        Open and the error is returned. hrms may refuse an approval by the employee's own
        login when 'prevent self approval' is on. Requires confirm=true."""
        decision = _choice("decision", decision, ("approve", "reject"))
        note = require_reason("hr_leave_decide reject", reason) if decision == "reject" \
            else (reason or "").strip()
        require_confirm(f"hr_leave_decide {decision} {leave_application}", confirm)
        client = get_client()
        _require_hrms(client, _LA)
        doc = _get(client, _LA, leave_application.strip())
        if doc.get("docstatus") != 0 or doc.get("status") != "Open":
            raise WriteRefused(
                f"Refused: {_LA} {leave_application} is {doc.get('status')} (docstatus "
                f"{doc.get('docstatus')}); only an Open application can be decided here. "
                "Change a decided one in Desk (cancel, then amend).")
        status = "Approved" if decision == "approve" else "Rejected"
        _update(client, _LA, doc["name"], {"status": status})
        try:
            _submit(client, _LA, doc["name"])
        except FrappeAPIError as e:
            try:
                _update(client, _LA, doc["name"], {"status": "Open"})
                tail = "The application was put back to Open."
            except FrappeAPIError:
                tail = (f"Could not put it back: it is still saved as {status} but not "
                        "submitted — set it to Open in Desk.")
            raise RuntimeError(f"{_LA} {doc['name']} was not {status.lower()}: {e}. {tail}") from e
        out = {"leave_application": doc["name"], "status": status,
               "employee": doc.get("employee"), "employee_name": doc.get("employee_name"),
               "leave_type": doc.get("leave_type"), "from_date": doc.get("from_date"),
               "to_date": doc.get("to_date"), "total_leave_days": doc.get("total_leave_days"),
               "comment_recorded": False}
        if note:
            try:
                _comment(client, _LA, doc["name"], f"{status} via MCP: {note}")
                out["comment_recorded"] = True
            except FrappeAPIError as e:
                out["warning"] = f"The decision is saved but the reason could not be recorded: {e}"
        return out

    @mcp.tool()
    def hr_attendance_fix(
        action: Literal["add_checkin", "mark_attendance"], employee: str, reason: str,
        time: str | None = None, log_type: Literal["IN", "OUT"] | None = None,
        attendance_date: str | None = None, status: AttendanceStatus | None = None,
        working_hours: float | None = None, confirm: bool = False,
    ) -> Any:
        """Repair the attendance record. employee is the Employee id; reason is required
        and is recorded as a Comment on the created document. action:

        * add_checkin — a missed punch: needs time ('YYYY-MM-DD HH:MM[:SS]', store time
          zone, not in the future) and log_type IN | OUT. Creates an Employee Checkin; the
          shift is matched by hrms and the day's Attendance is made by hrms's own
          auto-attendance run, not here.
        * mark_attendance — needs attendance_date (YYYY-MM-DD, not in the future) and status
          (Present | Absent | Half Day | Work From Home); working_hours is optional. Creates
          AND submits an Attendance. Refused if the employee already has a non-cancelled
          Attendance that day: a submitted one is corrected in Desk (open it, Cancel, then
          Amend), a draft one is submitted or deleted there. Leave is not marked here: approve
          the Leave Application and hrms writes the Attendance.

        Arguments that do not belong to the chosen action are refused. Requires
        confirm=true."""
        action = _choice("action", action, _FIX_ARGS)
        _refuse_stray(f"action={action}", _FIX_ARGS[action], {
            "time": time, "log_type": log_type, "attendance_date": attendance_date,
            "status": status, "working_hours": working_hours})
        _need(f"hr_attendance_fix {action}", employee=employee)
        passed = {"time": time, "log_type": log_type, "attendance_date": attendance_date,
                  "status": status}
        missing = [k for k in _FIX_REQUIRED[action] if not _given(passed[k])]
        if missing:
            raise ValueError(f"action={action} needs {', '.join(missing)}.")
        reason = require_reason(f"hr_attendance_fix {action}", reason)
        if action == "add_checkin":
            when = _stamp(time)
            log_type = _choice("log_type", log_type, ("IN", "OUT"))
        else:
            when = _day("attendance_date", attendance_date)
            status = _choice("status", status, ("Present", "Absent", "Half Day", "Work From Home"))
            if working_hours is not None and not 0 <= working_hours <= 24:
                raise ValueError("working_hours is between 0 and 24.")
        require_confirm(f"hr_attendance_fix {action} {employee}", confirm)
        client = get_client()
        _require_hrms(client, _ATT)
        emp = _employee(client, employee.strip())
        now = _site_now(client)
        if (when > now) if isinstance(when, datetime) else (when > now.date()):
            raise ValueError(f"{when} is in the future (it is now {now:%Y-%m-%d %H:%M}).")

        if action == "add_checkin":
            doc = _insert(client, _CHECKIN, {
                "employee": emp["name"], "time": when.strftime("%Y-%m-%d %H:%M:%S"),
                "log_type": log_type})
            out = {"employee_checkin": doc["name"], "employee": emp["name"],
                   "employee_name": emp.get("employee_name"), "time": doc.get("time"),
                   "log_type": log_type}
            try:
                _comment(client, _CHECKIN, doc["name"], f"Added via MCP: {reason}")
            except (FrappeAPIError, WriteRefused) as e:
                out["warning"] = f"The check-in is saved but the reason could not be recorded: {e}"
            return out

        clash = _rows(client, _ATT, ["name", "status", "docstatus"],
                      [["employee", "=", emp["name"]], ["attendance_date", "=", str(when)],
                       ["docstatus", "<", 2]])
        if clash:
            c = clash[0]
            kind = "a submitted" if c.get("docstatus") == 1 else "a draft"
            fix = ("open it in Desk, Cancel it, then Amend it" if c.get("docstatus") == 1
                   else "submit or delete the draft in Desk")
            raise WriteRefused(
                f"Refused: {emp['name']} already has {kind} Attendance on {when} ({c['name']}, "
                f"{c.get('status')}). To correct it, {fix}.")
        values: dict[str, Any] = {"employee": emp["name"], "attendance_date": str(when),
                                  "status": status, "company": emp["company"]}
        if working_hours is not None:
            values["working_hours"] = working_hours
        doc = _insert(client, _ATT, values)
        name = doc["name"]
        try:
            _comment(client, _ATT, name, f"Marked via MCP: {reason}")
            _submit(client, _ATT, name)
        except FrappeAPIError as e:
            try:
                check_write_allowed(_ATT)
                client.delete_document(_ATT, name)
                tail = "The draft Attendance was removed."
            except (FrappeAPIError, WriteRefused):
                tail = f"The draft {name} is still there — submit or delete it in Desk."
            raise RuntimeError(f"Attendance for {emp['name']} on {when} was not marked: "
                               f"{e}. {tail}") from e
        return {"attendance": name, "employee": emp["name"],
                "employee_name": emp.get("employee_name"), "attendance_date": str(when),
                "status": status, "working_hours": working_hours, "submitted": True}

    @mcp.tool()
    def hr_onboard(
        user: str, first_name: str, last_name: str, gender: str, date_of_birth: str,
        date_of_joining: str, leave_approver: str | None = None, shift: str | None = None,
        holiday_list: str | None = None, leave_policy: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Make an existing login an employee, in one step. user is the User id (email);
        gender is a Gender name (Male, Female, ...); dates are YYYY-MM-DD. Refused if the
        user is already linked to an Employee, is disabled, or the site has more than one
        Company (the employee goes in the single company).

        Creates the Employee linked to the user (status Active) with:
        * create_user_permission = 0 when the user holds System Manager, HR Manager or HR User
          (admins and approvers must see every employee), else 1: staff are then limited to
          their own Employee and Company, the usual self-service setup.
        * holiday_list = the one given, else the company default; default_shift = the one
          given, else the only Shift Type if there is exactly one; leave_approver as given.
        * A Leave Policy Assignment (created and submitted, so the allocations exist) for
          leave_policy, or for the only submitted Leave Policy if there is exactly one.
          Its period is the joining-anniversary year that contains today: from the latest
          anniversary of date_of_joining on or before today (a future joining date starts
          on that date) to the day before the next one. Feb 29 anniversaries fall on Feb 28.

        Nothing is changed on the User or its Role Profile. If the user's Role Profile lacks
        Employee / Employee Self Service, the result carries a warning naming the profile:
        a later profile sync would strip the role and the employee could not use /hrms.
        Returns the created names plus `warnings` (a step after the Employee was created
        that failed is listed there as INCOMPLETE; the Employee exists either way).
        Needs a System Manager login (it reads the target User). Requires confirm=true."""
        _need("hr_onboard", user=user, first_name=first_name, last_name=last_name,
              gender=gender)
        user = user.strip()
        born = _day("date_of_birth", date_of_birth)
        joined = _day("date_of_joining", date_of_joining)
        if born >= joined:
            raise ValueError("date_of_birth must be before date_of_joining.")
        require_confirm(f"hr_onboard {user}", confirm)
        client = get_client()
        _require_hrms(client, "Leave Policy Assignment")
        today = _site_today(client)
        if born >= today:
            raise ValueError("date_of_birth is in the future.")

        try:
            user_doc = _get(client, "User", user)
        except FrappeAPIError as e:
            if e.status != 403:
                raise
            raise WriteRefused("Refused: reading another user's login needs System Manager; "
                               "onboard from a System Manager login, or in Desk.") from e
        if not int(user_doc.get("enabled") or 0):
            raise WriteRefused(f"Refused: user {user} is disabled.")
        linked = _rows(client, "Employee", ["name"], [["user_id", "=", user]])
        if linked:
            raise WriteRefused(f"Refused: user {user} is already employee {linked[0]['name']}.")
        roles = {r.get("role") for r in user_doc.get("roles") or []}
        sees_everyone = user == "Administrator" or bool(roles & _ADMIN_ROLES)

        companies = _rows(client, "Company", ["name", "default_holiday_list"])
        if len(companies) != 1:
            raise WriteRefused(
                f"Refused: this site has {len(companies)} companies; onboarding picks the "
                "company only when there is exactly one. Create the Employee in Desk.")
        company = companies[0]
        holiday = (holiday_list or "").strip() or company.get("default_holiday_list")
        shift_name = (shift or "").strip() or None
        if not shift_name:
            shifts = _rows(client, "Shift Type", ["name"])
            shift_name = shifts[0]["name"] if len(shifts) == 1 else None
        policy = (leave_policy or "").strip() or None
        warnings: list[str] = []
        if policy:
            if _get(client, "Leave Policy", policy).get("docstatus") != 1:
                raise WriteRefused(f"Refused: Leave Policy {policy} is not submitted.")
        else:
            policies = _rows(client, "Leave Policy", ["name"], [["docstatus", "=", 1]])
            if len(policies) == 1:
                policy = policies[0]["name"]
            else:
                warnings.append(
                    f"No leave allocation was made: {len(policies)} submitted Leave Policies "
                    "exist and none was named. Pass leave_policy, or assign one in Desk.")

        values: dict[str, Any] = {
            "first_name": first_name.strip(), "last_name": last_name.strip(),
            "gender": gender.strip(), "date_of_birth": str(born), "date_of_joining": str(joined),
            "company": company["name"], "status": "Active", "user_id": user,
            "create_user_permission": 0 if sees_everyone else 1}
        for key, value in (("holiday_list", holiday), ("default_shift", shift_name),
                           ("leave_approver", (leave_approver or "").strip() or None)):
            if value:
                values[key] = value
        emp = _insert(client, "Employee", values)
        out: dict[str, Any] = {
            "employee": emp["name"], "employee_name": emp.get("employee_name"), "user": user,
            "company": company["name"], "create_user_permission": values["create_user_permission"],
            "holiday_list": holiday, "default_shift": shift_name,
            "leave_approver": values.get("leave_approver"), "leave_policy_assignment": None}

        if policy:
            try:
                start, end = leave_period(joined, today)
                lpa = _insert(client, "Leave Policy Assignment", {
                    "employee": emp["name"], "leave_policy": policy, "company": company["name"],
                    "effective_from": str(start), "effective_to": str(end)})
                _submit(client, "Leave Policy Assignment", lpa["name"])
                out.update({"leave_policy_assignment": lpa["name"], "leave_policy": policy,
                            "effective_from": str(start), "effective_to": str(end)})
            except FrappeAPIError as e:
                warnings.append(f"INCOMPLETE: Employee {emp['name']} was created but the leave "
                                f"policy assignment failed: {e}. Assign it in Desk.")
        try:
            if values["create_user_permission"] and not _rows(
                    client, "User Permission", ["name"],
                    [["allow", "=", "Employee"], ["for_value", "=", emp["name"]],
                     ["user", "=", user]]):
                warnings.append(
                    f"INCOMPLETE: no User Permission was created for {user} on {emp['name']} "
                    "(the API user may lack the right); this login can see every employee.")
            gap = _role_profile_gap(client, user_doc)
            if gap:
                warnings.append(gap)
        except FrappeAPIError as e:
            warnings.append(f"Could not check the user's permission and role profile: {e}")
        out["warnings"] = warnings
        return out
