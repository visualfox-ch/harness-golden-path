from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_image_installs_exactly_the_locked_runtime_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "uv sync --locked --no-dev" in dockerfile
    assert "COPY pyproject.toml uv.lock" in dockerfile


def test_uv_lock_is_not_excluded_from_the_build_context():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert "uv.lock" not in ignored
