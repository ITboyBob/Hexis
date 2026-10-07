export interface VoiceBargeInRunProfile {
  schema_version: 2;
  profile_id: string;
  status: string;
  path_base: "repository_root";
  ui_url: string;
  api_url: string;
  realtime_config: string;
  runtime_config: string;
  detection_config: string;
  asset_manifest: string;
  playback_coordination_config: string;
  audio_case_spec: string;
  database: {
    pytest_service: "pytest-db";
    talk_service: "talk-db";
    user_env: "POSTGRES_USER";
    password_env: "POSTGRES_PASSWORD";
    admin_database: "postgres";
  };
  runtime_environment: {
    api_environment_record_env: "HEXIS_VOICE_BARGE_IN_ENV";
    api_start_record_env: "HEXIS_VOICE_BARGE_IN_API_START_RECORD";
    ui_database_url_env: "HEXIS_DATABASE_URL";
    ui_database_url_compat_env: "DATABASE_URL";
    ui_api_url_env: "HEXIS_API_URL";
  };
  record_outputs: Record<string, string>;
  browser: {
    inspection: "in_app";
    automation: "playwright_chrome";
    headless: false;
    sprint_usage: Record<string, string[]>;
  };
}

const expectedSprintUsage: Record<string, string[]> = {
  S1: ["inspection"],
  S2: ["inspection", "automation"],
  S3: [],
  S4: ["inspection"],
  S5: ["automation"],
  S6: ["inspection"],
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactStringFields(value: Record<string, unknown>, expected: Record<string, string>): boolean {
  return Object.entries(expected).every(([key, entry]) => value[key] === entry);
}

export function parseRunProfile(value: unknown): VoiceBargeInRunProfile {
  if (!isObject(value) || value.schema_version !== 2) {
    throw new Error("run profile must use schema_version 2");
  }
  if ("database_url_env" in value) {
    throw new Error("legacy database_url_env is not supported in schema_version 2");
  }
  const database = value.database;
  if (!isObject(database) || !exactStringFields(database, {
    pytest_service: "pytest-db",
    talk_service: "talk-db",
    user_env: "POSTGRES_USER",
    password_env: "POSTGRES_PASSWORD",
    admin_database: "postgres",
  })) {
    throw new Error("database profile must name the isolated pytest-db and talk-db services and credential variables");
  }
  const runtime = value.runtime_environment;
  if (!isObject(runtime) || !exactStringFields(runtime, {
    api_environment_record_env: "HEXIS_VOICE_BARGE_IN_ENV",
    api_start_record_env: "HEXIS_VOICE_BARGE_IN_API_START_RECORD",
    ui_database_url_env: "HEXIS_DATABASE_URL",
    ui_database_url_compat_env: "DATABASE_URL",
    ui_api_url_env: "HEXIS_API_URL",
  })) {
    throw new Error("runtime_environment does not match the explicit M6 environment contract");
  }
  const browser = value.browser;
  if (!isObject(browser) || browser.inspection !== "in_app" || browser.automation !== "playwright_chrome" || browser.headless !== false) {
    throw new Error("browser must define in_app inspection and visible Playwright Chrome automation");
  }
  if (!isObject(browser.sprint_usage) || JSON.stringify(browser.sprint_usage) !== JSON.stringify(expectedSprintUsage)) {
    throw new Error("browser sprint_usage mapping is incomplete or unsupported");
  }
  for (const field of ["realtime_config", "runtime_config", "detection_config", "asset_manifest", "playback_coordination_config", "audio_case_spec"]) {
    if (typeof value[field] !== "string" || value[field] === "") {
      throw new Error(`run profile is missing ${field}`);
    }
  }
  return value as unknown as VoiceBargeInRunProfile;
}
