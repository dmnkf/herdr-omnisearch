// Elements record their builder calls so a test can read the text and fire handlers.
export class Element {
  constructor(kind, text) {
    this.kind = kind;
    this.calls = [];
    this.kids = text === undefined ? [] : [String(text)];
    return new Proxy(this, {
      get(target, name) {
        if (name in target) return target[name];
        return (...args) => {
          target.calls.push([name, args]);
          if (name === "child") target.kids.push(args[0]);
          if (name === "children") target.kids.push(...args[0]);
          if (name === "when" && args[0]) args[1](target.proxy);
          return target.proxy;
        };
      },
    });
  }

  handler(name) {
    return this.calls.find(([n]) => n === name)?.[1][0];
  }

  text() {
    return this.kids.map((k) => (typeof k === "string" ? k : k.text())).join(" ");
  }

  find(id) {
    if (this.calls.some(([n, a]) => n === "id" && a[0] === id)) return this;
    for (const k of this.kids) {
      const hit = typeof k === "string" ? null : k.find(id);
      if (hit) return hit;
    }
    return null;
  }
}

function element(kind) {
  return (text) => {
    const e = new Element(kind, text);
    e.proxy = e;
    return e;
  };
}

export const div = element("div");
export const svg = element("svg");

export class View {}
