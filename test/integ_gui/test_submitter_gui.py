# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""GUI test for the Blender submitter.

Launches real (non-background) Blender with this repo's submitter add-on, opens the submitter
dialog through its Blender operator, uses xa11y to drive the Qt dialog, and submits a job to an
in-process mock Deadline Cloud backend (plus moto for S3 job attachments).

Adapted from aws-deadline/deadline-cloud test/blender_submitter_ui/test_blender_submitter_ui.py.
The difference is that Blender loads the add-on and deadline.blender_submitter from this repo's
src/, so the test covers the revision under test rather than the latest PyPI release.

Requirements: a GUI session that exposes accessibility (Xvfb + AT-SPI on Linux, the
Accessibility permission on macOS), Blender at BLENDER_EXECUTABLE, and a test Python whose
major.minor matches Blender's bundled Python. See DEVELOPMENT.md.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import IO, Iterator
from unittest.mock import MagicMock

import boto3
import pytest
import xa11y
from botocore.exceptions import ClientError
from moto.server import ThreadedMotoServer

from .mock_deadline_backend import MockDeadlineBackend, start_server

REGION = "us-west-2"
BUCKET = "blender-integ-test"
ROOT_PREFIX = "DeadlineCloud"
ACCESS_KEY = "testing"
SECRET_KEY = "testing"

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
ADDONS_DIR = SRC_DIR / "deadline" / "blender_submitter" / "addons"
OPEN_SUBMITTER_SCRIPT = Path(__file__).parent / "open_submitter.py"

STARTUP_TIMEOUT = 30.0
SUBMIT_TIMEOUT = 60.0
FARM_RESOLVE_TIMEOUT = 15.0

IS_WINDOWS = platform.system() == "Windows"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _find_blender() -> Path:
    location = os.environ.get("BLENDER_EXECUTABLE")
    if location:
        return Path(location)
    location = shutil.which("blender")
    if location:
        return Path(location)
    if IS_WINDOWS:
        p = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Blender Foundation"
        if p.exists():
            for d in sorted(p.iterdir(), reverse=True):
                candidate = d / "blender.exe"
                if candidate.exists():
                    return candidate
    elif platform.system() == "Darwin":
        candidate = Path("/Applications/Blender.app/Contents/MacOS/Blender")
        if candidate.exists():
            return candidate
    else:
        opt = Path("/opt")
        if opt.exists():
            for d in sorted(opt.glob("blender-*-linux-x64"), reverse=True):
                candidate = d / "blender"
                if candidate.exists():
                    return candidate
    pytest.fail(
        "Blender not found. Set BLENDER_EXECUTABLE or install Blender to a standard location."
    )


