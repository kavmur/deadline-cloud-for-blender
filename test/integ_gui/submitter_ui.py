# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Drive the Blender submitter dialog through the accessibility tree with xa11y.

Controls are found by role and accessible name. The Job-specific settings controls get their
names from setAccessibleName calls in scene_settings_widget.py, so renaming one there means
updating the matching constant here.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any, Callable, Optional

import pytest
import xa11y

STARTUP_TIMEOUT = 30.0
SUBMIT_TIMEOUT = 60.0
FARM_RESOLVE_TIMEOUT = 15.0
WIDGET_TIMEOUT = 15.0

TAB_SHARED = "Shared job settings"
TAB_JOB_SPECIFIC = "Job-specific settings"

# Job-specific settings controls (scene_settings_widget.py).
PROJECT_PATH = "Project Path"
OUTPUT_DIRECTORY = "Output Directory"
OUTPUT_FILE_PREFIX = "Output File Prefix"
GPU_RENDERING = "Cycles GPU Rendering"
GPU_DEVICE = "GPU Device"
OVERRIDE_FRAME_RANGE = "Override Frame Range"
FRAME_RANGE = "Frame Range"

# Depth bound for tree-text searches. Caps `app.dump()` traversal so heavy
# trees (Blender's main window) don't blow the test timeout. Submitter dialog
# labels we look for ("Submit", "Deadline", "TestFarm") sit within ~10 levels
# of the root.
_TREE_SEARCH_MAX_DEPTH = 12

# The submitter's own "Scene has unsaved changes." prompt (sanity_checks.py).
_DONT_SAVE_BUTTONS = ("Don't Save", "Don’t Save")

# Element.checked values that mean "checked" across AT-SPI, UIA and AX.
_CHECKED_VALUES = ("on", "true", "1", "checked")


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


def _blender_apps(proc: subprocess.Popen) -> list:
    """The accessible apps owned by the Blender process under test.

    Only these are searched or clicked, so a terminal or editor showing the same text can't be
    mistaken for the dialog, and other apps' prompts are never answered.
    """
    return [a for a in _list_apps() if a.pid == proc.pid]


def _tree_text(app) -> str:
    """The app's accessibility tree as text (names and values), or "" if it can't be read."""
    try:
        return app.dump(max_depth=_TREE_SEARCH_MAX_DEPTH)
    except xa11y.XA11yError:
        return ""


def _press_if_present(app, selector: str) -> bool:
    """Press the first element matching *selector*; False if absent or gone mid-press."""
    try:
        button = app.locator(selector)
        if button.exists():
            button.press()
            return True
    except xa11y.XA11yError as e:
        print(f"Pressing {selector} failed, will retry: {e}", flush=True)
    return False


def _dump_tree(app, out: IO[str], max_depth: int | None = None) -> None:
    """Write the accessibility tree for diagnostics."""
    out.write("\n--- accessibility tree ---\n")
    try:
        out.write(app.dump(max_depth=max_depth))
        out.write("\n")
    except Exception as e:
        out.write(f"<tree error: {e}>\n")
    out.write("--- end tree ---\n")


def _screenshot(path: Path) -> None:
    try:
        xa11y.screenshot().save_png(str(path))
    except Exception as e:
        print(f"screenshot({path.name}) failed: {e}", flush=True)


def _wait_for(description: str, predicate: Callable[[], bool], timeout: float) -> None:
    """Poll *predicate* until it's true, retrying accessibility errors, or fail the test."""
    end = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except xa11y.XA11yError as e:
            last_error = e
        time.sleep(0.25)
    detail = f" Last error: {last_error}" if last_error else ""
    pytest.fail(f"Timed out after {timeout}s waiting for {description}.{detail}")


