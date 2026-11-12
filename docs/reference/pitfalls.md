# 历史踩坑记录

> 维护约定：新问题记入对应小节；`AGENTS.md` 只保留高频环境速查与本文件指引。

## 环境与工具

| 错误 | 原因 | 解决 |
|---|---|---|
| Vite 502 Bad Gateway | 后端进程死 | `paper serve --detach` 重启 |
| `python3 backend/app.py` 被 SIGKILL | opencode bash timeout | 进程内 test_client 冒烟（`create_app()`）或独立后台启动 |
| `sqlite3.OperationalError: no such table` | 数据库未跑 init_db | `PYTHONPATH=. python3 -c 'from backend.db.conn import init_db; init_db()'` |
| `no such column: bibtex/status_embedded` | 旧库缺列 | 跑对应 migration（幂等）或 init_db |
| PDF 预览 404 但元数据正常 | 历史 `legacy:` 路径在磁盘上缺失 | 预期降级；`resolve_path` 返回 None，前端不抛异常 |
| verbatimModuleSyntax 报 type import 错 | TS5 严格模式 | 类型用 `import type {...}` |
| 同秒并发上传同名覆盖 | time.time() 碰撞 | `secrets.token_hex(4)` 后缀（已修） |
| `setsid -f` 包装导致 shell 工具卡死 | 后台 bash 抓住 stdout 管道不放 | `&` 写在 `bash -c` 单引号**内部**，子 shell 立即退出 |
| 本机 fake-IP DNS（Clash/mihomo）下 URL 入库全部被拒 | 所有域名解析进 RFC 2544 网段 198.18.0.0/15，`ipaddress` 判 is_private，SSRF 校验拦下一切下载 | `pdf_download.py` 精确豁免该网段（`_FAKEIP_NET`；fake-IP 模式下实际连接目标是本地代理，拦截无防护意义，已修） |

## ingest 与 DOI 匹配

| 错误 | 原因 | 解决 |
|---|---|---|
| OpenAlex 匹配到错误论文 | LLM 幻觉 DOI | DOI 只接受页面标识符/打分反查/手动三源 |
| 同名勘误/来信/评论抢占原论文 DOI | 标题 sim≈1.0，纯阈值无法区分 | 候选逐条打分的反查（作者+年份信号）+ 衍生标题前缀过滤（已修） |
| SI 组件/书章/工具书条目抢 DOI | Crossref `component`/`reference-*` 类型未过滤 | type 硬跳过 + `.sNNN` DOI 后缀识别（已修） |
| enrich 交叉验证通过错配记录 | pdf_title 无效时回退 paper.title 构成循环论证（sim 恒 1.00） | 基准只用 pdf_title；用数据库标题时提高阈值并叠加年份检查（已修） |
| 同名撞 dir_name 正文被 SI 覆盖 | 消歧只在有 DOI 时检查 | 已有归档文件且大小不同即加后缀（不论 DOI 有无）；同大小且 DOI 冲突同理（已修） |
| 老 Elsevier 论文 PII 漏提 | `PII: 0165-...` 无 S 前缀变体未覆盖 | `_PII_RE` 的 S 改可选；拼 DOI 时 as-printed 与 S 切换两变体都试（注册形态因年代而异，已修） |
| 超短标题论文首页 DOI 被校验拒绝 | 以纯大写缩略词为标题的论文被 `is_junk_title` 过滤后校验参照只剩带作者前缀的 stem | ref_titles 始终附加 stem_title + `titles_compatible` 主标题（冒号前）精确相等分支（≥5 字符，已修） |
| Nature/Science 老 SI 附件漏 DOI | 首页无印刷标识符也无 SI 字样，只有 `BFnature04676_MOESM1_ESM` / `science.xxx_sm` 式文件名 | SI 判定加文件名信号（`filename_suggests_si`）+ 文件名派生母论文 DOI 作为最后候选（`filename_derived_dois`，已修） |
| 标题反查检索不到确有记录的论文（实测） | `pdf_title` 直接当查询串，带行末断词伪影且连字符后无空格（`RE-GULARIZATION`），OpenAlex/Crossref 的 search 排不出该文；断词伪影无法与合法连字符词机械区分 | 查询前折叠空白；仍无候选时用标准大小写、无断词的标题重试同一打分反查（实测命中 score 1.35） |
| 无标识符论文错配摘要入库（多篇实测，摘要属于无关工作） | 标题反查命中无关记录后摘要随之写入，此前无正文级校验，污染 papers_fts 的关键词检索 | 全库审计 `scripts/abstract_audit.py` + enrich 在线重合校验低分警告（`backend/services/abstract_check.py`）；摘要重补以重合 >= 0.8 为采纳门槛 |
| 出现 unknown0_xxx 无效论文记录 | year=0 入库 | ingest raise 拒绝（已修） |
| 不同 DOI 的 REVIEW 组仍被 `dedup_papers.py --apply` 删除 | `main()` 只检查条目的 safe 标记，未检查组级 review；`status_enrich` 人工等待也被当成内容可删依据 | REVIEW 组整组不加入删除清单；`status_enrich` 不单独作为自动删除依据（已修） |
| 两位数字开头的真实题名被 `is_junk_title` 误判 | `^[0-9]{2}\s+[A-Z]` 可匹配任意题名前缀 | 仅完整匹配大写稿件码加 `_x` 后缀这类完整形态，并保留合法题名负例（已修） |
| OpenAlex 请求错误被 `audit-citations` 报成 oa_missing | 批量非 200 跳过、单发非 200 一律返回空字典 | 批量所有非 200 抛错；单发仅 404 返回空字典（已修） |
| 预印本文件名年份覆盖正式版年份 | 页面 DOI 已过 Crossref 自指校验，但 OpenAlex `pub_year` 与文件名年份相差数年，`ydiff>=3` 把整条 OpenAlex 记录丢弃；`year` 回退到文件名，enrich 有 `pdf_title` 时又只比标题，最终 DOI/摘要/BibTeX 是新记录、`papers.year` 与目录停留在旧年份 | 页面 DOI/PII 来源不按文件名年份否决 OpenAlex，只告警；enrich 对年份差 >=3 单独告警；重复 DOI 复用时推断目录与现有记录不一致也告警（已修） |

