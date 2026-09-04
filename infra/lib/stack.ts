import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
  aws_ec2 as ec2,
  aws_iam as iam,
  aws_lambda as lambda,
  RemovalPolicy,
  aws_logs as logs,
  aws_rds as rds,

} from "aws-cdk-lib";
import type { Construct } from "constructs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

export type TimesfmStackProps = StackProps & {
  /** ssm path holding DATABASE_URL, BETTER_AUTH_SECRET and the github oauth pair */
  ssmPrefix: string;
};

export class TimesfmStack extends Stack {
  constructor(scope: Construct, id: string, props: TimesfmStackProps) {
    super(scope, id, props);

    // public subnets only and no nat gateway. the lambdas stay outside the vpc
    // so they keep internet access for the weather api and github oauth without
    // a nat, which would be about 32 usd a month on its own.
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    const dbSecurityGroup = new ec2.SecurityGroup(this, "DbSg", {
      vpc,
      description: "postgres, reachable from lambda outside the vpc",
    });
    // lambdas outside a vpc have no stable egress address, so this cannot be
    // narrowed to a cidr. tls is forced and the password is generated, but the
    // instance is internet reachable and that is the trade for skipping nat.
    dbSecurityGroup.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(5432), "postgres");

    const parameterGroup = new rds.ParameterGroup(this, "DbParams", {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      parameters: { "rds.force_ssl": "1" },
    });

    const database = new rds.DatabaseInstance(this, "Db", {
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      publiclyAccessible: true,
      securityGroups: [dbSecurityGroup],
      parameterGroup,
      allocatedStorage: 20,
      maxAllocatedStorage: 50,
      multiAz: false,
      databaseName: "tfm",
      credentials: rds.Credentials.fromGeneratedSecret("tfm"),
      backupRetention: Duration.days(1),
      deleteAutomatedBackups: true,
      removalPolicy: RemovalPolicy.DESTROY,
      deletionProtection: false,
    });

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
      timeout: Duration.minutes(2),
      logGroup: new logs.LogGroup(this, "InferenceLogs", {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
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
      logGroup: new logs.LogGroup(this, "GatewayLogs", {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
      environment: {
        INFERENCE_URL: inferenceUrl.url,
        INFERENCE_AUTH: "iam",
        SSM_PREFIX: props.ssmPrefix,
        DB_SECRET_ARN: database.secret!.secretArn,
        DB_NAME: "tfm",
        NODE_ENV: "production",
      },
    });

    database.secret!.grantRead(gateway);

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

    // the parameters themselves are created out of band, not by this stack, so
    // that rotating a secret is not a deploy. missing ones are logged and
    // skipped at boot, which is how github sign in stays optional.

    new CfnOutput(this, "GatewayUrl", { value: gatewayUrl.url });
    new CfnOutput(this, "InferenceUrl", { value: inferenceUrl.url });
    new CfnOutput(this, "SsmPrefix", { value: props.ssmPrefix });
    new CfnOutput(this, "DbEndpoint", { value: database.dbInstanceEndpointAddress });
    new CfnOutput(this, "DbSecretArn", { value: database.secret!.secretArn });
  }
}
