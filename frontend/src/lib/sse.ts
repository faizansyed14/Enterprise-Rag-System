// Server-sent events over a POST request (EventSource only supports GET). Parses `event:` / `data:` blocks from
// the streamed body and calls onEvent for each one.

import { apiHeaders } from "./api";

export type StreamEvent = { event: string; data: any };

export async function postStream(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/v1${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { ...apiHeaders("POST"), Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (response.status === 401) {
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- session expired: full reload to sign in
    window.location.href = "/login";
    return;
  }
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    onEvent({ event: "error", data: { message: detail, code: String(response.status) } });
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index: number;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue;
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) {
        try {
          onEvent({ event: name, data: JSON.parse(data.join("\n")) });
        } catch {
          /* ignore malformed blocks */
        }
      }
    }
  }
}
