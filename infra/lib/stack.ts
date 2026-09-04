import { join } from "node:path";

import {
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_ssm as ssm,
} from "aws-cdk-lib";
import type { Construct } from "constructs";

const root = join(import.meta.dirname, "..", "..");

export type TimesfmStackProps = StackProps & {
  /** ssm path holding DATABASE_URL, BETTER_AUTH_SECRET and the github oauth pair */
  ssmPrefix: string;
};

export class TimesfmStack extends Stack {
  constructor(scope: Construct, id: string, props: TimesfmStackProps) {
    super(scope, id, props);

    // arm64 everywhere: the images build natively on an apple silicon laptop
    // and graviton lambda is about 20% cheaper per gb second.
    const architecture = lambda.Architecture.ARM_64;

    const inference = new lambda.DockerImageFunction(this, "Inference", {
      architecture,
      code: lambda.DockerImageCode.fromImageAsset(root, {
        file: "Dockerfile",
        buildArgs: { TORCH: "cpu" },
        platform: { platform: "linux/arm64" },
      }),
      // the model needs the memory, and lambda scales cpu with memory so this
      // is also what keeps a forecast under a second once warm
      memorySize: 4096,
      ephemeralStorageSize: undefined,
      timeout: Duration.minutes(2),
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // iam auth, so the model endpoint is not an open compute faucet
    const inferenceUrl = inference.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
    });

    const gateway = new lambda.DockerImageFunction(this, "Gateway", {
      architecture,
      code: lambda.DockerImageCode.fromImageAsset(join(root, "gateway"), {
        platform: { platform: "linux/arm64" },
      }),
      memorySize: 1024,
      timeout: Duration.seconds(60),
      logRetention: logs.RetentionDays.ONE_WEEK,
      environment: {
        INFERENCE_URL: inferenceUrl.url,
        INFERENCE_AUTH: "iam",
        SSM_PREFIX: props.ssmPrefix,
        NODE_ENV: "production",
      },
    });

    const gatewayUrl = gateway.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
    });

    // the gateway signs its calls to the model endpoint
    inferenceUrl.grantInvokeUrl(gateway);

    // secrets stay in parameter store and are read at cold start, so nothing
    // sensitive sits in the function config where the console shows it
    gateway.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameters"],
        resources: [
          Stack.of(this).formatArn({
            service: "ssm",
            resource: "parameter",
            resourceName: `${props.ssmPrefix.replace(/^\//, "")}/*`,
          }),
        ],
      }),
    );

    // the gateway reads these at boot; create them before deploying
    for (const name of ["DATABASE_URL", "BETTER_AUTH_SECRET"]) {
      ssm.StringParameter.fromSecureStringParameterAttributes(this, `Param${name}`, {
        parameterName: `${props.ssmPrefix}/${name}`,
      });
    }

    new CfnOutput(this, "GatewayUrl", { value: gatewayUrl.url });
    new CfnOutput(this, "InferenceUrl", { value: inferenceUrl.url });
    new CfnOutput(this, "SsmPrefix", { value: props.ssmPrefix });
  }
}
