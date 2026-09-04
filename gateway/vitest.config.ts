import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // integration tests talk to a real postgres. locally that is the docker
    // compose one, in ci it is the postgres service container.
    env: {
      DATABASE_URL:
        process.env.TEST_DATABASE_URL ?? "postgresql://tfm:tfm@localhost:5432/tfm_test",
    },
    fileParallelism: false,
  },
});
