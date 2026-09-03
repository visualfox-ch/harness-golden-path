from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def test_readme_describes_the_harness():
    text = README.read_text(encoding="utf-8")
    assert "Control Harness" in text
