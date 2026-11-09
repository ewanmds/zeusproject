import {
  Play, Square, PanelsTopLeft, BookOpen, Crosshair, ArrowLeftRight,
  ChartNoAxesCombined, Wallet, Link2, ShieldCheck, Check, X, ChevronDown,
  ArrowRight, Info, LogOut, Clock, Sparkles, RefreshCw, Search, Copy,
  ExternalLink, Settings2, Bell, Keyboard, CircleAlert, UserRound, List,
  LayoutGrid, Ellipsis, Eye, EyeOff, ArrowUpDown,
} from "lucide-react";
const icons = { run: Play, stop: Square, sessions: PanelsTopLeft, book: BookOpen,
  target: Crosshair, auto: ArrowLeftRight, chart: ChartNoAxesCombined, wallet: Wallet,
  link: Link2, shield: ShieldCheck, check: Check, close: X, chevron: ChevronDown,
  arrow: ArrowRight, info: Info, logout: LogOut, clock: Clock, spark: Sparkles,
  refresh: RefreshCw, search: Search, copy: Copy, external: ExternalLink,
  settings: Settings2, bell: Bell, keyboard: Keyboard, alert: CircleAlert,
  user: UserRound, list: List, grid: LayoutGrid, more: Ellipsis, eye: Eye,
  hidden: EyeOff, sort: ArrowUpDown };
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const Glyph = icons[name as keyof typeof icons] || Info;
  return <Glyph size={size} strokeWidth={1.6} aria-hidden="true" />;
}
