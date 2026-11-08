export type Strategy = "outcome" | "bbo" | "mid";
export type View = "run" | "sessions" | "performance" | "account";
export interface Provider {
  request(args: { method: string; params?: unknown[] }): Promise<any>;
  on?: (name: string, cb: (data: any) => void) => void;
  removeListener?: (name: string, cb: (data: any) => void) => void;
}
export interface Wallet {
  info: { uuid: string; name: string; icon?: string; rdns?: string };
  provider: Provider;
}
export interface Identity {
  wallet: string;
  account: string;
  csrf: string;
  agent: { address: string; status: string; expires: number } | null;
}
export interface Agent {
  address: string;
  status: string;
  expires: number;
  typed_data?: any;
}
export interface Balance {
  account: string;
  usdc_total: number;
  usdc_available: number;
  agent: Agent | null;
}
export interface Config {
  walletconnect_project_id: string;
  features: { pause: boolean; session_targets: boolean };
}
export interface Draft {
  strategy: Strategy;
  order_usd: number;
  position_cap: number;
  market: string;
  side: "yes" | "no";
  spread_bps: number;
  offset_bps: number;
  duration_sec: number | null;
  target_volume_usd: number | null;
  stop_loss_usd: number;
}
export interface Metrics {
  active: number;
  accounting_complete: boolean;
  volume_usd: number | null;
  net_pnl: number | null;
  fees: number | null;
  fills: number | null;
  cost_per_1m: number | null;
}
export interface Session {
  id: string;
  kind: "outcome" | "grid";
  strategy: Strategy;
  name: string;
  market: string;
  phase: string;
  running: boolean;
  cleanup_confirmed: boolean;
  started: number;
  duration_sec: number;
  volume_usd: number | null;
  net_pnl: number | null;
  fees: number | null;
  fills: number | null;
  cost_per_1m: number | null;
  position_usd: number | null;
  stale: boolean;
  accounting_complete: boolean;
  state: string;
  reason: string;
  rotations: number;
  config: Partial<Draft> & { order_size_usd?: number; outcome_side?: string };
  quotes: any[];
}
export interface Snapshot {
  type: "workspace";
  owner: string;
  source_id: string;
  revision: number;
  published_at: number;
  sessions: Session[];
  metrics: Metrics;
}
export interface Operation {
  command: string;
  phase: string;
  result: { session_id?: string; message?: string };
}
