# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Fixtures for the Blender submitter GUI tests.

Each test launches real (non-background) Blender with this repo's submitter add-on, opens the
submitter dialog through its Blender operator, drives the Qt dialog with xa11y, and submits a job
to an in-process mock Deadline Cloud backend (plus moto for S3 job attachments). Blender loads the
add-on and deadline.blender_submitter from this repo's src/, so the tests cover the revision under
test rather than the latest PyPI release.

Requirements: a GUI session that exposes accessibility (Xvfb + AT-SPI on Linux, the Accessibility
permission on macOS), Blender at BLENDER_EXECUTABLE, and a test Python whose major.minor matches
Blender's bundled Python. See DEVELOPMENT.md.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any, Iterator

import boto3
import pytest
import yaml
from moto.server import ThreadedMotoServer
from openjd.model import (
    ParameterValue,
    ParameterValueType,
    UnsupportedSchema,
    create_job,
    decode_job_template,
)

from .mock_deadline_backend import (
    MockDeadlineBackend,
    _validation_exception,
    route,
    start_server,
)
from .submitter_ui import GuiSubmitter

REGION = "us-west-2"
BUCKET = "blender-integ-test"
ROOT_PREFIX = "DeadlineCloud"
ACCESS_KEY = "testing"
SECRET_KEY = "testing"

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
ADDONS_DIR = SRC_DIR / "deadline" / "blender_submitter" / "addons"
OPEN_SUBMITTER_SCRIPT = Path(__file__).parent / "open_submitter.py"

IS_WINDOWS = platform.system() == "Windows"


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


@pytest.fixture
def artifact_dir(request, tmp_path_factory) -> Path:
    """Where this test's failure diagnostics go. CI sets GUI_TEST_ARTIFACT_DIR and uploads it."""
    configured = os.environ.get("GUI_TEST_ARTIFACT_DIR")
    root = Path(configured) if configured else tmp_path_factory.getbasetemp() / "gui-test-artifacts"
    path = root / re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    # The S3/STS mock has no authentication, so listen on loopback only.
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    _, port = server.get_host_and_port()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


# CreateJob job parameter value types, as sent by the submitter.
_PARAMETER_VALUE_TYPES = {
    "string": ParameterValueType.STRING,
    "int": ParameterValueType.INT,
    "float": ParameterValueType.FLOAT,
    "path": ParameterValueType.PATH,
}


class RecordingDeadlineBackend(MockDeadlineBackend):
    """The mock Deadline Cloud backend, plus a record of every CreateJob request.

    The base mock instantiates each submitted template with no parameter values, which rejects any
    template with required parameters (the Blender one has several). This subclass instantiates it
    with the values CreateJob actually sent, so openjd-model validates the template and the values.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.create_job_requests: list[dict] = []

    def clear(self) -> None:
        super().clear()
        self.create_job_requests = []

    @route("POST", "/farms/{farmId}/queues/{queueId}/jobs", "CreateJob")
    def create_job(self, **kwargs: Any) -> dict:
        self.create_job_requests.append(kwargs)
        return super().create_job(**kwargs)

    def _parse_template(self, template: str) -> Any:
        request = self.create_job_requests[-1] if self.create_job_requests else {}
        submitted = request.get("parameters") or {}
        try:
            job_template = decode_job_template(
                template=yaml.safe_load(template), supported_extensions=["TASK_CHUNKING"]
            )
            values = {
                name: ParameterValue(type=_PARAMETER_VALUE_TYPES[kind], value=value)
                for name, typed_value in submitted.items()
                for kind, value in typed_value.items()
            }
            return create_job(job_template=job_template, job_parameter_values=values)
        except (yaml.YAMLError, ValueError, UnsupportedSchema, TypeError, KeyError) as e:
            raise _validation_exception(f"Invalid job template or parameters: {e!r}", "CreateJob")


@pytest.fixture(scope="session")
def mock_backend_server() -> Iterator[tuple[RecordingDeadlineBackend, str]]:
    backend = RecordingDeadlineBackend(validate_params=False)
    server, base_url, _ = start_server(backend)
    base_url = base_url.replace("127.0.0.1", "localhost")
    try:
        yield backend, base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def mock_backend(mock_backend_server) -> tuple[RecordingDeadlineBackend, str]:
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
    blender_user_dir = tmp_path / "blender-user"

    # Blender imports packages from PYTHONPATH (--python-use-system-env). Put this repo's
    # src/ and the add-on directory first so the code under test wins over any installed
    # copy, then this Python's site-packages for deadline, PySide6, and the other
    # dependencies. Editable installs don't help here: Blender doesn't process .pth files
    # in PYTHONPATH entries.
    pythonpath = [
        str(p)
        for p in [
            SRC_DIR,
            ADDONS_DIR,
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
            os.environ.get("PYTHONPATH"),
        ]
        if p
    ]

    env = {
        # Drop the caller's AWS settings (profile, region, credential files), which would point
        # Blender at real accounts or at profiles that don't exist under the redirected HOME.
        **{k: v for k, v in os.environ.items() if not k.upper().startswith("AWS_")},
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        # Keep Blender's own user files out of the developer's profile.
        "BLENDER_USER_CONFIG": str(blender_user_dir / "config"),
        "BLENDER_USER_SCRIPTS": str(blender_user_dir / "scripts"),
        "BLENDER_USER_DATAFILES": str(blender_user_dir / "datafiles"),
        "AWS_ENDPOINT_URL_DEADLINE": deadline_url,
        "AWS_ENDPOINT_URL_S3": moto_server,
        "AWS_ENDPOINT_URL_STS": moto_server,
        "AWS_ACCESS_KEY_ID": ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": SECRET_KEY,
        "AWS_DEFAULT_REGION": REGION,
        "DEADLINE_CONFIG_FILE_PATH": str(config_file),
        "DEADLINE_CLOUD_TELEMETRY_OPT_OUT": "true",
        # hatch's default environment turns the developer options on, and every hatch
        # environment inherits that. Test the dialog customers see.
        "DEADLINE_ENABLE_DEVELOPER_OPTIONS": "False",
        "PYTHONPATH": os.pathsep.join(pythonpath),
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


@pytest.fixture
def gui_submitter(blender_exe, blender_env, artifact_dir) -> GuiSubmitter:
    # --factory-startup keeps the developer's preferences, add-ons and startup file out.
    cmd = [
        str(blender_exe),
        "--factory-startup",
        "--python",
        str(OPEN_SUBMITTER_SCRIPT),
        "--python-use-system-env",
        "--python-exit-code",
        "1",
        "--",
        "--output-dir",
        blender_env["scene_dir"],
    ]
    return GuiSubmitter(
        cmd,
        blender_env["env"],
        blender_env["backend"],
        Path(blender_env["scene_dir"]),
        artifact_dir,
    )
