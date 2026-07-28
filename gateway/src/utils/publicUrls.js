function publicHost(req = null) {
  return process.env.PREVIEW_PUBLIC_HOST
    || process.env.PUBLIC_HOST
    || req?.headers?.["x-forwarded-host"]?.split(",")[0]?.trim()
    || req?.headers?.host?.split(":")[0]
    || "localhost";
}

function publicProtocol(req = null) {
  return process.env.PREVIEW_PUBLIC_PROTOCOL
    || process.env.PUBLIC_PROTOCOL
    || req?.headers?.["x-forwarded-proto"]?.split(",")[0]?.trim()
    || "http";
}

export function publicUrlForPort(port, req = null) {
  if (!port) return null;
  return `${publicProtocol(req)}://${publicHost(req)}:${port}`;
}

export function normalizePublicPreviewUrl(url, fallbackPort, req = null) {
  if (!url && fallbackPort) return publicUrlForPort(fallbackPort, req);
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (["localhost", "127.0.0.1", "0.0.0.0"].includes(parsed.hostname)) {
      return publicUrlForPort(parsed.port || fallbackPort, req);
    }
  } catch {
    return fallbackPort ? publicUrlForPort(fallbackPort, req) : url;
  }
  return url;
}
