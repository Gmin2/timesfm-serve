import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  CfnOutput,
  Duration,
  Stack,
  type StackProps,
  aws_apprunner as apprunner,
  aws_ec2 as ec2,
  aws_ecr_assets as ecr_assets,
  aws_iam as iam,
  aws_lambda as lambda,
  RemovalPolicy,
  aws_logs as logs,
  aws_rds as rds,
  aws_ssm as ssm,

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

    /**
     * the model does not belong on lambda. measured there it spent 95 seconds
     * importing torch and loading weights on every cold start, then used 2986
     * of its 3008 mb ceiling. a model server wants to load once and stay warm,
     * so it runs as one always-on app runner container instead. x86_64 because
     * app runner has no architecture setting and only takes amd64 images.
     */
    const inferenceImage = new ecr_assets.DockerImageAsset(this, "InferenceImage", {
      directory: root,
      file: "Dockerfile",
      buildArgs: { TORCH: "cpu" },
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    const inferenceKeyParam = ssm.StringParameter.fromSecureStringParameterAttributes(
      this,
      "InferenceKeyParam",
      { parameterName: `${props.ssmPrefix}/INFERENCE_API_KEY` },
    );

    const appRunnerAccessRole = new iam.Role(this, "AppRunnerEcrRole", {
      assumedBy: new iam.ServicePrincipal("build.apprunner.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSAppRunnerServicePolicyForECRAccess"),
      ],
    });

    const inferenceInstanceRole = new iam.Role(this, "AppRunnerInstanceRole", {
      assumedBy: new iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
    });
    inferenceKeyParam.grantRead(inferenceInstanceRole);

    const inference = new apprunner.CfnService(this, "Inference", {
      serviceName: "timesfm-inference",
      sourceConfiguration: {
        autoDeploymentsEnabled: false,
        authenticationConfiguration: { accessRoleArn: appRunnerAccessRole.roleArn },
        imageRepository: {
          imageIdentifier: inferenceImage.imageUri,
          imageRepositoryType: "ECR",
          imageConfiguration: {
            port: "8000",
            runtimeEnvironmentSecrets: [
              { name: "INFERENCE_API_KEY", value: inferenceKeyParam.parameterArn },
            ],
          },
        },
      },
      instanceConfiguration: {
        cpu: "1 vCPU",
        memory: "3 GB",
        instanceRoleArn: inferenceInstanceRole.roleArn,
      },
      healthCheckConfiguration: {
        protocol: "HTTP",
        path: "/health",
        // loading the model takes a while on the first boot after a deploy
        interval: 10,
        timeout: 5,
        healthyThreshold: 1,
        unhealthyThreshold: 5,
      },
    });

    const gateway = new lambda.DockerImageFunction(this, "Gateway", {
      architecture,
      code: lambda.DockerImageCode.fromImageAsset(join(root, "gateway"), {
        platform: { platform: "linux/arm64" },
      }),
      memorySize: 1024,
      timeout: Duration.seconds(70),
      logGroup: new logs.LogGroup(this, "GatewayLogs", {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
      environment: {
        INFERENCE_URL: `https://${inference.attrServiceUrl}`,
        INFERENCE_TIMEOUT_MS: "60000",
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
    new CfnOutput(this, "InferenceUrl", { value: `https://${inference.attrServiceUrl}` });
    new CfnOutput(this, "SsmPrefix", { value: props.ssmPrefix });
    new CfnOutput(this, "DbEndpoint", { value: database.dbInstanceEndpointAddress });
    new CfnOutput(this, "DbSecretArn", { value: database.secret!.secretArn });
  }
}
