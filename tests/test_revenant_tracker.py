from parsers import RevenantTracker


def rev(name, time, system="Tau Ceti / Oort", planet="Erebos", bar=0.0):
    return {"name": name, "danger": name.startswith("XC"), "time": time, "system": system,
            "planet": planet, "info": "", "status": "", "bar": bar}


FIVE = [rev("SC-Neman", "04:15"), rev("SC-Tiscenon", "15:34"), rev("XC-Rusvo", "20:01", "Antares", "Lustra"),
        rev("XC-Soon", "21:15", "Draconis", ""), rev("SC-Nela", "21:32", planet="")]


def names(tracker):
    return sorted(e["name"] for e in tracker.snapshot())


def tracked():
    t = RevenantTracker()
    for _ in range(3):
        t.update(True, FIVE)
    return t


def test_close_names_with_different_times_are_two_revenants():
    # « SC-Neman » et « SC-Nela » se ressemblent à 80 % : c'est l'heure qui les distingue
    assert names(tracked()) == sorted(e["name"] for e in FIVE)


def test_misread_name_with_same_time_is_the_same_revenant():
    t = tracked()
    events = t.update(True, [rev("SC-Nernan", "04:15")] + FIVE[1:])
    events += t.update(True, [rev("SC-Nernan", "04:15")] + FIVE[1:])
    assert len(t.snapshot()) == 5
    assert not [e for e in events if e["type"] in ("new", "gone")]


def test_time_missing_once_does_not_steal_another_revenant():
    t = tracked()
    events = t.update(True, FIVE[:4] + [rev("SC-Nela", "")])
    assert names(t) == sorted(e["name"] for e in FIVE)
    assert events == []


def test_new_revenant_needs_two_scans_and_gone_after_six():
    t = tracked()
    six = FIVE + [rev("XB-Kora", "22:00")]
    assert t.update(True, six) == []
    assert [e["type"] for e in t.update(True, six)] == ["new"]
    for _ in range(5):
        assert t.update(True, FIVE) == []
    assert [e["type"] for e in t.update(True, FIVE)] == ["gone"]
