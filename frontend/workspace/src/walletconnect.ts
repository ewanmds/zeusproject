import { createAppKit } from "@reown/appkit";
import { EthersAdapter } from "@reown/appkit-adapter-ethers";
import { mainnet, arbitrum } from "@reown/appkit/networks";
import type { Provider } from "./types";
let modal: ReturnType<typeof createAppKit> | null = null;
export async function connectWalletConnect(
  projectId: string,
): Promise<Provider> {
  if (!projectId)
    throw new Error(
      "WalletConnect nécessite un identifiant de projet Reown. Les wallets installés restent disponibles.",
    );
  modal ??= createAppKit({
    adapters: [new EthersAdapter()],
    networks: [mainnet, arbitrum],
    projectId,
    metadata: {
      name: "Zeus",
      description: "Zeus Trading Workspace",
      url: location.origin,
      icons: [],
    },
    themeMode: "dark",
    themeVariables: {
      "--w3m-accent": "#8b5cf6",
      "--w3m-border-radius-master": "2px",
    },
    features: { analytics: false, email: false, socials: [] },
  });
  if (modal.getAccount("eip155")?.isConnected)
    return modal.getWalletProvider() as Provider;
  return new Promise((resolve, reject) => {
    let done = false;
    let opened = false;
    let offProvider = () => {};
    let offState = () => {};
    let timer: ReturnType<typeof setTimeout>;
    const finish = (provider?: Provider) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      offProvider();
      offState();
      provider ? resolve(provider) : reject(new Error("Connexion annulée."));
    };
    offProvider = modal!.subscribeProviders((state) => {
      if (state.eip155) finish(state.eip155 as Provider);
    });
    offState = modal!.subscribeState((state) => {
      if (state.open) opened = true;
      else if (opened && !modal!.getAccount("eip155")?.isConnected) finish();
    });
    timer = setTimeout(() => finish(), 180000);
    modal!.open({ view: "Connect", namespace: "eip155" }).catch(() => finish());
  });
}
