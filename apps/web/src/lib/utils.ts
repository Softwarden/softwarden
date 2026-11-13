import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** "A", "A and B", "A, B and C" (or with `word` "or"). */
export function andList(items: string[], word = "and") {
  return items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} ${word} ${items[items.length - 1]}`;
}
