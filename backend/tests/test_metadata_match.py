"""元数据反查打分制 / 强标识符抽取的单元测试。

对应 50 篇错配复盘的 F1-F5 修复：
- F1 PDF 强标识符（首页 DOI / arXiv 水印 / PII）抽取
- F2 Crossref/OpenAlex 打分制反查（score_item / score_work 纯函数）
- F3 衍生文献识别扩展（勘误/评论/SI 组件 .sNNN）
- F5 文件名解析补洞（SI_ 前缀 / 等。变体）
"""
from backend.pipeline.pdf_classify import (
    extract_identifiers,
    looks_like_supporting_material,
    filename_author_year,
    filename_derived_dois,
    filename_suggests_si,
    strip_filename_prefix,
    is_junk_title,
    is_derivative_literature,
    titles_compatible,
)
from backend.external.crossref import score_item
from backend.external.openalex import score_work


# ── F1: 强标识符抽取 ─────────────────────────────────────

def test_extract_identifiers_doi():
    text = (
        "Example large-scale brain-wide neural recording study\n"
        "https://doi.org/10.1234/example.2025.09  \n"
        "Received 15 May 2025; doi:10.1234/example.2025.09.\n"
    )
    ids = extract_identifiers(text)
    # 同一 DOI 大小写/尾随标点变体应去重为一条
    assert ids["dois"] == ["10.1234/example.2025.09"]
    assert ids["arxiv"] is None
    assert ids["piis"] == []


def test_extract_identifiers_arxiv_and_pii():
    ids = extract_identifiers("arXiv:1234.56789v2 [q-bio.NC] 14 Aug 2024")
    assert ids["arxiv"] == "1234.56789"
    ids = extract_identifiers("PII: S0165-0270(02)00092-4\navailable online")
    assert ids["piis"] == ["S0165-0270(02)00092-4"]


def test_extract_identifiers_multiple_texts():
    ids = extract_identifiers("", "10.1234/example.47188.001", "10.1234/example.47188")
    assert ids["dois"] == ["10.1234/example.47188.001", "10.1234/example.47188"]


def test_looks_like_supporting_material():
    assert looks_like_supporting_material("Supporting Online Material for\nExampleProbe 2.0 ...")
    assert looks_like_supporting_material("Supplementary Information\nFigure S1")
    assert not looks_like_supporting_material("Abstract\nWe recorded neurons...")


def test_headline_suggests_supporting_material():
    """严格版：标题形态命中；SI 链接 boilerplate / 带 Abstract 的首页不命中。"""
    from backend.pipeline.pdf_classify import headline_suggests_supporting_material as strict
    assert strict("Supporting Online Material for\nExampleProbe 2.0 ...")
    assert strict("# Supplementary Materials for\n\n# Some Title")
    assert strict("Supplementary information\n\n# Modelling and prediction")
    # Elsevier/Cell Press 措辞 "Supplemental Information"（2026-10-17 sample2015 mmc1 实测形态）
    assert strict("Neuron, Volume 87\nSupplemental Information\nAbstract Context Representations")
    # 正文首页 boilerplate（2026-10-15 回填实测假阳性形态）
    assert not strict("doi: https://doi.org/10.1162/x\n\nsupporting information: https://doi.org/10.1162/x_supp")
    assert not strict("supporting online material can be found at: http://www.sciencemag.org")
    assert not strict("Supplemental Information: https://doi.org/10.1016/j.x")
    assert not strict("* Supporting Information\n\nAbstract: a wide range of biomedical")
    assert not strict("Abstract\nWe recorded neurons from cortex")


# ── F5: 文件名解析 ───────────────────────────────────────

def test_strip_filename_prefix_si_and_chinese_variants():
    assert (
        strip_filename_prefix("SI_Liu 等。 - 2015 - Syringe-injectable electronics")
        == "Syringe-injectable electronics"
    )
    assert (
        strip_filename_prefix("Zheng和Meister - 2024 - The unbearable slowness of being")
        == "The unbearable slowness of being"
    )
    # SI 开头但不是 Zotero 前缀格式的，原样返回（不能误伤）
    assert strip_filename_prefix("SI joint pain study") == "SI joint pain study"


def test_filename_author_year():
    assert filename_author_year("Example et al_2012_Memory Enhancement") == ("example", 2012)
    assert filename_author_year("SI_Liu 等。 - 2015 - Syringe-injectable electronics") == ("liu", 2015)
    assert filename_author_year("Zheng和Meister - 2024 - The unbearable slowness") == ("zheng", 2024)
    assert filename_author_year("Sample_2002_Theta Oscillations") == ("sample", 2002)
    assert filename_author_year("Example_Sample_1993_Dynamics of the Hippocampal") == ("example", 1993)
    assert filename_author_year("no year in this name") == (None, None)


