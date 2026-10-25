from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"


def test_readme_links_every_policy_file():
    text = README.read_text(encoding="utf-8")
    missing = [
        p.name
        for p in sorted((ROOT / "policies").iterdir())
        if f"](policies/{p.name})" not in text
    ]
    assert not missing, f"README.md does not link: {', '.join(missing)}"
