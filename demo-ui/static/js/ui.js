// Small DOM helpers shared by the panels.

export const $ = (selector, root = document) => root.querySelector(selector);

// A log function writing newest-first into `element`.
export function logger(element) {
  return (text, kind = "") => {
    const line = document.createElement("div");
    line.className = `log-line ${kind}`;
    line.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
    element.prepend(line);
  };
}

// Runs an async action, logging its error instead of throwing.
export async function attempt(log, action) {
  try {
    return await action();
  } catch (error) {
    log(error.message, "error");
    return undefined;
  }
}

export function button(label, onClick) {
  const element = document.createElement("button");
  element.textContent = label;
  element.onclick = onClick;
  return element;
}

// A two-column table from [label, value] pairs.
export function table(rows) {
  const element = document.createElement("table");
  for (const [label, value] of rows) {
    const row = element.insertRow();
    row.insertCell().textContent = label;
    row.insertCell().textContent = value ?? "—";
  }
  return element;
}

export const time = (iso) => (iso ? new Date(iso).toLocaleTimeString() : null);

// About `km` kilometres north of a point.
export const north = (point, km) => ({ lat: point.lat + km / 111.2, lng: point.lng });
