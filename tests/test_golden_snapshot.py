from pathlib import Path

from a_share_t1_engine import run_engine


def test_sample_report_matches_golden_snapshot() -> None:
    fixture = Path(__file__).parent / "fixtures" / "sample_t1_report.pdf"
    snapshot = Path(__file__).parent / "snapshots" / "sample_report.md"

    assert run_engine(fixture) == snapshot.read_text(encoding="utf-8")


def test_scoring_module_does_not_reference_llm() -> None:
    scoring = Path("src/a_share_t1_engine/scoring.py").read_text(encoding="utf-8").lower()
    forbidden = ("openai", "llm", "chatcompletion", "responses.create")

    assert all(token not in scoring for token in forbidden)
