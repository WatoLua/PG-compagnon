import hashlib
import io
import json
import sys

import pytest

import updater


def test_parse_version():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.10.0") > updater.parse_version("1.9.9")
    assert updater.parse_version("latest") is None


class FakeGitHub:
    def __init__(self, tag, exe_bytes, checksum=None):
        self.release = {"tag_name": tag, "assets": [
            {"name": "PGCompanion.exe", "browser_download_url": "https://dl/exe"},
            {"name": "PGCompanion.exe.sha256", "browser_download_url": "https://dl/sha"},
        ]}
        self.files = {"https://dl/exe": exe_bytes,
                      "https://dl/sha": f"{checksum or hashlib.sha256(exe_bytes).hexdigest()}  PGCompanion.exe\n".encode()}

    def __call__(self, url, accept=None):
        if url == updater.LATEST:
            return io.BytesIO(json.dumps(self.release).encode())
        return io.BytesIO(self.files[url])


@pytest.fixture
def frozen_exe(tmp_path, monkeypatch):
    exe = tmp_path / "PGCompanion.exe"
    exe.write_bytes(b"old")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    restarted = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: restarted.append((args, kw)))
    monkeypatch.setattr(updater.os, "_exit", lambda code: restarted.append(code))
    return exe, restarted


def test_installs_newer_release_and_restarts(frozen_exe, monkeypatch):
    exe, restarted = frozen_exe
    monkeypatch.setattr(updater, "_open", FakeGitHub("v1.1.0", b"new"))
    updater.update_and_restart()
    assert exe.read_bytes() == b"new"
    assert (exe.parent / "PGCompanion.exe.old").read_bytes() == b"old"
    (args, kw), code = restarted
    assert args[0] == str(exe) and kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and code == 0


def test_keeps_current_version_when_up_to_date(frozen_exe, monkeypatch):
    exe, restarted = frozen_exe
    monkeypatch.setattr(updater, "_open", FakeGitHub("v1.0.0", b"new"))
    updater.update_and_restart()
    assert exe.read_bytes() == b"old" and restarted == []


def test_rejects_corrupted_download(frozen_exe, monkeypatch):
    exe, restarted = frozen_exe
    monkeypatch.setattr(updater, "_open", FakeGitHub("v1.1.0", b"new", checksum="0" * 64))
    updater.update_and_restart()
    assert exe.read_bytes() == b"old" and restarted == []
    assert sorted(p.name for p in exe.parent.iterdir()) == ["PGCompanion.exe"]


def test_offline_keeps_running(frozen_exe, monkeypatch):
    exe, restarted = frozen_exe

    def offline(url, accept=None):
        raise OSError("pas de réseau")

    monkeypatch.setattr(updater, "_open", offline)
    updater.update_and_restart()
    assert exe.read_bytes() == b"old" and restarted == []


def test_removes_previous_old_exe(frozen_exe, monkeypatch):
    exe, _ = frozen_exe
    (exe.parent / "PGCompanion.exe.old").write_bytes(b"older")
    monkeypatch.setattr(updater, "_open", FakeGitHub("v1.0.0", b"new"))
    updater.update_and_restart()
    assert not (exe.parent / "PGCompanion.exe.old").exists()


def test_does_nothing_from_sources(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(updater, "_open", lambda *a, **k: pytest.fail("aucun appel réseau depuis les sources"))
    updater.update_and_restart()
