import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="capture GDI et OCR de Windows")


class FakeShot:
    size = (1, 1)
    bgra = b"\0\0\0\0"


class FakeSct:
    def __init__(self, fails):
        self.fails = fails
        self.closed = False

    def grab(self, box):
        import mss
        if self.fails:
            raise mss.ScreenShotError("Windows graphics function failed (no error provided): BitBlt")
        return FakeShot()

    def close(self):
        self.closed = True


@pytest.fixture
def capture(monkeypatch):
    import capture
    capture._local.__dict__.pop("sct", None)
    yield capture
    capture._local.__dict__.pop("sct", None)


def test_recreates_screen_context_after_bitblt_failure(capture, monkeypatch):
    stale, fresh = FakeSct(fails=True), FakeSct(fails=False)
    made = [stale, fresh]
    monkeypatch.setattr(capture.mss, "MSS", lambda: made.pop(0))
    assert capture._grab(0, 0, 1, 1).size == (1, 1)
    assert stale.closed and made == []


def test_persistent_failure_raises_capture_error(capture, monkeypatch):
    monkeypatch.setattr(capture.mss, "MSS", lambda: FakeSct(fails=True))
    with pytest.raises(capture.CaptureError):
        capture._grab(0, 0, 1, 1)
