import { useEffect, useRef } from "react";
import { usd } from "../data";

export function LiveAmount({ value }: { value: number }) {
  const element = useRef<HTMLSpanElement>(null);
  const previous = useRef(value);
  useEffect(() => {
    if (previous.current !== value && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      element.current?.getAnimations().forEach((animation) => animation.cancel());
      element.current?.animate([
        { opacity: .45, transform: "translateY(2px)" },
        { opacity: 1, transform: "translateY(0)" },
      ], { duration: 220, easing: "ease-out" });
    }
    previous.current = value;
  }, [value]);
  return <span className="scanner-amount" ref={element}>{usd(value)}</span>;
}
