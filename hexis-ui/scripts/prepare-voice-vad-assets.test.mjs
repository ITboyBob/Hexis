import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";
import { prepareAssets } from "./prepare-voice-vad-assets.mjs";

const temporaryDirectories = [];

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "hexis-vad-assets-"));
  temporaryDirectories.push(root);
  const manifestPath = path.join(root, "manifest.json");
  const packageRoot = path.join(root, "node_modules");
  const vadPackage = path.join(packageRoot, "@ricky0123", "vad-web");
  const ortPackage = path.join(packageRoot, "onnxruntime-web");
  const source = path.join(vadPackage, "dist");
  const runtime = path.join(ortPackage, "dist");
  await mkdir(source, { recursive: true });
  await mkdir(runtime, { recursive: true });
  const assets = [
    ["@ricky0123/vad-web", vadPackage, source, "vad.onnx", "models/vad.onnx", "ISC", "model-bytes"],
    ["onnxruntime-web", ortPackage, runtime, "ort.wasm", "ort/ort.wasm", "MIT", "wasm-bytes"],
  ];
  const crypto = await import("node:crypto");
  const packages = [];
  for (const [pkg, directory, sourceDirectory, member, target, license, contents] of assets) {
    const bytes = Buffer.from(contents);
    await writeFile(path.join(directory, "package.json"), JSON.stringify({ name: pkg, version: "1.0.0", license }));
    await writeFile(path.join(sourceDirectory, member), bytes);
    packages.push({ name: pkg, version: "1.0.0", license });
    const entry = {
      id: member,
      package: pkg,
      package_version: "1.0.0",
      source_member: `dist/${member}`,
      target_relative_path: `hexis-ui/public/voice-realtime/vad-assets/${target}`,
      bytes: bytes.length,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    };
    if (!globalThis.manifestAssets) globalThis.manifestAssets = [];
    globalThis.manifestAssets.push(entry);
  }
  await writeFile(manifestPath, JSON.stringify({
    schema_version: 1,
    delivery_status: "not_materialized",
    packages,
    assets: globalThis.manifestAssets,
    model_license: { source_url: "https://example.test/model-license", text_sha256: "a".repeat(64) },
  }));
  return { root, manifestPath, packageRoot };
}

afterEach(async () => {
  globalThis.manifestAssets = [];
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

test("copies only manifest assets and returns their verified hashes", async () => {
  const testFixture = await fixture();
  const result = await prepareAssets({
    repositoryRoot: testFixture.root,
    manifestPath: testFixture.manifestPath,
    packageRoot: testFixture.packageRoot,
  });
  assert.equal(result.assets.length, 2);
  for (const asset of result.assets) {
    const output = path.join(testFixture.root, asset.target_relative_path);
    assert.equal((await readFile(output)).length, asset.bytes);
    assert.equal(asset.sha256.length, 64);
  }
  const repeated = await prepareAssets({
    repositoryRoot: testFixture.root,
    manifestPath: testFixture.manifestPath,
    packageRoot: testFixture.packageRoot,
  });
  assert.deepEqual(repeated.assets, result.assets);
});

test("rejects hash mismatches without writing output", async () => {
  const testFixture = await fixture();
  const manifest = JSON.parse(await readFile(testFixture.manifestPath, "utf8"));
  manifest.assets[0].sha256 = "0".repeat(64);
  await writeFile(testFixture.manifestPath, JSON.stringify(manifest));
  await assert.rejects(
    prepareAssets({
      repositoryRoot: testFixture.root,
      manifestPath: testFixture.manifestPath,
      packageRoot: testFixture.packageRoot,
    }),
    /SHA-256 mismatch/,
  );
  await assert.rejects(readFile(path.join(testFixture.root, manifest.assets[0].target_relative_path)));
});

test("rejects missing package members with an actionable message", async () => {
  const testFixture = await fixture();
  await rm(path.join(testFixture.packageRoot, "@ricky0123/vad-web/dist/vad.onnx"));
  await assert.rejects(
    prepareAssets({
      repositoryRoot: testFixture.root,
      manifestPath: testFixture.manifestPath,
      packageRoot: testFixture.packageRoot,
    }),
    /missing VAD package member/,
  );
});

test("rejects package license drift before copying assets", async () => {
  const testFixture = await fixture();
  const packageJsonPath = path.join(testFixture.packageRoot, "@ricky0123/vad-web/package.json");
  await writeFile(packageJsonPath, JSON.stringify({ name: "@ricky0123/vad-web", version: "1.0.0", license: "UNVERIFIED" }));
  await assert.rejects(
    prepareAssets({
      repositoryRoot: testFixture.root,
      manifestPath: testFixture.manifestPath,
      packageRoot: testFixture.packageRoot,
    }),
    /package license mismatch/,
  );
  await assert.rejects(readFile(path.join(testFixture.root, "hexis-ui/public/voice-realtime/vad-assets/models/vad.onnx")));
});

test("rejects asset paths that escape the repository", async () => {
  const testFixture = await fixture();
  const manifest = JSON.parse(await readFile(testFixture.manifestPath, "utf8"));
  manifest.assets[0].target_relative_path = "../../outside.onnx";
  await writeFile(testFixture.manifestPath, JSON.stringify(manifest));
  await assert.rejects(
    prepareAssets({
      repositoryRoot: testFixture.root,
      manifestPath: testFixture.manifestPath,
      packageRoot: testFixture.packageRoot,
    }),
    /escapes repository root/,
  );
});
