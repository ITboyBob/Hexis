import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

function safeResolve(base, relativePath, label) {
  if (typeof relativePath !== "string" || path.isAbsolute(relativePath)) {
    throw new Error(`${label} must be a relative path`);
  }
  const resolved = path.resolve(base, relativePath);
  const relative = path.relative(base, resolved);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error(`${label} escapes repository root`);
  }
  return resolved;
}

export async function prepareAssets({
  repositoryRoot,
  manifestPath = path.join(repositoryRoot, "hexis-ui/lib/voice-realtime/vad-assets-manifest.json"),
  packageRoot = path.join(repositoryRoot, "hexis-ui/node_modules"),
}) {
  const root = path.resolve(repositoryRoot);
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  if (!Array.isArray(manifest.assets) || manifest.assets.length === 0) {
    throw new Error("VAD assets manifest has no assets");
  }

  const packageSpecs = new Map((manifest.packages ?? []).map((entry) => [entry.name, entry]));
  const verified = [];
  for (const asset of manifest.assets) {
    const packageSpec = packageSpecs.get(asset.package);
    const packageVersion = packageSpec?.version;
    if (!packageSpec || !packageVersion || packageVersion !== asset.package_version) {
      throw new Error(`manifest package version mismatch for ${asset.package}`);
    }
    const packageDirectory = safeResolve(packageRoot, asset.package, "package path");
    const packageJson = JSON.parse(await readFile(path.join(packageDirectory, "package.json"), "utf8"));
    if (packageJson.name !== asset.package || packageJson.version !== packageVersion) {
      throw new Error(`installed package mismatch for ${asset.package}: expected ${packageVersion}`);
    }
    if (!packageSpec.license || packageJson.license !== packageSpec.license) {
      throw new Error(`installed package license mismatch for ${asset.package}: expected ${packageSpec.license ?? "a declared license"}`);
    }
    const source = safeResolve(packageDirectory, asset.source_member, "source member");
    const target = safeResolve(root, asset.target_relative_path, "asset target");
    const bytes = await readFile(source).catch((error) => {
      throw new Error(`missing VAD package member ${asset.package}/${asset.source_member}: ${error.message}`);
    });
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    if (bytes.length !== asset.bytes) {
      throw new Error(`byte length mismatch for ${asset.id}: expected ${asset.bytes}, received ${bytes.length}`);
    }
    if (sha256 !== asset.sha256) {
      throw new Error(`SHA-256 mismatch for ${asset.id}: expected ${asset.sha256}, received ${sha256}`);
    }
    verified.push({ asset, source, target, sha256, bytes: bytes.length });
  }
  if (
    typeof manifest.model_license?.source_url !== "string"
    || !manifest.model_license.source_url.startsWith("https://")
    || !/^[0-9a-f]{64}$/i.test(manifest.model_license.text_sha256 ?? "")
  ) {
    throw new Error("manifest must identify the model license source and its verified SHA-256");
  }

  for (const { target } of verified) await mkdir(path.dirname(target), { recursive: true });
  for (const { source, target } of verified) await copyFile(source, target);

  const nextManifest = {
    ...manifest,
    delivery_status: "materialized",
    materialized_assets: verified.map(({ asset, sha256, bytes }) => ({
      id: asset.id,
      target_relative_path: asset.target_relative_path,
      bytes,
      sha256,
    })),
  };
  await writeFile(manifestPath, `${JSON.stringify(nextManifest, null, 2)}\n`);
  return { assets: nextManifest.materialized_assets };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
  const repositoryRoot = path.resolve(scriptDirectory, "../..");
  try {
    const result = await prepareAssets({ repositoryRoot });
    console.log(`Prepared ${result.assets.length} VAD runtime assets from locked packages.`);
  } catch (error) {
    console.error(`Could not prepare VAD runtime assets: ${error.message}`);
    process.exitCode = 1;
  }
}
