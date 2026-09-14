import assetManifestJson from "../../data/assets.json";

interface AtlasRecord { id: string; file: string; columns: number; rows: number; }
interface AssetRecord { id: string; materialId: string; atlasId: string; index: number; anchor?: { x: number; y: number }; }
interface AssetManifest { schemaVersion: number; atlases: AtlasRecord[]; assets: AssetRecord[]; }

const manifest = assetManifestJson as AssetManifest;
if (manifest.schemaVersion !== 1) throw new Error(`Unsupported White Castle asset schema ${manifest.schemaVersion}.`);

export function atlasRecord(id: string): AtlasRecord {
  const atlas = manifest.atlases.find((candidate) => candidate.id === id);
  if (!atlas) throw new Error(`Unknown White Castle atlas ${id}.`);
  return atlas;
}

export function materialAsset(materialId: string): AssetRecord {
  const asset = manifest.assets.find((candidate) => candidate.materialId === materialId);
  if (!asset) throw new Error(`No White Castle asset mapped for ${materialId}.`);
  return asset;
}

export function materialAssetCard(materialId: string): number {
  return materialAsset(materialId).index + 1;
}

export function atlasCss(atlasId: string, index: number): string {
  const atlas = atlasRecord(atlasId);
  const column = index % atlas.columns;
  const row = Math.floor(index / atlas.columns);
  const x = atlas.columns === 1 ? 0 : (column / (atlas.columns - 1)) * 100;
  const y = atlas.rows === 1 ? 0 : (row / (atlas.rows - 1)) * 100;
  return `background-image:url('/${atlas.file}');background-size:${atlas.columns * 100}% ${atlas.rows * 100}%;background-position:${x}% ${y}%`;
}

export function materialAssetCss(materialId: string): string {
  const asset = materialAsset(materialId);
  return atlasCss(asset.atlasId, asset.index);
}
