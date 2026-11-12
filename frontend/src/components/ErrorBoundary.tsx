import { Component, type ReactNode, type ErrorInfo } from "react";

interface Props {
  children: ReactNode;
}
interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center h-full p-8 gap-3 text-center">
          <div className="text-red-400 text-lg font-medium">页面出错了</div>
          <pre className="text-xs text-gray-400 max-w-lg whitespace-pre-wrap break-all">
            {this.state.error?.message ?? "未知错误"}
          </pre>
          <button
            className="btn"
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
