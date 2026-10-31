"""Set-level fact: NO tool on this server can write GunBroker Settings'
environment, credential or money fields.

This file exists because the r1 fix was correct and still wrong. The guard was
attached to `update_settings`, one tool out of the whole write face, and
`frappe_update_document` walked straight past it with sandbox_mode. Every test
written at the time asserted the guard function, or that one tool — none asked
"can this doctype still be written from anywhere on this server", which is the
only question whose answer anyone actually cares about.

So the assertions here are deliberately about the SURFACE, not about a function:

* `EveryWritePath` — drive each generic write tool with a forbidden payload and
  require a refusal, with a client that trips if anything reaches the wire.
* `NoNewToolCanForget` — a static check over every registered tool: anything
  that can write a document body must call check_fields_writable. This is the
  part that catches the NEXT tool, which is how the last gap got in.
"""
from __future__ import annotations

import ast
import inspect
import os
import textwrap
import unittest
from unittest.mock import patch

from gunstore_mcp import config as config_mod
from gunstore_mcp import safety, server
from gunstore_mcp.frappe_client import get_client
from gunstore_mcp.safety import MCP_NEVER_WRITES, WriteRefused
from gunstore_mcp.tools import (compliance, curated, customers, distributor, generic, hr, orders,
                                range_board, reports, services, shopfloor)

DT = "GunBroker Settings"
FORBIDDEN = sorted(MCP_NEVER_WRITES[DT])

# One payload per forbidden key, plus the mixed case: a real caller sets an
# innocuous field in the same breath, and a strip-instead-of-refuse would land
# the innocuous half and report success.
PAYLOADS = [{k: "x"} for k in FORBIDDEN] + [{"page_size": 50, "sandbox_mode": 0}]


class TripwireClient:
    """Fails loudly if a forbidden key ever reaches the HTTP boundary."""

    def __init__(self):
        self.calls = []

    def _check(self, doctype, values):
        if doctype in MCP_NEVER_WRITES:
            leaked = sorted(set(values) & MCP_NEVER_WRITES[doctype])
            if leaked:
                raise AssertionError(
                    f"{leaked} reached the wire on {doctype} — a guard was skipped")

    def create_document(self, doctype, values):
        self._check(doctype, values)
        self.calls.append(("POST", doctype, values))
        return {"ok": True}

    def update_document(self, doctype, name, values):
        self._check(doctype, values)
        self.calls.append(("PUT", doctype, name, values))
        return {"ok": True}

    def call_method(self, method, kwargs=None):
        self.calls.append(("METHOD", method, kwargs))
        return {"ok": True}

    def describe_doctype(self, doctype):
        # The real Password fields, so strip_passwords behaves as in production.
        return [{"fieldname": f, "fieldtype": "Password"}
                for f in ("password", "dev_key", "sandbox_dev_key")]


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


# check_write_allowed reads the config, so the write paths need credentials to
# exist. Throwaway values pointed at an unroutable host: every client call in
# this file goes to TripwireClient, so nothing is ever sent anywhere.
FAKE_CREDS = {
    "FRAPPE_BASE_URL": "http://never-writes-probe.invalid",
    "FRAPPE_API_KEY": "dummy",
    "FRAPPE_API_SECRET": "dummy",
}


def _creds():
    """Install the throwaway config and force it to be rebuilt from them."""
    ctx = patch.dict(os.environ, FAKE_CREDS)
    ctx.start()
    config_mod._config = None
    return ctx


# The modules server.register_tools() registers, in its order. Kept as a tuple
# rather than inlined so test_the_surface_covers_every_module_the_server_registers
# can compare it against server.py itself: this file's claim to describe "the whole
# registered surface" is only as good as this list, and it was wrong once already
# (generic + curated only, while the server registers four — an unguarded writer
# dropped into distributor.py went unnoticed by the static check below).
_SURFACE_MODULES = (generic, curated, distributor, reports, shopfloor, range_board, orders,
                    customers, services, compliance, hr)


def _surface():
    """Every tool a full-mode server registers, BOTH action gates open — the
    widest surface this server can ever present.

    Both gates are cleared out of the ambient environment before being set, so a
    GUNSTORE_MCP_* in the developer's shell cannot decide how wide "widest" is:
    with distributor's gate left to the ambient value, "widest" was whatever the
    developer happened to have exported."""
    mcp = FakeMCP()
    env = {k: v for k, v in os.environ.items()
           if k not in (curated.GUNBROKER_ACTIONS_ENV, distributor.ACTIONS_ENV, range_board.RANGE_ENV)}
    env[curated.GUNBROKER_ACTIONS_ENV] = "1"
    env[distributor.ACTIONS_ENV] = "1"
    env[range_board.RANGE_ENV] = "1"
    with patch.dict(os.environ, env, clear=True), \
            patch.object(curated, "_load_env", lambda: None), \
            patch.object(distributor, "_load_env", lambda: None), \
            patch.object(range_board, "_load_env", lambda: None):
        for module in _SURFACE_MODULES:
            module.register(mcp)
    return mcp.tools


