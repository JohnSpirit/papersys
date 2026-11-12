# papersys 前端

React + TypeScript + Vite + Tailwind 构建的文献库、阅读器、任务与设置界面。后端、CLI、部署与 MCP 说明见仓库根目录 [README.md](../README.md)，用户操作见 [docs/guides/user.md](../docs/guides/user.md)。

## 常用命令

```bash
npm ci
npm run dev          # 开发服务器，/api 代理到 127.0.0.1:5050
npm run build:verify # 类型检查，输出到 dist-verify
npm run lint
npm test             # Vitest
npm run test:e2e     # Playwright，首次先运行 npm run test:e2e:install
```

容器构建使用根路径发布：`ALLOW_ROOT_BASE_BUILD=1 npm run build`。
