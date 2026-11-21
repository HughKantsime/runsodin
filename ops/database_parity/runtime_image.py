"""Verify the local ODIN runtime before starting a parity probe."""
import os

from ops.database_parity.run_ams_adoption import run


def runtime_image_receipt():
    image = os.environ.get("ODIN_PARITY_IMAGE")
    expected_id = os.environ.get("ODIN_PARITY_IMAGE_ID")
    if not image:
        raise SystemExit("Set ODIN_PARITY_IMAGE to the local parity image.")
    if not expected_id:
        raise SystemExit("Set ODIN_PARITY_IMAGE_ID to the expected local image ID.")

    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()
    if image_id != expected_id:
        raise SystemExit("The local parity image ID does not match ODIN_PARITY_IMAGE_ID.")
    architecture = run("docker", "image", "inspect", "--format", "{{.Architecture}}", image_id).strip()
    return {"runtime_image": image_id, "runtime_architecture": architecture}