def test_is_junk_title_publisher_templates():
    assert is_junk_title("untitled")
    assert is_junk_title("Supporting Online Material for")
    assert is_junk_title("Supplementary Information")
    assert not is_junk_title("Supporting cast of glial cells in memory formation")


def test_is_junk_title_typesetting_artifacts():
    """出版社排版/元数据工件（2026-10-26 enrich 假警报 14 篇实录）。"""
    assert is_junk_title("ANRV314-NE30-21.tex")
    assert is_junk_title("2C_fnsys-02-004.indd")
    assert is_junk_title("D:EJSNE9403.DVI")
    assert is_junk_title("/c/1997c/tops/dvips")
    assert is_junk_title("doi:10.1234/example.2005.06")
    assert is_junk_title("Manuscript_Harvard_Liu_11092021")
    assert is_junk_title("14.10 Insight 782 Author")
    assert is_junk_title("04 AUTHOR_c")
    assert is_junk_title("Art - Author (Q)")
    assert is_junk_title("Science Journals — AAAS")
    # 负例：相近形态的真实标题不得误杀
    assert not is_junk_title("2020 COVID-19 lockdown effects on neural circuits")
    assert not is_junk_title("20 Years of neural recordings")
    assert not is_junk_title("12 Rules for neural data analysis")
    assert not is_junk_title("Actions from thoughts")
    assert not is_junk_title("Art and the brain: a perspective (review)")
    assert not is_junk_title("Signal processing for neural data")


def test_titles_compatible_subtitle_truncation():
    from backend.pipeline.pdf_classify import titles_compatible
    # 副标题截断：文件名只有主标题，出版记录带完整副标题
    assert titles_compatible(
        "Tetrode technology",
        "Tetrode technology: advances in implantable hardware, neuroimaging",
    )
    # 完全无关不兼容
    assert not titles_compatible("Tetrode technology", "Deep learning")
    # 常规相似度路径
    assert titles_compatible("Deep learning", "Deep Learning", min_sim=0.6)


# ── F3: 衍生文献识别扩展 ─────────────────────────────────

def test_derivative_erratum_comment_rejoinder():
    assert is_derivative_literature("Erratum: Power failure: why small sample size undermines", "")
    assert is_derivative_literature("Corrigendum to 'Deep learning'", "")
    assert is_derivative_literature('Comment on "Detection, Stimulation, and Inhibition"', "")
    assert is_derivative_literature("Measurement of Linear Dependence: Rejoinder", "")
    # ACS/Science SI 组件 DOI 后缀
    assert is_derivative_literature("", "10.1234/example.2019.10.s002")
    # 原论文不受影响
    assert not is_derivative_literature("Example power analysis: why small sample size undermines", "10.1234/example.2013.23")
    assert not is_derivative_literature("Response inhibition in the prefrontal cortex", "")


# ── F2: Crossref 打分制 ──────────────────────────────────

def _cr_item(title, doi, typ="journal-article", year=2012, families=None, relation=None):
    return {
        "title": [title],
        "DOI": doi,
        "type": typ,
        "issued": {"date-parts": [[year]]},
        "author": [{"family": f} for f in (families or [])],
        "relation": relation or {},
    }


def test_score_item_same_title_letter_vs_article():
    """NEJM 同名来信 vs 原论文：标题 sim 都是 1.0，作者信号决定胜负。"""
    q = "Therapeutic deep-brain stimulation for memory enhancement"
    letter = _cr_item(q, "10.1234/sample.2012.11", families=["Sample"])
    article = _cr_item(q, "10.1234/example.2012.12", families=["Example", "Extra"])
    s_letter = score_item(letter, q, query_author="example", query_year=2012)
    s_article = score_item(article, q, query_author="example", query_year=2012)
    assert s_article > s_letter


def test_score_item_hard_skips():
    q = "Injectable mesh electronics for neural recording"
    # SI 组件（type=component + is-component-of）硬跳过
    comp = _cr_item("Injectable mesh electronics SI", "10.1234/example.2019.10.s002",
                    typ="component", relation={"is-component-of": [{}]})
    assert score_item(comp, q) is None
    # 工具书条目硬跳过
    ref = _cr_item("Example, Author", "10.1234/example.ref.01",
                   typ="reference-book", year=2011)
    assert score_item(ref, "Example technology") is None
    # 勘误标题硬跳过
    err = _cr_item("Erratum: Example power analysis", "10.1234/example.2013.24", year=2013)
    assert score_item(err, "Example power analysis") is None
    # 标题太不相似直接 None
    far = _cr_item("Completely different topic", "10.1/x", families=["Sample"], year=2015)
    assert score_item(far, q, "liu", 2015) is None


