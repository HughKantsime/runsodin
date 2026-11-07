"""The bundled Obico model ships with its AGPL-3.0 text, notice and source pointer, inside the image."""
import fnmatch
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = ROOT / "backend" / "vision_models_default"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
# sha256 of https://www.gnu.org/licenses/agpl-3.0.txt
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
SHIPPED = ("LICENSE-AGPL-3.0.txt", "NOTICE.txt", "obico-model.cfg", "obico-model.names")


def _model_digest(path):
    """The shipped bytes' SHA-256, or the LFS OID when the checkout holds only the pointer."""
    data = path.read_bytes()
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        oid = [line for line in data.decode().splitlines() if line.startswith("oid sha256:")]
        assert len(oid) == 1, "malformed LFS pointer"
        return oid[0].removeprefix("oid sha256:")
    return hashlib.sha256(data).hexdigest()


def test_every_bundled_model_has_a_notice_naming_its_digest():
    notice = (DEFAULTS / "NOTICE.txt").read_text(encoding="utf-8")
    models = sorted(p.name for p in DEFAULTS.glob("*.onnx"))
    assert models == ["obico_spaghetti.onnx"], "a new bundled model needs its own license notice"
    # Replacing the model (or its LFS pointer) without updating the notice must fail here.
    assert _model_digest(DEFAULTS / "obico_spaghetti.onnx") == MODEL_SHA256
    assert "obico_spaghetti.onnx" in notice and MODEL_SHA256 in notice
    assert "AGPL-3.0" in notice and "LICENSE-AGPL-3.0.txt" in notice
    assert "github.com/TheSpaghettiDetective/obico-server" in notice
    assert "obico-server/tree/1e7abb0bdf1c185b38a913d8bdbdbc3daa3df634/ml_api/model" in notice
    assert "e37bc5c0757c79c6da084f18fe82d56fbb11299ae5bf0b90cc96dd844e76d1e9" in notice  # matching Darknet weights
    assert "github.com/HughKantsime/runsodin/releases/tag/third-party/obico-model-1e7abb0b" in notice  # our mirror


def test_agpl_text_is_the_unmodified_fsf_text():
    data = (DEFAULTS / "LICENSE-AGPL-3.0.txt").read_bytes()
    assert hashlib.sha256(data).hexdigest() == AGPL_SHA256


def test_license_terms_and_notices_carve_the_model_out_of_the_bsl():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "does not include the separately" in license_text and "obico_spaghetti.onnx" in license_text
    terms = (ROOT / "TERMS_OF_SERVICE.md").read_text(encoding="utf-8")
    assert "Nothing in this Agreement restricts the rights those licenses give you." in terms
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "confirmed compatible with the Business Source License" not in notices
    assert "backend/vision_models_default/obico_spaghetti.onnx" in notices and "AGPL-3.0" in notices
    assert "obico_spaghetti.onnx" in (ROOT / "NOTICE").read_text(encoding="utf-8")


def test_image_build_keeps_the_notice_files():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY backend/ ./backend/" in dockerfile
    ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]
    for name in SHIPPED:
        path = f"backend/vision_models_default/{name}"
        assert (ROOT / path).is_file(), path
        assert not any(fnmatch.fnmatch(path, rule) or fnmatch.fnmatch(name, rule) or path.startswith(rule.rstrip("/") + "/")
                       for rule in ignored), (path, "excluded from the image by .dockerignore")
