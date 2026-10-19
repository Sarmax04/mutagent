"""A stand-in for contree_sdk.ContreeSync that runs the sandbox commands in local temp folders.

The sandbox paths /work and /tmp are mapped into a private folder per image, so the real
shell lines built by mutagent.sandbox are executed end to end (pytest, coverage, tar, rm).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class FakeResult:
    def __init__(self, cost):
        self.cost = cost


class FakeDone:
    def __init__(self, code, out, err):
        self.exit_code, self.stdout, self.stderr = code, out, err
        self.result = FakeResult(0.25)


class FakeRun:
    def __init__(self, image, shell, files, disposable, timeout):
        self.image, self.shell, self.files, self.disposable, self.timeout = image, shell, files, disposable, timeout

    def wait(self):
        client = self.image.client
        client.calls.append({"shell": self.shell, "files": sorted((self.files or {}).keys()), "disposable": self.disposable})
        if client.fail:
            raise RuntimeError("sandbox is down")
        root = Path(tempfile.mkdtemp(prefix="fakecontree-"))
        shutil.copytree(self.image.root, root, dirs_exist_ok=True)
        (root / "work").mkdir(exist_ok=True)
        (root / "tmp").mkdir(exist_ok=True)
        for dest, data in (self.files or {}).items():
            target = root / dest.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else Path(data).read_bytes())
        if self.shell.startswith("pip install"):
            code, out, err = 0, "", ""
        else:
            command = re.sub(r"(?<![\w/])/(work|tmp)\b", lambda m: f"{root}/{m.group(1)}", self.shell)
            env = {**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}
            proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=env)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        if self.disposable:
            shutil.rmtree(root, ignore_errors=True)
            return FakeDone(code, out, err)
        image = FakeImage(client, root)
        done = FakeDone(code, out, err)
        done.run = image.run
        return done


class FakeImage:
    def __init__(self, client, root):
        self.client, self.root = client, root

    def run(self, shell=None, files=None, disposable=True, timeout=None):
        return FakeRun(self, shell, files, disposable, timeout)


class FakeImages:
    def __init__(self, client):
        self.client = client

    def use(self, ref):
        root = Path(tempfile.mkdtemp(prefix="fakebase-"))
        return FakeImage(self.client, root)


class FakeContree:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.images = FakeImages(self)
