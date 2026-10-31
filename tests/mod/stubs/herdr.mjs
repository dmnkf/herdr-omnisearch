// The app's state and a log of what the mod asked of it, set per test on globalThis.__herdr.
const h = () => globalThis.__herdr;

export const exec = (command, args, options) => h().exec(command, args, options);
export const machines = () => h().machines;
export const panes = () => h().panes;
export const agents = () => h().agents;
export const spaces = () => h().spaces;
export const run = (name, args) => (h().ran.push([name, args]), null);
export const notify = (n) => (h().notified.push(n), "n1");
export const theme = () => new Proxy({}, { get: (_t, name) => (name === "missing" ? undefined : `#${String(name)}`) });
export const props = () => ({ overlay: "o1" });
export const closeOverlay = (id) => (h().closed.push(id), null);