class SubmitterDialog:
    """The open submitter dialog in the Blender process under test."""

    def __init__(self, app: xa11y.App) -> None:
        self.app = app

    def switch_to_tab(self, name: str) -> None:
        # Qt exposes tabs as page_tab or tab on Linux and Windows, and radio_button on macOS.
        tab = self.app.locator(
            f'page_tab[name="{name}"], tab[name="{name}"], radio_button[name="{name}"]'
        )
        tab.wait_visible(timeout=WIDGET_TIMEOUT)
        tab.press()
        # Inactive tab pages can be missing from the tree, so wait for the page's controls.
        if name == TAB_JOB_SPECIFIC:
            self.text_field(OUTPUT_FILE_PREFIX).wait_visible(timeout=WIDGET_TIMEOUT)

    def text_field(self, name: str) -> xa11y.Locator:
        return self.app.locator(f'text_field[name="{name}"]')

    def check_box(self, name: str) -> xa11y.Locator:
        return self.app.locator(f'check_box[name="{name}"]')

    def combo_box(self, name: str) -> xa11y.Locator:
        return self.app.locator(f'combo_box[name="{name}"]')

    def get_text(self, name: str) -> str:
        return self.text_field(name).element().value or ""

    def set_text(self, name: str, value: str) -> None:
        """Replace a text field's contents and wait until the field shows them."""
        field = self.text_field(name)
        field.wait_visible(timeout=WIDGET_TIMEOUT)
        field.set_value(value)
        _wait_for(
            f"{name!r} to read {value!r}",
            lambda: (field.element().value or "") == value,
            WIDGET_TIMEOUT,
        )

    def is_checked(self, name: str) -> bool:
        return (self.check_box(name).element().checked or "").lower() in _CHECKED_VALUES

    def set_checkbox(self, name: str, checked: bool) -> None:
        """Set a checkbox and wait until its state reads back."""
        self.check_box(name).wait_visible(timeout=WIDGET_TIMEOUT)
        if self.is_checked(name) != checked:
            self.check_box(name).toggle()
        _wait_for(
            f"{name!r} to be {'checked' if checked else 'unchecked'}",
            lambda: self.is_checked(name) == checked,
            WIDGET_TIMEOUT,
        )

    def wait_enabled(self, locator: xa11y.Locator, enabled: bool = True) -> None:
        _wait_for(
            f"{locator.selector} to be {'enabled' if enabled else 'disabled'}",
            lambda: locator.element().enabled == enabled,
            WIDGET_TIMEOUT,
        )


def _wait_for_dialog_app(proc: subprocess.Popen, timeout: float = STARTUP_TIMEOUT) -> xa11y.App:
    """Wait for the submitter dialog to appear in Blender's accessibility tree."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            pytest.fail(
                f"Blender exited early with code {proc.returncode}. "
                "Its output is printed below the test failure."
            )
        for app in _blender_apps(proc):
            text = _tree_text(app)
            if "Submit" in text and "Deadline" in text:
                return app
        time.sleep(0.5)
    apps = ", ".join(f"{a.name} (pid {a.pid})" for a in _list_apps())
    pytest.fail(
        f"Submitter dialog not found in Blender (pid {proc.pid}) within {timeout}s. "
        f"Accessible apps: {apps}"
    )


def _wait_farm_resolved(app: xa11y.App, timeout: float = FARM_RESOLVE_TIMEOUT) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if "TestFarm" in _tree_text(app):
            return
        time.sleep(0.5)
    _dump_tree(app, sys.stderr)
    pytest.fail(f"Farm name 'TestFarm' not resolved within {timeout}s")


# The title of the message box that shows an error raised while the submitter builds the job,
# such as a failed sanity check (sanity_checks.py).
_SUBMISSION_ERROR_TITLE = "Blender job submission"


def _create_job_parameters(backend: Any) -> list:
    """The job parameters of each CreateJob request so far, for failure messages."""
    return [request.get("parameters") for request in backend.create_job_requests]


def _wait_submission_result(
    proc: subprocess.Popen,
    backend: Any,
    artifact_dir: Path,
    expected_error: Optional[str] = None,
    timeout: float = SUBMIT_TIMEOUT,
) -> None:
    """Wait for the submission to finish, answering prompts on the way.

    Without *expected_error*, the progress dialog must report "Submission complete": a CreateJob
    request alone isn't success. With it, the submitter must show an error containing that text.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            pytest.fail(f"Blender exited with code {proc.returncode} during submission")
        for app in _blender_apps(proc):
            text = _tree_text(app)
            if expected_error is not None and expected_error in text:
                return
            if "Submission complete" in text:
                if expected_error is None:
                    return
                pytest.fail(f"Submission completed, but expected the error {expected_error!r}")
            # "Error occurred:" is what the progress dialog's log shows when a request
            # such as CreateJob raises; the other two are its status label on failure.
            for failed in (
                _SUBMISSION_ERROR_TITLE,
                "Error occurred:",
                "Submission error",
                "Submission canceled",
            ):
                if failed in text:
                    _screenshot(artifact_dir / "submission_error.png")
                    _dump_tree(app, sys.stderr)
                    pytest.fail(
                        f"Submission failed with {failed!r}"
                        + (f", expected the error {expected_error!r}" if expected_error else "")
                        + f". CreateJob parameters: {_create_job_parameters(backend)}"
                    )
            if "Scene has unsaved changes" in text:
                for name in _DONT_SAVE_BUTTONS:
                    if _press_if_present(app, f'button[name="{name}"]'):
                        break
            if "Job Submission Confirmation" in text:
                _press_if_present(app, 'button[name="OK"]')
        time.sleep(0.5)
    _screenshot(artifact_dir / "submission_timeout.png")
    for app in _blender_apps(proc):
        _dump_tree(app, sys.stderr)
    outcome = f"show the error {expected_error!r}" if expected_error else "complete"
    pytest.fail(
        f"Submission did not {outcome} within {timeout}s. Backend calls: {backend.call_counts}"
    )


