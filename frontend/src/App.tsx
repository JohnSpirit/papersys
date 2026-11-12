import { Suspense, useEffect, useState } from "react";
import {
  createBrowserRouter,
  Link,
  NavLink,
  Outlet,
  RouterProvider,
} from "react-router-dom";
import {
  BookOpen,
  Library,
  ListChecks,
  Menu,
  Search,
  Settings,
  Share2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { LoadingState } from "@/components/Feedback";

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, lazy: async () => ({ Component: (await import("@/pages/LibraryPage")).LibraryPage }) },
      { path: "paper/:id", lazy: async () => ({ Component: (await import("@/pages/PaperDetailPage")).PaperDetailPage }) },
      { path: "tasks", lazy: async () => ({ Component: (await import("@/pages/TasksPage")).TasksPage }) },
      { path: "glossary", lazy: async () => ({ Component: (await import("@/pages/GlossaryPage")).GlossaryPage }) },
      { path: "discover", lazy: async () => ({ Component: (await import("@/pages/DiscoverPage")).DiscoverPage }) },
      { path: "citations", lazy: async () => ({ Component: (await import("@/pages/CitationsPage")).CitationsPage }) },
      { path: "settings", lazy: async () => ({ Component: (await import("@/pages/SettingsPage")).SettingsPage }) },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
], { basename: import.meta.env.BASE_URL });

export default function App() {
  return <RouterProvider router={router} />;
}

export function AppLayout() {
  const [version, setVersion] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    let active = true;
    api.health().then((health) => {
      if (active) setVersion(health.version);
    }).catch(() => {});
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!navOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNavOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [navOpen]);

  return (
    <div className="flex h-dvh min-h-0">
      <a href="#main-content" className="sr-only z-[80] bg-blue-700 px-3 py-2 text-white focus:not-sr-only focus:fixed focus:left-2 focus:top-2">
        跳到主内容
      </a>
      {navOpen && (
        <button
          type="button"
          aria-label="关闭导航"
          className="fixed inset-0 z-40 bg-black/60 lg:hidden"
          onClick={() => setNavOpen(false)}
        />
      )}
      <nav
        aria-label="主导航"
        className={`fixed inset-y-0 left-0 z-50 flex w-64 shrink-0 flex-col gap-1 border-r border-side-border bg-side-bg p-3 transition-transform lg:static lg:w-52 lg:translate-x-0 ${navOpen ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div className="mb-1 flex items-center justify-between px-2 pb-2 pt-1 text-xs font-medium uppercase tracking-wider text-gray-400">
          <span>文献调研{version ? ` ${version}` : ""}</span>
          <button type="button" className="btn p-1 lg:hidden" onClick={() => setNavOpen(false)} aria-label="关闭导航">
            <X size={15} aria-hidden="true" />
          </button>
        </div>
        <NavItem to="/" icon={<Library size={16} />} label="文献库" onNavigate={() => setNavOpen(false)} />
        <NavItem to="/tasks" icon={<ListChecks size={16} />} label="任务" onNavigate={() => setNavOpen(false)} />
        <NavItem to="/glossary" icon={<BookOpen size={16} />} label="热词库" onNavigate={() => setNavOpen(false)} />
        <NavItem to="/discover" icon={<Search size={16} />} label="发现" onNavigate={() => setNavOpen(false)} />
        <NavItem to="/citations" icon={<Share2 size={16} />} label="引文网络" onNavigate={() => setNavOpen(false)} />
        <div className="flex-1" />
        <NavItem to="/settings" icon={<Settings size={16} />} label="设置" onNavigate={() => setNavOpen(false)} />
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-side-border bg-side-bg/95 px-4 py-2 backdrop-blur lg:hidden">
          <button type="button" className="btn px-2" onClick={() => setNavOpen(true)} aria-label="打开导航" aria-expanded={navOpen}>
            <Menu size={17} aria-hidden="true" />
          </button>
          <span className="text-sm font-medium">文献调研{version ? ` ${version}` : ""}</span>
        </header>
        <main id="main-content" className="min-w-0 flex-1 overflow-y-auto" tabIndex={-1}>
          <Suspense fallback={<div className="p-4 sm:p-6"><LoadingState label="正在加载页面…" /></div>}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}

function NavItem({ to, icon, label, onNavigate }: {
  to: string;
  icon: React.ReactNode;
  label: string;
  onNavigate: () => void;
}) {
  return (
    <NavLink
      to={to}
      onClick={onNavigate}
      className={({ isActive }) =>
        `flex items-center gap-2 rounded px-3 py-2 text-sm transition-colors ${
          isActive
            ? "bg-blue-800/40 text-blue-100"
            : "text-gray-300 hover:bg-gray-800/60 hover:text-gray-100"
        }`
      }
    >
      <span aria-hidden="true">{icon}</span>
      {label}
    </NavLink>
  );
}

function NotFoundPage() {
  return (
    <div className="p-6">
      <div className="card mx-auto mt-16 max-w-lg text-center">
        <h1 className="text-xl font-semibold text-gray-100">页面不存在</h1>
        <p className="mt-2 text-sm text-gray-400">当前地址没有对应页面，请返回文献库继续浏览。</p>
        <Link to="/" className="btn-primary mt-4 inline-flex">返回文献库</Link>
      </div>
    </div>
  );
}
