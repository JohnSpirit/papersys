# Zotero RIS 导出 L2 linked-file 格式未经实测（待验证）

状态：**待实测**（2026-08 审查确认，未修）。

## 背景

`backend/services/zotero_export.py` 生成 RIS 时，PDF 附件链接写为：

```
L2  - file:pdf/<year>/<dir_name>/<dir_name>.pdf
```

即 `file:pdf/` 前缀 + 相对 library_root 的路径。这是按 Zotero linked-file 工作流的假设写的（导入时配合 Zotero 的 linked attachment base directory 设置指向 `library/`），但**从未在真实 Zotero 中导入验证过**：

- `file:pdf/` 前缀是否被 Zotero RIS 导入器识别为相对 base dir 的链接附件，存疑；
- 若不被识别，导入后条目没有附件关联，需手工处理或调整导出格式（备选：去掉 `pdf/` 前缀、改用绝对 `file://` URL、或改用 BibTeX 的 `file` 字段约定）。

## 验证方法（决策时执行）

1. `paper zotero-export -f ris -o out.ris`
2. Zotero 设置 linked attachment base directory = `library_root`（文库根目录，容器部署为 `/data/library`）
3. 导入 out.ris，检查条目是否自动挂上 PDF 链接附件
4. 失败则按上述备选调整 `synth_ris` 的 L2 行并重测
