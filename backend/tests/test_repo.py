"""Test: repo 层 list_papers 状态过滤与 stats 聚合（status_embedded 同步）。"""
from backend.db.conn import get_conn


def _insert_paper(dir_name: str, **status_cols) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title) VALUES (?, 2023, 'a', ?)",
            (dir_name, dir_name),
        )
        pid = cur.lastrowid
        for col, val in status_cols.items():
            conn.execute(f"UPDATE papers SET {col} = ? WHERE id = ?", (val, pid))
        return pid
    finally:
        conn.close()


def test_list_papers_status_embedded_filter(temp_db):
    from backend.db.repo import list_papers
    pid_ok = _insert_paper("repo_emb_ok", status_embedded="ok")
    _insert_paper("repo_emb_missing")  # status_embedded 默认 missing

    conn = get_conn()
    try:
        rows = list_papers(conn, status_filter={"status_embedded": "ok"})
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid_ok]


def test_list_papers_unknown_status_column_ignored(temp_db):
    from backend.db.repo import list_papers
    _insert_paper("repo_emb_any", status_embedded="ok")
    conn = get_conn()
    try:
        # 非白名单列静默忽略（防 SQL 注入），不应抛错也不应过滤
        rows = list_papers(conn, status_filter={"status_bogus": "ok"})
    finally:
        conn.close()
    assert len(rows) == 1


def test_list_papers_q_matches_coauthor(temp_db):
    """q 匹配 authors 列（JSON 数组字符串子串）：非第一作者姓名可命中。"""
    from backend.db.repo import list_papers
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, authors)
               VALUES ('repo_q_auth', 2024, 'liu', 'High-density probe',
                       '["Yuanyuan Liu", "Xiaojie Duan"]')""",
        )
        pid = cur.lastrowid
        rows = list_papers(conn, q="Duan")
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid]


def test_list_papers_q_thousand_separator(temp_db):
    """q="1024" 能命中标题里的 "1,024-channel"（replace 消除千分位逗号）。"""
    from backend.db.repo import list_papers
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title)
               VALUES ('repo_q_num', 2024, 'liu', '1,024-channel brain-wide recordings')""",
        )
        pid = cur.lastrowid
        no_comma = list_papers(conn, q="1024")
        with_comma = list_papers(conn, q="1,024")
    finally:
        conn.close()
    assert [r["id"] for r in no_comma] == [pid]
    assert [r["id"] for r in with_comma] == [pid]


def test_fts_search_hyphenated_multiword_query(temp_db):
    """fulltext 多词带连字符：一级 MATCH 报 no such column，逐词引号回退按 AND 命中。"""
    from backend.db.repo import list_papers, update_fts
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title) VALUES ('repo_fts_hy', 2024, 'a', 'Probe Paper')"
        )
        pid = cur.lastrowid
        update_fts(conn, pid, title="Probe Paper", abstract="",
                   parsed_text="Xiaojie Duan et al present a high-density probe for recordings.")
        rows = list_papers(conn, fulltext="Duan high-density probe")
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid]


def test_stats_with_embedded(temp_db):
    from backend.db.repo import stats
    _insert_paper("repo_st_ok", status_embedded="ok")
    _insert_paper("repo_st_hold", status_embedded="manual_hold")
    _insert_paper("repo_st_err", status_embedded="error")
    _insert_paper("repo_st_plain", status_parsed="ok")

    conn = get_conn()
    try:
        s = stats(conn)
    finally:
        conn.close()
    assert s["total"] == 4
    assert s["with_embedded"] == 1
    # manual_hold / errors 聚合与其它 status_* 列一致，含 status_embedded
    assert s["manual_hold"] == 1
    assert s["errors"] == 1


def test_list_papers_by_doi_exact_and_case(temp_db):
    """DOI 精确匹配大小写不敏感：存大写 JNEUROSCI，小写输入命中。"""
    from backend.db.repo import list_papers_by_doi
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('repo_doi_case', 2013, 'example', 'Spiking Example Neurons',"
            " '10.1234/EXAMPLE.2013.17')"
        )
        pid = cur.lastrowid
        rows = list_papers_by_doi(conn, "10.1234/example.2013.17")
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid]


