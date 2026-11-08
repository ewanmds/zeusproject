import { describe, it, expect } from "vitest";
import { Wallet } from "ethers";
import { signTypedData, SignTypedDataVersion } from "@metamask/eth-sig-util";
import { signAgentApproval } from "./authorization";
import type { Provider } from "./types";

const owner = Wallet.createRandom();
const typed = {
  domain: {
    name: "HyperliquidSignTransaction",
    version: "1",
    chainId: 42161,
    verifyingContract: "0x" + "0".repeat(40),
  },
  primaryType: "HyperliquidTransaction:ApproveAgent",
  // Deliberately omit the domain definition, as the old API did.
  types: {
    "HyperliquidTransaction:ApproveAgent": [
      { name: "hyperliquidChain", type: "string" },
      { name: "agentAddress", type: "address" },
      { name: "agentName", type: "string" },
      { name: "nonce", type: "uint64" },
    ],
  },
  message: {
    hyperliquidChain: "Mainnet",
    agentAddress: Wallet.createRandom().address,
    agentName: "Zeus test",
    nonce: 123456789,
  },
};

describe("agent wallet authorization", () => {
  it("sends a full V4 domain and verifies a real MetaMask signature", async () => {
    let signed: any;
    let chain = "0x1";
    const provider: Provider = {
      request: async ({ method, params }) => {
        if (method === "eth_accounts") return [owner.address];
        if (method === "eth_chainId") return chain;
        if (method === "wallet_switchEthereumChain") {
          chain = (params![0] as any).chainId;
          return null;
        }
        if (method === "eth_signTypedData_v4") {
          signed = JSON.parse(params![1] as string);
          return signTypedData({
            privateKey: Buffer.from(owner.privateKey.slice(2), "hex"),
            data: signed,
            version: SignTypedDataVersion.V4,
          });
        }
        throw Error("Unexpected wallet request");
      },
    };
    const signature = await signAgentApproval(provider, owner.address, typed);
    expect(signature).toMatch(/^0x[0-9a-f]{130}$/i);
    expect(signed.types.EIP712Domain.map((x: any) => x.name)).toEqual([
      "name",
      "version",
      "chainId",
      "verifyingContract",
    ]);
    expect(chain).toBe("0xa4b1");
  });

  it("rejects a wallet account change during the network switch", async () => {
    let switched = false,
      requests = 0;
    const provider: Provider = {
      request: async ({ method }) => {
        if (method === "eth_accounts")
          return [switched ? Wallet.createRandom().address : owner.address];
        if (method === "eth_chainId") return "0x1";
        if (method === "wallet_switchEthereumChain") {
          switched = true;
          return null;
        }
        if (method === "eth_signTypedData_v4") requests++;
      },
    };
    await expect(
      signAgentApproval(provider, owner.address, typed),
    ).rejects.toThrow("Reconnectez");
    expect(requests).toBe(0);
  });

  it("rejects a signature from another wallet before any relay", async () => {
    const other = Wallet.createRandom();
    const provider: Provider = {
      request: async ({ method, params }) => {
        if (method === "eth_accounts") return [owner.address];
        if (method === "eth_chainId") return "0xa4b1";
        if (method === "eth_signTypedData_v4")
          return signTypedData({
            privateKey: Buffer.from(other.privateKey.slice(2), "hex"),
            data: JSON.parse(params![1] as string),
            version: SignTypedDataVersion.V4,
          });
      },
    };
    await expect(
      signAgentApproval(provider, owner.address, typed),
    ).rejects.toThrow("autre compte");
  });
});
