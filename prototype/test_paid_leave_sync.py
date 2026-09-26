import ast
import builtins
import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from .models import Employee, MonthlyShift, ShiftAssignment, Store
from .backup import ShiftBackup
from .paid_leave_sync import (
    AttendanceSender, CONFIRMATION_KEY, ConflictError, Document,
    GitHubSyncRepository, JST, STATE_ROOT, SyncError, build_confirmation,
    format_jst, load_state, payload_hash, read_locked_payload, set_paused,
    sync_month, validate_employees,
)
from .run_paid_leave_sync import main


MONTH = "2026-10"
NOW = datetime(2026, 10, 1, 6, 17, tzinfo=JST)
PATH = f"{STATE_ROOT}/{MONTH}.json"


class MemoryRepo:
    def __init__(self):
        self.files = {}
        self.counter = 0
        self.reads = []
        self.fail_put_at = 0

    def head(self):
        return "a" * 40

    def get(self, path, ref=None):
        self.reads.append((path, ref))
        return copy.deepcopy(self.files.get(path))

    def list_names(self, path, ref):
        self.reads.append((path, ref))
        return [key[len(path) + 1:] for key in self.files
                if key.startswith(path + "/") and "/" not in key[len(path) + 1:]]

    def put(self, path, value, sha):
        if self.fail_put_at == self.counter + 1:
            raise SyncError("保存失敗")
        current = self.files.get(path)
        if (current.sha if current else None) != sha:
            raise ConflictError("同時更新")
        self.counter += 1
        self.files[path] = Document(copy.deepcopy(value), str(self.counter))
        return str(self.counter)


def fixture_repo():
    repo = MemoryRepo()
    lock = {"year": 2026, "month": 10, "snapshot_file": "shift_finalized_2026-09-30_170000.json",
            "locked_at": "2026-09-30T17:00:00+09:00"}
    employees = [Employee("テスト甲", employee_id="049"), Employee("テスト乙")]
    assignments = [ShiftAssignment(employee="テスト甲", day=1, store=Store.AKABANE),
                   ShiftAssignment(employee="テスト乙", day=1, store=Store.OFF)]
    confirmation = build_confirmation(2026, 10, {"テスト甲": 3}, employees, assignments, NOW - timedelta(days=1))
    snapshot = {"year": 2026, "month": 10, "kind": "finalized",
                "metadata": {CONFIRMATION_KEY: confirmation, "private_comment": "do not send"},
                "assignments": [], "note": "do not send"}
    repo.put(f"locks/{MONTH}.lock", lock, None)
    repo.put(f"backups/{MONTH}/{lock['snapshot_file']}", snapshot, None)
    return repo


def receipt(payload, digest):
    return {"schema_version": 1, "status": "accepted", "sync_id": payload["sync_id"],
            "month": payload["month"], "payload_sha256": digest,
            "employee_count": len(payload["employees"]),
            "total_paid_leave_days": sum(row["paid_leave_days"] for row in payload["employees"]),
            "received_at": NOW.isoformat()}


class ConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.employees = [Employee("テスト甲", employee_id="049")]
        self.assignments = [ShiftAssignment("テスト甲", 1, Store.AKABANE)]

    def build(self, days):
        return build_confirmation(2026, 10, days, self.employees, self.assignments, NOW)

    def test_no_network_and_explicit_zero_when_totals_are_known(self):
        with patch("requests.Session.request", side_effect=AssertionError("network")):
            result = self.build({})
        self.assertEqual(0, result["employees"][0]["paid_leave_days"])
        self.assertEqual("049", result["employees"][0]["employee_id"])

    def test_missing_totals_not_silently_zero(self):
        with self.assertRaises(SyncError):
            self.build(None)

    def test_invalid_totals_and_unknown_employee(self):
        for days in ({"テスト甲": -1}, {"テスト甲": True}, {"テスト甲": 0.5},
                     {"テスト甲": "3"}, {"テスト甲": 32}, {"不明": 1}):
            with self.subTest(days=days), self.assertRaises(SyncError):
                self.build(days)

    def test_paid_leave_not_inferred_from_off_days(self):
        self.assignments = [ShiftAssignment("テスト甲", day, Store.OFF) for day in range(1, 32)]
        self.assertEqual(0, self.build({})["employees"][0]["paid_leave_days"])

    def test_paid_leave_plus_work_cannot_exceed_calendar_days(self):
        self.assignments = [ShiftAssignment("テスト甲", day, Store.AKABANE) for day in range(1, 31)]
        with self.assertRaises(SyncError):
            self.build({"テスト甲": 2})

    def test_missing_employee_shift_is_not_exported(self):
        self.assignments = []
        with self.assertRaises(SyncError):
            self.build({})

    def test_auxiliary_is_not_included(self):
        self.employees.append(Employee("補助テスト", is_auxiliary=True))
        self.assertEqual(1, len(self.build({})["employees"]))

    def test_duplicate_ids_are_rejected(self):
        rows = [{"employee_name": n, "employee_id": "049", "paid_leave_days": 0} for n in ("甲", "乙")]
        with self.assertRaises(SyncError):
            validate_employees(rows, MONTH)

    def test_receipt_display_is_japanese_time(self):
        self.assertEqual("2026/10/01 06:17:00", format_jst("2026-09-30T21:17:00+00:00"))


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.repo = fixture_repo()

    def test_reads_one_git_revision_and_only_confirmed_data(self):
        self.repo.put("preferences/2026-10/changed.json", {"paid_leave_days": 9}, None)
        payload = read_locked_payload(self.repo, MONTH, NOW)
        self.assertEqual([3, 0], [r["paid_leave_days"] for r in payload["employees"]])
        self.assertTrue(all(ref == "a" * 40 for path, ref in self.repo.reads))
        self.assertNotIn("do not send", json.dumps(payload))

    def test_unlock_history_wins_over_stale_mirror(self):
        self.repo.put(f"locks/{MONTH}/unlock_20260930-180000.json", {}, None)
        with self.assertRaisesRegex(SyncError, "解除中"):
            read_locked_payload(self.repo, MONTH, NOW)

    def test_newest_lock_history_after_unlock(self):
        mirror = self.repo.get(f"locks/{MONTH}.lock").value
        self.repo.put(f"locks/{MONTH}/unlock_20260930-180000.json", {}, None)
        self.repo.put(f"locks/{MONTH}/lock_20260930-190000.json", mirror, None)
        self.assertEqual(MONTH, read_locked_payload(self.repo, MONTH, NOW)["month"])

    def test_same_second_unlock_is_fail_closed(self):
        mirror = self.repo.get(f"locks/{MONTH}.lock").value
        self.repo.put(f"locks/{MONTH}/lock_20260930-190000.json", mirror, None)
        self.repo.put(f"locks/{MONTH}/unlock_20260930-190000.json", {}, None)
        with self.assertRaises(SyncError):
            read_locked_payload(self.repo, MONTH, NOW)

    def test_source_failures_never_zero_fill(self):
        for mode in ("no_lock", "no_snapshot", "no_metadata", "wrong_month", "draft", "traversal"):
            with self.subTest(mode=mode):
                repo = fixture_repo()
                lock = repo.files[f"locks/{MONTH}.lock"].value
                path = f"backups/{MONTH}/{lock['snapshot_file']}"
                if mode == "no_lock":
                    del repo.files[f"locks/{MONTH}.lock"]
                elif mode == "no_snapshot":
                    del repo.files[path]
                elif mode == "no_metadata":
                    repo.files[path].value["metadata"] = {}
                elif mode == "wrong_month":
                    repo.files[path].value["month"] = 9
                elif mode == "draft":
                    repo.files[path].value["kind"] = "draft"
                else:
                    lock["snapshot_file"] = "../wrong.json"
                with self.assertRaises(SyncError):
                    read_locked_payload(repo, MONTH, NOW)

    def test_real_backup_round_trip_keeps_confirmation(self):
        employees = [Employee("テスト甲", employee_id="049")]
        shift = MonthlyShift(year=2026, month=10)
        shift.assignments = [ShiftAssignment("テスト甲", day, Store.OFF) for day in range(1, 32)]
        confirmation = build_confirmation(2026, 10, {"テスト甲": 3}, employees, shift.assignments, NOW)
        with tempfile.TemporaryDirectory() as temp:
            backup = ShiftBackup(Path(temp))
            saved = backup.save_shift(shift, kind="finalized", metadata={CONFIRMATION_KEY: confirmation})
            self.assertEqual(confirmation, backup.load_shift_metadata(saved)[CONFIRMATION_KEY])
            restored = backup.load_shift(saved)
            self.assertEqual(31, len(restored.assignments))
            repo = MemoryRepo()
            repo.put(f"locks/{MONTH}.lock", {
                "year": 2026, "month": 10, "snapshot_file": saved.name, "locked_at": NOW.isoformat(),
            }, None)
            repo.put(f"backups/{MONTH}/{saved.name}", json.loads(saved.read_text()), None)
            receiver = Mock(side_effect=receipt)
            self.assertEqual("sent", sync_month(repo, receiver, MONTH, NOW).status)
            self.assertEqual(3, receiver.call_args.args[0]["employees"][0]["paid_leave_days"])


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.repo = fixture_repo()
        self.sender = Mock(side_effect=receipt)

    def run_sync(self, now=NOW, month=MONTH, manual=False):
        return sync_month(self.repo, self.sender, month, now, manual)

    def test_sends_once_and_relocks_do_not_overwrite(self):
        result = self.run_sync()
        self.assertEqual("sent", result.status)
        original = copy.deepcopy(self.repo.files[PATH].value)
        lock = self.repo.files[f"locks/{MONTH}.lock"].value
        self.repo.files[f"backups/{MONTH}/{lock['snapshot_file']}"].value["metadata"][CONFIRMATION_KEY]["employees"][0]["paid_leave_days"] = 8
        self.run_sync(NOW + timedelta(days=3))
        self.run_sync(NOW + timedelta(days=3), manual=True)
        self.assertEqual(1, self.sender.call_count)
        self.assertEqual(original, self.repo.files[PATH].value)

    def test_future_month_never_sends_even_manually(self):
        self.assertEqual("not_due", self.run_sync(month="2026-11", manual=True).status)
        self.sender.assert_not_called()

    def test_jst_boundary_and_first_morning(self):
        for now in (datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc),
                    datetime(2026, 9, 30, 20, 59, tzinfo=timezone.utc)):
            self.assertEqual("not_due", self.run_sync(now).status)
        self.assertEqual("sent", self.run_sync(datetime(2026, 9, 30, 21, 17, tzinfo=timezone.utc)).status)

    def test_auto_does_not_backfill_prior_months(self):
        self.assertEqual("not_due", self.run_sync(month="2026-09").status)
        self.sender.assert_not_called()

    def test_hold_survives_new_session_and_manual_send(self):
        set_paused(self.repo, MONTH, True, NOW)
        self.assertEqual("paused", self.run_sync().status)
        self.assertEqual("paused", self.run_sync(manual=True).status)
        set_paused(self.repo, MONTH, False, NOW)
        self.assertEqual("sent", self.run_sync().status)

    def test_failure_retains_payload_on_retry_after_source_changes(self):
        self.sender.side_effect = SyncError("通信失敗")
        self.assertEqual("failed", self.run_sync().status)
        frozen = copy.deepcopy(self.repo.files[PATH].value["payload"])
        del self.repo.files[f"locks/{MONTH}.lock"]
        self.sender.side_effect = receipt
        self.assertEqual("sent", self.run_sync(NOW + timedelta(days=1)).status)
        self.assertEqual(frozen, self.sender.call_args.args[0])

    def test_outbox_must_be_durable_before_network(self):
        self.repo.fail_put_at = self.repo.counter + 1
        with self.assertRaises(SyncError):
            self.run_sync()
        self.sender.assert_not_called()

    def test_receipt_save_failure_retries_same_id_and_hash(self):
        accepted = {}

        def receiver(payload, digest):
            if payload["sync_id"] in accepted:
                self.assertEqual(digest, accepted[payload["sync_id"]])
                answer = receipt(payload, digest)
                answer["status"] = "already_accepted"
                return answer
            accepted[payload["sync_id"]] = digest
            return receipt(payload, digest)

        self.sender.side_effect = receiver
        self.repo.fail_put_at = self.repo.counter + 2
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertEqual("sending", self.repo.files[PATH].value["status"])
        self.repo.fail_put_at = 0
        self.assertEqual("sent", self.run_sync(NOW + timedelta(minutes=16)).status)
        self.assertEqual(1, len(accepted))

    def test_concurrent_send_and_pause_are_prevented(self):
        def receiver(payload, digest):
            with self.assertRaises(SyncError):
                set_paused(self.repo, MONTH, True, NOW)
            self.assertEqual("sending", self.run_sync().status)
            return receipt(payload, digest)
        self.sender.side_effect = receiver
        self.assertEqual("sent", self.run_sync().status)
        self.assertEqual(1, self.sender.call_count)

    def test_cas_conflict_does_not_send(self):
        self.repo.put = Mock(side_effect=ConflictError("競合"))
        with self.assertRaises(ConflictError):
            self.run_sync()
        self.sender.assert_not_called()

    def test_mismatched_receipt_is_not_success(self):
        for change in ({"month": "2026-09"}, {"employee_count": 0}, {"total_paid_leave_days": 4},
                       {"status": "queued"}, {"payload_sha256": "bad"}, {"received_at": "bad"}):
            with self.subTest(change=change):
                self.repo = fixture_repo()
                self.sender.side_effect = lambda p, h: {**receipt(p, h), **change}
                self.assertEqual("failed", self.run_sync().status)

    def test_corrupt_outbox_stops_instead_of_starting_over(self):
        self.run_sync()
        self.repo.files[PATH].value["payload"]["employees"][0]["paid_leave_days"] = 4
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertEqual(1, self.sender.call_count)

    def test_sent_state_without_receipt_not_accepted(self):
        self.run_sync()
        self.repo.files[PATH].value.pop("receipt")
        with self.assertRaises(SyncError):
            load_state(self.repo, MONTH)

    def test_missing_source_does_not_create_zero_or_sent_record(self):
        del self.repo.files[f"locks/{MONTH}.lock"]
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertNotIn(PATH, self.repo.files)
        self.sender.assert_not_called()

    def test_manual_confirmation_rejects_a_different_snapshot(self):
        with self.assertRaises(ConflictError):
            sync_month(self.repo, self.sender, MONTH, NOW, manual=True, expected_snapshot_sha256="old")
        self.sender.assert_not_called()
        self.assertNotIn(PATH, self.repo.files)

    def test_held_payload_remains_frozen_after_resume(self):
        self.sender.side_effect = SyncError("不明")
        self.run_sync()
        payload = copy.deepcopy(self.repo.files[PATH].value["payload"])
        set_paused(self.repo, MONTH, True, NOW)
        set_paused(self.repo, MONTH, False, NOW)
        self.sender.side_effect = receipt
        self.run_sync(NOW + timedelta(hours=1))
        self.assertEqual(payload, self.sender.call_args.args[0])


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.payload = read_locked_payload(fixture_repo(), MONTH, NOW)
        self.digest = payload_hash(self.payload)
        self.session = Mock()
        self.sender = AttendanceSender("https://attendance.example/api/integrations/paid-leave", "x" * 32, self.session)

    def test_secure_request_and_matching_receipt(self):
        self.session.post.return_value = Mock(status_code=201, json=lambda: receipt(self.payload, self.digest))
        answer = self.sender(self.payload, self.digest)
        self.assertEqual("accepted", answer["status"])
        args = self.session.post.call_args.kwargs
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(self.payload["sync_id"], args["headers"]["Idempotency-Key"])
        self.assertEqual(self.digest, hashlib.sha256(args["data"]).hexdigest())

    def test_rejects_insecure_endpoint_and_weak_secret(self):
        for url in ("http://example.test", "https://user:pass@example.test", "https://example.test?a=b", "https://example.test#fragment"):
            with self.assertRaises(SyncError):
                AttendanceSender(url, "x" * 32)
        with self.assertRaises(SyncError):
            AttendanceSender("https://example.test", "weak")

    def test_no_secret_leak_from_http_errors(self):
        for status in (301, 401, 403, 409, 422, 500):
            self.session.post.return_value = Mock(status_code=status, text="SECRET private contents")
            with self.assertRaises(SyncError) as caught:
                self.sender(self.payload, self.digest)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_network_error_is_unknown_not_success(self):
        self.session.post.side_effect = requests.Timeout("SECRET url and token")
        with self.assertRaises(SyncError) as caught:
            self.sender(self.payload, self.digest)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_github_public_repo_rejected(self):
        self.session.request.return_value = Mock(status_code=200, json=lambda: {"private": False})
        repo = GitHubSyncRepository("secret", "owner/data", session=self.session)
        with self.assertRaises(SyncError):
            repo.get(PATH)

    def test_github_missing_and_permission_failure_distinguished(self):
        repo = GitHubSyncRepository("secret", "owner/data", session=self.session)
        repo._private_checked = True
        self.session.request.return_value = Mock(status_code=404)
        self.assertIsNone(repo.get(PATH))
        self.session.request.return_value = Mock(status_code=403)
        with self.assertRaises(SyncError):
            repo.get(PATH)

    def test_github_put_uses_expected_sha_and_fails_on_conflict(self):
        repo = GitHubSyncRepository("secret", "owner/data", session=self.session)
        repo._private_checked = True
        self.session.request.return_value = Mock(status_code=409)
        with self.assertRaises(ConflictError):
            repo.put(PATH, {"schema_version": 1}, "old-sha")
        self.assertEqual("old-sha", self.session.request.call_args.kwargs["json"]["sha"])

    def test_runner_disabled_never_accesses_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("requests.Session.request", side_effect=AssertionError("network")):
            self.assertEqual(0, main([]))

    def test_runner_start_month_prevents_accidental_current_month_import(self):
        with patch.dict(os.environ, {"PAID_LEAVE_SYNC_ENABLED": "true", "PAID_LEAVE_SYNC_START_MONTH": "2099-12"}, clear=True), \
                patch("prototype.run_paid_leave_sync.GitHubSyncRepository") as repo:
            self.assertEqual(0, main([]))
            repo.assert_not_called()

    def test_runner_requires_start_month_before_auto_activation(self):
        with patch.dict(os.environ, {"PAID_LEAVE_SYNC_ENABLED": "true"}, clear=True), \
                patch("prototype.run_paid_leave_sync.GitHubSyncRepository") as repo:
            self.assertEqual(1, main([]))
            repo.assert_not_called()


