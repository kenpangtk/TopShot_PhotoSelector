import { invoke } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { open } from "@tauri-apps/plugin-dialog";

export interface EngineInfo {
  port: number;
  token: string;
}

let engineInfo: EngineInfo | null = null;
let requestId = 0;

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function initEngine(): Promise<EngineInfo> {
  if (engineInfo) return engineInfo;
  if (isTauri()) {
    engineInfo = await invoke<EngineInfo>("engine_info");
  } else {
    // Browser preview can receive a local engine from Vite env vars or from
    // URL query params (useful for a shared local dev link).
    const params = new URLSearchParams(window.location.search);
    const port = Number(import.meta.env.VITE_ENGINE_PORT || params.get("enginePort") || 0);
    const token = String(import.meta.env.VITE_ENGINE_TOKEN || params.get("engineToken") || "");
    if (!port || !token) {
      throw new Error(
        "Browser preview needs VITE_ENGINE_PORT/VITE_ENGINE_TOKEN or ?enginePort=&engineToken= from a manually started engine.",
      );
    }
    engineInfo = { port, token };
  }
  return engineInfo;
}

export function assetUrl(path: string): string {
  if (!engineInfo) return "";
  return `http://127.0.0.1:${engineInfo.port}${path}?token=${encodeURIComponent(engineInfo.token)}`;
}

export async function rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  const info = await initEngine();
  const response = await fetch(`http://127.0.0.1:${info.port}/rpc`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Topshot-Token": info.token,
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++requestId, method, params }),
  });
  if (!response.ok) {
    throw new Error(`Engine request failed (${response.status})`);
  }
  const payload = (await response.json()) as {
    result?: T;
    error?: { message: string };
  };
  if (payload.error) throw new Error(payload.error.message);
  return payload.result as T;
}

export async function pickFolder(title: string): Promise<string | null> {
  if (!isTauri()) throw new Error("Folder picker requires the desktop app.");
  const selected = await open({ directory: true, multiple: false, title });
  return typeof selected === "string" ? selected : null;
}

export async function openPath(path: string): Promise<void> {
  if (!isTauri()) throw new Error("Open Destination requires the desktop app.");
  await invoke("open_path", { path });
}

export async function listenForFolderDrop(
  onDrop: (paths: string[]) => void,
  onHover: (over: boolean) => void,
): Promise<() => void> {
  if (!isTauri()) return () => undefined;
  return getCurrentWebview().onDragDropEvent((event) => {
    if (event.payload.type === "over") onHover(true);
    else if (event.payload.type === "leave") onHover(false);
    else if (event.payload.type === "drop") {
      onHover(false);
      onDrop(event.payload.paths);
    }
  });
}
