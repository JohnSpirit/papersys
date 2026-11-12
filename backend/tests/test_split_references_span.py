"""Test: split_references 标题识别与 refs 段边界（2026-09-09 Outline 导入暴露）。

`REFERENCES_RE` 此前只认 H1；带 `## References` 的文档落到回退正则后，
Introduction 的编号列表（"1. We relax..."）被误判为参考文献起点，其后全部
内容（含附录）并入单个不切块的 chunk——嵌入粒度退化、翻译整段被跳过。
"""
from backend.pipeline.translator import split_into_chunks, split_references


def test_h2_references_heading_detected():
    text = "# Title\n\nIntro body.\n\n## References\n\n[1] X.\n[2] Y."
    body, refs = split_references(text)
    assert "## References" in refs
    assert "[1] X." in refs
    assert "Intro body." in body
    assert "[1]" not in body


def test_refs_end_at_next_same_level_heading():
    """arXiv 式：References 之后还有附录，附录必须留在 body。"""
    text = ("# Title\n\nIntro.\n\n## Methods\n\nBody.\n\n"
            "## References\n\n[1] X.\n\n## A. Appendix\n\nAppendix body.")
    body, refs = split_references(text)
    assert "[1] X." in refs
    assert "A. Appendix" not in refs
    assert "Appendix body." in body
    chunks = split_into_chunks(text)
    ref_chunks = [c for c in chunks if c.is_references]
    app_chunks = [c for c in chunks if "Appendix body." in c.en_text]
    assert len(ref_chunks) == 1
    assert app_chunks and not app_chunks[0].is_references


def test_higher_level_heading_also_ends_refs():
    text = ("# Title\n\n## References\n\n[1] X.\n\n"
            "# Supporting Information\n\nSI body.")
    body, refs = split_references(text)
    assert "[1] X." in refs
    assert "SI body." in body


def test_numbered_intro_list_not_treated_as_references():
    """带 References 标题时，Introduction 的编号列表不得触发回退正则。"""
    text = ("# Title\n\nWe contribute:\n\n1. We relax the assumptions on dynamics.\n"
            "2. We build on prior work.\n\n## Methods\n\nBody.\n\n"
            "## References\n\n[1] X.\n[2] Y.")
    body, refs = split_references(text)
    assert "We relax" in body
    assert "Body." in body
    assert refs.startswith("\n## References")


def test_long_refs_section_char_split():
    """refs 段超 cap 时按段落/行边界切分为多个 is_references chunk。"""
    entries = "\n".join(
        f"[{i}] Author{i}, A. Title{i}. Journal, 2020." for i in range(1, 400)
    )
    text = f"# Title\n\nIntro.\n\n## References\n\n{entries}"
    chunks = split_into_chunks(text, max_chars=2000)
    ref_chunks = [c for c in chunks if c.is_references]
    assert len(ref_chunks) >= 2
    for c in ref_chunks:
        assert len(c.en_text) <= 2500
    joined = "\n".join(c.en_text for c in ref_chunks)
    assert "[1] Author1" in joined and "[399] Author399" in joined


def test_fallback_without_heading_unchanged():
    """无 References 标题时仍走回退正则（命中处到文末，历史行为）。"""
    text = "# Title\n\nIntro.\n\n1. Kelso JS. Dynamic Patterns. MIT Press; 1995.\n2. Other."
    body, refs = split_references(text)
    assert "1. Kelso" in refs
    assert "Intro." in body
