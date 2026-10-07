import { readFileSync } from "node:fs";
import path from "node:path";
import { parseRunProfile } from "./run-profile";

describe("voice barge-in run profile", () => {
  const profilePath = path.resolve(process.cwd(), "../config/voice-barge-in/run-profile.json");

  it("accepts the explicit v2 profile and browser use by sprint", () => {
    const profile = JSON.parse(readFileSync(profilePath, "utf8"));
    expect(parseRunProfile(profile)).toMatchObject({
      schema_version: 2,
      database: { pytest_service: "pytest-db", talk_service: "talk-db", admin_database: "postgres" },
      runtime_environment: {
        api_environment_record_env: "HEXIS_VOICE_BARGE_IN_ENV",
        api_start_record_env: "HEXIS_VOICE_BARGE_IN_API_START_RECORD",
        ui_database_url_env: "HEXIS_DATABASE_URL",
        ui_api_url_env: "HEXIS_API_URL",
      },
      browser: {
        inspection: "in_app",
        automation: "playwright_chrome",
        headless: false,
      },
    });
  });

  it("rejects the legacy v1 profile", () => {
    const legacy = JSON.parse(readFileSync(profilePath, "utf8"));
    legacy.schema_version = 1;
    expect(() => parseRunProfile(legacy)).toThrow(/schema_version 2/);
  });

  it("rejects incomplete browser sprint mapping", () => {
    const profile = JSON.parse(readFileSync(profilePath, "utf8"));
    profile.browser.sprint_usage.S5 = ["inspection"];
    expect(() => parseRunProfile(profile)).toThrow(/sprint_usage/);
  });
});
