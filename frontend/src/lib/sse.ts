import { useEffect, useRef, useState } from "react";
import { apiUrl, type SSEvent } from "@/lib/api";

interface UseEventStreamOpts {
  taskId?: number;
  paperId?: number;
  maxEvents?: number;
  enabled?: boolean;
}

export function useEventStream({ taskId, paperId, maxEvents = 500, enabled = true }: UseEventStreamOpts = {}) {
  const [events, setEvents] = useState<SSEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const lastIdRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    const params = new URLSearchParams();
    params.set("since", String(lastIdRef.current));
    if (taskId) params.set("task_id", String(taskId));
    if (paperId) params.set("paper_id", String(paperId));

    const source = new EventSource(apiUrl(`/api/events?${params}`));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    const handler = (event: MessageEvent) => {
      try {
        const nextEvent = JSON.parse(event.data) as SSEvent;
        if (nextEvent.id <= lastIdRef.current) return;
        lastIdRef.current = nextEvent.id;
        setEvents((current) => {
          const next = [...current, nextEvent];
          return next.length > maxEvents ? next.slice(-maxEvents) : next;
        });
      } catch {
        return;
      }
    };
    source.addEventListener("info", handler);
    source.addEventListener("warn", handler);
    source.addEventListener("err", handler);
    source.addEventListener("message", handler);

    return () => {
      source.close();
      setConnected(false);
    };
  }, [taskId, paperId, enabled, maxEvents]);

  return { events, connected };
}
