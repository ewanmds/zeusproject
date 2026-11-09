import type { Dispatch, SetStateAction } from "react";
import type { Draft, View, Session, Operation, Metrics } from "./types";
import type { useWorkspace } from "./useWorkspace";
type Setter<T> = Dispatch<SetStateAction<T>>;
type Workspace = ReturnType<typeof useWorkspace>;
export interface RunProps {
  w: Workspace;
  draft: Draft;
  update: (key: keyof Draft, value: string | number | null) => void;
  setDraft: Setter<Draft>;
  setReview: Setter<any>;
  markets: any[];
  search: string;
  setSearch: Setter<string>;
  scanner: any;
  owner?: string;
  ready: boolean;
  preview: () => Promise<void>;
  pendingOperation: Operation | null;
  busy: string;
  setShowHelp: Setter<boolean>;
  actionError: string;
}
export interface SessionsProps {
  owner?: string;
  metrics?: Metrics;
  stale: boolean;
  filtered: Session[];
  filter: string;
  setFilter: Setter<string>;
  now: number;
  w: Workspace;
  navigate: (view: View) => void;
  setStop: Setter<Session | null>;
  setSelected: Setter<Session | null>;
  setNotice: Setter<string>;
  setWalletModal: Setter<boolean>;
}
export interface PerformanceProps {
  loading: boolean;
  owner?: string;
  period: number;
  setPeriod: Setter<number>;
  history: Session[];
  volume: number | null;
  net: number | null;
  sum: (field: "volume_usd" | "net_pnl" | "fees" | "fills") => number | null;
  setWalletModal: Setter<boolean>;
}
export interface AccountProps {
  actionError: string;
  owner?: string;
  w: Workspace;
  ready: boolean;
  busy: string;
  authorize: () => Promise<void>;
  navigate: (view: View) => void;
  setNotice: Setter<string>;
  setWalletModal: Setter<boolean>;
}
