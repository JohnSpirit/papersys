"""解析 Zotero RDF 导出 -> ground truth JSON（供入库准确率验证）。

用法：
    python3 scripts/zotero_rdf.py <rdf路径> <导出根目录> -o groundtruth.json

输出：{"<pdf相对路径 files/ID/name.pdf>": {title, doi, url, year, first_author, item_type}}

结构要点（Zotero RDF export）：
  - 书目条目为顶层 bib:Article / bib:Book 等元素，dc:title / dc:date /
    bib:authors>rdf:Seq>rdf:li>foaf:Person
  - DOI 有三种藏匿位置（同一导出文件中三种可并存）：
      a) 条目直属 dc:identifier（"DOI 10.xxx" 前缀）
      b) 嵌套在 dcterms:isPartOf > bib:Journal 内的 dc:identifier
      c) 紧随条目之后的顶层 bib:Journal 兄弟节点内的 dc:identifier
  - 附件为顶层 z:Attachment（z:path 指向 files/<id>/<name>.pdf）
  - 父条目通过 <link:link rdf:resource="#item_N"/> 引用附件
  - URL 藏在 dc:identifier 的嵌套 dcterms:URI > rdf:value（一条目可带
    forum + pdf 两个 URI；取 forum 弃 pdf 直链，doi.org 跳转链不取，
    http 升 https——与 enrich 落 url 列的口径一致）
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "z": "http://www.zotero.org/namespaces/export#",
    "dcterms": "http://purl.org/dc/terms/",
    "bib": "http://purl.org/net/biblio#",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "link": "http://purl.org/rss/1.0/modules/link/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

TOP_LEVEL_SKIP = {"attachment"}


def _text(el, path: str) -> str:
    child = el.find(path, NS)
    return (child.text or "").strip() if child is not None and child.text else ""


def _year_of(date_str: str) -> int:
    m = re.search(r"(\d{4})", date_str or "")
    return int(m.group(1)) if m else 0


def parse_rdf(rdf_path: Path, export_root: Path) -> dict:
    tree = ET.parse(str(rdf_path))
    root = tree.getroot()

    # 第一遍：附件 #item_N -> files/<id>/<name>.pdf
    attachments: dict[str, str] = {}  # about -> 相对路径
    for el in root:
        if _text(el, "z:itemType") != "attachment":
            continue
        about = el.attrib.get(f"{{{NS['rdf']}}}about", "")
        path_el = el.find("z:path", NS)
        res = path_el.attrib.get(f"{{{NS['rdf']}}}resource", "") if path_el is not None else ""
        if about and res.lower().endswith(".pdf"):
            attachments[about] = res

    # 第二遍：书目条目 -> 元数据 + 关联附件
    children = list(root)

    def _doi_of(el) -> str:
        """DOI 可能在条目任意深度的 dc:identifier（含嵌套 Journal）。"""
        for ident in el.iter(f"{{{NS['dc']}}}identifier"):
            t = (ident.text or "").strip()
            if t.upper().startswith("DOI "):
                return t[4:].strip()
        return ""

    def _url_of(el) -> str:
        """条目的源文档链接：dc:identifier 嵌套 dcterms:URI > rdf:value。

        一条目可带多个 URI（openreview 的 forum + pdf 直链）；跳过 pdf 直链
        与 doi.org 跳转链（与 doi 字段重复），http 升 https，取首个幸存者。
        """
        for ident in el.iter(f"{{{NS['dc']}}}identifier"):
            uri = ident.find("dcterms:URI/rdf:value", NS)
            u = (uri.text or "").strip() if uri is not None else ""
            if not u and (ident.text or "").strip().lower().startswith("http"):
                u = (ident.text or "").strip()
            if not u:
                continue
            if re.search(r"/pdf(\?|$)|\.pdf$", u, re.IGNORECASE):
                continue
            if u.lower().startswith(("https://doi.org/", "http://doi.org/")):
                continue
            if u.startswith("http://"):
                u = "https://" + u[len("http://"):]
            return u
        return ""

    out: dict[str, dict] = {}
    n_no_pdf = 0
    for idx, el in enumerate(children):
        item_type = _text(el, "z:itemType")
        if not item_type or item_type in TOP_LEVEL_SKIP:
            continue
        title = _text(el, "dc:title")
        doi = _doi_of(el)
        if not doi and idx + 1 < len(children):
            # DOI 也可能在紧随其后的顶层 bib:Journal 兄弟节点里
            sib = children[idx + 1]
            if sib.tag == f"{{{NS['bib']}}}Journal":
                doi = _doi_of(sib)
        year = _year_of(_text(el, "dc:date"))
        first_author = ""
        first_li = el.find("bib:authors/rdf:Seq/rdf:li", NS)
        if first_li is not None:
            surname = _text(first_li, "foaf:Person/foaf:surname")
            first_author = re.sub(r"[^A-Za-z]", "", surname).lower()

        # 该条目引用的附件
        pdfs = []
        for link in el.findall("link:link", NS):
            ref = link.attrib.get(f"{{{NS['rdf']}}}resource", "")
            if ref in attachments:
                pdfs.append(attachments[ref])
        if not pdfs:
            n_no_pdf += 1
            continue
        for rel in pdfs:
            # 磁盘上真实存在才入 ground truth
            if not (export_root / rel).exists():
                continue
            out[rel] = {
                "title": title,
                "doi": doi.lower(),
                "url": _url_of(el),
                "year": year,
                "first_author": first_author,
                "item_type": item_type,
            }
    print(f"书目条目带 PDF: {len(out)}；无 PDF 条目: {n_no_pdf}；附件总数: {len(attachments)}",
          file=sys.stderr)
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    out_file = None
    if "-o" in sys.argv:
        out_file = sys.argv[sys.argv.index("-o") + 1]
    rdf_path = Path(args[0])
    export_root = Path(args[1])
    gt = parse_rdf(rdf_path, export_root)
    text = json.dumps(gt, ensure_ascii=False, indent=1)
    if out_file:
        Path(out_file).write_text(text, encoding="utf-8")
        print(f"written -> {out_file}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
