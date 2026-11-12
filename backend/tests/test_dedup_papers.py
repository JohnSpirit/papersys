"""重复论文清理的 REVIEW 组保护。"""
import importlib.util
from pathlib import Path
import sys


def _load_dedup_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "dedup_papers.py"
    spec = importlib.util.spec_from_file_location("dedup_papers_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paper(module, paper_id, doi, *, status_enrich="ok"):
    statuses = {col: "ok" for col in module._STATUS_COLS}
    statuses["status_enrich"] = status_enrich
    return {
        "id": paper_id,
        "dir_name": f"paper{paper_id}_test",
        "title": "Same Title",
        "doi": doi,
        "year": 2024,
        "journal": "Journal",
        **statuses,
    }


def test_enrich_hold_with_different_dois_requires_review(monkeypatch):
    module = _load_dedup_module()
    keep = _paper(module, 1, "10.1/a")
    drop = _paper(module, 2, "10.1/b", status_enrich="manual_hold")
    monkeypatch.setattr(module, "_load_groups", lambda: [[keep, drop]])

    report = module.analyze()[0]

    assert report["keep"] is keep
    assert report["drops"] == [(drop, False)]
    assert report["review"] is True


def test_apply_never_deletes_review_group(monkeypatch, capsys):
    module = _load_dedup_module()
    keep = _paper(module, 1, "10.1/a")
    drop = _paper(module, 2, "10.1/b", status_enrich="manual_hold")
    monkeypatch.setattr(module, "analyze", lambda: [{
        "group": [keep, drop],
        "keep": keep,
        "drops": [(drop, True)],
        "review": True,
    }])

    import delete_papers
    delete_calls = []
    monkeypatch.setattr(
        delete_papers,
        "delete_one",
        lambda paper_id: delete_calls.append(paper_id) or {"ok": True, "msg": "unexpected"},
    )
    monkeypatch.setattr(sys, "argv", ["dedup_papers.py", "--apply"])

    module.main()

    assert delete_calls == []
    output = capsys.readouterr().out
    assert "safe DROP 0 篇" in output
    assert "REVIEW 组 1 个" in output