def test_list_papers_by_doi_prefix_tolerant(temp_db):
    """https://doi.org/ 与 doi: 前缀输入均命中同一篇。"""
    from backend.db.repo import list_papers_by_doi
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('repo_doi_pfx', 2014, 'example', 'ExampleGrid', '10.1234/example.2014.07')"
        )
        pid = cur.lastrowid
        https_rows = list_papers_by_doi(conn, "https://doi.org/10.1234/example.2014.07")
        doi_rows = list_papers_by_doi(conn, "doi:10.1234/example.2014.07")
    finally:
        conn.close()
    assert [r["id"] for r in https_rows] == [pid]
    assert [r["id"] for r in doi_rows] == [pid]


def test_list_papers_by_doi_duplicates_all_returned(temp_db):
    """同一 DOI 重复入库：全部返回，按 id 升序。"""
    from backend.db.repo import list_papers_by_doi
    conn = get_conn()
    try:
        cur1 = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('repo_doi_dup1', 2021, 'example', 'Example Probe 2.0',"
            " '10.1234/example.2015.06')"
        )
        cur2 = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('repo_doi_dup2', 2021, 'example', 'Example Probe 2.0 copy',"
            " '10.1234/example.2015.06')"
        )
        pid1, pid2 = cur1.lastrowid, cur2.lastrowid
        rows = list_papers_by_doi(conn, "10.1234/example.2015.06")
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid1, pid2]


def test_list_papers_by_doi_no_match(temp_db):
    """无命中返回 []；空输入与纯前缀不查库返回 []。"""
    from backend.db.repo import list_papers_by_doi
    _insert_paper("repo_doi_absent")
    conn = get_conn()
    try:
        absent = list_papers_by_doi(conn, "10.9999/none")
        empty = list_papers_by_doi(conn, "")
        prefix_only = list_papers_by_doi(conn, "doi:")
    finally:
        conn.close()
    assert absent == []
    assert empty == []
    assert prefix_only == []


def test_list_papers_q_matches_doi(temp_db):
    """q 的 LIKE 字段含 doi 列：DOI 子串元数据命中，大小写不敏感；count_papers 同步。"""
    from backend.db.repo import list_papers, count_papers
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('repo_doi_q', 1994, 'example', 'Example correlated discharge',"
            " '10.1234/example.1994.18')"
        )
        pid = cur.lastrowid
        rows = list_papers(conn, q="10.1234/example.1994.18")
        rows_case = list_papers(conn, q="10.1234/EXAMPLE.1994.18")
        total = count_papers(conn, q="10.1234/example.1994.18")
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [pid]
    assert [r["id"] for r in rows_case] == [pid]
    assert total == 1


def test_fts_search_ranked_by_bm25(temp_db):
    """_fts_search 按 bm25 相关度排序（越小越相关，词频高者优先）。"""
    from backend.db.repo import update_fts, _fts_search
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title)"
            " VALUES ('repo_fts_dense', 2020, 'a', 'Dense')"
        )
        pid_dense = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title)"
            " VALUES ('repo_fts_sparse', 2025, 'b', 'Sparse')"
        )
        pid_sparse = cur.lastrowid
        update_fts(conn, pid_dense, title="Dense", abstract="",
                   parsed_text=" ".join(["quantum"] * 20))
        update_fts(conn, pid_sparse, title="Sparse", abstract="",
                   parsed_text="quantum " + " ".join(["filler"] * 50))
        ids = _fts_search(conn, "quantum")
    finally:
        conn.close()
    assert ids == [pid_dense, pid_sparse]


def test_is_preprint_record():
    """预印本判定：journal 含预印本服务器名或 DOI 前缀 10.1101（3.1.1 双版本选取）。"""
    from backend.db.repo import is_preprint_record
    assert is_preprint_record("bioRxiv (Cold Spring Harbor Laboratory)", None)
    assert is_preprint_record("Nature Neuroscience", "10.1101/2023.02.01.000001")
    assert is_preprint_record("arXiv", None)
    assert is_preprint_record("medRxiv", None)
    assert not is_preprint_record("Nature Neuroscience", "10.1234/example.2025.09")
    assert not is_preprint_record(None, None)
    assert not is_preprint_record("Nature (2007)", "10.1234/example.2007.25")
