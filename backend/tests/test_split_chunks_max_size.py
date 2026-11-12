"""Test: split_into_chunks 字符上限二次切分（P1-2）。

超长 section（无 H1/H2 边界，但内容 >MAX_CHUNK_CHARS）必须被按段落边界切分，
任意单 chunk 字符数都不超过 cap。
"""
from backend.pipeline.translator import split_into_chunks, MAX_CHUNK_CHARS


def test_split_short_text_one_chunk():
    text = "# Intro\n\nHello world."
    chunks = split_into_chunks(text)
    assert len(chunks) == 1
    assert "Hello world" in chunks[0].en_text


def test_split_by_h2_boundaries():
    text = "# Intro\n\nA.\n\n## Methods\n\nB.\n\n## Results\n\nC."
    chunks = split_into_chunks(text)
    assert len(chunks) == 3
    headings = [c.heading for c in chunks]
    assert headings == ["Intro", "Methods", "Results"]


def test_split_long_section_secondary_cap():
    """单个 H2 章节超过 MAX_CHUNK_CHARS 时被二次切分。"""
    para = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 30
    big_section = "\n\n".join([para] * 30)
    text = f"# Header\n\nIntro\n\n## BigSection\n\n{big_section}\n"

    chunks = split_into_chunks(text, max_chars=2000)
    assert len(chunks) >= 3, f"expected >=3 chunks after secondary split, got {len(chunks)}"
    for c in chunks:
        assert len(c.en_text) <= 2500, f"chunk too large: {len(c.en_text)} chars (cap=2000)"


def test_split_references_kept_separate():
    text = "# Intro\n\nIntro body.\n\n# References\n\n[1] X.\n[2] Y."
    chunks = split_into_chunks(text)
    ref_chunks = [c for c in chunks if c.is_references]
    assert len(ref_chunks) == 1
    assert "[1] X." in ref_chunks[0].en_text


def test_indexes_are_contiguous():
    text = "# A\n\n" + ("p\n\n" * 5) + "## B\n\n" + ("q\n\n" * 5)
    chunks = split_into_chunks(text, max_chars=50)
    for i, c in enumerate(chunks):
        assert c.index == i, f"chunk index mismatch at position {i}: {c.index}"


def test_default_cap_uses_config():
    assert MAX_CHUNK_CHARS > 0
    assert MAX_CHUNK_CHARS >= 1000