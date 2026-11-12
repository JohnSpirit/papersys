# 项目根放 pyproject.toml(破 AGENTS.md 配置文件禁令)

AGENTS.md「目录使用规范」禁根目录配置文件(例外仅 README / AGENTS.md / config.toml)。为获全局 `paper` 命令(`[project.scripts] paper = "backend.cli:main"`),决定破例放行 pyproject.toml,并同步修订 AGENTS.md 例外清单。

理由:全局命令体验显著优于 `scripts/paper.sh` 包装;`pip install -e .` 是 editable 安装,仅写 `.pth` 指向源码,卸载干净。权衡后认为打破封闭白名单的收益值得。

**Considered Options**:
- `scripts/paper.sh` 包装——零侵入但体验差,每次需 conda activate
- pyproject.toml 破例(采纳)
- pyproject.toml 放 `backend/`——技术不可行,`pip install -e .` 要求 pyproject.toml 在项目根
