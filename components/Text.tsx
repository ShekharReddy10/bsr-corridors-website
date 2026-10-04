import { isPlaceholder } from "@/content/hotel";

/** Renders content text, flagging unfilled [placeholder] values so they are easy to spot. */
export default function Text({ value }: { value: string }) {
  if (!isPlaceholder(value)) return <>{value}</>;
  return (
    <span className="placeholder" title="Placeholder — edit content/hotel.ts">
      {value.slice(1, -1)}
    </span>
  );
}
