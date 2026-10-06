import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Keeps a failure inside the Meet++ board from taking the page down with it:
 * without a boundary React unmounts the whole root — in the room the app goes
 * blank, and on the egress page the cleanup ends the recording or livestream.
 * Shows a quiet placeholder and retries after a few seconds (the next state
 * usually renders again).
 */
export default class BoardErrorBoundary extends Component<
  { children: ReactNode; compact?: boolean },
  { failed: boolean }
> {
  state = { failed: false };
  private timer: ReturnType<typeof setTimeout> | null = null;

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Meet++ board failed to render", error, info.componentStack);
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.setState({ failed: false });
    }, 5000);
  }

  componentWillUnmount(): void {
    if (this.timer) clearTimeout(this.timer);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div
        className="flex h-full w-full items-center justify-center rounded-md bg-slate-100 px-3 text-center text-xs text-slate-500"
        data-testid="meetpp-board-error"
      >
        {this.props.compact ? "Meet++" : "The Meet++ board is reloading…"}
      </div>
    );
  }
}
