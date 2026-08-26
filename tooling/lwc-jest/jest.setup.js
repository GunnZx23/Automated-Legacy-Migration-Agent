const { expect } = require('@jest/globals');
const { jestConfig } = require('@salesforce/sfdx-lwc-jest/config');

// The upstream LWC preset registers custom matchers through a global `expect`.
// Bridge that legacy setup only, then remove the binding before candidate or
// controller test modules are evaluated. Test files import every Jest API.
if (Object.prototype.hasOwnProperty.call(globalThis, 'expect')) {
    throw new Error('Jest injected an unexpected global expect binding');
}
Object.defineProperty(globalThis, 'expect', {
    configurable: true,
    value: expect
});
try {
    for (const setupPath of jestConfig.setupFilesAfterEnv) {
        require(setupPath);
    }
} finally {
    delete globalThis.expect;
}
if (Object.prototype.hasOwnProperty.call(globalThis, 'expect')) {
    throw new Error('temporary global expect binding was not removed');
}
