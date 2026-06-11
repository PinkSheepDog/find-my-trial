// API client. Reads the CSRF token from the non-HttpOnly cookie set at login and
// sends it on every state-changing POST (double-submit pattern). The session cookie
// itself is HttpOnly and is sent automatically by the browser — JS never touches it.

function getCsrf() {
  const m = document.cookie.match(/(?:^|;\s*)fmt_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request(path, { method = "GET", body, isForm = false } = {}) {
  const headers = {};
  if (!isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["x-csrf-token"] = getCsrf();

  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: isForm ? body : body ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  health: () => request("/health"),
  me: () => request("/api/me"),
  login: (username, password) =>
    request("/api/login", { method: "POST", body: { username, password } }),
  logout: () => request("/api/logout", { method: "POST" }),
  extractText: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/api/extract-text", { method: "POST", body: fd, isForm: true });
  },
  deidentify: (text) =>
    request("/api/deidentify", { method: "POST", body: { text } }),
  match: (payload) => request("/api/match", { method: "POST", body: payload }),
};
