Copied from github.com/quiknode-labs/hyperliquid-sdk/go v0.1.7 (MIT; LICENSE retained).
Local patch: WithAccountAddress selects the trading account returned by Address,
while SignerAddress and wallet.SignHash retain the API wallet as the signer.
This also fixes account selection inside SDK ClosePosition and CancelAll helpers.
No signing keys are included.

WithDirectExchange enables native Hyperliquid info/exchange routing and local
MessagePack/EIP-712 Agent signing. No builder field, approval, remote build/hash
service or builder fee is used. Supported bot actions: perpetual orders,
batchModify, cancel, cancel-all, updateLeverage, and reduce-only IOC closes.
HIP-3 IDs retain exact DEX namespaces. Unsupported actions fail without fallback.
Tests compare digests against the official Python SDK and recover API signatures
on a local fake HTTP exchange. No live order was used for verification.
