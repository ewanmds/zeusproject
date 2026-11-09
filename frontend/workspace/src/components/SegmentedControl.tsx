// Adapted from @nolly-studio/halo-toggle-group on Layro (MIT).
// Native React/CSS implementation; keeps Zeus styling and avoids new UI runtimes.
import { useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";

interface Option<T> {
  value: T;
  label: string;
  disabled?: boolean;
}

export function SegmentedControl<T extends string | number>({
  label,
  value,
  options,
  onValueChange,
  className = "",
}: {
  label: string;
  value: T;
  options: readonly Option<T>[];
  onValueChange: (value: T) => void;
  className?: string;
}) {
  const track = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState({ x: 0, width: 0 });
  const [interacted, setInteracted] = useState(false);
  const optionKey = JSON.stringify(options);
  const focusValue =
    options.find((option) => option.value === value && !option.disabled)
      ?.value ?? options.find((option) => !option.disabled)?.value;
  useLayoutEffect(() => {
    const group = track.current;
    if (!group) return;
    let mounted = true;
    const measure = () => {
      if (!mounted) return;
      const active = group.querySelector<HTMLButtonElement>(
        '[aria-pressed="true"]',
      );
      const next = active
        ? { x: active.offsetLeft, width: active.offsetWidth }
        : { x: 0, width: 0 };
      setThumb((old) =>
        old.x === next.x && old.width === next.width ? old : next,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(group);
    group
      .querySelectorAll("button")
      .forEach((button) => observer.observe(button));
    document.fonts?.addEventListener("loadingdone", measure);
    return () => {
      mounted = false;
      observer.disconnect();
      document.fonts?.removeEventListener("loadingdone", measure);
    };
  }, [value, optionKey]);

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const direction =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!direction && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    setInteracted(true);
    const enabled = options
      .map((option, i) => ({ option, i }))
      .filter(({ option }) => !option.disabled);
    if (!enabled.length) return;
    const current = enabled.findIndex(({ i }) => i === index);
    const next =
      event.key === "Home"
        ? enabled[0]
        : event.key === "End"
          ? enabled.at(-1)!
          : enabled[(current + direction + enabled.length) % enabled.length];
    onValueChange(next.option.value);
    track.current
      ?.querySelectorAll<HTMLButtonElement>("button")
      [next.i]?.focus();
  }

  return (
    <div
      ref={track}
      role="group"
      aria-label={label}
      data-layro="nolly-studio/halo-toggle-group"
      className={`segments zeus-segmented ${className}`}
      data-interacted={interacted || undefined}
      style={
        {
          "--thumb-x": thumb.x + "px",
          "--thumb-width": thumb.width + "px",
        } as CSSProperties
      }
    >
      <span className="segment-thumb" aria-hidden="true" />
      {options.map((option, index) => (
        <button
          key={option.value}
          type="button"
          disabled={option.disabled}
          aria-pressed={value === option.value}
          tabIndex={focusValue === option.value ? 0 : -1}
          className={value === option.value ? "active" : ""}
          onClick={() => {
            setInteracted(true);
            onValueChange(option.value);
          }}
          onKeyDown={(event) => onKeyDown(event, index)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