class PaidLeaveSettingsImportTest(unittest.TestCase):
    @staticmethod
    def guard_source():
        source = (Path(__file__).resolve().parents[1] / "app" / "app.py").read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "render_paid_leave_sync_settings_panel")
        return ast.get_source_segment(source, node)

    @staticmethod
    def missing_import(module):
        original = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == "prototype.paid_leave_sync_ui":
                raise ModuleNotFoundError(f"No module named '{module}'", name=module)
            return original(name, *args, **kwargs)

        return importing

    def test_missing_integration_files_leave_other_settings_visible(self):
        from streamlit.testing.v1 import AppTest
        source = (
            "import streamlit as st\n" + self.guard_source()
            + '\nrender_paid_leave_sync_settings_panel()\nst.text_input("その他の設定")\n'
        )
        for module in ("prototype.paid_leave_sync_ui", "prototype.paid_leave_sync"):
            with self.subTest(module=module), \
                    patch("builtins.__import__", side_effect=self.missing_import(module)), \
                    patch("requests.Session.request", side_effect=AssertionError("network")):
                app = AppTest.from_string(source).run()
                self.assertEqual(0, len(app.exception))
                self.assertEqual(1, len(app.warning))
                self.assertIn(module.replace(".", "/") + ".py", app.warning[0].value)
                self.assertEqual("その他の設定", app.text_input[0].label)
                self.assertEqual(0, len(app.button))

    def test_available_panel_renders_without_network_or_enabling_sync(self):
        from streamlit.testing.v1 import AppTest
        with patch("prototype.github_backup._get_secret", side_effect=lambda key, default="": default), \
                patch("requests.Session.request", side_effect=AssertionError("network")), \
                patch("prototype.paid_leave_sync_ui.sync_month") as send:
            app = AppTest.from_string(
                "import streamlit as st\n" + self.guard_source()
                + "\nrender_paid_leave_sync_settings_panel()\n"
            ).run()
            self.assertEqual(0, len(app.exception))
            self.assertEqual(0, len(app.warning))
            self.assertTrue(any("送信はまだできません" in item.value for item in app.info))
            self.assertEqual("連携状態を確認・更新", app.button(key="leave_sync_refresh").label)
            send.assert_not_called()

    def test_unrelated_dependency_error_is_not_hidden(self):
        scope = {"st": Mock()}
        exec(self.guard_source(), scope)
        with patch("builtins.__import__", side_effect=self.missing_import("requests")):
            with self.assertRaises(ModuleNotFoundError) as result:
                scope["render_paid_leave_sync_settings_panel"]()
        self.assertEqual("requests", result.exception.name)
        scope["st"].warning.assert_not_called()

    def test_panel_runtime_error_is_not_reported_as_missing_file(self):
        scope = {"st": Mock()}
        exec(self.guard_source(), scope)
        with patch("prototype.paid_leave_sync_ui.render_paid_leave_sync_panel", side_effect=RuntimeError("broken panel")):
            with self.assertRaisesRegex(RuntimeError, "broken panel"):
                scope["render_paid_leave_sync_settings_panel"]()
        scope["st"].warning.assert_not_called()

    def test_settings_uses_guard_instead_of_direct_import(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        settings = next(n for n in ast.walk(tree) if isinstance(n, ast.With)
                        and any(isinstance(i.context_expr, ast.Name)
                                and i.context_expr.id == "setting_tab_leave" for i in n.items))
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                            and n.func.id == "render_paid_leave_sync_settings_panel"
                            for n in ast.walk(settings)))
        self.assertFalse(any(isinstance(n, ast.ImportFrom)
                             and n.module == "prototype.paid_leave_sync_ui"
                             for n in ast.walk(settings)))


