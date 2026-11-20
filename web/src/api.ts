// Small API client. A PIN (if the server has one) is kept in a cookie set by /api/auth.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401) {
    const pin = prompt("TreeLights PIN");
    if (pin !== null) {
      await fetch("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }) });
      return api<T>(path, opts);
    }
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

/** A WebSocket that reconnects with backoff. */
export class LiveSocket {
  ws: WebSocket | null = null;
  private closed = false;
  private delay = 500;
  onmessage: (ev: MessageEvent) => void = () => {};
  onopen: () => void = () => {};
  onstate: (open: boolean) => void = () => {};

  constructor(private path: string, private binaryType: BinaryType = "arraybuffer") {
    this.connect();
  }

  private connect(): void {
    if (this.closed) return;
    const ws = new WebSocket(wsUrl(this.path));
    ws.binaryType = this.binaryType;
    ws.onopen = () => {
      this.delay = 500;
      this.onstate(true);
      this.onopen();
    };
    ws.onmessage = (ev) => this.onmessage(ev);
    ws.onclose = () => {
      this.onstate(false);
      if (!this.closed) setTimeout(() => this.connect(), (this.delay = Math.min(this.delay * 2, 8000)));
    };
    this.ws = ws;
  }

  send(data: string | ArrayBuffer | Blob): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(data);
      return true;
    }
    return false;
  }

  sendJson(obj: unknown): boolean {
    return this.send(JSON.stringify(obj));
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
  }
}

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, any> = {},
  ...children: (Node | string | null | undefined | false)[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") (el as any)[k.toLowerCase()] = v;
    else if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k in el && typeof v !== "string") (el as any)[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

export function fmtErr(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export type Page = { mount(root: HTMLElement): void; unmount?(): void };
