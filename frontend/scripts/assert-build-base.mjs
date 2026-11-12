// 防止未设 VITE_BASE_PATH 的构建覆盖生产 dist：base 为 '/' 时资源引用指向域名根，
// 而本机 frontend/dist 由 paper-frontend.service 直接服务（/papersys/ 部署），
// 覆盖后线上页面静态资源 404、白屏（2026-11-11 事故）。
// 验证构建请用 npm run build:verify；生产构建请用 npm run build:prod。
if (!process.env.VITE_BASE_PATH && !process.env.ALLOW_ROOT_BASE_BUILD) {
  console.error(
    "拒绝构建：未设置 VITE_BASE_PATH，产物将以 base='/' 写入 dist。\n" +
      "frontend/dist 由 paper-frontend.service 直接服务，/papersys/ 部署下会白屏。\n" +
      "验证构建：npm run build:verify（输出 dist-verify）；生产构建：npm run build:prod。\n" +
      "确实需要根 base 产物时加 ALLOW_ROOT_BASE_BUILD=1。",
  );
  process.exit(1);
}