## 翻译

| 错误 | 原因 | 解决 |
|---|---|---|
| 翻译 worker 0% CPU 不退出 | bash+timeout hang | 进程内 import + watchdog |
| 超长 chunk 触发 LLM 截断 | 无字符上限 | 二次切分（已修） |

历史问题回归测试：

| 历史问题 | 当前防御 |
|---|---|
| 占位符末尾追加碎片 | 步骤 6b 仅警告（已修） |
| 某论文 ZH 译文反斜杠双重转义 | 走 `json.dumps` 不预转义 |
| 参考文献被翻译 | 检测 `# References` 回退，整段 skip |
| Introduction 编号列表被误判为参考文献起点，其后全部内容（含附录）不切块、翻译整段跳过 | `REFERENCES_RE` 扩到 H1–H6；refs 段止于下一同级或更高级标题（附录回到正文）；refs 段超 `max_chunk_chars` 照常二次切分（已修） |
| LLM 幻觉 DOI | DOI 三源约定（红线 6） |

## 渲染与阅读器

| 错误 | 原因 | 解决 |
|---|---|---|
| preview 与阅读器内容不一致 | `chunk_index=-1` 处理不一致 | reader API 与 preview 共用 `_doc_text`（已修） |
| LaTeX 公式含 `<` 渲染成 katex-error | 旧链路把公式内尖括号转义成 `&lt;`/`&gt;`，KaTeX 拿到字面实体后抛 ParseError | 不做实体预处理，原文直传；当前链路为 remark-gfm + remark-math -> rehype-raw -> rehype-sanitize（白名单）-> rehype-katex，`$a < b$` 有组件与浏览器回归测试；`lib/markdown-pre.ts` 仍不恢复 |
| 管道表格显示成文本、HTML 表格与上下标显示成标签 | 旧阅读器只注册 remark-math + rehype-katex，未解析 GFM 表格与论文所需 HTML | 使用 remark-gfm 与安全白名单；MinerU `<details><summary>` 块单独分段，块内 Markdown 继续解析（已修） |
| 页签切换或保存期间输入导致草稿丢失 | 草稿随 DocReader 卸载；保存后无条件重新加载并覆盖编辑框 | `PaperDetail` 按 `paperId + docType` 保留草稿；保存只替换已提交文本，新增输入继续保持 dirty；`base_hash` 冲突检查与写入放在同一 `BEGIN IMMEDIATE` 事务（已修） |

## 并发与任务

| 错误 | 原因 | 解决 |
|---|---|---|
| MinerU 短连接限流 | 每次新建 TCP | `requests.Session()` 复用 + 令牌窗口（已修） |
| 同一 task 并发重复 claim | claim 非原子 | `UPDATE ... RETURNING`（已修） |
| feed pipeline 恢复报 `inbox file missing` | 首次 ingest 已删除 inbox，重启却从 ingest 重跑 | 任务已关联 paper_id 时跳过 ingest，继续后续步骤（已修） |
| 测试污染真实 library/cache | fixture 或子进程在改写路径前导入捕获配置的模块 | 先隔离路径再 import；沙箱 `bootstrap` 拒绝提前加载数据库、缓存和调度模块 |
| MCP get_paper 与 repo 函数重名遮蔽 | FastMCP tool 覆盖模块符号 | repo 导入用 `_repo_get_paper` 别名 |
| `status_enrich` 迁移把同秒事件顺序或重跑后的当前状态判错 | 秒级 `MAX(ts)` 无法区分同秒先后；列已存在时仍执行历史状态写入 | 按 `MAX(events.id)` 判断事件先后；只在新增列时写入历史状态（已修） |
| `audit-citations --fix` 被引数已更新但操作事件缺失 | 自动提交模式下 UPDATE 与 INSERT 分别生效 | 每篇的被引数更新与事件写入放在显式事务中，失败回滚后可重跑（已修） |
