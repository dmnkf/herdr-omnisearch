const STUBS = { "gpui-kit": "gpui-kit.mjs", "gpui-base": "gpui-base.mjs", herdr: "herdr.mjs" };

export async function resolve(specifier, context, next) {
  if (STUBS[specifier]) return { url: new URL(`./stubs/${STUBS[specifier]}`, import.meta.url).href, shortCircuit: true };
  return next(specifier, context);
}
