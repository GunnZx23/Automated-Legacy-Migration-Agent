# Pinned LWC Jest toolchain

This directory is validator-owned tooling, not migration output. Its manifest and
lock pin `@salesforce/sfdx-lwc-jest` at `7.9.0`; generated Salesforce candidates
must not create or modify these files. Install dependencies only here with
`npm ci`.

## Two independent test suites

The Engineer model (Qwen) generates the candidate test at
`force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js`.
That suite is migration output: it documents the generated component's intended
behavior and is retained with the migrated project. It is useful evidence, but
Qwen-generated tests cannot independently certify Qwen-generated implementation.

The validator owns the immutable
`controller-tests/accountContactExplorer.controller.test.js` suite. It lives
outside the candidate workspace and is run separately. A Salesforce migration
passes local LWC validation only when the deterministic candidate contract, the
generated candidate suite, and the immutable controller suite all pass.

## Direct runs

Run both commands with the candidate workspace as the current working directory.
`NODE_PATH` must point to this toolchain's `node_modules`; dependencies must not
be copied into or resolved from the candidate. Replace the angle-bracket paths
with absolute paths before running the commands.

Generated candidate suite:

```sh
cd <candidate-root>
NODE_PATH=<repo-root>/tooling/lwc-jest/node_modules \
  <node-executable> \
  <repo-root>/tooling/lwc-jest/node_modules/jest/bin/jest.js \
  --config <repo-root>/tooling/lwc-jest/jest.config.js \
  --rootDir <candidate-root> \
  --runInBand --no-cache \
  --runTestsByPath \
  <candidate-root>/force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js
```

Immutable controller suite against that same candidate:

```sh
cd <candidate-root>
NODE_PATH=<repo-root>/tooling/lwc-jest/node_modules \
  <node-executable> \
  <repo-root>/tooling/lwc-jest/node_modules/jest/bin/jest.js \
  --config <repo-root>/tooling/lwc-jest/jest.config.js \
  --rootDir <repo-root>/tooling/lwc-jest \
  --runInBand --no-cache \
  --runTestsByPath \
  <repo-root>/tooling/lwc-jest/controller-tests/accountContactExplorer.controller.test.js
```

These commands exercise the pinned local JavaScript harness only. Passing them
does not claim that Apex tests ran or that Salesforce accepted a deployment.