def _unguarded_probe(mcp):
    """A write tool with no guard — the thing the static check must catch."""
    @mcp.tool()
    def probe_unguarded_writer(doctype: str, values: dict):
        return get_client().update_document(doctype, "x", values)


class EveryWritePath(unittest.TestCase):
    def setUp(self):
        ctx = _creds()
        self.addCleanup(lambda: (ctx.stop(), setattr(config_mod, "_config", None)))
        self.tools = _surface()
        self.client = TripwireClient()
        safety._pw_cache.clear()
        for mod in (generic, curated, safety):
            p = patch.object(mod, "get_client", return_value=self.client)
            p.start()
            self.addCleanup(p.stop)

    def _refuses(self, tool, build):
        for values in PAYLOADS:
            with self.subTest(tool=tool, values=values):
                before = len(self.client.calls)
                with self.assertRaises(WriteRefused):
                    self.tools[tool](*build(values))
                self.assertEqual(len(self.client.calls), before,
                    "refused call still reached the client")

    def test_frappe_update_document(self):
        self._refuses("frappe_update_document", lambda v: (DT, DT, v))

    def test_frappe_create_document(self):
        self._refuses("frappe_create_document", lambda v: (DT, v))

    def test_update_settings(self):
        """Regression: the one path r1 did cover."""
        self._refuses("update_settings", lambda v: ("gunbroker", v))

    def test_frappe_run_method_field_setters(self):
        """run_method reaches any dotted path, so there is no document body to
        inspect — the rule has to be enforced on the field name."""
        for method in ("frappe.db.set_single_value", "frappe.client.set_value",
                "ffl_integrations.gunbroker.settings.set_value",
                "some.app.api.db_set_field"):
            for kwargs in (
                {"doctype": DT, "fieldname": "sandbox_mode", "value": 0},
                {"doctype": DT, "name": DT, "fieldname": "dev_key", "value": "x"},
                {"doctype": DT, "fieldname": {"sandbox_mode": 0, "page_size": 5}},
            ):
                with self.subTest(method=method, kwargs=kwargs):
                    before = len(self.client.calls)
                    with self.assertRaises(WriteRefused):
                        self.tools["frappe_run_method"](method, kwargs)
                    self.assertEqual(len(self.client.calls), before)

    def test_blocked_generic_mutators_are_still_refused(self):
        """Regression for the layer that was already there: Frappe's own
        document mutators are refused wholesale by _BLOCKED_GENERIC_METHODS,
        independently of any field-name rule."""
        for method in sorted(generic._BLOCKED_GENERIC_METHODS):
            with self.subTest(method=method):
                before = len(self.client.calls)
                with self.assertRaises(WriteRefused):
                    self.tools["frappe_run_method"](method, {"doctype": "Item"})
                self.assertEqual(len(self.client.calls), before)

    def test_the_blocked_list_does_not_cover_the_db_setters(self):
        """Why check_method_call_writable exists as well.

        _BLOCKED_GENERIC_METHODS is entirely frappe.client.* — it does NOT
        contain frappe.db.set_single_value or frappe.db.set_value, which write
        a Single's field just as effectively. Reviewed as "already blocked" once;
        it is not, and this asserts the gap so the next reader can see it rather
        than re-deriving it. If these names ever DO join the blocked list, this
        test fails and the comment above it should be revisited — it will not
        silently pretend to guard something."""
        for method in ("frappe.db.set_single_value", "frappe.db.set_value"):
            with self.subTest(method=method):
                self.assertNotIn(method, generic._BLOCKED_GENERIC_METHODS)
        # ...and they are refused anyway, by the field-name rule.
        for method in ("frappe.db.set_single_value", "frappe.db.set_value"):
            with self.subTest(method=method, via="field name"):
                with self.assertRaises(WriteRefused):
                    self.tools["frappe_run_method"](
                        method, {"doctype": DT, "fieldname": "sandbox_mode", "value": 0})

    def test_ordinary_writes_still_work(self):
        """The guard must not become a blanket refusal on the doctype."""
        self.tools["frappe_update_document"](DT, DT, {"page_size": 50})
        self.assertEqual(self.client.calls[-1],
            ("PUT", DT, DT, {"page_size": 50}))
        self.tools["frappe_run_method"]("osa_dropship.listing_sync.sync_listings")
        self.tools["frappe_run_method"](
            "some.app.get_value", {"doctype": DT, "fieldname": "sandbox_mode"})

    def test_other_doctypes_are_not_narrowed(self):
        self.tools["frappe_update_document"]("WooCommerce Settings", "WooCommerce Settings",
            {"enabled": 1})
        self.assertEqual(self.client.calls[-1][1], "WooCommerce Settings")


WRITER_CALLS = ("create_document", "update_document")


