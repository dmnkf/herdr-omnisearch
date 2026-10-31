import { div } from "gpui-kit";

export const v_flex = () => div();
export const h_flex = () => div();

export const InputState = {
  new(options) {
    const handlers = {};
    const state = {
      options,
      text: "",
      on: (event, fn) => (handlers[event] = fn),
      value: () => state.text,
      focus: () => {},
      fire: (event, cx) => handlers[event]?.({}, cx),
    };
    globalThis.__input = state;
    return state;
  },
};

export const Input = { new: () => div() };
