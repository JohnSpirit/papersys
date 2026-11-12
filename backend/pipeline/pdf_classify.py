"""PDF 类型判断：矢量 vs 扫描 / 元信息提取。"""
import re
import subprocess
from pathlib import Path
from typing import Optional

# PDF magic bytes：所有合法 PDF 文件均以 "%PDF-" 开头（可能前置 BOM/空白）。
# 用于在 ingest/parse 入口拦截伪装成 PDF 的 HTML/文本文件，避免 MinerU 返回
# 含糊的 400 错误（实际错误体 "Unsupported file type: html" 会被旧代码丢掉）。
_PDF_MAGIC = b"%PDF-"


def is_real_pdf(path: str) -> bool:
    """检查文件头是否为 PDF magic bytes（容忍前置空白/BOM）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        return _PDF_MAGIC in head
    except OSError:
        return False


def pdfinfo(pdf_path: str) -> dict:
    """通过 PyMuPDF 获取页数 / 大小 / 标题 / 作者。"""
    import fitz
    info: dict = {}
    try:
        with fitz.open(pdf_path) as doc:
            info["pages"] = doc.page_count
            info["size_bytes"] = Path(pdf_path).stat().st_size
            meta = doc.metadata or {}
            info["pdf_title"] = (meta.get("title") or "").strip() or None
            info["pdf_authors"] = (meta.get("author") or "").strip() or None
            info["text_chars"] = 0
            # 抽前 3 页文本判类型
            for i in range(min(3, doc.page_count)):
                info["text_chars"] += len(doc.load_page(i).get_text())
    except Exception as e:
        info["error"] = str(e)
    return info


def classify_pdf(info: dict) -> str:
    """根据 text_chars / pages 推断 vector vs scan。"""
    pages = info.get("pages") or 1
    chars = info.get("text_chars") or 0
    per_page = chars / max(min(3, pages), 1)
    if per_page >= 1500:
        return "vector"
    if per_page < 200:
        return "scan"
    return "vector"  # 兜底走 pipeline


def extract_first_page_text(pdf_path: str, max_chars: int = 4000) -> str:
    import fitz
    try:
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return ""
            return doc.load_page(0).get_text()[:max_chars]
    except Exception:
        return ""


def extract_pages_text(pdf_path: str, n_pages: int = 2, max_chars: int = 9000) -> str:
    """抽前 n 页全文（拼接），用于扫描首页/次页印刷的 DOI、arXiv 水印、PII。"""
    import fitz
    try:
        with fitz.open(pdf_path) as doc:
            out = []
            for i in range(min(n_pages, doc.page_count)):
                out.append(doc.load_page(i).get_text())
            return "\n".join(out)[:max_chars]
    except Exception:
        return ""


# ── 文件名/PDF 标题 → 候选 first_author / year / keyword ──

_AUTHOR_RE = re.compile(r"^([A-Za-z][\w\-]+?)(?:\s*[,&]|\s+et\s+al)", re.IGNORECASE)
# 年份边界用"数字前后不是数字"判定而非 \b：Zotero/出版商文件名里年份常被下划线
# 包住（如 "Sample et al_2021_xxx"、"41586_2006_BFnature01234_MOESM1_ESM"），
# \w 含下划线导致 \b 失效、年份漏提（全量实测 4 例 year_unknown）。
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# Zotero 导出文件名常带 "作者_年份_" / "作者 等 - 年份 - " 前缀；
# 整个 stem 直接当标题去查 Crossref 会被作者/年份干扰导致漏配（实测 PNAS 论文漏 DOI）。
_FN_YEAR_SEP_RE = re.compile(r"[\s_\-–]((?:19|20)\d{2})[\s_\-–]+")
_FN_COMPACT_AUTHOR_YEAR_RE = re.compile(
    r"^([A-Za-z][A-Za-z\-']{1,40}?)((?:19|20)\d{2})(?:$|[\s_\-–])"
)
_FN_AUTHOR_STOP = {"et", "al", "al.", "等"}


def extract_title_from_text(text: str, max_lines: int = 24) -> Optional[str]:
    """从首页正文猜论文标题；无法识别时返回 None。

    老 PDF 的 metadata 常为空或只有排版文件名，首页前几十行仍保留真实题名。
    这里只取单行候选，避免把作者单位拼进标题；跨行题名由标题前缀匹配兜底。
    """
    if not text:
        return None
    skip = re.compile(
        r"(?i)(?:^|\b)(?:doi\b|https?://|www\.|copyright|creative commons|"
        r"received|accepted|published|abstract\b|introduction\b|references\b|"
        r"department\b|university\b|institute\b|laboratory\b|"
        r"journal\b|volume\b|issue\b|pages?\b|elsevier|springer|"
        r"supporting\s+(?:online\s+)?(?:material|information)|"
        r"supplementary\s+(?:material|information))"
    )
    production = re.compile(r"^[A-Za-z0-9_.-]+\s+\d+\.\.\d+$")
    lines = [re.sub(r"^\s*#{1,6}\s*", "", line).strip()
             for line in text.splitlines()[:max_lines]]
    for line in lines:
        if not 15 <= len(line) <= 240 or production.match(line):
            continue
        if skip.search(line) or line.isupper() or line.count(",") > 2:
            continue
        letters = sum(c.isalpha() for c in line)
        words = re.findall(r"[A-Za-z][A-Za-z\-']*", line)
        if letters < 10 or letters / len(line) < 0.5 or len(words) < 3:
            continue
        if sum(c.isdigit() for c in line) > max(4, len(line) // 8):
            continue
        return line
    return None


def first_author_from_pdf_metadata(authors: Optional[str]) -> Optional[str]:
    """把 PDF metadata 的作者串转成一作姓 slug；单单词条目不采纳。"""
    if not authors:
        return None
    first = re.split(r"\s*(?:;|\band\b|&)\s*", authors.strip(), maxsplit=1)[0].strip()
    if not first or re.fullmatch(r"(?i)et\s+al\.?", first):
        return None
    if "," in first:
        surname = first.split(",", 1)[0].strip()
    else:
        words = first.split()
        if len(words) < 2:
            return None
        surname = words[-1]
    return normalize_author(surname) or None


def _looks_like_authors(head: str) -> bool:
    """年份前的片段像不像作者列表：1-4 个词，每词首字母大写或为 et/al/等/和X/中文。"""
    words = [w.strip("。.·,、") for w in head.replace("和", " ").split()]
    words = [w for w in words if w]
    if not 1 <= len(words) <= 4 or len(head) > 60:
        return False
    return all(
        w[0].isupper() or w.lower() in _FN_AUTHOR_STOP or "一" <= w[0] <= "鿿"
        for w in words
    )


def strip_filename_prefix(stem: str) -> str:
    """剥掉 Zotero 文件名的 '作者_年份_' 前缀，返回纯标题部分（不像前缀则原样返回）。

    SI 附件常带 "SI_" / "SI " 前缀（如 "SI_Liu 等。 - 2015 - xxx"），先按原样尝试，
    不匹配再剥 SI 前缀重试；两轮都不像作者前缀才原样返回（避免误伤 "SI joint ..."）。
    """
    variants = [stem, re.sub(r"(?i)^si[_\s\-]+", "", stem)]
    for s in dict.fromkeys(variants):
        m = _FN_YEAR_SEP_RE.search(s)
        if not m:
            continue
        head = s[:m.start()].strip(" -_–")
        rest = s[m.end():].strip()
        if head and rest and _looks_like_authors(head):
            return rest
    return stem


def filename_author_year(stem: str) -> tuple[Optional[str], Optional[int]]:
    """从 Zotero 风格文件名提取 (一作姓 slug, 年份)；不像则返回 (None, None)。

    反查校验信号：同名勘误/来信/评论与原论文标题几乎一致，只有作者+年份能区分
    （实测 NEJM 同名来信抢位、Neuron 同名勘误抢位各多例）。
    """
    s = re.sub(r"(?i)^si[_\s\-]+", "", stem)
    compact = _FN_COMPACT_AUTHOR_YEAR_RE.match(s)
    if compact:
        author = normalize_author(compact.group(1)) or None
        if author and author not in ("si", "et", "al"):
            return author, int(compact.group(2))
    m = _FN_YEAR_SEP_RE.search(s)
    if not m:
        return None, None
    head = s[:m.start()].strip(" -_–")
    if not head or not _looks_like_authors(head):
        return None, None
    first = re.split(r"[\s_和,]+", head.strip())[0]
    author = normalize_author(first.strip("。.·,")) or None
    if author in ("si", "et", "al"):
        author = None
    return author, int(m.group(1))


def is_junk_title(t: Optional[str]) -> bool:
    """pdfinfo 元数据标题可能是垃圾，应回退到文件名 stem。判定：
    - 空 / 字母过少 / 字母占比过低（如 '16264397976580 1..35'）
    - MS Word 模板默认标题 'Microsoft Word - NNNN'（SI 附件常见，全量实测
      此类标题查 Crossref 会命中无关 PeerJ 附件记录，导致 dir_name 合并灾难）
    - 出版社模板标题 'untitled' / 'Supporting Online Material for' 等：
      不含论文标题信息，拿去反查只会命中同名垃圾记录
    """
    if not t:
        return True
    s = t.strip()
    # Word 模板默认名一律以 "Microsoft Word -" 开头（'Microsoft Word - 1234'、
    # 'Microsoft Word - 13_EXAMPLE202007066(1#-R-III-示例- 13p).docx' 等变体）
    if re.match(r"(?i)^microsoft word\s*-", s):
        return True
    if re.match(r"(?i)^untitled(?:[-_\s]*\d+)?$", s):
        return True
    if re.match(r"(?i)^overleaf\s+example$", s):
        return True
    if re.match(r"(?i)^(supporting|supplementary)\s+(online\s+)?(material|information)\b", s):
        return True
    # 排版系统写入的"刊名/稿件号 + 起止页"，例如 ELIFE04677 1..21、
    # jcn.a.54 2295..2312、SBJNL_EX_0042 101..109，不是论文题名。
    if re.match(r"^[A-Za-z0-9_.-]+\s+\d+\.\.\d+$", s):
        return True
    # 排版/元数据工件（enrich 假警报实测：pdf_title 是出版社内部工件，
    # 而 DOI 与库内标题均正确——'ANRV314-NE30-21.tex'、'D:EJSNE9403.DVI'、
    # '2C_fnsys-02-004.indd'、'doi:10...'、'Manuscript_Harvard_Author_11092021'、
    # '14.10 Insight 782 Author'、'04 AUTHOR_c'、'Art - Author (Q)'、
    # 'Science Journals — AAAS'、路径形态）
    if re.match(r"(?i)^doi:\s*10\.", s):
        return True
    if re.search(r"(?i)\.(tex|indd|dvi|eps|fm|idml|qxd|qxp|docx?|pptx?)$", s):
        return True
    if re.match(r"^[\\/]", s):
        return True
    if re.match(r"(?i)^manuscript[_\s-]", s):
        return True
    if re.match(r"(?i)^science journals\b", s):
        return True
    if re.match(r"(?i)^\d+(\.\d+)?\s+insight\b", s):
        return True
    if re.match(r"(?i)^art\b.*\([A-Za-z]\)$", s):
        return True
    # 两位数字前缀需同时具备大写稿件码和 _x 后缀，避免误杀真实题名。
    if re.fullmatch(r"[0-9]{2}\s+[A-Z][A-Z0-9]*_[A-Za-z]", s):
        return True
    letters = sum(c.isalpha() for c in t)
    return letters < 8 or letters / max(len(t), 1) < 0.3


def normalize_author(name: str) -> str:
    """作者姓 -> ASCII slug。变音符先 NFKD 音译（Şahin->sahin, Müller->muller），
    而不是直接删除（直接删除会变成 ahin/mller）。"""
    import unicodedata
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z]", "", n).lower()


def guess_year(*texts: str) -> Optional[int]:
    for t in texts:
        if not t:
            continue
        m = _YEAR_RE.search(t)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2030:
                return y
    return None


def guess_first_author(*texts: str) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        m = _AUTHOR_RE.search(t.strip())
        if m:
            return m.group(1).lower()
    return None


def slugify_keyword(title: str, max_words: int = 2) -> str:
    if not title:
        return "untitled"
    words = re.findall(r"[A-Za-z]+", title.lower())
    stopwords = {"the", "a", "an", "of", "in", "on", "and", "to", "for", "with"}
    keep = [w for w in words if w not in stopwords][:max_words]
    return "_".join(keep) or "untitled"


def build_dir_name(first_author: str, year: int, keyword: str) -> str:
    keyword = re.sub(r"[^a-z0-9_]+", "_", keyword.lower()).strip("_") or "x"
    return f"{first_author.lower()}{year}_{keyword}"


# ── 衍生文献识别 ──────────────────────────────────────────
# Faculty Opinions / F1000 等推荐性衍生文献会出现在 Crossref/OpenAlex
# 搜索结果前列，且标题常为 "Faculty Opinions recommendation of <原论文标题>"，
# 导致标题相似度校验失效、张冠李戴。反查时一律跳过此类条目。
# eLife 的 Decision letter / Author response 同理（type=peer-review，
# DOI 带 .saN 后缀，标题 "Decision letter: <原论文标题>"，试点实测抢位）。
# 勘误/评论/回复/来信与原文标题几乎一致（sim>0.9），纯标题阈值无法拦截，
# 是错配复盘里最大的单一类别（Erratum/Corrigendum/Comment 各多例）。

_DERIVATIVE_TITLE_KEYWORDS = (
    "faculty opinions recommendation of",
    "f1000 prime recommendation",
    "f1000 recommendation",
    "recommendation of:",
    "decision letter",
    "author response",
    "letter to the editor",
)

# 前缀型：勘误/更正常以 "Erratum:"/"Corrigendum to" 等开头且后接原文标题
_DERIVATIVE_TITLE_PREFIX_RE = re.compile(
    r"^(erratum|errata|corrigendum|correction to|comment on|commentary on"
    r"|reply to|response to)\b",
    re.IGNORECASE,
)
# 后缀型：JASA 等期刊讨论稿 "…: Rejoinder"（实测抢位 Geweke 1982 正文）
_DERIVATIVE_TITLE_SUFFIX_RE = re.compile(r":\s*rejoinder\s*$", re.IGNORECASE)

# eLife 评审材料 DOI 后缀：.sa1=decision letter / .sa2=author response；
# PeerJ 附件 DOI 后缀 /supp-N（OpenAlex 里 title 常为 null，会把 enrich 搞崩，
# 且垃圾查询的 Crossref 首 hit 经常是这类记录——全量实测 dir_name 合并灾难源头）；
# ACS/Science 等的 SI 组件 DOI 后缀 .sNNN（Crossref type=component，
# 实测抢位 5+ 篇：10.1234/example.2019.10.s002 等）
_DERIVATIVE_DOI_RE = re.compile(r"(\.sa\d+$|/supp-\d+$|\.s\d{2,3}$)")

# 封面介绍（cover profile）：标题以 "(期刊缩写. 期/年)" 结尾，如
# "Microelectrode Arrays: <原题> (Adv. Biosys. 3/2019)"——独立 DOI 的记录，非原论文。
_DERIVATIVE_COVER_RE = re.compile(r"\([A-Za-z. ]+\d+/\d{4}\)\s*$")


def is_derivative_literature(title: str, doi: str = "") -> bool:
    """识别 Faculty Opinions / F1000 / eLife 评审材料 / 勘误评论回复 / SI 组件 /
    封面介绍这类衍生文献（非原论文）。"""
    t = (title or "").lower().strip()
    if any(kw in t for kw in _DERIVATIVE_TITLE_KEYWORDS):
        return True
    if _DERIVATIVE_TITLE_PREFIX_RE.match(t):
        return True
    if _DERIVATIVE_TITLE_SUFFIX_RE.search(t):
        return True
    if _DERIVATIVE_COVER_RE.search(title or ""):
        return True
    d = (doi or "").lower()
    if d.startswith("10.3410/f"):
        return True
    if _DERIVATIVE_DOI_RE.search(d):
        return True
    return False


# ── PDF 自身携带的强标识符（首页 DOI / arXiv 水印 / Elsevier PII）────────────
# 比标题模糊搜索可靠得多（错配复盘抽样：大部分前两页/pdf_title
# 直接印着正确标识符，Nature/Science/eLife/IEEE 均在页眉页脚印 DOI）。
_DOI_TEXT_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>\[\]{}()]+")
_ARXIV_TEXT_RE = re.compile(r"\barxiv:(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
# Elsevier PII：老论文 PDF 元数据常只有 'PII: S0165-0270(02)00092-4'，
# 拼上 10.1016/ 前缀即 DOI（多篇 1990 年代论文实测命中）。
# S 前缀可选：存在无前缀变体 'PII: 0165-0270(83)90097-3'（1983 年论文实测），
# 其注册 DOI 也无 s；S 前缀与注册 DOI 是否一致因年代而异，两个变体都要试（见 ingest 1b）。
_PII_RE = re.compile(r"\bS?\d{4}-\d{4}\(\d{2}\)\d{4,5}-[\dXx]\b")


def extract_identifiers(*texts: str) -> dict:
    """从文本（前两页正文 / pdf_title 等）抽取 DOI / arXiv id / PII，按出现顺序去重。"""
    dois: list[str] = []
    piis: list[str] = []
    arxiv: Optional[str] = None
    for t in texts:
        if not t:
            continue
        for m in _DOI_TEXT_RE.finditer(t):
            d = m.group(0).rstrip(".,;:")
            if d.lower() not in {x.lower() for x in dois}:
                dois.append(d)
        for m in _PII_RE.finditer(t):
            p = m.group(0)
            if p.upper() not in {x.upper() for x in piis}:
                piis.append(p)
        if not arxiv:
            m = _ARXIV_TEXT_RE.search(t)
            if m:
                arxiv = m.group(1)
    return {"dois": dois, "piis": piis, "arxiv": arxiv}


_SI_PHRASES = (
    "supporting online material",
    "supporting information",
    "supplementary material",
    "supplementary information",
    # Elsevier/Cell Press 系用 "Supplemental Information"（2026-10-17 sample2015
    # mmc1 实测：Neuron SI 首页第 2 行即此措辞，旧词表漏检导致真附件未标记）
    "supplemental material",
    "supplemental information",
)


def looks_like_supporting_material(text: str) -> bool:
    """前两页文本是 SI 附件（此时页面上印的 DOI 是母论文的，校验放宽直接采纳）。"""
    head = (text or "")[:800].lower()
    return any(k in head for k in _SI_PHRASES)


# abstract 节标记，两种形态：(a) "Abstract:"/"ABSTRACT:" 后随任意正文（冒号决定
# 它是节标题而非题名）；(b) 独行 "Abstract"（可带 #/* 前缀）。不能用 \babstract
# 词界判定——论文标题以 Abstract 起首时（2026-10-17 sample2015 Neuron SI 首页标题
# "Abstract Context Representations ..." 实测）会被误判成摘要节，把真 SI 拒之门外。
_ABSTRACT_HEAD_RE = re.compile(r"(?im)^\W{0,4}abstract\s*[:：]|^\W{0,4}abstract\s*$")


def headline_suggests_supporting_material(text: str) -> bool:
    """严格版 SI 头部判定（ingest 与回填共用）：SI 短语以标题形态出现
    （行首起、后随非冒号、非 "can be found"），且头部窗口内无 abstract 标记。

    looks_like_supporting_material 的精度加强版。正文首页常见指向 SI 的
    boilerplate 不构成命中（回填实测存在宽松判定假阳性）：
    - MIT Press 页脚 "supporting information: https://doi.org/..."；
    - Science 页脚 "supporting online material can be found at ..."；
    - ACS 导航栏 "* Supporting Information" + 同页 Abstract。

    假阴性取舍：真 SI 首页以 "Supporting/Supplementary ..." 为标题行；
    带 Abstract 节的补充文件不会被判中——此时未标记等于维持入库前行为，
    代价小于误标（误标会把正文排除出 Zotero 导出与语义检索）。
    """
    h = (text or "")[:800].lower()
    if _ABSTRACT_HEAD_RE.search(h):
        return False
    for line in h.splitlines():
        s = line.strip(" #*\t")
        for kw in _SI_PHRASES:
            if s.startswith(kw):
                rest = s[len(kw):]
                if rest.startswith(":") or rest.startswith(" can be found"):
                    continue
                return True
    return False


# ── 文件名携带的 SI / 出版商标识信号 ─────────────────────────────
# Nature 2006-2010 附件命名 '41586_2006_BFnature01234_MOESM1_ESM'、Science 附件
# 'science.aabb1234_sm'：这类老 SI 前两页没有任何印刷标识符也没有 SI 字样，
# 只有文件名能还原母论文 DOI（全量实测 nature01234 SI 因此漏 DOI）。
_FN_NATURE_SI_RE = re.compile(r"_BF(nature\d{4,})_", re.IGNORECASE)
_FN_SCIENCE_SI_RE = re.compile(
    r"(science\.[a-z0-9]+)[_\-]sm\d*(?:[_.\-]|$)", re.IGNORECASE
)
_SI_STEM_RE = re.compile(
    r"(?i)(^si[_\s\-]|moesm\d|[_\-]esm\d*(?:[_.\-]|$)|[_\-]sm\d*(?:[_.\-]|$)"
    r"|[_\-]supp(?:lementary|l)?\b)"
)


def filename_suggests_si(stem: str) -> bool:
    """文件名形如 SI 附件（SI_ 前缀 / MOESM / _sm / _ESM / _supp 后缀）。
    首页文本无 SI 字样时的补充信号（Nature 老 SI 首页只有图表标题）。"""
    return bool(_SI_STEM_RE.search(stem or ""))


def filename_derived_dois(stem: str) -> list[str]:
    """从出版商 SI 附件命名规则还原母论文 DOI（首页无印刷标识符时的兜底候选）。"""
    out: list[str] = []
    m = _FN_NATURE_SI_RE.search(stem or "")
    if m:
        out.append("10.1038/" + m.group(1).lower())
    m = _FN_SCIENCE_SI_RE.search(stem or "")
    if m:
        out.append("10.1126/" + m.group(1).lower())
    return out


def titles_compatible(a: str, b: str, min_sim: float = 0.5) -> bool:
    """标题近似判定：difflib 相似度达标，或一方是另一方的主标题/前缀
    （副标题截断场景：文件名 'Tetrode technology' vs 出版记录
    'Tetrode technology: advances in implantable hardware...'，difflib 只有 0.32）。
    """
    from difflib import SequenceMatcher
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= min_sim:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 12 and long_.startswith(short):
        return True
    main_long = long_.split(":", 1)[0].strip()
    if len(main_long) >= 12 and (short == main_long or short.startswith(main_long)):
        return True
    # 主标题（冒号前）精确相等放宽到 5 字符：超短主标题场景 difflib 只有 0.1-0.2、
    # 前缀分支的 12 字符门槛会误杀（全量实测 'SHORT' vs 'SHORT: a sample subtitle'、
    # 'ExampleGrid' vs 'ExampleGrid: an artificial subtitle' 各 1 例漏 DOI）。
    if len(short) >= 5 and short == main_long:
        return True
    return False