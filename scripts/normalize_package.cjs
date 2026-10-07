const path = require('node:path');
const { execFileSync } = require('node:child_process');

// Run after dependency injection and before Serverless hashes the Lambda code.
module.exports = class NormalizePackage {
  constructor(serverless) {
    this.hooks = {
      'before:package:compileFunctions': () => {
        const artifact = serverless.service.package.artifact;
        if (!artifact) throw new Error('Lambda package artifact was not generated');
        execFileSync('python', [path.join(__dirname, 'normalize_package.py'), artifact],
          { stdio: 'inherit' });
      },
    };
  }
};