def _called(fn):
    """Names of functions/methods this tool calls, in source order.

    Parsed, not grepped: the guard call sits next to a comment explaining
    that it must precede strip_passwords, and a substring search finds the
    comment. Order assertions on text would be measuring prose."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name:
                out.append((node.lineno, node.col_offset, name))
    return [n for _, _, n in sorted(out)]


def _writers(tools=None):
    """{tool name: calls it makes} for every tool that writes a document body."""
    found = {}
    for name, fn in sorted((tools if tools is not None else _surface()).items()):
        try:
            calls = _called(fn)
        except OSError:  # pragma: no cover
            continue
        if any(w in calls for w in WRITER_CALLS):
            found[name] = calls
    return found


def _unguarded(tools=None):
    """The tools that write a document body without calling the guard."""
    return [n for n, calls in _writers(tools).items()
            if "check_fields_writable" not in calls]


class NoNewToolCanForget(unittest.TestCase):
    """Static: any tool that can write a document body must call the guard.

    A behavioural test only covers the tools somebody remembered to list. This
    one reads the source of every registered tool, so a new write tool fails
    here on the day it is written rather than in a review two rounds later."""

    def _writers(self):
        return _writers()

    def test_every_document_writer_calls_check_fields_writable(self):
        missing = _unguarded()
        self.assertEqual(missing, [], (
            "these tools write a document body without calling "
            "check_fields_writable(doctype, values) — add it BEFORE "
            "strip_passwords, or the refusal degrades into a silent strip"))

    def test_the_check_runs_before_strip_passwords(self):
        """Order is load-bearing: after strip_passwords the credential keys are
        gone, so the call looks clean and the refusal never fires."""
        for name, calls in self._writers().items():
            if "strip_passwords" not in calls:
                continue
            with self.subTest(tool=name):
                self.assertLess(calls.index("check_fields_writable"),
                    calls.index("strip_passwords"),
                    f"{name} strips credentials before checking them")

    def test_run_method_is_guarded_too(self):
        calls = _called(_surface()["frappe_run_method"])
        self.assertIn("check_method_call_writable", calls)

    def test_this_test_is_actually_looking_at_something(self):
        """A guard that inspects an empty set proves nothing — and if the tools
        are ever renamed or restructured so nothing matches, that silence must
        fail rather than pass."""
        writers = self._writers()
        self.assertGreaterEqual(len(writers), 4, sorted(writers))
        for expected in ("frappe_create_document", "frappe_update_document",
                "update_settings", "set_serial_title"):
            self.assertIn(expected, writers)


class TheSurfaceIsTheWholeSurface(unittest.TestCase):
    """m-1 (review-pr4a-r3): the static check above is only as wide as _surface().

    It used to enumerate two of the four modules server.py registers, so the same
    unguarded writer was caught in curated.py and waved through in distributor.py.
    Nothing was exploitable — neither distributor.py nor reports.py writes a
    document body today — but a guard whose docstring says "every tool" and whose
    body reads half of them is a claim that will be believed and is not true.

    Two assertions, because they fail differently: one pins the module list to
    server.py so a fifth module cannot be added without this file following, and
    one mutates each module in turn to show the check would actually name the
    offender wherever it is defined."""

    @staticmethod
    def _modules_server_registers():
        """The `X.register(...)` calls in server.register_tools, read from source."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(server.register_tools)))
        return sorted({
            node.func.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register" and isinstance(node.func.value, ast.Name)
        })

    def test_the_surface_covers_every_module_the_server_registers(self):
        expected = self._modules_server_registers()
        self.assertTrue(expected, "no X.register(...) found — server.py was restructured")
        self.assertEqual(
            sorted(m.__name__.rsplit(".", 1)[-1] for m in _SURFACE_MODULES), expected,
            "_SURFACE_MODULES no longer matches server.register_tools — the "
            "never-writes check would silently stop covering part of the server")

    def test_the_widest_surface_really_contains_every_bucket(self):
        """Reading the module list off server.py proves nothing if registering them
        yields nothing; name one tool that can only come from each bucket, and both
        opt-in sets, so an unset gate cannot shrink this surface unnoticed."""
        tools = _surface()
        for name in ("frappe_run_method", "update_settings", "distributor_route_queue",
                     "sales_report", "inventory_count_finalize", "gb_push_serial",
                     "distributor_confirm_order", "range_book", "create_order",
                     "customer_manage", "gunsmith_job_action", "correct_firearm",
                     "gb_update_listing", "list_pos_actions", "hr_onboard"):
            self.assertIn(name, tools)

    def test_an_unguarded_writer_is_named_no_matter_which_module_defines_it(self):
        """The mutation. Drop the same guard-less write tool into each registered
        module and require test_every_document_writer_calls_check_fields_writable's
        own predicate to name it. Before the fix this passed for generic/curated and
        FAILED for distributor/reports — measured, not assumed."""
        self.assertEqual(_unguarded(), [], "baseline is not clean")
        for module in _SURFACE_MODULES:
            with self.subTest(module=module.__name__):
                real = module.register

                def patched(mcp, _real=real):
                    _real(mcp)
                    _unguarded_probe(mcp)

                with patch.object(module, "register", patched):
                    self.assertIn("probe_unguarded_writer", _unguarded())


if __name__ == "__main__":
    unittest.main()
