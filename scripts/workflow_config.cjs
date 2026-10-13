// CloudFormation policies and optional private-RDS network configuration.
const queueArn = { 'Fn::GetAtt': ['AcquisitionQueue', 'Arn'] };
module.exports.identity = () => process.env.WF_DEV_IDENTITY || JSON.stringify({ userId: 'demo-user', allowedOrganizations: [{ organizationId: 'org-demo', displayName: 'Demo organization' }] });
const bucketArn = { 'Fn::GetAtt': ['WorkflowBucket', 'Arn'] };
const objectArn = prefix => ({ 'Fn::Sub': '${WorkflowBucket.Arn}/workflow/' + prefix + '/*' });
const secrets = { Effect: 'Allow', Action: ['secretsmanager:GetSecretValue'], Resource:
  process.env.WF_DATABASE_SECRET_ARN || 'arn:aws:secretsmanager:ap-southeast-2:000000000000:secret:unconfigured' };
const s3 = (prefixes, actions) => ({ Effect: 'Allow', Action: actions, Resource: prefixes.map(objectArn) });
// HeadObject needs bucket ListBucket permission to distinguish absent objects (404) from 403.
// Object reads/writes remain restricted to each function's prefixes.
const listing = () => ({ Effect: 'Allow', Action: ['s3:ListBucket'], Resource: bucketArn });
const versions = { Effect: 'Allow', Action: ['s3:ListBucketVersions'], Resource: bucketArn,
  Condition: { StringLike: { 's3:prefix': ['workflow/temporary/*'] } } };
const send = { Effect: 'Allow', Action: ['sqs:SendMessage'], Resource: queueArn };
const role = statements => ({ Type: 'AWS::IAM::Role', Properties: {
  AssumeRolePolicyDocument: { Version: '2012-10-17', Statement: [{ Effect: 'Allow', Action: 'sts:AssumeRole', Principal: { Service: 'lambda.amazonaws.com' } }] },
  ManagedPolicyArns: [{ 'Fn::Sub': 'arn:${AWS::Partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole' }],
  Policies: [{ PolicyName: 'workflow-data', PolicyDocument: { Version: '2012-10-17', Statement: [secrets, ...statements] } }],
} });
module.exports.roles = () => ({
  WorkflowApiRole: role([listing(), s3(['temporary', 'output', 'generated'], ['s3:GetObject', 's3:GetObjectVersion']), s3(['temporary', 'output'], ['s3:PutObject']), send]),
  WorkflowWorkerRole: role([listing(), s3(['source', 'generated'], ['s3:GetObject', 's3:GetObjectVersion']), s3(['generated'], ['s3:PutObject']), { Effect: 'Allow', Action: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes', 'sqs:ChangeMessageVisibility'], Resource: queueArn }]),
  WorkflowMaintenanceRole: role([listing(), versions, s3(['temporary', 'output'], ['s3:GetObject', 's3:GetObjectVersion']), s3(['temporary'], ['s3:DeleteObject', 's3:DeleteObjectVersion']), send]),
});
module.exports.extensions = () => {
  const subnets = (process.env.WF_VPC_SUBNET_IDS || '').split(',').map(value => value.trim()).filter(Boolean);
  const groups = (process.env.WF_VPC_SECURITY_GROUP_IDS || '').split(',').map(value => value.trim()).filter(Boolean);
  if (!subnets.length && !groups.length) return {};
  if (!subnets.length || !groups.length) throw new Error('Configure both workflow VPC subnets and security groups');
  const Properties = { VpcConfig: { SubnetIds: subnets, SecurityGroupIds: groups } };
  return Object.fromEntries(['WorkflowApiLambdaFunction', 'AcquisitionWorkerLambdaFunction', 'WorkflowMaintenanceLambdaFunction'].map(name => [name, { Properties }]));
};
