# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""GUI tests for the Blender submitter.

Each test changes settings in the real dialog, presses Submit, and checks the CreateJob request
the mock backend received, or the error the submitter showed instead. The fixtures are in
conftest.py and the dialog helpers in submitter_ui.py.

The scene that open_submitter.py saves has one frame (1-1), renders to <scene dir>/render_####
with Cycles on the CPU, and is the only scene in the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .submitter_ui import (
    FRAME_RANGE,
    GPU_DEVICE,
    GPU_RENDERING,
    OUTPUT_DIRECTORY,
    OUTPUT_FILE_PREFIX,
    OVERRIDE_FRAME_RANGE,
    PROJECT_PATH,
    TAB_JOB_SPECIFIC,
    TAB_SHARED,
    GuiSubmitter,
    SubmitterDialog,
    job_parameter,
)


def _same_path(actual: str, expected: Path) -> bool:
    # Resolve both, so /var vs /private/var on macOS or 8.3 names on Windows still compare equal.
    return Path(actual).resolve() == expected.resolve()


class TestSubmit:
    def test_submit_with_defaults(self, gui_submitter: GuiSubmitter):
        """The dialog shows the scene's settings, and those reach CreateJob unchanged."""
        shown: dict[str, str] = {}

        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            for name in (PROJECT_PATH, OUTPUT_DIRECTORY, OUTPUT_FILE_PREFIX):
                shown[name] = dialog.get_text(name)

        request = gui_submitter.submit(configure)

        scene_dir = gui_submitter.scene_dir
        assert _same_path(shown[PROJECT_PATH], scene_dir / "cube.blend"), shown
        assert _same_path(shown[OUTPUT_DIRECTORY], scene_dir), shown
        assert shown[OUTPUT_FILE_PREFIX] == "render_####", shown

        assert _same_path(job_parameter(request, "BlenderFile"), scene_dir / "cube.blend")
        assert _same_path(job_parameter(request, "OutputDir"), scene_dir)
        assert job_parameter(request, "OutputFileName") == "render_####"
        assert job_parameter(request, "Frames") == "1-1"
        assert job_parameter(request, "RenderScene") == "Scene"
        assert job_parameter(request, "RenderEngine") == "cycles"
        assert job_parameter(request, "GPUDevice") == "NONE"
        assert job_parameter(request, "ResolutionX") == "64"
        assert job_parameter(request, "ResolutionY") == "64"


class TestJobSpecificSettings:
    def test_override_frame_range(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            frame_range = dialog.text_field(FRAME_RANGE)
            dialog.wait_enabled(frame_range, False)
            dialog.set_checkbox(OVERRIDE_FRAME_RANGE, True)
            dialog.wait_enabled(frame_range)
            dialog.set_text(FRAME_RANGE, "2-4,7")

        request = gui_submitter.submit(configure)

        assert job_parameter(request, "Frames") == "2-4,7"

    def test_frame_range_ignored_without_override(self, gui_submitter: GuiSubmitter):
        """Unticking the override goes back to the scene's frame range."""

        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_checkbox(OVERRIDE_FRAME_RANGE, True)
            dialog.set_text(FRAME_RANGE, "2-4,7")
            dialog.set_checkbox(OVERRIDE_FRAME_RANGE, False)

        request = gui_submitter.submit(configure)

        assert job_parameter(request, "Frames") == "1-1"

    def test_output_file_prefix(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_text(OUTPUT_FILE_PREFIX, "beauty_####")

        request = gui_submitter.submit(configure)

        assert job_parameter(request, "OutputFileName") == "beauty_####"

    def test_new_output_directory_is_created(self, gui_submitter: GuiSubmitter):
        output_dir = gui_submitter.scene_dir / "renders" / "shot_010"

        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_text(OUTPUT_DIRECTORY, str(output_dir))

        request = gui_submitter.submit(configure)

        assert _same_path(job_parameter(request, "OutputDir"), output_dir)
        assert output_dir.is_dir(), "The submitter should create a missing output directory"

    def test_gpu_rendering_uses_selected_device(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            gpu_device = dialog.text_field(GPU_DEVICE)
            dialog.wait_enabled(gpu_device, False)
            dialog.set_checkbox(GPU_RENDERING, True)
            dialog.wait_enabled(gpu_device)
            assert dialog.get_text(GPU_DEVICE) == "CUDA"

        request = gui_submitter.submit(configure)

        assert job_parameter(request, "GPUDevice") == "CUDA"

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "SceneSettingsWidget.update_settings prefers the GPU Device combo's currentData(), "
            "which still belongs to the first item (CUDA) until the user presses Enter, so a "
            "typed device is ignored."
        ),
    )
    def test_gpu_rendering_uses_typed_device(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_checkbox(GPU_RENDERING, True)
            dialog.wait_enabled(dialog.text_field(GPU_DEVICE))
            dialog.set_text(GPU_DEVICE, "VULKAN")

        request = gui_submitter.submit(configure)

        assert job_parameter(request, "GPUDevice") == "VULKAN"


class TestSanityChecks:
    """Settings the submitter must refuse before it creates a job (sanity_checks.py)."""

    @pytest.mark.parametrize(
        ("frames", "error"),
        [
            pytest.param(
                "5-1", "first number in the range is smaller than the second", id="reversed"
            ),
            pytest.param("1,3,1", "no duplicate frames", id="duplicate"),
        ],
    )
    def test_invalid_frame_range(self, gui_submitter: GuiSubmitter, frames: str, error: str):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_checkbox(OVERRIDE_FRAME_RANGE, True)
            dialog.set_text(FRAME_RANGE, frames)

        gui_submitter.submit_expecting_error(configure, error)

    def test_empty_output_file_prefix(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_JOB_SPECIFIC)
            dialog.set_text(OUTPUT_FILE_PREFIX, "")

        gui_submitter.submit_expecting_error(
            configure, "Please enter a prefix for the output files"
        )

    def test_empty_job_name(self, gui_submitter: GuiSubmitter):
        def configure(dialog: SubmitterDialog) -> None:
            dialog.switch_to_tab(TAB_SHARED)
            dialog.set_text("Name", "")

        gui_submitter.submit_expecting_error(configure, "No name was given for this submission")