def run_submission(
    cmd: list[str],
    env: dict[str, str],
    backend: Any,
    artifact_dir: Path,
    configure: Optional[Callable[[SubmitterDialog], None]] = None,
    expected_error: Optional[str] = None,
) -> None:
    """Launch Blender with *cmd*, let *configure* change the dialog, then submit and wait.

    See _wait_submission_result for *expected_error*. Blender's output, a screenshot and the
    accessibility tree go to *artifact_dir* when the submission fails.
    """
    # Send Blender's output to a file rather than an unread pipe, which could fill up and
    # block Blender. The file is printed and kept as an artifact if the test fails.
    blender_log = artifact_dir / "blender.log"
    log_file = blender_log.open("w", encoding="utf-8", errors="replace")
    popen_kwargs: dict = {"env": env, "stdout": log_file, "stderr": subprocess.STDOUT}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    try:
        app = _wait_for_dialog_app(proc)
        _wait_farm_resolved(app)

        if configure is not None:
            dialog = SubmitterDialog(app)
            configure(dialog)
            # Submit is outside the tabs, but go back to the first tab so the dialog is in the
            # same state whichever tab configure left it on.
            dialog.switch_to_tab(TAB_SHARED)

        submit_btn = app.locator('button[name="Submit"]')
        submit_btn.wait_visible(timeout=STARTUP_TIMEOUT)
        submit_btn.press()

        _wait_submission_result(proc, backend, artifact_dir, expected_error)

        # Close the progress dialog or the error message.
        for blender_app in _blender_apps(proc):
            for ok_name in ("OK", "Ok"):
                if _press_if_present(blender_app, f'button[name="{ok_name}"]'):
                    break

    except BaseException:
        # Catch BaseException, not Exception: the dialog-wait timeout raises
        # pytest.Failed (a BaseException subclass), so a bare `except Exception`
        # never fires and the failure diagnostics below are skipped.
        _screenshot(artifact_dir / "blender_submitter_failure.png")
        try:
            # Blender's tree in full, and only the names of the other apps: a full dump of
            # every app on a busy desktop can take longer than the job's timeout.
            with (artifact_dir / "accessibility_tree.txt").open("w", encoding="utf-8") as out:
                for a in _list_apps():
                    if a.pid == proc.pid:
                        out.write(f"--- Blender app: {a.name} (pid {a.pid}) ---\n")
                        _dump_tree(a, out)
                    else:
                        out.write(f"--- other app: {a.name} (pid {a.pid}) ---\n")
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


class GuiSubmitter:
    """Runs one submission through the real dialog, in a new Blender process each time.

    ``configure`` gets the open dialog after the farm and queue have loaded, and before Submit is
    pressed.
    """

    def __init__(
        self,
        cmd: list[str],
        env: dict[str, str],
        backend: Any,
        scene_dir: Path,
        artifact_dir: Path,
    ) -> None:
        self.cmd = cmd
        self.env = env
        self.backend = backend
        self.scene_dir = scene_dir
        self.artifact_dir = artifact_dir

    def submit(self, configure: Optional[Callable[[SubmitterDialog], None]] = None) -> dict:
        """Submit, require "Submission complete" and one CreateJob, and return that request."""
        run_submission(self.cmd, self.env, self.backend, self.artifact_dir, configure)
        requests = self.backend.create_job_requests
        assert len(requests) == 1, f"Expected 1 CreateJob request, got {len(requests)}"
        assert len(self.backend.jobs) == 1, f"Expected 1 job, got {len(self.backend.jobs)}"
        return requests[0]

    def submit_expecting_error(
        self, configure: Callable[[SubmitterDialog], None], error: str
    ) -> None:
        """Submit and require an error message containing *error*, with no job created."""
        run_submission(
            self.cmd, self.env, self.backend, self.artifact_dir, configure, expected_error=error
        )
        assert (
            not self.backend.create_job_requests
        ), f"Expected no CreateJob request, got {_create_job_parameters(self.backend)}"


def job_parameter(create_job_request: dict, name: str) -> str:
    """The value of job parameter *name* in a CreateJob request, whatever its type."""
    parameters = create_job_request.get("parameters") or {}
    if name not in parameters:
        raise AssertionError(f"CreateJob has no {name!r} parameter; got {sorted(parameters)}")
    (value,) = parameters[name].values()
    return str(value)
