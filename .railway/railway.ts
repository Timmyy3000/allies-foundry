import {
  defineRailway,
  github,
  postgres,
  preserve,
  project,
  service,
  volume,
} from "railway/iac";

export default defineRailway(({ environment }) => {
  if (environment !== "staging")
    throw new Error("This configuration manages staging only.");
  const Postgres = postgres("Postgres", { region: "ams" });
  const postgresVolume = volume("postgres-volume", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: "ams",
    sizeMB: 5000,
  });
  const EventPublisher = service("Event  Publisher", {
    source: github("alliesai/allies-foundry", {
      branch: "staging",
      checkSuites: true,
    }),
    start:
      "uv run --no-sync python manage.py publish_event_deliveries --watch --interval 1",
    networking: { privateNetworkEndpoint: "allies-foundry" },
    replicas: { ams: 1 },

    env: {
      ALLIES_CLOUD_EVENT_DELIVERY_ENABLED: preserve(),
      ALLIES_CLOUD_EVENT_SERVICE_TOKEN: preserve(),
      ALLIES_CLOUD_SERVICE_TOKEN: preserve(),
      ALLIES_CLOUD_URL: preserve(),
      ALLIES_RUNTIME_IDLE_STOP_ENABLED: preserve(),
      ALLIES_WIDE_EVENTS_SLOW_MS: preserve(),
      ALLIES_WIDE_EVENTS_SUCCESS_SAMPLE_RATE: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_ALLOWED_HOSTS: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      DJANGO_TRUSTED_PROXY_IPS: preserve(),
      DJANGO_TRUST_PROXY_HEADERS: preserve(),
      FLY_API_TOKEN: preserve(),
      FLY_ORG: preserve(),
      FLY_REGION: preserve(),
      FOUNDRY_ORIGIN: preserve(),
      HERMES_IMAGE: preserve(),
      PROFILE_PROVISIONING_API_KEY: preserve(),
      RUNTIME_IMAGE: preserve(),
    },
  });
  const Foundry = service("Foundry", {
    source: github("alliesai/allies-foundry", {
      branch: "staging",
      checkSuites: true,
      rootDirectory: "/",
    }),
    build: {
      buildCommand: "",
      buildEnvironment: "V3",
      builder: "DOCKERFILE",
      dockerfilePath: "/Dockerfile",
    },
    replicas: { ams: 1 },
    deploy: { preDeployCommand: ["uv run python manage.py migrate --noinput"] },
    domains: [{ domain: "foundry.staging.yourallies.io", port: 8000 }],
    networking: { privateNetworkEndpoint: "foundry" },

    env: {
      ALLIES_CLOUD_EVENT_DELIVERY_ENABLED: preserve(),
      ALLIES_CLOUD_EVENT_SERVICE_TOKEN: preserve(),
      ALLIES_CLOUD_SERVICE_TOKEN: preserve(),
      ALLIES_CLOUD_URL: preserve(),
      ALLIES_PROCESS_TYPE: preserve(),
      ALLIES_RUNTIME_IDLE_STOP_ENABLED: preserve(),
      ALLIES_WIDE_EVENTS_ENABLED: preserve(),
      ALLIES_WIDE_EVENTS_MAX_BYTES: preserve(),
      ALLIES_WIDE_EVENTS_MAX_QUEUE_SIZE: preserve(),
      ALLIES_WIDE_EVENTS_SINK_ENABLED: preserve(),
      ALLIES_WIDE_EVENTS_SLOW_MS: preserve(),
      ALLIES_WIDE_EVENTS_SUCCESS_SAMPLE_RATE: preserve(),
      DATABASE_URL: preserve(),
      DJANGO_ALLOWED_HOSTS: preserve(),
      DJANGO_DEBUG: preserve(),
      DJANGO_SECRET_KEY: preserve(),
      DJANGO_TRUSTED_PROXY_IPS: preserve(),
      DJANGO_TRUST_PROXY_HEADERS: preserve(),
      FLY_API_TOKEN: preserve(),
      FLY_ORG: preserve(),
      FLY_REGION: preserve(),
      FOUNDRY_ORIGIN: preserve(),
      HERMES_IMAGE: preserve(),
      PORT: preserve(),
      PROFILE_PROVISIONING_API_KEY: preserve(),
      RAILPACK_PYTHON_VERSION: preserve(),
      RUNTIME_IMAGE: preserve(),
    },
  });

  const ReadinessPublisher = service("Readiness Publisher", {
    source: github("alliesai/allies-foundry", {
      branch: "staging",
      checkSuites: true,
    }),
    build: { builder: "DOCKERFILE", dockerfilePath: "/Dockerfile" },
    start:
      "uv run --no-sync python manage.py publish_profile_readiness_hints --watch",
    replicas: { ams: 1 },
    env: {
      DATABASE_URL: Foundry.env.DATABASE_URL,
      DJANGO_DEBUG: Foundry.env.DJANGO_DEBUG,
      DJANGO_SECRET_KEY: Foundry.env.DJANGO_SECRET_KEY,
      ALLIES_CLOUD_SERVICE_TOKEN: Foundry.env.ALLIES_CLOUD_SERVICE_TOKEN,
      ALLIES_CLOUD_URL: Foundry.env.ALLIES_CLOUD_URL,
      ALLIES_CLOUD_EVENT_SERVICE_TOKEN:
        Foundry.env.ALLIES_CLOUD_EVENT_SERVICE_TOKEN,
    },
  });

  return project("foundry", {
    resources: [
      Postgres,
      EventPublisher,
      Foundry,
      postgresVolume,
      ReadinessPublisher,
    ],
  });
});
