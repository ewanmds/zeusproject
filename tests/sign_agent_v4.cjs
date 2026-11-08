// Strict EIP-712 V4 implementation used by MetaMask; input uses disposable keys.
const { signTypedData, SignTypedDataVersion } = require('../frontend/workspace/node_modules/@metamask/eth-sig-util');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', () => {
  const { typed, key } = JSON.parse(raw);
  const privateKey = Buffer.from(key.replace(/^0x/, ''), 'hex');
  const version = SignTypedDataVersion.V4;
  const valid = signTypedData({ privateKey, data: typed, version });
  const missingDomain = structuredClone(typed);
  delete missingDomain.types.EIP712Domain;
  const legacy = signTypedData({ privateKey, data: missingDomain, version });
  process.stdout.write(JSON.stringify({ valid, legacy }));
});
