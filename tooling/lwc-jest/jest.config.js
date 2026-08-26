const { jestConfig } = require('@salesforce/sfdx-lwc-jest/config');

module.exports = {
    ...jestConfig,
    injectGlobals: false,
    setupFilesAfterEnv: [require.resolve('./jest.setup.js')]
};