@pytest.fixture(scope="session")
def blender_exe() -> Path:
    exe = _find_blender()
    # Blender imports deadline, PySide6 and the other dependencies from this Python's
    # site-packages, so compiled wheels only load if both Pythons have the same major.minor.
    result = subprocess.run(
        [
            str(exe),
            "--background",
            "--factory-startup",
            "--python-expr",
            "import sys; print('BLENDER_PYTHON=%d.%d' % sys.version_info[:2])",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    blender_python = next(
        (
            line.split("=", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("BLENDER_PYTHON=")
        ),
        None,
    )
    test_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if blender_python != test_python:
        pytest.fail(
            f"{exe} bundles Python {blender_python}, but the test runs on Python {test_python}. "
            "Run the test with a Python matching Blender's bundled Python."
        )
    return exe


@pytest.fixture(scope="session")
def artifact_dir(tmp_path_factory) -> Path:
    """Where failure diagnostics go. CI sets GUI_TEST_ARTIFACT_DIR and uploads it."""
    configured = os.environ.get("GUI_TEST_ARTIFACT_DIR")
    path = Path(configured) if configured else tmp_path_factory.mktemp("gui-test-artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://127.0.0.1:{port}" if host == "0.0.0.0" else f"http://{host}:{port}"
    try:
        yield url
    finally:
        server.stop()


@pytest.fixture(scope="session")
def mock_backend_server() -> Iterator[tuple[MockDeadlineBackend, str]]:
    backend = MockDeadlineBackend(validate_params=False)

    # The mock instantiates each submitted template with no parameter values, so a template
    # with required parameters (the Blender one requires BlenderFile, OutputDir, ResolutionX
    # and ResolutionY) is rejected even though CreateJob sent them. Accept that one case with
    # a minimal job; any other template error still fails CreateJob.
    original_parse = backend._parse_template

    def _parse_template_without_parameter_values(template):
        try:
            return original_parse(template)
        except ClientError as e:
            if "Values missing for required job parameters" not in str(e):
                raise
            job = MagicMock()
            job.name = "cube"
            job.steps = []
            job.jobEnvironments = []
            return job

    backend._parse_template = _parse_template_without_parameter_values  # type: ignore[method-assign]

    server, base_url, _ = start_server(backend)
    base_url = base_url.replace("127.0.0.1", "localhost")
    try:
        yield backend, base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def mock_backend(mock_backend_server) -> tuple[MockDeadlineBackend, str]:
    backend, base_url = mock_backend_server
    backend.clear()
    return backend, base_url


@pytest.fixture
def s3_client(moto_server):
    return boto3.client(
        "s3",
        endpoint_url=moto_server,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
    )


@pytest.fixture
def blender_env(tmp_path, mock_backend, moto_server, s3_client):
    """Set up env vars, config, farm/queue, and S3 bucket for the Blender subprocess."""
    backend, deadline_url = mock_backend

    farm = backend.create_farm(displayName="TestFarm", description="")
    queue = backend.create_queue(
        farmId=farm["farmId"],
        displayName="TestQueue",
        description="",
        defaultBudgetAction="NONE",
        jobAttachmentSettings={"s3BucketName": BUCKET, "rootPrefix": ROOT_PREFIX},
    )

    try:
        s3_client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        pass

    config_file = tmp_path / "deadline.config"
    config_file.write_text(
        "[defaults]\n"
        "aws_profile_name = (default)\n"
        "\n"
        "[profile-(default) defaults]\n"
        f"farm_id = {farm['farmId']}\n"
        "\n"
        f"[profile-(default) {farm['farmId']} defaults]\n"
        f"queue_id = {queue['queueId']}\n"
        "\n"
        "[profile-(default) settings]\n"
        f"job_history_dir = {tmp_path / 'job_history'}\n"
        "\n"
        # The submitter checks a remote manifest at startup and, if it advertises a newer
        # version than the installed one, opens a blocking "update available" dialog that
        # nobody dismisses in CI. Turn the notification off so the test exercises the
        # submitter, not the update prompt.
        "[settings]\n"
        "submitter_update_notification = false\n"
    )

    scene_dir = tmp_path / "scene"
    scene_dir.mkdir()

    # Blender imports packages from PYTHONPATH (--python-use-system-env). Put this repo's
    # src/ and the add-on directory first so the code under test wins over any installed
    # copy, then this Python's site-packages for deadline, PySide6, and the other
    # dependencies. Editable installs don't help here: Blender doesn't process .pth files
    # in PYTHONPATH entries.
    extra_paths = os.pathsep.join(
        str(p)
        for p in [
            SRC_DIR,
            ADDONS_DIR,
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
        ]
        if p
    )

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "AWS_ENDPOINT_URL_DEADLINE": deadline_url,
        "AWS_ENDPOINT_URL_S3": moto_server,
        "AWS_ENDPOINT_URL_STS": moto_server,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": SECRET_KEY,
        "AWS_DEFAULT_REGION": REGION,
        "DEADLINE_CONFIG_FILE_PATH": str(config_file),
        "DEADLINE_CLOUD_TELEMETRY_OPT_OUT": "true",
        "PYTHONPATH": extra_paths + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "PYTHONUNBUFFERED": "1",
        "QT_ACCESSIBILITY": "1",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
    }
    (tmp_path / "home").mkdir(exist_ok=True)

    return {
        "backend": backend,
        "env": env,
        "scene_dir": str(scene_dir),
        "farm_id": farm["farmId"],
        "queue_id": queue["queueId"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Depth bound for tree-text searches. Caps `app.dump()` traversal so heavy
# trees (Blender's main window) don't blow the test timeout. Submitter dialog
# labels we look for ("Submit", "Deadline", "TestFarm") sit within ~10 levels
# of the root.
_TREE_SEARCH_MAX_DEPTH = 12


def _list_apps() -> list:
    """List accessible apps, or return [] if the platform accessibility API times out.

    On Windows, UI Automation can time out (PlatformError 0x80131505) while a window in the
    session is busy, for example Blender while it builds the dialog. Every caller polls, so treat
    a timeout as "nothing yet" and let the caller's own timeout decide.
    """
    try:
        return list(xa11y.App.list())
    except xa11y.PlatformError as e:
        print(f"Listing accessible apps failed, will retry: {e}", flush=True)
        return []


def _tree_contains_text(app, needle: str) -> bool:
    """True if any element under *app* has *needle* in its name or value."""
    try:
        return needle in app.dump(max_depth=_TREE_SEARCH_MAX_DEPTH)
    except Exception:
        return False


def _dump_tree(app, out: IO[str]) -> None:
    """Write the accessibility tree for diagnostics."""
    out.write("\n--- accessibility tree ---\n")
    try:
        out.write(app.dump())
        out.write("\n")
    except Exception as e:
        out.write(f"<tree error: {e}>\n")
    out.write("--- end tree ---\n")


def _screenshot(path: Path) -> None:
    try:
        xa11y.screenshot().save_png(str(path))
    except Exception as e:
        print(f"screenshot({path.name}) failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestBlenderSubmitterGUI:
    """Open the Blender submitter dialog, submit a job via xa11y, verify the mock backend."""

    def test_submit_job_via_gui(self, blender_exe, blender_env, artifact_dir):
        backend = blender_env["backend"]
        env = blender_env["env"]
        scene_dir = blender_env["scene_dir"]

        # Launch Blender in the foreground with the submitter script
        cmd = [
            str(blender_exe),
            "--python",
            str(OPEN_SUBMITTER_SCRIPT),
            "--python-use-system-env",
            "--python-exit-code",
            "1",
            "--",
            "--output-dir",
            scene_dir,
        ]
        # Send Blender's output to a file rather than an unread pipe, which could fill up and
        # block Blender. The file is printed and kept as an artifact if the test fails.
        blender_log = artifact_dir / "blender.log"
        log_file = blender_log.open("w", encoding="utf-8", errors="replace")
        popen_kwargs: dict = {"env": env, "stdout": log_file, "stderr": subprocess.STDOUT}
        if not IS_WINDOWS:
            popen_kwargs["start_new_session"] = True

        baseline_apps = {a.name for a in _list_apps()}
        proc = subprocess.Popen(cmd, **popen_kwargs)

        try:
            # Wait for the Qt submitter dialog to appear via xa11y.
            # The dialog may appear as a new app OR within the Blender app.
            app = self._wait_for_dialog_app(proc)

            # Wait for farm/queue to resolve
            self._wait_farm_resolved(app)

            # Click Submit
            submit_btn = app.locator('button[name="Submit"]')
            submit_btn.wait_visible(timeout=STARTUP_TIMEOUT)
            submit_btn.press()

            # Blender may show "Scene has unsaved changes" dialog - dismiss it
            time.sleep(1)
            for a in _list_apps():
                for dismiss_name in ("Don't Save", "Dont Save", "Don\u2019t Save", "No"):
                    btn = a.locator(f'button[name="{dismiss_name}"]')
                    if btn.exists():
                        btn.press()
                        break

            # Dismiss "Job Submission Confirmation" dialog if it appears
            time.sleep(1)
            ok_btn = app.locator('button[name="OK"]')
            if ok_btn.exists():
                ok_btn.press()

            # Wait for submission to complete
            self._wait_submission_complete(app, backend, artifact_dir)

            # Click OK to dismiss the success dialog (if still visible)
            time.sleep(1)
            for ok_name in ("Ok", "OK"):
                ok_btn = app.locator(f'button[name="{ok_name}"]')
                if ok_btn.exists():
                    ok_btn.press()
                    break

            # Verify job was created in mock backend
            assert (
                backend.call_counts.get("CreateJob", 0) == 1
            ), f"Expected 1 CreateJob call, got {backend.call_counts}"
            assert len(backend.jobs) == 1, f"Expected 1 job, got {len(backend.jobs)}"

        except BaseException:
            # Catch BaseException, not Exception: the dialog-wait timeout raises
            # pytest.Failed (a BaseException subclass), so a bare `except Exception`
            # never fires and the failure diagnostics below are skipped.
            _screenshot(artifact_dir / "blender_submitter_failure.png")
            try:
                # Dump every app tree, not just newly-appeared ones: when the dialog
                # never opens the baseline set is all we have, and it's the evidence.
                with (artifact_dir / "accessibility_tree.txt").open("w", encoding="utf-8") as out:
                    for a in _list_apps():
                        origin = "new" if a.name not in baseline_apps else "baseline"
                        out.write(f"--- app ({origin}): {a.name} ---\n")
                        _dump_tree(a, out)
            except Exception as e:
                print(f"Could not dump the accessibility trees: {e}", file=sys.stderr)
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            log_file.close()
            sys.stderr.write(f"\n--- Blender output ({blender_log}) ---\n")
            sys.stderr.write(blender_log.read_text(encoding="utf-8", errors="replace"))
            sys.stderr.write("--- end Blender output ---\n")

    def _wait_for_dialog_app(
        self, proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT
    ) -> xa11y.App:
        """Wait for the submitter dialog to appear in any accessibility app."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if proc.poll() is not None:
                pytest.fail(
                    f"Blender exited early with code {proc.returncode}. "
                    "Its output is printed below the test failure."
                )
            # Check all apps for the submitter dialog
            for app in _list_apps():
                if _tree_contains_text(app, "Submit") and _tree_contains_text(app, "Deadline"):
                    return app
            time.sleep(0.5)
        # Also dump all app trees (one level deep) for diagnostics
        all_trees = ""
        for app in _list_apps():
            all_trees += f"\n=== APP: {app.name} ===\n"
            try:
                all_trees += app.dump(max_depth=1)
            except Exception as e:
                all_trees += f"<tree error: {e}>"
        pytest.fail(f"Submitter dialog not found within {timeout}s\nApps: {all_trees}")

    def _wait_farm_resolved(self, app: xa11y.App, timeout: float = FARM_RESOLVE_TIMEOUT):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if _tree_contains_text(app, "TestFarm"):
                return
            time.sleep(0.5)
        _dump_tree(app, sys.stderr)
        pytest.fail(f"Farm name 'TestFarm' not resolved within {timeout}s")

    def _wait_submission_complete(
        self, app: xa11y.App, backend, artifact_dir: Path, timeout: float = SUBMIT_TIMEOUT
    ):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(backend.jobs) >= 1:
                return
            # Dismiss "Scene has unsaved changes" — may be in any app (Blender native dialog)
            for a in _list_apps():
                for dismiss_name in ("Don't Save", "Dont Save", "Don\u2019t Save", "No"):
                    btn = a.locator(f'button[name="{dismiss_name}"]')
                    if btn.exists():
                        btn.press()
                        break
            try:
                statuses = [
                    (elt.name or "").strip() for elt in app.locator("static_text").elements()
                ]
            except xa11y.PlatformError as e:
                print(f"Reading the dialog failed, will retry: {e}", flush=True)
                statuses = []
            for name in statuses:
                if name == "Submission complete":
                    return
                if name in ("Submission error", "Submission canceled"):
                    _screenshot(artifact_dir / "submission_error.png")
                    _dump_tree(app, sys.stderr)
                    pytest.fail(f"Submission failed with status: {name!r}")
            time.sleep(0.5)
        _screenshot(artifact_dir / "submission_timeout.png")
        _dump_tree(app, sys.stderr)
        pytest.fail(
            f"Submission did not complete within {timeout}s. Backend calls: {backend.call_counts}"
        )
