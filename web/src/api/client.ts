export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
  });
  const text = await response.text();
  const body = text ? (JSON.parse(text) as unknown) : null;
  if (!response.ok) {
    throw new ApiError(response.status, detailMessage(body, response.status));
  }
  return body as T;
}

/** 后端 detail 有三种形态：字符串、`{code, message}` 对象（409），
 *  以及 issue 数组（422 房间绑定校验）。直接 String() 会得到 [object Object]。 */
function detailMessage(body: unknown, status: number): string {
  const fallback = `HTTP ${status}`;
  if (typeof body !== "object" || body === null || !("detail" in body)) return fallback;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map(issueMessage).filter(Boolean);
    return messages.length ? messages.join("；") : fallback;
  }
  return issueMessage(detail) || fallback;
}

function issueMessage(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value !== "object" || value === null) return "";
  const record = value as Record<string, unknown>;
  // FastAPI 校验错误用 msg，本项目的 Issue 用 message。
  const text = record.message ?? record.msg;
  return typeof text === "string" ? text : "";
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "未知错误";
}
