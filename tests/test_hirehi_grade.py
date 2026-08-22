from datetime import date

import pytest

from backend.intelligence.hirehi_grade import hirehi_grades


def experience(months: int, *, start="2020-01-01"):
    year, month = map(int, start[:7].split("-"))
    end_month = year * 12 + month - 1 + months
    return [{"start_date": start, "end_date": f"{end_month // 12:04d}-{end_month % 12 + 1:02d}-01"}]


@pytest.mark.parametrize(
    ("months", "grades"),
    [(0, ["intern"]), (11, ["intern"]), (12, ["intern", "junior"]),
     (23, ["intern", "junior"]), (24, ["intern", "junior", "middle"]),
     (47, ["intern", "junior", "middle"]), (48, ["senior"]),
     (72, ["senior"]), (73, ["lead", "head"])],
)
def test_grade_boundaries(months, grades):
    years, actual = hirehi_grades({"experiences": experience(months)}, today=date(2030, 1, 1))
    assert years == months / 12
    assert actual == grades


def test_overlapping_intervals_are_not_double_counted():
    resume = {"experiences": [{"start_date": "2020-01-01", "end_date": "2022-01-01"},
                               {"start_date": "2021-01-01", "end_date": "2023-01-01"}]}
    assert hirehi_grades(resume, today=date(2030, 1, 1)) == (3, ["intern", "junior", "middle"])


def test_current_and_missing_end_date_use_today():
    resume = {"experiences": [{"start_date": "2020-01-01", "current": True},
                               {"start_date": "2020-01-01"}]}
    assert hirehi_grades(resume, today=date(2022, 1, 1))[0] == 2


def test_invalid_dates_are_ignored():
    resume = {"experiences": [{"start_date": "nope", "end_date": "2020-01-01"},
                               {"start_date": "2020-01", "end_date": "bad"}]}
    assert hirehi_grades(resume, today=date(2022, 1, 1)) == (0, ["intern"])
