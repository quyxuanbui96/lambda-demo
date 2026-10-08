const path = require('node:path');
const { execFileSync } = require('node:child_process');

// Run after dependency injection and before Serverless hashes the Lambda code.
module.exports = class NormalizePackage {
  constructor(serverless) {
    this.hooks = {
      'before:package:compileFunctions': () => {
        const health = serverless.service.functions.health;
        const healthArtifact = health?.package?.artifact;
        if (health && (!healthArtifact || healthArtifact === serverless.service.package.artifact)) {
          throw new Error('Health must have a separate Lambda package');
        }
        const artifacts = new Set([
          serverless.service.package.artifact,
          ...Object.values(serverless.service.functions).map(fn => fn.package?.artifact),
        ].filter(Boolean));
        if (!artifacts.size) throw new Error('Lambda package artifact was not generated');
        for (const artifact of artifacts) {
          const args = [path.join(__dirname, 'normalize_package.py'), artifact];
          if (artifact === healthArtifact) {
            // Framework v4 injects shared requirements into individually packaged Python
            // functions too. Keep exactly the health source files before hashing/uploading.
            args.push('--include', ...health.package.patterns.filter(p => !p.startsWith('!')));
          }
          execFileSync('python', args, { stdio: 'inherit' });
        }
      },
    };
  }
};
