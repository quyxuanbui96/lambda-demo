const fs = require('node:fs');
const path = require('node:path');

// Let pip in Docker verify the same corporate CA as the host, without packaging it.
module.exports.dockerArgs = () => {
  const bundle = process.env.AWS_CA_BUNDLE;
  if (!bundle) return [];
  const absolute = path.resolve(bundle);
  if (!fs.existsSync(absolute)) throw new Error('AWS_CA_BUNDLE file not found');
  return [
    '-v', `${absolute.replaceAll('\\', '/')}:/tmp/project-ca-bundle.pem:ro`,
    '-e', 'PIP_CERT=/tmp/project-ca-bundle.pem',
  ];
};

// Keep the existing-bucket notification helper on the chosen Node.js runtime.
module.exports.extensions = () => process.env.CSV_BUCKET_EXISTING === 'true' ? {
  CustomDashresourceDashexistingDashs3LambdaFunction: {
    Properties: { Runtime: 'nodejs22.x' },
  },
} : {};
