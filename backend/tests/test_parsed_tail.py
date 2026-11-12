"""Test: strip_preceding_tail（扫描合订本别文末尾页的保守剥离）。"""
from backend.services.doc_text import strip_preceding_tail

_TITLE = "Neural Population Coding of Reaching Direction"


def _tail_page() -> str:
    refs = "\n".join(
        f"[{i}] Smith J, Doe A. Some unrelated earlier article title. Science, 1980."
        for i in range(1, 12)
    )
    return refs + "\n\n"


def test_strips_reference_tail_before_title():
    text = _tail_page() + "# Neural Population Coding of Reaching Direction\n\nReal abstract here."
    out, n = strip_preceding_tail(text, _TITLE)
    assert n > 0
    assert out.startswith("# Neural Population Coding")
    assert "Some unrelated earlier article" not in out
    assert "Real abstract here." in out


def test_no_strip_when_title_at_start():
    text = "# Neural Population Coding of Reaching Direction\n\nBody."
    out, n = strip_preceding_tail(text, _TITLE)
    assert (out, n) == (text, 0)


def test_no_strip_for_short_prefix():
    """前文不足 800 字符（期刊页眉级别噪声）不剥。"""
    text = "Neuron, Volume 87\n\n# Neuronal Population Coding of Movement Direction\n\nBody."
    out, n = strip_preceding_tail(text, _TITLE)
    assert (out, n) == (text, 0)


def test_no_strip_when_prefix_not_tail_shaped():
    """前文够长、无参考文献形态、且占全文 <20%：不像别文末尾，不剥。"""
    filler = ("General introduction prose that is clearly part of this very paper. " * 40)
    body = "Real body of the paper. " * 600
    text = filler + "\n\n# Neural Population Coding of Reaching Direction\n\n" + body
    out, n = strip_preceding_tail(text, _TITLE)
    assert (out, n) == (text, 0)


def test_strips_when_prefix_huge_even_without_ref_pattern():
    """前文占全文 ≥20% 时即使无文献条目也剥（大段别文正文的场景）。"""
    other_article = ("This is the last column of a completely different article. " * 60)
    body = "Real body of the paper. " * 200
    text = other_article + "\n\n# Neural Population Coding of Reaching Direction\n\n" + body
    out, n = strip_preceding_tail(text, _TITLE)
    assert n > 0 and out.startswith("# Neural Population")


def test_title_line_wrapped_by_mineru():
    """MinerU 折行导致标题跨行：归一化后仍能锚定。"""
    text = _tail_page() + "# Neural Population Coding of Reaching\nDirection\n\nBody."
    out, n = strip_preceding_tail(text, _TITLE)
    assert n > 0 and "unrelated earlier article" not in out


def test_missing_title_or_anchor():
    assert strip_preceding_tail("whatever text", "") == ("whatever text", 0)
    text = _tail_page() + "# Totally Different Title\n\nBody."
    out, n = strip_preceding_tail(text, _TITLE)
    assert (out, n) == (text, 0)


def test_idempotent_after_strip():
    text = _tail_page() + "# Neural Population Coding of Reaching Direction\n\nBody."
    once, n1 = strip_preceding_tail(text, _TITLE)
    twice, n2 = strip_preceding_tail(once, _TITLE)
    assert n1 > 0 and n2 == 0 and twice == once


def test_anchor_matches_title_with_html_markup_at_top():
    """实测形状：文首标题带 <sub> 标记，锚点不得落到文末参考文献里的
    纯文本重名（否则会截肢整篇正文，只留文献尾巴）。"""
    title = "Form vision via a high-channel-count cortical implant in example primates"
    refs_tail = "\n".join(
        f"{i}. J. Author{i}, Some citation. Science, 20{i:02d}." for i in range(1, 60)
    )
    text = ("NEUROSCIENCE\n\n# Form vision via a high<sub>-</sub>channel"
            "<sub>-</sub>count cortical implant in example primates\n\n"
            "Example Author et al.\n\nReal body. " * 1 + "Real body paragraph. " * 400
            + "\n\n## References\n\n" + refs_tail
            + "\n\n42. A. Sample, Data repository for \u201cForm vision via a "
              "high-channel-count cortical implant in example primates,\u201d Mendeley.")
    out, n = strip_preceding_tail(text, title)
    assert n == 0
    assert out.startswith("NEUROSCIENCE")


def test_own_supplementary_prefix_not_stripped():
    """实测形状：前文是「本篇」SI 图注（Supplementary Figure），不是
    别文末尾，不得剥。"""
    title = "Long-term mapping of single-neuron activity in example cortex"
    si = "\n\n".join(
        f"## Supplementary Figure {i}\n\nSchematic step {i} of the fabrication. "
        f"Components include silicon wafer and mesh electronics. " * 8
        for i in range(1, 8)
    )
    text = si + "\n\n# Long-term mapping of single-neuron activity in example cortex\n\nExample Author et al.\n\nBody. " * 1 + "Body. " * 500
    out, n = strip_preceding_tail(text, title)
    assert n == 0
    assert "Supplementary Figure 1" in out
