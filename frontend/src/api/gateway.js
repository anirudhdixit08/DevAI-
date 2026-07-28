export const gatewayUrl = import.meta.env.VITE_GATEWAY_URL || "http://localhost:3000";
export const defaultPreviewFrontendUrl = "http://localhost:15173";

export function publicUrlForPort(port) {
  if (!port) return null;
  const protocol = window.location.protocol || "http:";
  const host = window.location.hostname || "localhost";
  return `${protocol}//${host}:${port}`;
}

export function normalizePreviewUrl(url, fallbackPort) {
  if (!url && fallbackPort) return publicUrlForPort(fallbackPort);
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (["localhost", "127.0.0.1", "0.0.0.0"].includes(parsed.hostname)) {
      return publicUrlForPort(parsed.port || fallbackPort);
    }
  } catch {
    return fallbackPort ? publicUrlForPort(fallbackPort) : url;
  }
  return url;
}

export async function gatewayJson(path, options = {}) {
  const response = await fetch(`${gatewayUrl}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Gateway request failed: ${response.status}`);
  }
  return data;
}

export function normalizeStreamEvent(rawEvent) {
  return {
    type: rawEvent?.type || "unknown",
    node: rawEvent?.node || "gateway",
    message: rawEvent?.message || "",
    state: rawEvent?.state || null,
  };
}
