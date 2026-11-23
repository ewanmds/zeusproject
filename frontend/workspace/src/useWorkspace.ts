import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  APIError,
  setCSRF,
  SnapshotStore,
  invalidateIdentity,
  workspaceSocketUrl,
} from "./data";
import type {
  Identity,
  Snapshot,
  Provider,
  Config,
  Balance,
  Wallet,
} from "./types";

export function useWorkspace() {
  const [identity, setIdentity] = useState<Identity | null>(null),
    [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [config, setConfig] = useState<Config | null>(null),
    [balance, setBalance] = useState<Balance | null>(null);
  const [booting, setBooting] = useState(true),
    [connection, setConnection] = useState("offline");
  const [received, setReceived] = useState(0),
    [error, setError] = useState("");
  const [wallets, setWallets] = useState<Wallet[]>([]);
  const provider = useRef<Provider | null>(null),
    epoch = useRef(0),
    refreshRef = useRef<() => void>(() => {}),
    acceptRef = useRef<(value: Snapshot) => void>(() => {});
  const clear = useCallback(() => {
    epoch.current++;
    invalidateIdentity();
    setIdentity(null);
    setSnapshot(null);
    setBalance(null);
    setReceived(0);
    setConnection("offline");
    setCSRF("");
  }, []);
  const loadIdentity = useCallback(async () => {
    const generation = epoch.current;
    try {
      const me = await api<Identity>("/me");
      if (generation !== epoch.current) return;
      setCSRF(me.csrf);
      setIdentity(me);
    } catch (e) {
      if (e instanceof APIError && e.status === 401) clear();
      else setError("Serveur indisponible. Réessayez la connexion.");
    } finally {
      setBooting(false);
    }
  }, [clear]);
  useEffect(() => {
    api<Config>("/config")
      .then(setConfig)
      .catch(() => setError("Connexion au workspace indisponible."));
    loadIdentity();
  }, [loadIdentity]);
  useEffect(() => {
    const announce = (event: Event) => {
      const detail = (event as CustomEvent<Wallet>).detail;
      if (detail?.provider && detail.info?.uuid)
        setWallets((old) =>
          old.some((w) => w.info.uuid === detail.info.uuid)
            ? old
            : [...old, detail],
        );
    };
    window.addEventListener("eip6963:announceProvider", announce);
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    const injected = (window as unknown as { ethereum?: Provider }).ethereum;
    if (injected)
      setWallets((old) =>
        old.length
          ? old
          : [
              {
                info: { uuid: "injected", name: "Wallet installé" },
                provider: injected,
              },
            ],
      );
    return () =>
      window.removeEventListener("eip6963:announceProvider", announce);
  }, []);
  const connect = useCallback(
    async (p: Provider) => {
      clear();
      const generation = epoch.current;
      setError("");
      provider.current = p;
      const accounts = await p.request({ method: "eth_requestAccounts" });
      const address = accounts?.[0];
      if (!address) throw Error("Aucun compte sélectionné.");
      const chain = parseInt(await p.request({ method: "eth_chainId" }), 16);
      const { message } = await api<{ message: string }>("/auth/challenge", {
        address,
        chain_id: chain,
      });
      const encoded =
        "0x" +
        Array.from(new TextEncoder().encode(message))
          .map((v) => v.toString(16).padStart(2, "0"))
          .join("");
      const signature = await p.request({
        method: "personal_sign",
        params: [encoded, address],
      });
      if (generation !== epoch.current) throw new Error("Connexion annulée.");
      await api("/auth/verify", { signature });
      await loadIdentity();
    },
    [clear, loadIdentity],
  );
  useEffect(() => {
    const p = provider.current;
    if (!identity || !p?.on) return;
    const change = (accounts: string[]) => {
      if (accounts?.[0]?.toLowerCase() === identity.wallet) return;
      // Keep the old cookie only long enough to revoke it; clear UI synchronously.
      const logout = api("/auth/logout", {}).catch(() => {});
      clear();
      setError("Wallet modifié. Connectez le compte sélectionné.");
      void logout;
    };
    p.on("accountsChanged", change);
    return () => p.removeListener?.("accountsChanged", change);
  }, [identity?.wallet, clear]);
  useEffect(() => {
    if (!identity) return;
    const generation = epoch.current,
      owner = identity.wallet,
      store = new SnapshotStore(owner);
    let alive = true,
      socket: WebSocket | null = null,
      reconnect = 0,
      reconnectAttempt = 0;
    const accept = (value: Snapshot) => {
      if (alive && generation === epoch.current && store.accept(value)) {
        setSnapshot(value);
        setReceived(store.received);
        setError("");
      }
    };
    acceptRef.current = accept;
    const poll = async () => {
      try {
        accept(await api<Snapshot>("/workspace"));
      } catch (e) {
        if (e instanceof APIError && e.status === 401) {
          clear();
          return;
        }
        // Keep the last usable snapshot; the header shows the connection
        // state. Never replace real data with a global delayed banner.
        setConnection("polling");
      }
    };
    const funds = async () => {
      try {
        const b = await api<Balance>("/account");
        if (alive && generation === epoch.current) setBalance(b);
      } catch {
        /* Optional balance must not freeze live stats. */
      }
    };
    const scheduleReconnect = () => {
      if (!alive) return;
      const delay = Math.min(10000, 1000 * 2 ** Math.min(reconnectAttempt, 3));
      reconnectAttempt += 1;
      reconnect = window.setTimeout(() => void open(), delay);
    };
    const open = async () => {
      if (!alive) return;
      if (!config?.ws_origin?.trim()) {
        // Same-origin Vercel sockets cannot be upgraded. REST polling is the
        // guaranteed path; no failed WebSocket loop and no false banner.
        setConnection("polling");
        return;
      }
      let ticket = "";
      try {
        ticket = (await api<{ ticket: string }>("/ws-ticket")).ticket;
      } catch {
        setConnection("polling");
        scheduleReconnect();
        return;
      }
      if (!alive || generation !== epoch.current) return;
      const url = workspaceSocketUrl(config, location.origin, ticket);
      if (!url) {
        setConnection("polling");
        return;
      }
      const ws = new WebSocket(url);
      socket = ws;
      ws.onopen = () => {
        if (!alive || socket !== ws) return;
        reconnectAttempt = 0;
        setConnection("live");
      };
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.type === "workspace") accept(d);
        } catch {}
      };
      ws.onclose = () => {
        if (!alive || socket !== ws) return;
        setConnection("polling");
        void poll();
        scheduleReconnect();
      };
      ws.onerror = () => ws.close();
    };
    refreshRef.current = () => {
      void poll();
      void funds();
    };
    void poll();
    void funds();
    void open();
    const timer = window.setInterval(() => {
      if (!document.hidden && Date.now() - store.received > 2000) void poll();
    }, 3000);
    const balanceTimer = window.setInterval(funds, 20000);
    const visible = () => {
      if (!document.hidden) {
        void poll();
        void funds();
      }
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      alive = false;
      window.clearTimeout(reconnect);
      window.clearInterval(timer);
      window.clearInterval(balanceTimer);
      socket?.close();
      document.removeEventListener("visibilitychange", visible);
      refreshRef.current = () => {};
      acceptRef.current = () => {};
    };
  }, [identity?.wallet, config?.ws_origin, clear]);
  const logout = async () => {
    try {
      await api("/auth/logout", {});
    } finally {
      clear();
      provider.current = null;
    }
  };
  return {
    identity,
    snapshot,
    config,
    balance,
    booting,
    connection,
    received,
    error,
    setError,
    wallets,
    provider,
    connect,
    logout,
    loadIdentity,
    cancelConnection: clear,
    refresh: () => refreshRef.current(),
    applySnapshot: (value: Snapshot) => acceptRef.current(value),
  };
}
