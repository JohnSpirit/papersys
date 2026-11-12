"""Test: 文件名前缀剥离 / 垃圾 pdf_title 检测 / 作者变音符音译。

源自 Zotero 全库导入试点的真实缺陷：
  - "Sample et al_2015_<标题>" 整串查 Crossref -> 漏配 DOI
  - pdf_title="16264397976580 1..35" -> 反查全灭、dir_name 退化为 untitled
  - Şahin/Müller 直接删变音符 -> ahin/mller（应音译为 sahin/muller）
"""
from backend.pipeline.pdf_classify import (
    strip_filename_prefix, is_junk_title, normalize_author,
)


def test_strip_zotero_prefix_underscore():
    assert strip_filename_prefix(
        "Sample et al_2015_High-speed spelling with a noninvasive brain-computer interface"
    ) == "High-speed spelling with a noninvasive brain-computer interface"


def test_strip_zotero_prefix_cjk_deng():
    assert strip_filename_prefix(
        "Sample 等 - 2024 - Explaining Multi-Scale Choice Dynamics"
    ) == "Explaining Multi-Scale Choice Dynamics"


def test_strip_zotero_prefix_multi_author():
    assert strip_filename_prefix(
        "Example_Sample_2021_The population doctrine in cognitive neuroscience"
    ) == "The population doctrine in cognitive neuroscience"
    assert strip_filename_prefix(
        "Example和Sample - 2014 - Dimensionality reduction for large-scale neural recordings"
    ) == "Dimensionality reduction for large-scale neural recordings"


def test_strip_prefix_keeps_plain_title():
    # 无作者前缀的标题原样返回
    assert strip_filename_prefix("Attention is all you need") == "Attention is all you need"
    # 标题中间含年份但前缀不像作者（小写词）-> 不剥
    t = "Neural dynamics in 2015 revisited"
    assert strip_filename_prefix(t) == t


def test_is_junk_title():
    assert is_junk_title("16264397976580 1..35")
    assert is_junk_title("")
    assert is_junk_title(None)
    assert not is_junk_title("High-speed spelling with a noninvasive brain-computer interface")


def test_normalize_author_diacritics():
    assert normalize_author("Şahin") == "sahin"
    assert normalize_author("Müller") == "muller"
    assert normalize_author("O'Brien") == "obrien"
    assert normalize_author("Smith") == "smith"


def test_derivative_elife_review_materials():
    from backend.pipeline.pdf_classify import is_derivative_literature
    # eLife decision letter / author response（试点实测抢位）
    assert is_derivative_literature(
        "Decision letter: Reconciling functional differences in populations of neurons",
        "10.7554/elife.69068.sa1")
    assert is_derivative_literature("Author response: Some paper", "")
    assert is_derivative_literature("", "10.7554/elife.69068.sa2")
    # 原论文不受影响
    assert not is_derivative_literature(
        "Reconciling functional differences in populations of neurons",
        "10.7554/elife.69068")


def test_derivative_cover_profile():
    from backend.pipeline.pdf_classify import is_derivative_literature
    # 封面介绍：独立 DOI，标题带 "(期刊缩写. 期/年)" 尾巴（试点实测抢位）
    assert is_derivative_literature(
        "Microelectrode Arrays: Transparent, Flexible, Penetrating Microelectrode Arrays "
        "with Capabilities of Single-Unit Electrophysiology (Adv. Biosys. 3/2019)",
        "10.1002/adbi.201970033")
    # 原论文标题不含该尾巴 -> 不误伤
    assert not is_derivative_literature(
        "Transparent, Flexible, Penetrating Microelectrode Arrays with Capabilities of "
        "Single-Unit Electrophysiology", "10.1002/adbi.201800276")


def test_guess_year_underscore_wrapped():
    from backend.pipeline.pdf_classify import guess_year
    # 年份被下划线包住时 \b 失效（全量实测 4 例 year_unknown 由此而来）
    assert guess_year("Sample et al_2021_Reinforcement of neural probes") == 2021
    assert guess_year("41586_2006_BFnature01234_MOESM1_ESM") == 2006
    # 5 位以上数字串不误判
    assert guess_year("fninf-12-00024") is None
    assert guess_year("10.7554/eLife.69068") is None


def test_junk_title_microsoft_word():
    from backend.pipeline.pdf_classify import is_junk_title
    assert is_junk_title("Microsoft Word - 1234")
    assert is_junk_title("Microsoft Word - 5678.doc")
    assert not is_junk_title("Microsoft Word alternatives for academic writing in neuroscience")


def test_derivative_peerj_supp():
    from backend.pipeline.pdf_classify import is_derivative_literature
    # PeerJ 附件记录（OpenAlex title 为 null，会把 enrich 搞崩 + 引发合并）
    assert is_derivative_literature("", "10.7287/peerj.preprints.1711v2/supp-3")
    # 主记录不受影响
    assert not is_derivative_literature("", "10.7287/peerj.preprints.1711v2")


def test_junk_title_word_variants():
    from backend.pipeline.pdf_classify import is_junk_title
    # 长变体也要判垃圾（全量实测）
    assert is_junk_title("Microsoft Word - 13_EXAMPLE202007066(1#-R-III-示例- 13p).docx")
    assert is_junk_title("Microsoft Word - 1234")
