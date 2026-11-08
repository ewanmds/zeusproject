import type { Provider } from "./types";

/** One explicit wallet request. Verify locally before relaying an authorization. */
export async function signAgentApproval(
  provider: Provider,
  owner: string,
  typed: any,
) {
  const { TypedDataEncoder, verifyTypedData } = await import("ethers");
  const { EIP712Domain: _domain, ...types } = typed.types;
  const payload = TypedDataEncoder.getPayload(
    typed.domain,
    types,
    typed.message,
  );
  if (payload.primaryType !== typed.primaryType)
    throw Error("Format d’autorisation invalide.");
  const checkAccount = async () => {
    const accounts = await provider.request({ method: "eth_accounts" });
    if (accounts?.[0]?.toLowerCase() !== owner.toLowerCase())
      throw Error("Reconnectez le wallet qui possède ce compte.");
  };
  await checkAccount();
  const chain = parseInt(await provider.request({ method: "eth_chainId" }), 16);
  const requiredChain = Number(typed.domain.chainId);
  if (chain !== requiredChain)
    await provider.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: "0x" + requiredChain.toString(16) }],
    });
  await checkAccount();
  const signature = await provider.request({
    method: "eth_signTypedData_v4",
    params: [owner, JSON.stringify(payload)],
  });
  await checkAccount();
  const signer = verifyTypedData(
    payload.domain,
    types,
    payload.message,
    signature,
  );
  if (signer.toLowerCase() !== owner.toLowerCase())
    throw Error(
      "La signature provient d’un autre compte ou d’un message modifié. Vérifiez le wallet sélectionné, puis réessayez.",
    );
  return signature as string;
}
