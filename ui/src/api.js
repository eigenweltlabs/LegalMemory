const DEV_PRINCIPALS = import.meta.env.VITE_DEV_PRINCIPALS || "";

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function api(path, options = {}) {
  const principals = localStorage.getItem("ki.devPrincipals") || DEV_PRINCIPALS;
  const headers = new Headers(options.headers || {});
  if (principals) headers.set("x-ki-principals", principals);
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  if (options.body && !isFormData && !headers.has("content-type")) headers.set("content-type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let body;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw new ApiError(body?.detail || `${response.status} ${response.statusText}`, response.status, body);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function setDevPrincipals(value) {
  if (value) localStorage.setItem("ki.devPrincipals", value);
  else localStorage.removeItem("ki.devPrincipals");
  window.location.reload();
}
