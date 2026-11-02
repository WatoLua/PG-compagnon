import pytest

from parsers import read_when, row_when


@pytest.mark.parametrize("text, expected", [
    ("Oct 31 · 04:15", ("04:15", "10-31")),
    ("Nov 1 · 21:32", ("21:32", "11-01")),
    ("Nov 1, 04:15", ("04:15", "11-01")),    # le jour n'est pas pris pour l'heure
    ("Nov 1 • 4.15", ("04:15", "11-01")),
    ("Nov 1 · O4:l5", ("04:15", "11-01")),   # lettres lues à la place des chiffres
    ("Nov 1 · 04 15", ("04:15", "11-01")),   # « : » non lu
    ("21:32", ("21:32", "")),
    ("Nov 1", ("", "11-01")),
    ("Statut", ("", "")),
])
def test_read_when(text, expected):
    assert read_when(text) == expected


def line(text, x, y):
    return {"text": text, "x": x, "y": y}


def test_date_and_time_split_in_two_ocr_lines():
    lines = [line("04:15", 70, 12), line("Nov 1 ·", 10, 10), line("21:32", 70, 62), line("Nov 1", 10, 60)]
    assert row_when(lines, 10, 50) == ("04:15", "11-01")
    assert row_when(lines, 60, 50) == ("21:32", "11-01")
