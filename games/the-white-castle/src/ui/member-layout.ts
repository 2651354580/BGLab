export interface BoardPoint { x: number; y: number }
export interface MemberPlacement extends BoardPoint { width: number; height: number; z: number }
export interface MemberOwner { seat: number; indexInSeat: number }

const GARDENS: Record<string, BoardPoint> = {
  "garden-coral-1": { x: 174, y: 164 }, "garden-coral-2": { x: 27, y: 162 },
  "garden-black-1": { x: 174, y: 293 }, "garden-black-2": { x: 27, y: 291 },
  "garden-white-1": { x: 174, y: 422 }, "garden-white-2": { x: 27, y: 420 },
};
const ROOMS: Record<string, BoardPoint> = {
  "steward-1": { x: 443, y: 351 }, "steward-2": { x: 640, y: 351 }, "steward-3": { x: 838, y: 351 },
  "diplomat-1": { x: 482, y: 254 }, "diplomat-2": { x: 679, y: 254 },
};
const YARDS: Record<string, BoardPoint> = {
  "yard-1": { x: 677, y: 132 }, "yard-2": { x: 800, y: 132 }, "yard-3": { x: 772, y: 221 },
};

function piece(x: number, y: number, width: number, height: number): MemberPlacement {
  // Front rows cover back rows, independent of player color or DOM ordering.
  return { x, y, width, height, z: 20 + Math.round(y) };
}

/** Presentation only: never infer game ownership from a visual slot. */
export function memberPlacement(location: string, index: number, count: number, owner?: MemberOwner): MemberPlacement {
  const garden = GARDENS[location];
  if (garden) {
    const formations: BoardPoint[][] = [
      [{ x: 0, y: 0 }],
      [{ x: -13, y: 0 }, { x: 13, y: 0 }],
      [{ x: 0, y: -10 }, { x: -12, y: 8 }, { x: 12, y: 8 }],
      [{ x: -12, y: -8 }, { x: 12, y: -8 }, { x: -12, y: 8 }, { x: 12, y: 8 }],
    ];
    const right = location.endsWith("-1");
    const offset = { ...formations[Math.min(4, Math.max(1, count)) - 1][index] };
    if (right && offset.x) offset.x = Math.sign(offset.x) * 9;
    const width = count >= 3 ? (right ? 18 : 22) : 24, height = width * 114 / 110;
    return piece(garden.x + offset.x - width / 2, garden.y + offset.y - height / 2, width, height);
  }
  const room = ROOMS[location];
  if (room) {
    const width = count <= 4 ? 22 : count <= 10 ? 20 : 18;
    const height = width * 97 / 95;
    const rows = Math.ceil(count / 2), row = Math.floor(index / 2);
    const inRow = Math.min(2, count - row * 2);
    const gap = count <= 4 ? 29 : Math.min(12, 40 / Math.max(1, rows - 1));
    const stride = width - 4;
    const x = room.x + (40 - (width + (inRow - 1) * stride)) / 2 + (index % 2) * stride;
    return piece(x, room.y - row * gap, width, height);
  }
  const yard = YARDS[location];
  if (yard) {
    if (!owner) throw new Error("A training-yard piece requires its player seat and within-seat index.");
    const width = 18, height = width * 100 / 85;
    return piece(yard.x + owner.seat * 22, yard.y + owner.indexInSeat * 4, width, height);
  }
  if (location === "gate") {
    // Center the whole formation in the doorway, below its roof and beside the payment panel.
    const columns = Math.min(4, count), rows = Math.ceil(count / columns);
    const row = Math.floor(index / columns), inRow = Math.min(columns, count - row * columns);
    const width = count <= 4 ? 21 : count <= 10 ? 20 : 18, height = width * 97 / 95;
    const stride = width - 2;
    const rowGap = rows <= 2 ? 12 : Math.min(8, (37 - height) / (rows - 1));
    const groupHeight = height + (rows - 1) * rowGap;
    return piece(614 - (width + (inRow - 1) * stride) / 2 + (index % columns) * stride,
      441 - groupHeight / 2 + row * rowGap, width, height);
  }
  // Reward claims are pinned separately; only waiting Daimyo courtiers use this area.
  const columns = 4, row = Math.floor(index / columns);
  const inRow = Math.min(columns, count - row * columns);
  const width = count > 10 ? 18 : 22;
  return piece(512 + (columns - inRow) * width / 2 + (index % columns) * width,
    140 + row * 9, width, width * 97 / 95);
}

export function memberPosition(location: string, index: number, count: number, owner?: MemberOwner): BoardPoint {
  const { x, y } = memberPlacement(location, index, count, owner);
  return { x, y };
}
