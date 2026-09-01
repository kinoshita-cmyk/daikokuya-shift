import unittest

from .models import MonthlyShift, ShiftAssignment, Store
from .validator import validate


class PaidLeaveWorkdayValidationTest(unittest.TestCase):
    @staticmethod
    def _doi_shift(work_days: int) -> MonthlyShift:
        shift = MonthlyShift(year=2026, month=9)
        shift.assignments = [
            ShiftAssignment(
                employee="土井",
                day=day,
                store=Store.HIGASHIGUCHI,
            )
            for day in range(1, work_days + 1)
        ]
        shift.assignments.extend(
            ShiftAssignment(employee="土井", day=day, store=Store.OFF)
            for day in range(work_days + 1, 31)
        )
        return shift

    @staticmethod
    def _doi_workday_issues(result):
        return [
            issue
            for issue in result.issues
            if issue.employee == "土井"
            and issue.category in {"月間勤務日数不足", "月間勤務日数超過"}
        ]

    def test_paid_leave_counts_toward_monthly_work_target(self):
        result = validate(
            shift=self._doi_shift(19),
            paid_leave_days={"土井": 3},
            exact_holiday_days={"土井": 11},
        )

        self.assertEqual([], self._doi_workday_issues(result))
        self.assertIn(
            "実出勤19日＋有給3日＝基準算入22日",
            result.summary_stats["土井"],
        )
        self.assertIn("ぴったり", result.summary_stats["土井"])

    def test_same_attendance_without_paid_leave_is_an_error(self):
        result = validate(
            shift=self._doi_shift(19),
            exact_holiday_days={"土井": 11},
        )

        issues = self._doi_workday_issues(result)
        self.assertEqual(1, len(issues))
        self.assertEqual("ERROR", issues[0].severity)
        self.assertIn("3日不足", issues[0].message)

    def test_remaining_shortfall_uses_credited_day_difference(self):
        result = validate(
            shift=self._doi_shift(17),
            paid_leave_days={"土井": 3},
            exact_holiday_days={"土井": 13},
        )

        issues = self._doi_workday_issues(result)
        self.assertEqual(1, len(issues))
        self.assertEqual("WARNING", issues[0].severity)
        self.assertIn("実出勤17日＋有給3日＝基準算入20日", issues[0].message)
        self.assertIn("2日不足", issues[0].message)


if __name__ == "__main__":
    unittest.main()
