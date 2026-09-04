import { App } from "aws-cdk-lib";

import { TimesfmStack } from "../lib/stack.js";

const app = new App();

new TimesfmStack(app, "TimesfmServe", {
  ssmPrefix: process.env.SSM_PREFIX ?? "/timesfm-serve",
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
});