class PaidLeavePanelTest(unittest.TestCase):
    def test_panel_is_lazy_and_can_hold_without_receiver(self):
        from streamlit.testing.v1 import AppTest
        repo = fixture_repo()
        secret = lambda key, default="": default
        with patch("prototype.paid_leave_sync_ui.GitHubSyncRepository", return_value=repo) as factory, \
                patch("prototype.github_backup._get_secret", side_effect=secret):
            app = AppTest.from_string(
                "from prototype.paid_leave_sync_ui import render_paid_leave_sync_panel\n"
                "render_paid_leave_sync_panel()\n"
            ).run()
            self.assertEqual(0, len(app.exception))
            factory.assert_not_called()
            app.button(key="leave_sync_refresh").click().run()
            self.assertEqual(0, len(app.exception))
            self.assertTrue(app.button(key="leave_sync_send").disabled)
            app.button(key="leave_sync_hold").click().run()
            month = app.selectbox(key="leave_sync_month").value
            self.assertTrue(repo.files[f"{STATE_ROOT}/{month}.json"].value["paused"])
            app.button(key="leave_sync_hold").click().run()
            self.assertFalse(repo.files[f"{STATE_ROOT}/{month}.json"].value["paused"])

    def test_preview_survives_checkbox_then_sends_and_disables_overwrite(self):
        from streamlit.testing.v1 import AppTest
        repo = fixture_repo()
        secrets = {"ATTENDANCE_PAID_LEAVE_SYNC_URL": "https://attendance.example/api/integrations/paid-leave",
                   "ATTENDANCE_PAID_LEAVE_SYNC_TOKEN": "x" * 32}
        with patch("prototype.paid_leave_sync_ui.GitHubSyncRepository", return_value=repo), \
                patch("prototype.github_backup._get_secret", side_effect=lambda key, default="": secrets.get(key, default)), \
                patch("prototype.paid_leave_sync_ui.datetime") as clock, \
                patch("prototype.paid_leave_sync_ui.AttendanceSender", return_value=receipt):
            clock.now.return_value = NOW
            app = AppTest.from_string(
                "from prototype.paid_leave_sync_ui import render_paid_leave_sync_panel\n"
                "render_paid_leave_sync_panel()\n"
            ).run()
            app.button(key="leave_sync_refresh").click().run()
            self.assertTrue(app.button(key="leave_sync_send").disabled)
            app.button(key="leave_sync_preview").click().run()
            self.assertEqual(1, len(app.dataframe))
            app.checkbox(key=f"leave_sync_confirm_{MONTH}").check().run()
            self.assertEqual(1, len(app.dataframe))
            self.assertFalse(app.button(key="leave_sync_send").disabled)
            app.button(key="leave_sync_send").click().run()
            self.assertEqual(0, len(app.exception))
            self.assertEqual("sent", repo.files[PATH].value["status"])
            self.assertTrue(any("受領済み" in item.value for item in app.success))
            self.assertTrue(app.button(key="leave_sync_send").disabled)
            self.assertTrue(app.button(key="leave_sync_hold").disabled)


if __name__ == "__main__":
    unittest.main()