def test_score_item_book_chapter_penalized():
    q = "Injectable mesh electronics for neural recording"
    chapter = _cr_item("Injectable Mesh Electronics", "10.1234/example.book.05",
                       typ="book-chapter", year=2018, families=["Somebody"])
    s = score_item(chapter, q, query_author="sample", query_year=2015)
    # 年份差 3（-0.25）+ 作者不符（-0.10）+ 书章（-0.15），高标题相似度也救不回来
    assert s is not None and s < 0.75


def test_score_item_year_author_boost():
    q = "Fully integrated sample probes for high-density neural recording"
    good = _cr_item(q, "10.1234/example.2017.14", year=2017, families=["Sample"])
    s = score_item(good, q, query_author="sample", query_year=2017)
    assert s >= 0.75


# ── F2: OpenAlex 打分制 ──────────────────────────────────

def _oa_work(title, doi, typ="article", year=2021, names=None):
    return {
        "title": title,
        "doi": f"https://doi.org/{doi}",
        "type": typ,
        "publication_year": year,
        "authorships": [{"author": {"display_name": n}} for n in (names or [])],
    }


def test_score_work_paratext_skipped():
    q = "Injectable mesh electronics for neural recording"
    si = _oa_work("Nanoenabled direct contact interfacing of injectable mesh electronics",
                  "10.1234/example.2019.10.s002", typ="paratext", year=2019)
    assert score_work(si, q) is None


def test_score_work_relevance_noise_rejected():
    q = "Understanding the computation of time using neural network models"
    noise = _oa_work("Image classification with deep convolutional neural networks",
                     "10.1234/example.2017.15", year=2017, names=["Alex Example"])
    assert score_work(noise, q, query_author="sample", query_year=2020) is None


def test_score_work_correct_match_accepted():
    q = "High-order interactions explain collective behavior of example populations"
    good = _oa_work(q + " in executive but not sensory areas",
                    "10.1234/example.2021.16", year=2021,
                    names=["Mircea I. Example", "Valentin Sample"])
    s = score_work(good, q, query_author="example", query_year=2021)
    assert s is not None and s >= 0.75


# ── 全量导入实测漏 DOI 的 6 例补洞 ─────────────────────────────

def test_extract_identifiers_pii_without_s_prefix():
    # 1983 年论文：pdf_title 'PII: 0165-0270(83)90097-3' 无 S 前缀
    ids = extract_identifiers("PII: 0165-0270(83)90097-3")
    assert ids["piis"] == ["0165-0270(83)90097-3"]
    # 带 S 的常规形态不受影响
    ids = extract_identifiers("PII: S0165-0270(02)00092-4")
    assert ids["piis"] == ["S0165-0270(02)00092-4"]


def test_titles_compatible_short_main_title():
    # 超短主标题精确相等：12 字符前缀门槛误杀（全量实测 SHORT / ExampleGrid 漏 DOI）
    assert titles_compatible("SHORT", "SHORT: a sample subtitle fixture")
    assert titles_compatible(
        "ExampleGrid",
        "ExampleGrid: an artificial subtitle fixture",
    )
    # 无冒号且整标题不相等：不误判
    assert not titles_compatible("SHORT", "Shorthand notation for neural recordings")
    # 5 字符以下不放宽（防 'Cell' 式期刊名单词误配）
    assert not titles_compatible("Cell", "Cell: something else entirely")


def test_filename_suggests_si():
    assert filename_suggests_si("41586_2006_BFnature01234_MOESM1_ESM")
    assert filename_suggests_si("SI_Author 等。 - 2015 - Sample injectable electronics")
    assert filename_suggests_si("science.aabb1234_sm")
    assert filename_suggests_si("science.aabb1235_sm1")
    assert not filename_suggests_si("Example et al_2019_SHORT")
    assert not filename_suggests_si("Deep learning")
    # 标题词尾恰好是 smooth/smart 等：不误伤
    assert not filename_suggests_si("Wang_2021_Smooth muscle cells")


def test_filename_derived_dois():
    assert filename_derived_dois("41586_2006_BFnature01234_MOESM1_ESM") == [
        "10.1038/nature01234"
    ]
    assert filename_derived_dois("science.aabb1234_sm") == [
        "10.1126/science.aabb1234"
    ]
    assert filename_derived_dois("Example et al_2019_SHORT") == []
