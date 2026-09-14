import type { DieColor } from "../core";

const COLOR_NAMES: Record<DieColor, string> = { coral: "红", black: "黑", white: "白" };

export function castleRoomName(roomId: string, floor: "steward" | "diplomat"): string {
  const index = Number(roomId.at(-1));
  const side = floor === "steward" ? ["左", "中", "右"][index - 1] : ["左", "右"][index - 1];
  return `${floor === "steward" ? "家臣层" : "使节层"}${side ?? index}侧房间`;
}

export function castleTileActionLabel(
  roomId: string,
  floor: "steward" | "diplomat",
  color: DieColor,
  rowIndex: number,
  selector?: "light",
): string {
  const rowName = floor === "steward" ? ["上", "中", "下"][rowIndex] : ["上", "下"][rowIndex];
  const action = selector === "light" ? "浅色背景行动" : `${COLOR_NAMES[color]}色行动`;
  return `选择${castleRoomName(roomId, floor)}·${rowName ?? "对应"}排${action}`;
}
